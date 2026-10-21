"""Preview caption changes against cached production evidence without writing to the queue."""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

import httpx

from .analysis import Analyzer
from .captioning import POLICY_VERSION
from .config import Config
from .util import iso, safe_error


async def audit(config, job_ids, folder):
    if folder.exists():
        raise ValueError("Gunakan folder audit baru agar bukti sebelumnya tidak tertimpa.")
    database = config.database_path
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        selected = []
        for identity in dict.fromkeys(job_ids):
            row = connection.execute("SELECT id,url,payload,asset_id FROM jobs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("ID pekerjaan tidak ditemukan: " + identity)
            selected.append({**dict(row), "payload": json.loads(row["payload"])})
        previous = connection.execute("""SELECT id,json_extract(payload,'$.generated') AS generated FROM assets
            WHERE status != 'duplicate' AND json_type(payload,'$.generated.caption')='text'
            ORDER BY created_at DESC,rowid DESC LIMIT 9""").fetchall()
    folder.mkdir(parents=True)
    cfg = config.section("ai")
    analyzer = Analyzer(config, None)
    report = {"at": iso(), "policy_version": POLICY_VERSION, "published": False, "results": []}
    async with httpx.AsyncClient(base_url=cfg["base_url"].rstrip("/") + "/",
                                headers={"Authorization": "Bearer " + config.ai_key()}, timeout=180) as client:
        for job in selected:
            item = {"job_id": job["id"], "url": job["url"], "before": job["payload"].get("generated")}
            try:
                assets = config.runtime / "media" / job["asset_id"] / "analysis"
                frames = sorted(assets.glob("frame-*.jpg"))
                if not frames:
                    raise ValueError("Frame tersimpan tidak tersedia; gunakan preview URL untuk analisis baru.")
                recent = [json.loads(r["generated"]) for r in previous if r["id"] != job["asset_id"]][:8]
                result = await analyzer.generate(client, job, frames, job["payload"].get("transcript", ""),
                                                 folder / job["id"], recent)
                item.update(status="generated", after=result["generated"], review=result["caption_review"])
            except Exception as exc:
                item.update(status="needs_review", error=safe_error(exc))
            report["results"].append(item)
            target = folder / "report.json"
            partial = target.with_suffix(".partial")
            partial.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            partial.chmod(0o600)
            partial.replace(target)
            print(json.dumps(item, ensure_ascii=False), flush=True)
    return report


async def main():
    parser = argparse.ArgumentParser(description="Uji caption baru dari frame/transkrip tersimpan; tidak memposting.")
    parser.add_argument("--job", required=True, nargs="+", help="ID pekerjaan lengkap")
    parser.add_argument("--folder", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = await audit(Config.load(), args.job, args.folder)
    except Exception as exc:
        print(safe_error(exc))
        raise SystemExit(1) from None
    print(json.dumps({"report": str(args.folder / "report.json"), "generated": sum(r["status"] == "generated" for r in report["results"]),
                      "needs_review": sum(r["status"] != "generated" for r in report["results"]), "published": False}))
    if any(r["status"] != "generated" for r in report["results"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
