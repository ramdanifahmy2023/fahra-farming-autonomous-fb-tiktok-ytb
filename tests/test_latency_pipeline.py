import asyncio
import copy
import os
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

import test_pipeline as baseline
from fahra_automation.db import Store
from fahra_automation.personas import DEFAULT_PROFILE, PersonaService
from fahra_automation.repliz import ReplizError
from fahra_automation.util import iso


class LatencyPipelineTest(unittest.IsolatedAsyncioTestCase):
    rows = baseline.PipelineTest.rows
    lease = baseline.PipelineTest.lease

    def setUp(self):
        baseline.PipelineTest.setUp(self)
        self.config.data['downloader']['project_dir'] = 'fixture-mcp'
        self.config.data['drive']['sync_dir'] = 'fixture-bank'

    def storage_volumes(self):
        database = self.root / 'internal-db/workflow.sqlite3'
        database.parent.mkdir()
        self.store.path.rename(database)
        self.store = Store(database)
        self.pipeline.store = self.store
        self.config.data['workflow']['min_free_disk_gb'] = 10
        self.config.data['drive']['sync_dir'] = 'bank-volume/content'
        self.config.data['downloader']['project_dir'] = 'mcp-volume/project'
        volumes = {'database': database.parent, 'media': self.config.runtime,
                   'bank': self.root / 'bank-volume', 'downloader': self.root / 'mcp-volume'}
        for path in volumes.values():
            path.mkdir(exist_ok=True)
        return volumes

    @contextmanager
    def disk_space(self, volumes, *, low=None, shared_device=False):
        stat = Path.stat
        def device(path):
            if shared_device:
                return 1
            return next((index for index, root in enumerate(volumes.values(), start=1)
                         if path.is_relative_to(root)), 0)
        def fake_stat(path, *args, **kwargs):
            result = list(stat(path, *args, **kwargs))
            result[2] = device(path)
            return os.stat_result(result)
        def usage(path):
            free = (1 if low and Path(path).is_relative_to(volumes[low]) else 100) * 1024**3
            return SimpleNamespace(free=free)
        with patch.object(Path, 'stat', new=fake_stat), patch('fahra_automation.pipeline.shutil.disk_usage', side_effect=usage) as checked:
            yield checked

    async def test_low_media_bank_or_downloader_volume_holds_before_any_io_stage(self):
        volumes = self.storage_volumes()
        self.pipeline.downloader = SimpleNamespace(download=AsyncMock())
        self.pipeline.analyzer = SimpleNamespace(ready=Mock(), analyze=AsyncMock())
        self.pipeline.media = SimpleNamespace(probe=Mock(), render=Mock(), validate_shared=Mock())
        self.drive.ready, self.drive.upload = Mock(), Mock()
        for volume in ('media', 'bank', 'downloader'):
            for stage in ('queued', 'analyzed', 'rendered'):
                with self.subTest(volume=volume, stage=stage):
                    self.store.asset_checkpoint(self.asset_id, stage)
                    job = self.lease()
                    with self.disk_space(volumes, low=volume) as checked:
                        await self.pipeline.handle(job)
                    current = self.store.job(job['id'])
                    self.assertEqual(current['status'], stage)
                    self.assertEqual(current['attempts'], 0)
                    self.assertEqual(current['leased'], 0)
                    self.assertIn('Ruang disk', current['last_error'])
                    paths = [Path(call.args[0]) for call in checked.call_args_list]
                    self.assertIn(volumes['database'], paths)
                    self.assertIn(volumes[volume], paths)
                    self.pipeline.downloader.download.assert_not_awaited()
                    self.pipeline.analyzer.analyze.assert_not_awaited()
                    self.pipeline.media.probe.assert_not_called()
                    self.pipeline.media.render.assert_not_called()
                    self.drive.upload.assert_not_called()
                    self.remote.create.assert_not_awaited()
                    self.assertIsNone(self.journal.state(job['id']))

    def test_free_space_checks_existing_ancestors_for_all_distinct_volumes(self):
        volumes = self.storage_volumes()
        self.assertFalse((self.config.runtime / 'media').exists())
        self.assertFalse(self.config.path(self.config.section('drive')['sync_dir']).exists())
        self.assertFalse((self.config.path(self.config.section('downloader')['project_dir']) / 'downloads/mcp').exists())
        with self.disk_space(volumes) as checked:
            self.pipeline.require_free_space()
        self.assertEqual([Path(call.args[0]) for call in checked.call_args_list], list(volumes.values()))

    def test_same_volume_is_measured_once_without_creating_missing_directories(self):
        volumes = self.storage_volumes()
        with self.disk_space(volumes, shared_device=True) as checked:
            self.pipeline.require_free_space()
        checked.assert_called_once_with(volumes['database'])
        self.assertFalse((self.config.runtime / 'media').exists())
        self.assertFalse(self.config.path(self.config.section('drive')['sync_dir']).exists())
        self.assertFalse(self.config.path(self.config.section('downloader')['project_dir']).exists())

    def install_personas(self, effect=None):
        async def metadata(job, binding, folder):
            if effect:
                effect(job, binding)
            return {'title': 'Kucing penasaran', 'caption': 'Kucing melihat kardus.',
                    'description': 'Kucing melihat kardus. #kucing',
                    'hashtags': ['#kucing'], 'tags': ['kucing']}
        self.ai = SimpleNamespace(delivery_metadata=AsyncMock(side_effect=metadata))
        service = PersonaService(self.config, self.store, self.ai)
        self.pipeline.personas = service
        return service

    async def candidate(self):
        service = self.install_personas()
        job = self.lease()
        metadata = await service.prepare_delivery(job)
        current = self.store.job(job['id'])
        self.assertEqual(current['payload']['delivery_candidate'], metadata)
        self.assertNotIn('delivery_metadata', current['payload'])
        return service, current, metadata

    async def test_snapshot_change_keeps_candidate_and_restart_retry_avoids_ai(self):
        self.install_personas(lambda *_: self.store.begin_sync())
        job = self.lease()
        await self.pipeline.handle(job)
        waiting = self.store.job(job['id'])
        candidate = copy.deepcopy(waiting['payload']['delivery_candidate'])
        self.assertEqual(waiting['status'], 'drive_ready')
        self.assertEqual(waiting['attempts'], 0)
        self.assertNotIn('delivery_metadata', waiting['payload'])
        self.assertIsNone(self.journal.state(job['id']))
        self.drive.verify_delivery.assert_not_awaited()
        self.remote.create.assert_not_awaited()

        self.store.apply_snapshot(self.accounts, [], '2026-10-16', '2027-10-17')
        restarted = Store(self.store.path)
        self.pipeline.personas = PersonaService(self.config, restarted, self.ai)
        await self.pipeline.handle(self.lease())
        completed = self.store.job(job['id'])
        self.assertEqual(completed['status'], 'scheduled')
        self.assertEqual(completed['payload']['delivery_metadata'], candidate)
        self.ai.delivery_metadata.assert_awaited_once()
        self.drive.verify_delivery.assert_awaited_once()
        self.remote.create.assert_awaited_once()
        self.assertEqual(self.journal.state(job['id'])['state'], 'accepted')

    async def test_candidate_for_other_account_is_not_reused(self):
        service, job, old = await self.candidate()
        other = {**self.accounts[0], 'id': 'tiktok-two', 'username': 'other_fixture'}
        self.store.apply_snapshot(self.accounts + [other], [], '2026-10-16', '2027-10-17')
        with self.store.tx() as connection:
            connection.execute('UPDATE jobs SET account_id=? WHERE id=?', ('tiktok-two', job['id']))
        result = await service.prepare_delivery(self.store.job(job['id']))
        self.assertEqual(result['account_id'], 'tiktok-two')
        self.assertNotEqual(result['persona_id'], old['persona_id'])
        self.assertEqual(self.ai.delivery_metadata.await_count, 2)
        self.assertEqual(self.store.job(job['id'])['payload']['delivery_candidate'], result)

    async def test_unaccepted_candidate_follows_changed_persona_version(self):
        service, job, old = await self.candidate()
        binding = service.registry.bind(job['account_id'], job['platform'],
            {**DEFAULT_PROFILE, 'instructions': 'Bahasa Indonesia formal, tanpa emoji.'})
        result = await service.prepare_delivery(self.store.job(job['id']))
        self.assertEqual(result['persona_version'], binding['persona_version'])
        self.assertNotEqual(result['persona_hash'], old['persona_hash'])
        self.assertEqual(self.ai.delivery_metadata.await_count, 2)
        self.assertEqual(self.ai.delivery_metadata.await_args.args[1]['profile'], binding['profile'])

    async def test_changed_evidence_regenerates_candidate(self):
        service, job, old = await self.candidate()
        self.store.checkpoint(job['id'], payload={'transcript': {'text': 'Ada suara pintu.'}})
        result = await service.prepare_delivery(self.store.job(job['id']))
        self.assertNotEqual(result['evidence_hash'], old['evidence_hash'])
        self.assertEqual(self.ai.delivery_metadata.await_count, 2)
        self.assertEqual(self.store.job(job['id'])['payload']['delivery_candidate'], result)

    async def test_candidate_is_not_saved_if_evidence_changes_during_ai(self):
        job = self.lease()
        def change_evidence(*_):
            self.store.checkpoint(job['id'], payload={'render_sha256': 'b' * 64})
        service = self.install_personas(change_evidence)
        await service.prepare_delivery(job)
        self.assertNotIn('delivery_candidate', self.store.job(job['id'])['payload'])
        service.analyzer.delivery_metadata.side_effect = None
        service.analyzer.delivery_metadata.return_value = {'caption': 'Kucing melihat kardus.'}
        await service.prepare_delivery(self.store.job(job['id']))
        self.assertEqual(self.ai.delivery_metadata.await_count, 2)

    async def test_evidence_change_during_ai_blocks_stale_metadata_before_drive(self):
        job = self.lease()
        def change_evidence(*_):
            self.store.checkpoint(job['id'], payload={'render_sha256': 'b' * 64})
        self.install_personas(change_evidence)
        await self.pipeline.handle(job)
        current = self.store.job(job['id'])
        self.drive.verify_delivery.assert_not_awaited()
        self.remote.create.assert_not_awaited()
        self.assertIsNone(self.journal.state(job['id']))
        self.assertEqual(current['status'], 'drive_ready')
        self.assertEqual(current['attempts'], 0)
        self.assertNotIn('delivery_metadata', current['payload'])

    async def test_drive_manifest_change_during_verification_prevents_intent(self):
        job = self.lease()
        async def change_manifest(*_):
            self.store.checkpoint(job['id'], payload={
                'drive_url': 'https://drive.usercontent.google.com/download?id=changedfixture'})
            return {'sha256': 'a' * 64, 'size': 2048}
        self.drive.verify_delivery.side_effect = change_manifest
        await self.pipeline.handle(job)
        current = self.store.job(job['id'])
        self.drive.verify_delivery.assert_awaited_once()
        self.remote.create.assert_not_awaited()
        self.assertIsNone(self.journal.state(job['id']))
        self.assertEqual(current['status'], 'drive_ready')
        self.assertEqual(current['attempts'], 0)
        self.assertEqual(current['leased'], 0)

    async def test_slot_expiring_during_ai_skips_drive_and_intent_then_reuses_copy(self):
        job = self.lease()
        lead = self.config.section('workflow')['schedule_lead_seconds']
        self.at = datetime.fromisoformat(job['planned_at']) - timedelta(seconds=lead + 5)
        self.store.apply_snapshot(self.accounts, [], '2026-10-16', '2027-10-17')
        def consume_slot(*_):
            self.at += timedelta(seconds=6)
        self.install_personas(consume_slot)
        await self.pipeline.handle(self.lease())
        waiting = self.store.job(job['id'])
        metadata = copy.deepcopy(waiting['payload']['delivery_metadata'])
        self.assertEqual(waiting['status'], 'drive_ready')
        self.assertEqual(waiting['next_attempt'], iso(self.at))
        self.assertEqual(waiting['attempts'], 0)
        self.assertEqual(waiting['leased'], 0)
        self.assertIn('Slot jadwal', waiting['last_error'])
        self.drive.verify_delivery.assert_not_awaited()
        self.remote.create.assert_not_awaited()
        self.assertIsNone(self.journal.state(job['id']))

        await self.pipeline.handle(self.lease())
        completed = self.store.job(job['id'])
        self.assertEqual(completed['status'], 'scheduled')
        self.assertNotEqual(completed['planned_at'], job['planned_at'])
        self.assertEqual(completed['payload']['delivery_metadata'], metadata)
        self.ai.delivery_metadata.assert_awaited_once()
        self.drive.verify_delivery.assert_awaited_once()
        self.remote.create.assert_awaited_once()

    async def test_two_preparers_and_sender_advance_while_first_download_waits(self):
        self.config.data['workflow']['min_free_disk_gb'] = 0
        self.store.enqueue(2, 2, 2, [
            ('https://www.instagram.com/reel/SLOW/', 'SLOW'),
            ('https://www.instagram.com/reel/FAST/', 'FAST')])
        waiting_download, reached_analysis = asyncio.Event(), asyncio.Event()
        never = asyncio.Event()
        source = self.root / 'source-fixture.mp4'
        source.write_bytes(b'offline downloader fixture, not a real MP4')
        downloads = []
        async def download(asset, media_store):
            downloads.append(asset['id'])
            if asset['source_id'] == 'SLOW':
                waiting_download.set()
                await never.wait()
            return {'source_path': str(source)}
        async def analyze(*args, **kwargs):
            reached_analysis.set()
            await never.wait()
        self.pipeline.downloader = SimpleNamespace(download=AsyncMock(side_effect=download))
        self.pipeline.analyzer = SimpleNamespace(ready=Mock(), analyze=AsyncMock(side_effect=analyze))
        self.pipeline.media = SimpleNamespace(probe=Mock(return_value=self.probe),
            validate_shared=Mock(), validate_delivery=Mock())
        self.drive.ready = Mock()
        self.remote.create.side_effect = ['schedule-tt', 'schedule-fb', 'schedule-yt']
        tasks = [asyncio.create_task(self.pipeline.worker_loop('prepare', index)) for index in range(2)]
        try:
            await asyncio.wait_for(waiting_download.wait(), timeout=3)
            await asyncio.wait_for(reached_analysis.wait(), timeout=3)
            self.assertEqual(len(downloads), 2)
            self.assertEqual(len(set(downloads)), 2)
            tasks.append(asyncio.create_task(self.pipeline.worker_loop('send')))
            async def sent_all():
                while any(job['status'] != 'scheduled' for job in self.store.asset_deliveries(self.asset_id)):
                    await asyncio.sleep(.001)
            await asyncio.wait_for(sent_all(), timeout=3)
            by_source = {job['source_id']: job for job in self.rows()}
            self.assertEqual(by_source['SLOW']['status'], 'queued')
            self.assertEqual(by_source['FAST']['status'], 'downloaded')
            self.assertEqual(self.remote.create.await_count, 3)
            self.assertEqual(self.drive.verify_delivery.await_count, 3)
            self.assertFalse(never.is_set())
        finally:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results), results)
        self.assertFalse(any(job['leased'] for job in self.rows()))
        with self.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM assets WHERE leased_job_id IS NOT NULL').fetchone()[0], 0)

    async def test_schedule_404_backs_off_without_resending_or_releasing_quota(self):
        job = self.lease()
        await self.pipeline.handle(job)
        self.remote.status.side_effect = ReplizError(404, 'Schedule not visible')
        self.store.checkpoint(job['id'], payload={'next_poll': ''})
        for expected in (90, 180, 360, 720, 1440, 1800, 1800):
            await self.pipeline.monitor()
            current = self.store.job(job['id'])
            next_at = datetime.fromisoformat(current['payload']['next_poll'])
            self.assertEqual((next_at - self.at).total_seconds(), expected)
            self.assertEqual(current['status'], 'scheduled')
            self.assertEqual(current['counts_quota'], 1)
            calls = self.remote.status.await_count
            self.at = next_at - timedelta(seconds=1)
            await self.pipeline.monitor()
            self.assertEqual(self.remote.status.await_count, calls)
            self.at = next_at
        self.remote.create.assert_awaited_once()
        self.remote.reconcile.assert_not_awaited()
        self.assertEqual(self.journal.state(job['id'])['state'], 'accepted')

    async def test_late_poll_error_cannot_downgrade_webhook_verified_publication(self):
        job = self.lease()
        await self.pipeline.handle(job)
        scheduled = self.store.job(job['id'])
        response = {**scheduled['payload']['submission'], 'id': scheduled['schedule_id']}
        first_get_started, release_old_error = asyncio.Event(), asyncio.Event()
        calls = 0
        async def status(schedule_id):
            nonlocal calls
            calls += 1
            self.assertEqual(schedule_id, scheduled['schedule_id'])
            if calls == 1:
                first_get_started.set()
                await release_old_error.wait()
                return {**response, 'status': 'error', 'errorMessage': 'Late stale error fixture'}
            return {**response, 'status': 'success', 'postId': '123456789012345',
                    'postUrl': 'https://www.tiktok.com/@tiktok_person/video/123456789012345'}
        self.remote.status.side_effect = status
        self.store.checkpoint(job['id'], payload={'next_poll': ''})
        old_poll = asyncio.create_task(self.pipeline.monitor())
        try:
            await asyncio.wait_for(first_get_started.wait(), 3)
            self.assertTrue(await self.pipeline.monitor(job['id']))
            published = self.store.job(job['id'])
            self.assertEqual(published['status'], 'published')
            release_old_error.set()
            self.assertTrue(await asyncio.wait_for(old_poll, 3))
        finally:
            release_old_error.set()
            if not old_poll.done():
                old_poll.cancel()
            await asyncio.gather(old_poll, return_exceptions=True)
        current = self.store.job(job['id'])
        self.assertEqual(current['status'], 'published')
        self.assertEqual(current['payload'], published['payload'])
        self.assertEqual(current['counts_quota'], 1)
        self.assertIsNone(current['last_error'])
        self.assertEqual(self.remote.status.await_count, 2)
        self.remote.create.assert_awaited_once()
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("""SELECT count(*) FROM events WHERE job_id=?
                AND kind='status_changed' AND json_extract(detail,'$.status')='failed'""",
                (job['id'],)).fetchone()[0], 0)

    async def test_repeated_webhook_monitor_for_published_job_is_idempotent_without_get(self):
        job = self.lease()
        await self.pipeline.handle(job)
        scheduled = self.store.job(job['id'])
        self.remote.status.return_value = {**scheduled['payload']['submission'],
            'id': scheduled['schedule_id'], 'status': 'success', 'postId': '123456789012345',
            'postUrl': 'https://www.tiktok.com/@tiktok_person/video/123456789012345'}
        self.assertTrue(await self.pipeline.monitor(job['id']))
        published = self.store.job(job['id'])
        self.remote.status.reset_mock()
        self.remote.reconcile.reset_mock()
        self.assertTrue(await self.pipeline.monitor(job['id']))
        self.assertEqual(self.store.job(job['id']), published)
        self.remote.status.assert_not_awaited()
        self.remote.reconcile.assert_not_awaited()
        self.remote.create.assert_awaited_once()

    async def test_preserve_published_checkpoint_cannot_change_evidence_quota_or_intent(self):
        job = self.lease()
        await self.pipeline.handle(job)
        self.store.checkpoint(job['id'], 'published', payload={'post_id': '123456789012345'}, leased=0)
        published = self.store.job(job['id'])
        with self.store.connect() as connection:
            events = connection.execute('SELECT count(*) FROM events').fetchone()[0]
            attempt = tuple(connection.execute('SELECT * FROM submission_attempts WHERE job_id=?',
                                               (job['id'],)).fetchone())
        changed = self.store.checkpoint(job['id'], 'failed', preserve_published=True,
            payload={'post_id': None, 'poll_failures': 9}, schedule_id=None, counts_quota=0,
            last_error='Late stale error fixture', leased=1)
        self.assertIs(changed, False)
        self.assertEqual(self.store.job(job['id']), published)
        with self.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM events').fetchone()[0], events)
            self.assertEqual(tuple(connection.execute('SELECT * FROM submission_attempts WHERE job_id=?',
                                                     (job['id'],)).fetchone()), attempt)

    async def test_unknown_poll_errors_never_delay_reconciliation_over_300_seconds(self):
        job = self.lease()
        self.remote.create.side_effect = httpx.ReadTimeout('fixture lost response')
        await self.pipeline.handle(job)
        initial = self.store.job(job['id'])
        self.assertEqual(initial['status'], 'submit_unknown')
        self.remote.reconcile.side_effect = ReplizError(404, 'Not visible yet')
        for expected in (90, 180, 300, 300, 300):
            await self.pipeline.monitor()
            current = self.store.job(job['id'])
            next_at = datetime.fromisoformat(current['payload']['next_poll'])
            self.assertEqual((next_at - self.at).total_seconds(), expected)
            self.assertEqual(current['status'], 'submit_unknown')
            self.assertEqual(current['counts_quota'], 1)
            self.assertEqual(current['payload']['submission'], initial['payload']['submission'])
            self.at = next_at
        self.remote.create.assert_awaited_once()
        self.remote.status.assert_not_awaited()
        self.assertEqual(self.journal.state(job['id'])['state'], 'unknown')
        with self.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM submission_attempts').fetchone()[0], 1)

    async def test_failed_schedule_404_eventually_polls_every_six_hours(self):
        job = self.lease()
        await self.pipeline.handle(job)
        self.store.checkpoint(job['id'], 'failed', payload={'next_poll': ''})
        self.remote.status.side_effect = ReplizError(404, 'Deleted remote schedule fixture')
        delays = []
        for _ in range(11):
            await self.pipeline.monitor()
            current = self.store.job(job['id'])
            next_at = datetime.fromisoformat(current['payload']['next_poll'])
            delays.append((next_at - self.at).total_seconds())
            self.assertEqual(current['status'], 'failed')
            self.assertEqual(current['counts_quota'], 1)
            self.at = next_at
        self.assertEqual(delays, [90, 180, 360, 720, 1440, 2880, 5760, 11520, 21600, 21600, 21600])
        self.remote.create.assert_awaited_once()
        self.assertEqual(self.journal.state(job['id'])['state'], 'accepted')
