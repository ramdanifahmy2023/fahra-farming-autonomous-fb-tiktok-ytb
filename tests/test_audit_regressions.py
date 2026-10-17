import asyncio
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import httpx

import test_pipeline as fixtures
from fahra_automation.config import ConfigurationError
from fahra_automation.downloader import Downloader
from fahra_automation.media import Media
from fahra_automation.repliz import Repliz


class AuditRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.PipelineTest.setUp(self)
        self.f = self

    rows = fixtures.PipelineTest.rows
    lease = fixtures.PipelineTest.lease

    def queued(self):
        f = self.f
        with f.store.connect() as c:
            c.execute("UPDATE assets SET status='queued',payload='{}'")
            c.execute("UPDATE jobs SET status='queued',payload='{}'")
        f.pipeline.analyzer = SimpleNamespace(ready=lambda: None, analyze=AsyncMock())
        f.pipeline.drive.ready = lambda: None

    async def test_last_transport_guard_rejects_changed_snapshot_disconnect_and_pilot(self):
        f = self.f
        for mode in ('snapshot', 'disconnect', 'pilot'):
            with self.subTest(mode=mode):
                f.store.set('pilot_source_id', None)
                f.store.apply_snapshot(f.accounts, [], '2026-10-16', '2027-10-17')
                job = f.lease()
                requests = []
                class Limiter:
                    lock = asyncio.Lock()
                    async def wait(self):
                        if mode == 'snapshot':
                            f.store.begin_sync()
                        elif mode == 'disconnect':
                            f.store.disconnect(job['account_id'])
                        else:
                            f.store.set('pilot_source_id', 'OTHER')
                def transport(request):
                    requests.append(request.method)
                    return httpx.Response(200, json={'scheduleId': 'unexpected'})
                client = httpx.AsyncClient(base_url='https://fixture.invalid', transport=httpx.MockTransport(transport))
                remote = Repliz(f.config, client=client, limiter=Limiter())
                remote.check_account = AsyncMock()
                f.pipeline.repliz = remote
                try:
                    with patch('fahra_automation.repliz.now', lambda: f.at):
                        await f.pipeline.handle(job)
                    self.assertEqual(requests, [])
                    self.assertEqual(f.store.job(job['id'])['status'], 'drive_ready')
                    self.assertEqual(f.journal.state(job['id'])['state'], 'not_sent')
                finally:
                    await client.aclose()

    async def test_small_source_fails_all_destinations_before_analysis(self):
        f = self.f
        self.queued()
        source = f.root / 'source.mp4'
        source.write_bytes(b'fixture')
        f.pipeline.downloader = SimpleNamespace(download=AsyncMock(return_value={'source_path': str(source)}))
        f.pipeline.media = SimpleNamespace(probe=lambda path: {**f.probe, 'width': 480, 'height': 854},
                                          validate_shared=Media.validate_shared)
        await f.pipeline.handle(f.lease())
        self.assertEqual([r['status'] for r in f.rows()], ['failed'] * 3)
        self.assertEqual([r['attempts'] for r in f.rows()], [1] * 3)
        self.assertEqual(f.store.asset(f.asset_id)['status'], 'failed')
        f.pipeline.analyzer.analyze.assert_not_awaited()

    async def test_identity_change_at_preflight_holds_before_persona_and_transport(self):
        self.remote.check_account.return_value = {'username':'renamed_account','isConnected':True}
        job = self.lease()
        await self.pipeline.handle(job)
        self.assertFalse(self.store.snapshot_fresh())
        self.assertEqual(self.store.job(job['id'])['status'],'drive_ready')
        self.assertEqual(self.store.job(job['id'])['attempts'],0)
        self.remote.create.assert_not_awaited()
        self.personas.prepare_delivery.assert_not_awaited()

    async def test_transient_preparation_uses_one_budget_for_the_shared_asset(self):
        f = self.f
        self.queued()
        f.pipeline.downloader = SimpleNamespace(download=AsyncMock(side_effect=RuntimeError('temporary fixture error')))
        for attempt in range(4):
            await f.pipeline.handle(f.lease(('tiktok', 'facebook', 'youtube')[attempt % 3]))
        self.assertEqual([r['status'] for r in f.rows()], ['failed'] * 3)
        self.assertEqual([r['attempts'] for r in f.rows()], [4] * 3)
        self.assertEqual(f.pipeline.downloader.download.await_count, 4)

    async def test_remote_failure_can_recover_on_the_same_schedule(self):
        f = self.f
        await f.pipeline.handle(f.lease())
        job = next(r for r in f.rows() if r['platform'] == 'tiktok')
        f.store.checkpoint(job['id'], payload={'next_poll': ''})
        remote = {'id': 'schedule-one', 'accountId': job['account_id'], 'type': 'video',
                  'scheduleAt': job['planned_at'], 'status': 'error', 'errorMessage': 'temporary'}
        f.remote.status.return_value = remote
        await f.pipeline.monitor()
        self.assertEqual(f.store.job(job['id'])['status'], 'failed')
        remote.update(status='success', postId='1234567890123456',
                      postUrl='https://www.tiktok.com/@tiktok_person/video/1234567890123456')
        f.store.apply_snapshot(f.accounts, [remote], '2026-10-16', '2027-10-17')
        await f.pipeline.monitor()
        self.assertEqual(f.store.job(job['id'])['status'], 'published')
        self.assertEqual(f.remote.create.await_count, 1)
        with f.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM outbox WHERE dedupe_key=?", ('published:' + job['id'],)).fetchone()[0], 1)

    async def test_wrapped_configuration_wait_and_uncertain_download_keep_their_meaning(self):
        f = self.f
        self.queued()
        downloader = Downloader(f.config)
        f.pipeline.downloader = downloader
        @asynccontextmanager
        async def stdio(params):
            async with anyio.create_task_group():
                yield None, None
        class Session:
            def __init__(self, *args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def initialize(self): pass
        state = {'job_id': 'fixture-mcp', 'url': 'https://www.instagram.com/reel/FIXTURE/',
                 'status': 'completed', 'files': [{'path': str(f.root / 'fixture.mp4')}]}
        async def call(client, name, args):
            return {'job_id': 'fixture-mcp'} if name == 'start_instagram_download' else state
        downloader.call = AsyncMock(side_effect=call)
        downloader.source_path = lambda *args: f.root / 'fixture.mp4'
        downloader.inspect_source = AsyncMock(side_effect=ConfigurationError('Metadata belum tersedia.'))
        with patch('fahra_automation.downloader.stdio_client', stdio), patch('fahra_automation.downloader.ClientSession', Session):
            for _ in range(4):
                await f.pipeline.handle(f.lease())
            self.assertEqual([r['attempts'] for r in f.rows()], [0] * 3)
            self.assertEqual([r['status'] for r in f.rows()], ['queued'] * 3)
            self.assertNotIn('TaskGroup', f.rows()[0]['last_error'])
            state['status'] = 'partial'
            await f.pipeline.handle(f.lease())
        self.assertEqual([r['status'] for r in f.rows()], ['needs_review'] * 3)
        self.assertEqual(sum(c.args[1] == 'start_instagram_download' for c in downloader.call.await_args_list), 1)
