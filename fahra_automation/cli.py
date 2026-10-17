from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import plistlib
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import httpx

from .analysis import Analyzer
from .config import Config
from .cleanup import Cleaner
from .db import Store, AssetStore
from .downloader import Downloader
from .drive import Drive
from .media import Media
from .pipeline import Pipeline
from .repliz import Repliz
from .telegram import Telegram
from .util import digest_file, normalize_url, process_lock, safe_error, iso
from .ownership import LABEL, legacy_locks
from .journal import AttemptJournal
from .replies import ReplyWorker
from .reply_store import ReplyStore



def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def initialize(config):
    config.runtime.mkdir(parents=True, exist_ok=True)
    (config.runtime / "logs").mkdir(exist_ok=True)
    (config.root / "scripts/docker-for-mcp.py").chmod(0o755)
    return Store(config.runtime / 'workflow.sqlite3', daily_limit=config.section('workflow')['daily_limit'],
                 window_start=config.section('workflow')['window_start'], window_end=config.section('workflow')['window_end'])


async def wait_for_startup(config, store, repliz, replies):
    while True:
        try:
            await repliz.sync(store)
            AttemptJournal(config).reconcile_store(store)
            config.require_live_action()
            store.recover()
            replies.db.recover()
            store.set('startup_health', {'ready': True, 'at': iso()})
            return
        except Exception as exc:
            store.invalidate_snapshot()
            store.set('startup_health', {'ready': False, 'at': iso(), 'error': safe_error(exc)})
            await asyncio.sleep(30)


async def run(config, store):
    config.require_production_checkout()
    config.telegram_token()
    with process_lock(config.runtime / 'worker.lock'), legacy_locks(config):
        repliz = Repliz(config)
        telegram = None
        try:
            config.require_live_action()
            store.invalidate_snapshot()
            store.set('startup_health', {'ready': False, 'at': iso(), 'phase': 'waiting_for_repliz'})
            media = Media(config)
            pipeline = Pipeline(config, store, repliz, Downloader(config), media, Analyzer(config, media), Drive(config))
            telegram = Telegram(config, store, pipeline)
            cleaner = Cleaner(config, store)
            replies = ReplyWorker(config, store, repliz)
            from .account_reports import AccountReports
            reports = AccountReports(config, store, repliz, telegram)
            telegram.reports = reports
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            tasks = []
            async def backups():
                while True:
                    try:
                        await asyncio.to_thread(store.backup, config.path(config.section('workflow')['backup_dir']))
                    except Exception as exc:
                        store.record('backup_failed', error=safe_error(exc))
                    await asyncio.sleep(config.section('workflow')['backup_interval_seconds'])
            async def operational_workers():
                await wait_for_startup(config, store, repliz, replies)
                async with asyncio.TaskGroup() as operations:
                    for worker in (pipeline.worker_loop, pipeline.monitor_loop, pipeline.sync_loop,
                                   cleaner.loop, replies.polling_loop, replies.worker_loop, replies.profile_loop):
                        operations.create_task(worker())
            print('Workflow berjalan. Status dan jejak tersimpan di SQLite.', flush=True)
            async with asyncio.TaskGroup() as group:
                for task in (telegram.polling_loop, telegram.outbox_loop, reports.loop, backups, operational_workers):
                    tasks.append(group.create_task(task()))
                await stop.wait()
                for task in tasks:
                    task.cancel()
        finally:
            store.record('process_stopped')
            if telegram is not None:
                await telegram.client.aclose()
            await repliz.close()


async def doctor(config, store):
    results = {}

    async def check(name, call):
        try:
            results[name] = {"ok": True, "detail": await call()}
        except Exception as exc:
            results[name] = {"ok": False, "error": safe_error(exc)}

    async def telegram():
        client = Telegram(config, store, None)
        try:
            bot = await client.call("getMe")
            hook = await client.call("getWebhookInfo")
            if hook.get("url"):
                raise RuntimeError("Webhook aktif; polling belum dapat dijalankan.")
            return {"username": bot["username"]}
        finally:
            await client.client.aclose()

    async def repliz():
        client = Repliz(config)
        try:
            await client.sync(store)
            return {"platforms": store.summary()["platforms"], "scope": "tiktok,facebook,youtube"}
        finally:
            await client.close()

    async def ai():
        Analyzer(config, Media(config)).ready()
        cfg = config.section("ai")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(cfg["base_url"].rstrip("/") + "/models", headers={"Authorization": "Bearer " + config.ai_key()})
            r.raise_for_status()
            if cfg["model"] not in {v["id"] for v in r.json().get("data", [])}:
                raise RuntimeError("Model tidak ditemukan pada endpoint AI.")
        return {"model": cfg["model"], "transcription": cfg["transcription_backend"]}

    async def drive():
        d = Drive(config)
        d.ready()
        if config.section("drive")["mode"] == "synced_public":
            entries = await asyncio.to_thread(d.public_entries, config.section("drive")["folder_id"])
            return {"mode": "synced_public", "visible_entries": len(entries), "sync_verified": store.get("drive_sync_verified", False)}
        await asyncio.to_thread(lambda: d.client().files().get(fileId=config.section("drive")["folder_id"], fields="id").execute())
        return {"mode": "api"}

    async def downloader():
        result = await Downloader(config).health()
        return result

    await asyncio.gather(*(check(name, call) for name, call in (
        ("telegram", telegram), ("repliz", repliz), ("ai", ai), ("drive", drive), ("downloader", downloader))))
    results["pairing"] = {"ok": bool(config.section("telegram")["allowed_user_ids"] or store.get("allowed_users", []))}
    output(results)
    return 0 if all(v["ok"] for k, v in results.items() if k != "pairing") else 1


async def preview(config, production_store, args):
    folder = config.runtime / "previews" / uuid.uuid4().hex
    store = Store(folder / "preview.sqlite3")
    url, code = normalize_url(args.url)
    result = store.enqueue(0, 0, 0, [(url, code)], can_allocate=False)
    delivery = store.job(store.queue()[0]["id"])
    media_store = AssetStore(store)
    job = store.asset(delivery["asset_id"])
    media, drive = Media(config), Drive(config)
    print("Preview: download melalui MCP…", flush=True)
    downloaded = await Downloader(config).download(job, media_store)
    media_store.checkpoint(job["id"], "downloaded", payload={**downloaded, "source_sha256": digest_file(Path(downloaded["source_path"]))})
    print("Preview: analisis visual dan transkrip…", flush=True)
    analysis = await Analyzer(config, media).analyze(store.asset(job["id"]), folder / "analysis",
                                                   recent_captions=production_store.recent_captions(job["id"]))
    media_store.checkpoint(job["id"], "analyzed", payload=analysis)
    target = folder / "final.mp4"
    print("Preview: render tanpa mirror, zoom, dan hook…", flush=True)
    probe = await asyncio.to_thread(media.render, Path(downloaded["source_path"]), target, analysis["generated"], job["id"])
    media_store.checkpoint(job["id"], "rendered", payload={"render_path": str(target), "render_probe": probe,
        "render_sha256": digest_file(target), "edits": {"mirror": False, "zoom_percent": 110,
        "hook_seconds": config.section('video')['hook_seconds']}})
    report = {"job_id": job["id"], "folder": str(folder), "probe": probe, "generated": analysis["generated"], "published": False,
              "caption_policy_version": analysis["caption_policy_version"], "caption_review": analysis["caption_review"]}
    if args.sync_drive:
        print("Preview: menunggu Drive (maksimal 10 menit)…", flush=True)
        for attempt in range(40):
            try:
                remote = await asyncio.to_thread(drive.upload, store.asset(job["id"]), media_store)
                await drive.verify_url(remote["drive_url"])
                report["drive"] = remote
                production_store.set("drive_sync_verified", True)
                production_store.record("preview_drive_verified", job_id=job["id"], file_id=remote["drive_file_id"])
                break
            except Exception as exc:
                if attempt == 39 or isinstance(exc, ValueError):
                    report["drive_error"] = safe_error(exc)
                    break
                await asyncio.sleep(15)
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    production_store.record("preview_completed", preview_id=job["id"], folder=str(folder), drive_verified="drive" in report)
    output(report)


def setup_whisper(config):
    target = config.path(config.section("ai")["whisper_model"])
    if target.exists():
        output({"model": str(target), "exists": True})
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
    digest = hashlib.sha256()
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        head = client.head(url, follow_redirects=False)
        expected = head.headers.get("x-linked-etag", "").strip('"')
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise RuntimeError("Checksum resmi model tidak tersedia; download dihentikan.")
        partial = target.with_suffix(".partial")
        with client.stream("GET", url) as response, partial.open("wb") as stream:
            response.raise_for_status()
            for chunk in response.iter_bytes(1024 * 1024):
                stream.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError("Checksum model Whisper tidak cocok.")
        partial.replace(target)
    output({"model": str(target), "sha256": expected})


def service(config, action):
    config.require_production_checkout()
    plist = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    domain = f"gui/{os.getuid()}"
    if action == "install":
        config.telegram_token()
        plist.parent.mkdir(parents=True, exist_ok=True)
        logs = Path.home() / "Library/Logs/FahraAutomation"
        logs.mkdir(parents=True, exist_ok=True)
        data = {"Label": LABEL, "ProgramArguments": [str(config.root / ".venv/bin/python"), "-m", "fahra_automation", "--root", str(config.root), "run"],
                "WorkingDirectory": str(Path.home()), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
                "StandardOutPath": str(logs / "service.log"), "StandardErrorPath": str(logs / "service-error.log"),
                "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONUNBUFFERED": "1"}}
        if plist.exists() and plistlib.loads(plist.read_bytes()).get("ProgramArguments") != data["ProgramArguments"]:
            raise RuntimeError("LaunchAgent yang ada menunjuk proyek berbeda; tidak ditimpa.")
        plist.write_bytes(plistlib.dumps(data))
        output({"plist": str(plist), "next": "service start"})
    elif action == "start":
        subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist)], check=True)
    elif action == "stop":
        subprocess.run(["/bin/launchctl", "bootout", domain + "/" + LABEL], check=True)
    elif action == "restart":
        subprocess.run(["/bin/launchctl", "kickstart", "-k", domain + "/" + LABEL], check=True)
    else:
        subprocess.run(["/bin/launchctl", "print", domain + "/" + LABEL], check=True)


def main():
    parser = argparse.ArgumentParser(description="Workflow Telegram, Instagram, Drive, Repliz.")
    parser.add_argument("--root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "run", "doctor", "status", "accounts", "sync", "pair-code", "drive-login", "setup-whisper", "backup"):
        sub.add_parser(name)
    p = sub.add_parser("preview")
    p.add_argument("url")
    p.add_argument("--sync-drive", action="store_true")
    p = sub.add_parser("service")
    p.add_argument("action", choices=["install", "start", "stop", "restart", "status"])
    p = sub.add_parser("events")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("cleanup", help="Daftar kandidat file lokal; --apply menghapus yang sudah memenuhi syarat.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--status", action="store_true")
    p = sub.add_parser('migrate')
    p.add_argument('action', choices=['snapshot','import','reconcile','activate'])
    p.add_argument('folder', nargs='?', type=Path)
    p.add_argument('--final', action='store_true')
    p.add_argument('--staging', action='store_true')
    p = sub.add_parser('reply')
    p.add_argument('action', choices=['status','pause','resume'])
    p = sub.add_parser('report', help='Laporan TikTok harian ke chat pribadi admin.')
    p.add_argument('action', choices=['preview', 'status', 'enable', 'disable', 'pilot'])
    p.add_argument('--credentials-from', type=Path, help='Folder Credential untuk preview GET terisolasi.')
    args = parser.parse_args()
    try:
        if args.command == 'report' and args.credentials_from and args.action != 'preview':
            raise ValueError('--credentials-from hanya untuk report preview.')
        config = Config.load(args.root)
        if args.command == 'report' and args.action == 'preview':
            from .report_cli import preview as report_preview
            output(asyncio.run(report_preview(config, args.credentials_from)))
            return 0
        store = initialize(config)
        if args.command == 'report':
            from .report_cli import operate
            output(asyncio.run(operate(config, store, args.action)))
        elif args.command == 'migrate':
            if args.staging and args.action not in {'import','reconcile'}:
                raise ValueError('--staging hanya untuk import atau reconcile, bukan aktivasi.')
            if args.final and args.action != 'snapshot':
                raise ValueError('--final hanya untuk snapshot.')
            from . import migration
            from .ownership import activate
            if args.action == 'snapshot':
                output({'folder': str(migration.snapshot(config, final=args.final))})
            elif args.action == 'import':
                if not args.folder:
                    raise ValueError('Lokasi snapshot wajib diberikan.')
                target = Store(config.runtime / 'migration-staging' / 'workflow.sqlite3') if args.staging else store
                output(migration.import_snapshots(target,args.folder))
            elif args.action == 'reconcile':
                target = Store(config.runtime / 'migration-staging' / 'workflow.sqlite3') if args.staging else store
                async def reconcile_migration():
                    api = Repliz(config)
                    try:
                        output(await migration.reconcile(config,target,api))
                    finally:
                        await api.close()
                asyncio.run(reconcile_migration())
            else:
                output(activate(config,store))
        elif args.command == 'reply':
            replies = ReplyStore(store)
            if args.action != 'status':
                if args.action == 'resume':
                    config.require_live_action()
                    replies.enroll_accounts(enable=True)
                else:
                    store.set('reply_enabled',False)
                store.record('reply_operator_state',enabled=args.action == 'resume')
            output(replies.summary())
        elif args.command == "init":
            output({"database": str(store.path), "status": "initialized"})
        elif args.command == "run":
            asyncio.run(run(config, store))
        elif args.command == "doctor":
            return asyncio.run(doctor(config, store))
        elif args.command == "status":
            output(store.summary())
        elif args.command == "cleanup":
            cleaner = Cleaner(config, store)
            if args.status:
                output({"enabled": cleaner.settings().get("enabled", False), "last_run": store.get("cleanup_last_run")})
            else:
                output(asyncio.run(cleaner.sweep()) if args.apply else cleaner.plan())
        elif args.command == "accounts":
            output(store.accounts())
        elif args.command == "backup":
            output({"backup": str(store.backup(config.path(config.section("workflow")["backup_dir"])) )})
        elif args.command == "events":
            with store.connect() as c:
                output([dict(r) for r in c.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (min(args.limit, 1000),))])
        elif args.command == "pair-code":
            print("Kirim ke chat pribadi bot Fahra Automation (berlaku 30 menit):\n/pair " + store.issue_pair_code())
        elif args.command == "sync":
            async def sync():
                client = Repliz(config)
                try:
                    await client.sync(store)
                    output(store.summary())
                finally:
                    await client.close()
            asyncio.run(sync())
        elif args.command == "preview":
            asyncio.run(preview(config, store, args))
        elif args.command == "setup-whisper":
            setup_whisper(config)
        elif args.command == "drive-login":
            Drive(config).login()
        elif args.command == "service":
            service(config, args.action)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(safe_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
