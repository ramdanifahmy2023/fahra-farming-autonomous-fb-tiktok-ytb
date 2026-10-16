import asyncio
import copy
import threading
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from fahra_automation.attribution import from_metadata
from fahra_automation.config import Config, ConfigurationError
from fahra_automation.db import Store
from fahra_automation.downloader import DownloadUncertain
from fahra_automation.journal import AttemptJournal
from fahra_automation.media import Media
from fahra_automation.pipeline import Pipeline
from fahra_automation.repliz import Repliz, ReplizError, MutationNotSent
from fahra_automation.util import iso


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.at = datetime(2026, 10, 17, 0, tzinfo=timezone.utc)
        for target in ('fahra_automation.db.now', 'fahra_automation.pipeline.now', 'fahra_automation.util.now'):
            p = patch(target, side_effect=lambda: self.at); p.start(); self.addCleanup(p.stop)
        data = copy.deepcopy(Config.load().data)
        data['workflow'].update(runtime_dir='runtime', journal_dir='runtime/attempts', publish_enabled=True)
        self.config = Config(self.root, data)
        self.config.require_live_action = Mock()
        self.store = Store(self.root / 'runtime/workflow.sqlite3')
        self.accounts = [{'id': p + '-one', 'username': p + '_person', 'name': p,
                          'type': p, 'isConnected': True} for p in ('tiktok', 'facebook', 'youtube')]
        self.store.apply_snapshot(self.accounts, [], '2026-10-16', '2027-10-17')
        self.store.enqueue(1, 1, 1, [('https://www.instagram.com/reel/FIXTURE/', 'FIXTURE')])
        self.asset_id = self.rows()[0]['asset_id']
        self.probe = {'duration': 30, 'width': 1080, 'height': 1920, 'codec': 'h264', 'pixel_format': 'yuv420p',
                      'audio': True, 'audio_codec': 'aac', 'fps': 30, 'size': 2048}
        self.store.asset_checkpoint(self.asset_id, 'drive_ready', payload={
            **from_metadata('https://www.instagram.com/reel/FIXTURE/', {'id': 'FIXTURE', 'extractor_key': 'Instagram', 'channel': 'fixture_creator'}),
            'render_probe': self.probe, 'drive_url': 'https://drive.usercontent.google.com/download?id=fixturevideo',
            'generated': {'hook': 'Kucing penasaran', 'caption': 'Kucing melihat kardus.', 'hashtags': ['#kucing']}})
        async def metadata(job):
            return {'account_id': job['account_id'], 'platform': job['platform'], 'persona_version': 1,
                    'title': 'Kucing penasaran', 'caption': 'Kucing melihat kardus.',
                    'description': 'Kucing melihat kardus. #kucing', 'hashtags': ['#kucing'], 'tags': ['kucing']}
        self.personas = SimpleNamespace(prepare_delivery=AsyncMock(side_effect=metadata))
        self.drive = SimpleNamespace(verify_delivery=AsyncMock(return_value={'sha256': 'a'*64, 'size': 2048}))
        self.remote = SimpleNamespace(check_account=AsyncMock(), payload=Repliz.payload,
                                      create=AsyncMock(return_value='schedule-one'), status=AsyncMock(), reconcile=AsyncMock())
        self.journal = AttemptJournal(self.config)
        self.pipeline = Pipeline(self.config, self.store, self.remote, None, Media, None, self.drive,
                                 personas=self.personas, journal=self.journal)

    def rows(self):
        with self.store.connect() as c:
            return [self.store.decode(r) for r in c.execute('SELECT * FROM jobs ORDER BY rowid')]

    def lease(self, platform='tiktok'):
        job = next(j for j in self.rows() if j['platform'] == platform)
        self.store.checkpoint(job['id'], leased=1)
        return self.store.job(job['id'])

    async def test_three_targets_share_asset_verify_every_target_and_send_once(self):
        self.remote.create.side_effect = ['tt-schedule', 'fb-schedule', 'yt-schedule']
        for platform in ('tiktok', 'facebook', 'youtube'):
            await self.pipeline.handle(self.lease(platform))
        self.assertEqual([j['status'] for j in self.rows()], ['scheduled'] * 3)
        self.assertEqual(len({j['asset_id'] for j in self.rows()}), 1)
        self.assertEqual(self.drive.verify_delivery.await_count, 3)
        self.assertEqual(self.remote.check_account.await_count, 3)
        self.assertEqual(self.remote.create.await_count, 3)
        for call in self.remote.create.await_args_list:
            self.assertEqual(call.args[0]['description'].count('source: @fixture_creator'), 1)
        for job in self.rows():
            await self.pipeline.handle(job)
            self.assertEqual(self.journal.state(job['id'])['state'], 'accepted')
        self.assertEqual(self.remote.create.await_count, 3)

    async def test_missing_source_credit_blocks_before_publication_intent(self):
        self.store.asset_checkpoint(self.asset_id, payload={'source_attribution': None})
        job = self.lease()
        await self.pipeline.handle(job)
        current = self.store.job(job['id'])
        self.remote.create.assert_not_awaited()
        self.assertIsNone(self.journal.state(current['id']))
        self.assertEqual(current['attempts'], 0)
        self.assertEqual(current['status'], 'drive_ready')
        self.assertIn('username sumber', current['last_error'])

    async def test_snapshot_invalidated_at_each_await_prevents_any_intent(self):
        for hook in ('account', 'persona', 'drive'):
            with self.subTest(hook=hook):
                self.store.apply_snapshot(self.accounts, [], '2026-10-16', '2027-10-17')
                job = self.lease()
                async def account(*args):
                    if hook == 'account': self.store.begin_sync()
                async def metadata(current):
                    if hook == 'persona': self.store.begin_sync()
                    return {'account_id': current['account_id'], 'platform': current['platform']}
                async def drive(*args):
                    if hook == 'drive': self.store.begin_sync()
                    return {}
                self.remote.check_account.side_effect = account
                self.personas.prepare_delivery.side_effect = metadata
                self.drive.verify_delivery.side_effect = drive
                await self.pipeline.handle(job)
                self.assertEqual(self.store.job(job['id'])['status'], 'drive_ready')
                self.assertIsNone(self.journal.state(job['id']))
        self.remote.create.assert_not_awaited()

    async def test_pilot_gate_holds_other_sources_without_attempt_or_intent(self):
        self.store.set('pilot_source_id', 'PILOT')
        for platform in ('tiktok', 'facebook', 'youtube'):
            job = self.lease(platform)
            await self.pipeline.handle(job)
            current = self.store.job(job['id'])
            self.assertEqual(current['status'], 'drive_ready')
            self.assertEqual(current['attempts'], 0)
            self.assertEqual(current['leased'], 0)
            self.assertIsNone(self.journal.state(job['id']))
        self.remote.check_account.assert_not_awaited()
        self.remote.create.assert_not_awaited()
        self.store.set('pilot_source_id', 'FIXTURE')
        for platform in ('tiktok', 'facebook', 'youtube'):
            await self.pipeline.handle(self.lease(platform))
        self.assertEqual(self.remote.create.await_count, 3)

    async def test_pilot_gate_rechecks_after_preflight_await(self):
        async def change_pilot(*args):
            self.store.set('pilot_source_id', 'OTHER')
        self.remote.check_account.side_effect = change_pilot
        job = self.lease()
        await self.pipeline.handle(job)
        self.remote.create.assert_not_awaited()
        self.assertIsNone(self.journal.state(job['id']))
        self.assertEqual(self.store.job(job['id'])['attempts'], 0)

    async def test_disconnect_during_preflight_never_sends_stale_account(self):
        job = self.lease()
        async def disconnect(*args): self.store.disconnect(job['account_id'])
        self.remote.check_account.side_effect = disconnect
        await self.pipeline.handle(job)
        current = self.store.job(job['id'])
        self.assertIsNone(current['account_id'])
        self.assertEqual(current['status'], 'drive_ready')
        self.remote.create.assert_not_awaited()

    async def test_preflight_timeout_has_no_intent_and_can_retry(self):
        job = self.lease()
        self.remote.check_account.side_effect = httpx.ReadTimeout('')
        await self.pipeline.handle(job)
        self.assertEqual(self.store.job(job['id'])['status'], 'drive_ready')
        self.assertIsNone(self.journal.state(job['id']))
        self.remote.check_account.side_effect = None
        await self.pipeline.handle(self.lease())
        self.assertEqual(self.store.job(job['id'])['status'], 'scheduled')
        self.assertEqual(self.remote.create.await_count, 1)
        self.assertEqual(self.remote.check_account.await_count, 2)

    async def test_post_timeout_stays_unknown_across_restart_and_other_target_continues(self):
        first = self.lease()
        self.remote.create.side_effect = httpx.ReadTimeout('')
        await self.pipeline.handle(first)
        self.assertEqual(self.store.job(first['id'])['status'], 'submit_unknown')
        self.assertEqual(self.journal.state(first['id'])['state'], 'unknown')
        self.store.recover()
        await self.pipeline.handle(self.store.job(first['id']))
        self.remote.create.side_effect = None
        await self.pipeline.handle(self.lease('facebook'))
        self.assertEqual(self.remote.create.await_count, 2)
        self.assertEqual(self.store.job(first['id'])['status'], 'submit_unknown')
        self.assertEqual(next(j['status'] for j in self.rows() if j['platform'] == 'facebook'), 'scheduled')

    async def test_drive_mismatch_blocks_post_and_does_not_consume_attempt_budget(self):
        job = self.lease()
        self.drive.verify_delivery.side_effect = ConfigurationError('Checksum berubah.')
        await self.pipeline.handle(job)
        current = self.store.job(job['id'])
        self.assertEqual(current['status'], 'drive_ready')
        self.assertEqual(current['attempts'], 0)
        self.assertIsNone(self.journal.state(job['id']))
        self.remote.create.assert_not_awaited()

    async def test_atomic_intent_rejection_marks_journal_not_sent(self):
        job = self.lease()
        with patch.object(self.store, 'begin_submission', side_effect=ConfigurationError('Generation berubah.')):
            await self.pipeline.handle(job)
        self.assertEqual(self.journal.state(job['id'])['state'], 'not_sent')
        self.remote.create.assert_not_awaited()
        await self.pipeline.handle(self.lease())
        self.assertEqual(self.store.job(job['id'])['status'], 'scheduled')

    async def test_guard_revoked_before_http_is_safe_to_retry(self):
        job = self.lease()
        self.remote.create.side_effect = MutationNotSent('Owner berubah.')
        await self.pipeline.handle(job)
        self.assertEqual(self.journal.state(job['id'])['state'], 'not_sent')
        current = self.store.job(job['id'])
        self.assertEqual(current['status'], 'drive_ready')
        self.assertFalse(current['payload'].get('submission'))

    async def test_accepted_journal_prevents_create_after_database_rollback(self):
        job = self.lease()
        await self.pipeline.handle(job)
        self.store.checkpoint(job['id'], 'drive_ready', schedule_id=None, leased=1,
                              payload={'submission': None, 'submitted_at': None})
        await self.pipeline.handle(self.store.job(job['id']))
        self.assertEqual(self.remote.create.await_count, 1)
        self.assertEqual(self.journal.state(job['id'])['state'], 'accepted')

    async def test_response_is_journaled_before_database_checkpoint(self):
        job = self.lease()
        checkpoint = self.store.checkpoint
        def fail_scheduled(jid, status=None, **kwargs):
            if status == 'scheduled': raise OSError('database unavailable')
            return checkpoint(jid, status, **kwargs)
        with patch.object(self.store, 'checkpoint', side_effect=fail_scheduled):
            await self.pipeline.handle(job)
        self.assertEqual(self.journal.state(job['id'])['state'], 'accepted')
        self.assertEqual(self.store.job(job['id'])['status'], 'submitting')
        self.store.recover()
        await self.pipeline.handle(self.store.job(job['id']))
        self.assertEqual(self.remote.create.await_count, 1)

    async def test_definite_rate_limit_defers_account_without_resending_other_target(self):
        job = self.lease()
        self.remote.create.side_effect = ReplizError(429, 'rate limit', 120)
        await self.pipeline.handle(job)
        current = self.store.job(job['id'])
        self.assertEqual(current['status'], 'drive_ready')
        self.assertEqual(current['next_attempt'], iso(self.at + timedelta(seconds=120)))
        self.assertEqual(self.journal.state(job['id'])['state'], 'rejected')
        self.assertTrue(next(a for a in self.store.accounts() if a['id'] == job['account_id'])['blocked_until'])

    async def test_monitor_bootstraps_legacy_journal_and_requires_post_evidence(self):
        job = self.lease()
        metadata = await self.personas.prepare_delivery(job)
        job['payload']['delivery_metadata'] = metadata
        body = Repliz.payload(job, datetime.fromisoformat(job['planned_at']))
        self.store.checkpoint(job['id'], 'submit_unknown', payload={'submission': body}, leased=0)
        response = {**body, 'id': 'legacy-schedule', 'status': 'success'}
        self.remote.reconcile.return_value = response
        await self.pipeline.monitor()
        self.assertEqual(self.store.job(job['id'])['status'], 'scheduled')
        self.assertEqual(self.journal.state(job['id'])['state'], 'accepted')
        self.remote.status.return_value = {**response, 'postId': '123456789012345',
            'postUrl': 'https://www.tiktok.com/@person/video/123456789012345'}
        self.store.checkpoint(job['id'], payload={'next_poll': ''})
        await self.pipeline.monitor()
        self.assertEqual(self.store.job(job['id'])['status'], 'published')
        self.remote.create.assert_not_awaited()

    async def test_ambiguous_mcp_holds_all_unsent_destinations(self):
        job = self.lease()
        self.store.checkpoint(job['id'], 'queued')
        self.pipeline.prepare = AsyncMock(side_effect=DownloadUncertain('start unknown'))
        await self.pipeline.handle(self.store.job(job['id']))
        self.assertEqual({j['status'] for j in self.rows()}, {'needs_review'})
        self.assertFalse(any(j['counts_quota'] or j['leased'] for j in self.rows()))
        self.remote.create.assert_not_awaited()

    async def test_all_targets_outside_media_horizon_keep_url_without_download(self):
        self.store.enqueue(1, 1, 2, [(f'https://instagram.com/reel/F{i}/', f'F{i}') for i in range(40)])
        job = self.rows()[-1]
        self.pipeline.downloader = SimpleNamespace(download=AsyncMock())
        self.pipeline.analyzer = SimpleNamespace(ready=Mock())
        await self.pipeline.handle(job)
        self.assertEqual(self.store.job(job['id'])['status'], 'queued')
        self.assertEqual(self.store.job(job['id'])['attempts'], 0)
        self.pipeline.downloader.download.assert_not_awaited()
        self.assertFalse((self.config.runtime / 'media' / job['asset_id']).exists())

    async def test_future_youtube_does_not_block_media_for_current_tiktok(self):
        day = '2026-10-17'
        external = [{'id': f'yt-busy-{i}', 'accountId': 'youtube-one', 'type': 'video', 'status': 'pending',
                     'scheduleAt': f'{day}T02:{i:02d}:00+00:00'} for i in range(20)]
        self.store.apply_snapshot(self.accounts, external, '2026-10-16', '2027-10-17')
        self.store.asset_checkpoint(self.asset_id, 'queued')
        self.pipeline.require_media_horizon(self.asset_id)
        jobs = self.store.asset_deliveries(self.asset_id)
        self.assertEqual(next(j for j in jobs if j['platform'] == 'tiktok')['slot_date'], day)
        self.assertGreater(next(j for j in jobs if j['platform'] == 'youtube')['slot_date'], day)

    async def test_horizon_assigns_previously_unallocated_jobs_when_snapshot_recovers(self):
        self.store.begin_sync()
        self.store.enqueue(1, 1, 3, [('https://instagram.com/reel/UNALLOC/', 'UNALLOC')], can_allocate=False)
        job = self.rows()[-1]
        with self.assertRaises(ConfigurationError): self.pipeline.require_media_horizon(job['asset_id'])
        self.store.apply_snapshot(self.accounts, [], '2026-10-16', '2027-10-17')
        self.pipeline.require_media_horizon(job['asset_id'])
        self.assertTrue(all(j['account_id'] for j in self.store.asset_deliveries(job['asset_id'])))

    async def test_cancelled_local_stage_waits_for_thread_before_releasing_caller(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def work():
            entered.set()
            release.wait(5)
            finished.set()
        task = asyncio.create_task(Pipeline.local_stage(work))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(finished.is_set())
