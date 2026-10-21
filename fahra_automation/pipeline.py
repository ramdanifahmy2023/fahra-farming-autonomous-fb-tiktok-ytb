from __future__ import annotations

import asyncio
import math
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import ConfigurationError
from .db import ZONE, AssetStore, PREPARE
from .downloader import DownloadUncertain, DownloadFailed
from .repliz import AccountUnavailable, MutationNotSent, ReplizError, account_id, publication_details, timestamp, api_scope
from .telemetry import record_timing
from .personas import PersonaService
from .util import digest_file, iso, now, safe_error


class Pipeline:
    def __init__(self, config, store, repliz, downloader, media, analyzer, drive, *, personas=None, journal=None):
        self.config, self.store, self.repliz = config, store, repliz
        self.downloader, self.media, self.analyzer, self.drive = downloader, media, analyzer, drive
        if analyzer is not None and hasattr(analyzer, 'store'):
            analyzer.store = store
        if personas is None:
            from .personas import PersonaService
            personas = PersonaService(config, store, analyzer)
        if journal is None:
            from .journal import AttemptJournal
            journal = AttemptJournal(config)
        self.personas, self.journal = personas, journal
        self.work_available = asyncio.Event()

    def notify_work(self):
        self.work_available.set()

    def fresh_sync(self):
        return self.store.snapshot_fresh()

    @staticmethod
    async def local_stage(function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def revalidate(self, job, generation):
        current = self.store.job(job['id'])
        if not self.fresh_sync() or self.store.snapshot_generation() != generation:
            raise ConfigurationError('Data akun diperbarui; menunggu snapshot lengkap.', dependency='snapshot')
        if (not current or current['status'] != 'drive_ready' or not current['leased']
                or current.get('schedule_id') or current['payload'].get('submission')
                or any(current.get(key) != job.get(key) for key in ('account_id', 'slot_date', 'planned_at'))):
            raise ConfigurationError('Akun, jadwal, lease, atau snapshot berubah; preflight akan diulang.')
        if (PersonaService.evidence_hash(current['payload']) != PersonaService.evidence_hash(job['payload'])
                or any(current['payload'].get(key) != job['payload'].get(key) for key in (
                    'drive_url', 'drive_file_id', 'drive_root_id', 'drive_path_ids', 'drive_relative_path',
                    'drive_verified_sha256', 'drive_verified_size', 'bank_path', 'render_probe',
                    'source_attribution', 'delivery_metadata'))):
            raise ConfigurationError('Bukti video atau metadata berubah; preflight perlu diulang.')
        self.require_pilot_source(current)
        return current

    def require_slot_time(self, job):
        if timestamp(job['planned_at']) < now() + timedelta(seconds=self.config.section('workflow')['schedule_lead_seconds']):
            raise ConfigurationError('Slot jadwal terlalu dekat setelah preflight; alokasi akan diulang.', dependency='slot')

    @staticmethod
    def next_publication_poll(job, *, failed=False, exception=False):
        if exception:
            count = min(int(job['payload'].get('poll_failures', 0)), 9)
            maximum = 300 if job['status'] in {'submitting', 'submit_unknown'} else 21600 if job['status'] == 'failed' else 1800
            return iso(now() + timedelta(seconds=min(maximum, 90 * 2 ** count)))
        if failed:
            count = min(int(job['payload'].get('provider_poll_failures', 0)), 7)
            return iso(now() + timedelta(seconds=min(21600, 900 * 2 ** count)))
        if job.get('planned_at') and job.get('schedule_id'):
            remaining = (timestamp(job['planned_at']) - now()).total_seconds()
            return iso(now() + timedelta(seconds=max(30, min(1800, remaining - 90))))
        return iso(now() + timedelta(seconds=90))

    def require_pilot_source(self, job):
        source_id = self.store.get('pilot_source_id')
        if source_id and job.get('source_id') != source_id:
            raise ConfigurationError('Kiriman menunggu uji pilot tiga platform selesai.')

    async def step(self, job):
        sid = job["id"]
        state = job["status"]
        if state not in PREPARE:
            return
        if state != "drive_ready":
            await self.prepare(job)
            self.store.checkpoint(sid, leased=0, next_attempt=iso(), last_error=None)
            return
        self.require_pilot_source(job)
        if not self.fresh_sync():
            raise ConfigurationError("Sinkronisasi akun dan kuota Repliz belum tersedia atau sudah kedaluwarsa.", dependency='snapshot')
        self.config.require_live_action()
        when = now() + timedelta(seconds=self.config.section("workflow")["schedule_lead_seconds"])
        target_day = when.astimezone(ZONE).date()
        slot = self.store.ensure_slot(sid, target_day)
        if slot is None:
            raise ConfigurationError(f"Belum ada akun {job['platform']} aktif. Tujuan ini menunggu sinkronisasi.", dependency='capacity')
        if slot > target_day.isoformat():
            next_time = datetime.fromisoformat(slot).replace(tzinfo=ZONE)
            self.store.checkpoint(sid, next_attempt=iso(next_time), leased=0)
            return
        job = self.store.job(sid)
        generation = self.store.snapshot_generation()
        self.revalidate(job, generation)
        self.media.validate_delivery(job['payload']['render_probe'], job['platform'])
        try:
            account = await self.repliz.check_account(job["account_id"], job["platform"])
        except AccountUnavailable:
            self.store.disconnect(job["account_id"])
            raise
        if isinstance(account, dict):
            if account.get('publishEligible') is False:
                self.store.capability(job['account_id'], 'publish', 'held', 'provider_denied', 'Provider menahan posting.')
                raise ConfigurationError('Akun menunggu bukti kemampuan posting baru.')
            if str(account.get('username') or '').strip() != self.store.account_username(job['account_id'], identity=True):
                self.store.begin_sync()
                raise ConfigurationError('Identitas akun berubah; menunggu snapshot lengkap berikutnya.', dependency='snapshot')
        self.revalidate(job, generation)
        self.require_slot_time(job)
        metadata = await self.personas.prepare_delivery(job)
        self.revalidate(job, generation)
        self.store.checkpoint(sid, payload={'delivery_metadata': metadata})
        job = self.store.job(sid)
        self.require_slot_time(job)
        verified = await self.drive.verify_delivery(job)
        job = self.revalidate(job, generation)
        when = timestamp(job['planned_at'])
        self.require_slot_time(job)
        body = self.repliz.payload(job, when)
        self.config.require_live_action()
        self.store.checkpoint(sid, payload={'preflight_drive': verified, 'submission_marker': 'fa:' + sid})
        self.journal.begin(sid, body)
        try:
            self.store.begin_submission(sid, body, generation)
        except BaseException:
            self.journal.mark(sid, 'not_sent')
            raise
        try:
            schedule_id = await self.repliz.create(body, job["platform"],
                before_send=lambda: self.store.validate_submission(sid, body, generation))
        except MutationNotSent as exc:
            self.journal.mark(sid, 'not_sent', error=safe_error(exc))
            self.store.reject_submission(sid, safe_error(exc))
            raise
        except ReplizError as exc:
            if exc.status in {400, 401, 403, 404, 405, 413, 415, 422, 429}:
                self.journal.mark(sid, 'rejected', error=safe_error(exc))
                self.store.reject_submission(sid, safe_error(exc))
                if exc.status not in {401, 403, 429}:
                    raise ValueError(str(exc)) from None
                retry_at = iso(now() + timedelta(seconds=max(60, exc.retry_after)))
                if exc.status == 429:
                    self.store.defer_account(job['account_id'], retry_at, safe_error(exc))
                elif exc.status in {401, 403}:
                    self.store.capability(job['account_id'], 'publish', 'held', 'provider_denied', safe_error(exc))
                self.store.checkpoint(sid, leased=0, last_error=safe_error(exc), next_attempt=retry_at)
                return
            self.journal.mark(sid, 'unknown', error=safe_error(exc))
            self.store.checkpoint(sid, "submit_unknown", last_error=safe_error(exc), leased=0)
            self.store.notify("unknown:" + sid, self.store.batch_chat(job),
                              f"Kiriman {job['platform']} {sid[:8]} menunggu pengecekan Repliz. Permintaan tidak dikirim ulang.")
            return
        except asyncio.CancelledError:
            self.journal.mark(sid, 'unknown', error='Pengiriman terputus saat proses dihentikan.')
            self.store.checkpoint(sid, 'submit_unknown', leased=0)
            raise
        except Exception as exc:
            self.journal.mark(sid, 'unknown', error=safe_error(exc))
            self.store.checkpoint(sid, "submit_unknown", last_error=safe_error(exc), leased=0)
            return
        self.journal.mark(sid, 'accepted', schedule_id=schedule_id)
        self.store.checkpoint(sid, "scheduled", schedule_id=schedule_id, attempts=0, last_error=None,
                              leased=0, payload={"next_poll": self.next_publication_poll({**job, 'schedule_id': schedule_id})})

    def require_free_space(self):
        minimum = self.config.section('workflow').get('min_free_disk_gb', 10) * 1024**3
        if minimum <= 0:
            return
        paths = [self.store.path.parent, self.config.runtime / 'media',
                 self.config.path(self.config.section('drive')['sync_dir']),
                 self.config.path(self.config.section('downloader')['project_dir']) / 'downloads/mcp']
        checked = set()
        for path in paths:
            while not path.exists() and path != path.parent:
                path = path.parent
            device = path.stat().st_dev
            if device in checked:
                continue
            checked.add(device)
            if shutil.disk_usage(path).free < minimum:
                raise ConfigurationError('Ruang disk media/bank/database kurang. Persiapan menunggu ruang tersedia.')

    async def prepare(self, delivery):
        self.require_free_space()
        media_store = AssetStore(self.store)
        job = self.store.asset(delivery["asset_id"])
        sid, state = job["id"], job["status"]
        if state == 'queued':
            self.require_media_horizon(sid)
        self.analyzer.ready()
        self.drive.ready()
        folder = self.config.runtime / "media" / sid
        folder.mkdir(parents=True, exist_ok=True)
        if state == "queued":
            result = await self.downloader.download(job, media_store)
            info = await self.local_stage(self.media.probe, result["source_path"])
            media_store.checkpoint(sid, payload={**result, "source_probe": info})
            self.media.validate_shared(info)
            digest = await self.local_stage(digest_file, Path(result["source_path"]))
            if self.store.duplicate_hash(sid, digest):
                media_store.checkpoint(sid, "duplicate")
                return
            media_store.checkpoint(sid, "downloaded", payload={**result, "source_probe": info, "source_sha256": digest})
        elif state == "downloaded":
            info = job['payload'].get('source_probe') or await self.local_stage(self.media.probe, job['payload']['source_path'])
            self.media.validate_shared(info)
            result = await self.analyzer.analyze(job, folder / "analysis", recent_captions=self.store.recent_captions(sid))
            media_store.checkpoint(sid, "analyzed", payload=result)
        elif state == "analyzed":
            target = folder / "final.mp4"
            info = await self.local_stage(self.media.render, Path(job["payload"]["source_path"]), target,
                                           job["payload"]["generated"], sid)
            media_store.checkpoint(sid, "rendered", payload={"render_path": str(target), "render_probe": info,
                                                           "render_sha256": await self.local_stage(digest_file, target),
                                                           "edits": {"mirror": False, "zoom_percent": 110,
                                                                     "hook_seconds": self.config.section('video')['hook_seconds']}})
        elif state == "rendered":
            result = await self.local_stage(self.drive.upload, job, media_store)
            media_store.checkpoint(sid, "drive_ready", payload=result)
        elif state in {"drive_ready", "duplicate"}:
            self.store.checkpoint(delivery["id"], state, payload=job["payload"],
                                  **({"counts_quota": 0} if state == "duplicate" else {}))

    def require_media_horizon(self, asset_id):
        hours = self.config.section('workflow').get('render_horizon_hours', 24)
        if type(hours) not in {int, float} or not math.isfinite(hours) or hours <= 0:
            raise ConfigurationError('Horizon media harus berupa jumlah jam positif.')
        if not self.fresh_sync():
            raise ConfigurationError('Persiapan media menunggu snapshot akun dan kuota yang lengkap.', dependency='snapshot')
        at = now()
        day = (at + timedelta(seconds=self.config.section('workflow')['schedule_lead_seconds'])).astimezone(ZONE).date()
        for delivery in self.store.asset_deliveries(asset_id):
            if delivery['status'] in PREPARE and not delivery.get('schedule_id') and not delivery['payload'].get('submission'):
                self.store.ensure_slot(delivery['id'], day)
        accounts = {a['id']: a for a in self.store.accounts()}
        for delivery in self.store.asset_deliveries(asset_id):
            account = accounts.get(delivery.get('account_id'), {})
            if (delivery['status'] in PREPARE and account.get('connected') and account.get('eligible')
                    and delivery.get('planned_at') and at <= timestamp(delivery['planned_at']) <= at + timedelta(hours=hours)
                    and self.fresh_sync()):
                return
        raise ConfigurationError('URL tersimpan; download menunggu slot platform dalam horizon media.', dependency='capacity')

    async def handle(self, job):
        started = time.monotonic()
        try:
            with api_scope('publish' if job['status'] == 'drive_ready' else 'background'):
                await self.step(job)
        except asyncio.CancelledError:
            self.store.checkpoint(job["id"], leased=0)
            raise
        except DownloadUncertain as exc:
            self.store.hold_asset(job['asset_id'], safe_error(exc))
        except Exception as exc:
            current = self.store.job(job["id"])
            if current["status"] in {"submitting", "submit_unknown", "scheduled"}:
                self.store.checkpoint(job["id"], leased=0, last_error=safe_error(exc))
                return
            message = safe_error(exc)
            if isinstance(exc, ReplizError) and exc.status in {401, 403} and current.get('account_id'):
                self.store.capability(current['account_id'], 'publish', 'held', 'provider_denied', message)
                self.store.checkpoint(job['id'], leased=0, last_error=message,
                                      next_attempt=iso(now()+timedelta(seconds=60)))
                return
            if isinstance(exc, ConfigurationError):
                if exc.dependency == 'slot':
                    self.store.checkpoint(job['id'], leased=0, last_error=message, next_attempt=iso())
                elif exc.dependency in {'snapshot', 'drive', 'capacity'}:
                    self.store.defer_dependency(job['id'], message, dependency=exc.dependency,
                                                seconds=15 if exc.dependency == 'drive' else 60)
                elif current['status'] in PREPARE - {'drive_ready'}:
                    self.store.defer_asset(current['asset_id'], message)
                else:
                    self.store.checkpoint(job["id"], leased=0, last_error=message, next_attempt=iso(now() + timedelta(seconds=60)))
                if current["last_error"] != message:
                    self.store.record("configuration_blocked", job["id"], error=message)
                    if not self.store.has_progress(job["batch_id"]):
                        self.store.notify("blocked:" + job["id"] + ":" + str(hash(message)), self.store.batch_chat(job),
                                          f"Video {job['id'][:8]} menunggu layanan: {message}")
                return
            if current['status'] in PREPARE - {'drive_ready'}:
                self.store.fail_asset(current['asset_id'], exc,
                    max_attempts=self.config.section('workflow')['max_attempts'],
                    kind='source' if isinstance(exc, DownloadFailed) else
                         'analysis' if current['status'] == 'downloaded' else 'media' if isinstance(exc, ValueError) else 'transient')
                return
            attempts = current["attempts"] + 1
            terminal = isinstance(exc, ValueError) or attempts >= self.config.section("workflow")["max_attempts"]
            self.store.record("stage_failed", job["id"], stage=current["status"], attempt=attempts, error=message)
            if terminal:
                self.store.checkpoint(job["id"], "failed", payload={"resume_status": current["status"]}, counts_quota=0,
                                      leased=0, attempts=attempts, last_error=message)
            else:
                self.store.checkpoint(job["id"], leased=0, attempts=attempts, last_error=message,
                                      next_attempt=iso(now() + timedelta(seconds=min(1800, 30 * 2 ** attempts))))
        finally:
            record_timing(self.store, 'stage.' + job['status'], (time.monotonic()-started)*1000)
            self.notify_work()

    async def monitor(self, job_id=None):
        failures = []
        for job in ([self.store.job(job_id)] if job_id else self.store.pending_publications()):
            if job and job['status'] == 'published':
                continue
            if job_id and (not job or job['status'] not in {'failed','scheduled','submitting','submit_unknown'}):
                raise ValueError('Kiriman ini tidak memerlukan rekonsiliasi.')
            if job_id and not job['schedule_id'] and not job['payload'].get('submission'):
                raise ValueError('Belum ada intent kirim untuk dicocokkan.')
            if not job_id and job["payload"].get("next_poll", "") > iso():
                continue
            try:
                self.store.checkpoint(job["id"], preserve_published=True, payload={"next_poll": iso(now() + timedelta(seconds=90))})
                with api_scope('background' if job['status'] == 'failed' else 'monitor'):
                    if not job["schedule_id"]:
                        result = await self.repliz.reconcile(job)
                        if not result:
                            continue
                    else:
                        result = await self.repliz.status(job["schedule_id"])
                current = self.store.job(job['id'])
                if current and current['status'] == 'published':
                    continue
                if (not current or current['account_id'] != job['account_id']
                        or current['schedule_id'] not in (None, job['schedule_id'], result.get('id', result.get('_id')))):
                    raise ValueError('Identitas kiriman berubah selama rekonsiliasi.')
                job = current
                if account_id(result) != job["account_id"]:
                    raise ValueError("Jadwal Repliz tidak cocok dengan akun kiriman.")
                remote_id = result.get('id', result.get('_id'))
                if not remote_id or (job['schedule_id'] and remote_id != job['schedule_id']):
                    raise ValueError('ID jadwal tidak cocok dengan kiriman.')
                if self.journal.state(job['id']) is None and job['payload'].get('submission'):
                    self.journal.begin(job['id'], job['payload']['submission'])
                if self.journal.state(job['id']) is not None:
                    self.journal.mark(job['id'], 'accepted', schedule_id=remote_id)
                self.store.apply_remote_schedule(job['id'], result)
                self.store.checkpoint(job['id'], preserve_published=True, payload={'poll_failures': 0,
                    'next_poll': self.next_publication_poll(job)})
                if not job['schedule_id']:
                    self.store.checkpoint(job['id'], 'scheduled', preserve_published=True, schedule_id=remote_id)
                status = result.get("status")
                if status == "success":
                    details = publication_details(result, self.store.account_username(job["account_id"]), job["platform"])
                    if not details:
                        self.store.record('publication_evidence_waiting', job['id'])
                        continue
                    self.store.checkpoint(job["id"], "published", preserve_published=True, payload=details, last_error=None, leased=0)
                elif status == "error":
                    # Keep quota until an operator resolves the failed remote schedule; do not create another post.
                    self.store.checkpoint(job["id"], "failed", preserve_published=True, leased=0,
                                          payload={'next_poll': self.next_publication_poll(job, failed=True),
                                                   'provider_poll_failures': job['payload'].get('provider_poll_failures', 0) + 1,
                                                   'failure_kind': 'provider'},
                                          last_error=safe_error(result.get("errorMessage", "Repliz melaporkan error")))
            except Exception as exc:
                failures.append(safe_error(exc))
                self.store.checkpoint(job['id'], preserve_published=True, payload={
                    'next_poll': self.next_publication_poll(job, exception=True),
                    'poll_failures': job['payload'].get('poll_failures', 0) + 1})
                self.store.record("publication_poll_failed", job["id"], error=safe_error(exc))
        await self.share_link_reports()
        self.store.batch_reports()
        self.store.health('publication_monitor', 'error' if failures else 'ok', reason='; '.join(failures))
        return not failures

    async def share_link_reports(self):
        # Also recover published jobs whose notification was interrupted by a restart.
        for job in self.store.publications_to_report():
            try:
                if not job["payload"].get("post_url"):
                    result = await self.repliz.status(job["schedule_id"])
                    if account_id(result) != job["account_id"]:
                        raise ValueError("Akun pada tautan publikasi tidak cocok.")
                    details = publication_details(result, job["username"], job["platform"])
                    if not details:
                        raise RuntimeError("Repliz belum menyediakan ID/tautan posting yang valid.")
                    self.store.checkpoint(job["id"], payload={**details, "share_last_error": None})
                self.store.notify_publication(job["id"])
            except Exception as exc:
                attempts = job["payload"].get("share_attempts", 0) + 1
                message = safe_error(exc)
                self.store.checkpoint(job["id"], payload={"share_attempts": attempts, "share_last_error": message,
                    "share_next_poll": iso(now() + timedelta(seconds=min(3600, 90 * 2 ** min(attempts - 1, 6))))})
                if job["payload"].get("share_last_error") != message:
                    self.store.record("publication_link_waiting", job["id"], error=message)

    async def worker_loop(self, stage=None, worker_id=0):
        while True:
            job = self.store.claim(stage=stage)
            start = time.monotonic()
            if job:
                await self.handle(job)
            else:
                try:
                    await asyncio.wait_for(self.work_available.wait(), timeout=2)
                except TimeoutError:
                    pass
                finally:
                    self.work_available.clear()
            current = self.store.job(job['id']) if job else None
            state = ('error' if current['status'] == 'failed' else 'waiting') if current and current['last_error'] else 'ok'
            self.store.health('delivery_worker' if stage == 'send' else 'media_worker', state,
                              reason=current['last_error'] if current else '', duration_ms=(time.monotonic()-start)*1000,
                              details={'job_id': job['id'] if job else None, 'stage': stage, 'worker': worker_id})

    async def monitor_loop(self):
        while True:
            await self.monitor()
            await asyncio.sleep(5)

    async def sync_loop(self):
        while True:
            started = time.monotonic()
            delay = self.config.section("workflow")["sync_interval_seconds"]
            try:
                await self.repliz.sync(self.store)
                self.notify_work()
                self.store.health('account_sync', 'ok', duration_ms=(time.monotonic()-started)*1000)
            except Exception as exc:
                self.store.record("repliz_sync_failed", error=safe_error(exc))
                self.store.set('sync_error', safe_error(exc))
                self.store.health('account_sync', 'error', reason=safe_error(exc),
                                  duration_ms=(time.monotonic()-started)*1000)
                delay = min(delay, 30)
            await asyncio.sleep(delay)
