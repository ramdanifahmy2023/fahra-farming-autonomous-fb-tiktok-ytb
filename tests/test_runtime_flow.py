import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import test_operations as operations
import test_replies as replies
import test_foundation as foundation
from test_repliz import FixtureConfig

from fahra_automation.repliz import Repliz, SharedRateLimiter, MutationNotSent
from fahra_automation.reply_store import ReplyWait


class RequestFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_slow_get_leaves_one_lane_open_and_requests_remain_bounded(self):
        entered, release = asyncio.Event(), asyncio.Event()
        active = maximum = 0
        async def transport(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                if request.url.path == '/slow':
                    entered.set()
                    await release.wait()
                return httpx.Response(200, json={})
            finally:
                active -= 1
        async with httpx.AsyncClient(base_url='https://flow.invalid', transport=httpx.MockTransport(transport)) as client:
            api = Repliz(FixtureConfig(), client=client)
            slow = asyncio.create_task(api.request('GET', '/slow'))
            await entered.wait()
            try:
                await asyncio.wait_for(asyncio.gather(*(api.request('GET', '/fast') for _ in range(5))), 1)
                self.assertFalse(slow.done())
                self.assertEqual(maximum, 2)
            finally:
                release.set()
                await slow

    async def test_guard_runs_after_capacity_wait_and_sends_no_post(self):
        full, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def transport(request):
            calls.append(request.method)
            if len(calls) == 2:
                full.set()
            await release.wait()
            return httpx.Response(200, json={})
        config = FixtureConfig()
        async with httpx.AsyncClient(base_url='https://capacity.invalid', transport=httpx.MockTransport(transport)) as client:
            api = Repliz(config, client=client)
            reads = [asyncio.create_task(api.request('GET', '/read')) for _ in range(2)]
            await full.wait()
            write = asyncio.create_task(api.request('POST', '/write'))
            await asyncio.sleep(0)
            config.live = False
            release.set()
            await asyncio.gather(*reads)
            with self.assertRaises(MutationNotSent):
                await write
            self.assertEqual(calls, ['GET', 'GET'])

    async def test_mutations_stay_serial_while_get_is_available(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def transport(request):
            calls.append((request.method, request.url.path))
            if request.url.path == '/first':
                entered.set()
                await release.wait()
            return httpx.Response(200, json={})
        async with httpx.AsyncClient(base_url='https://mutations.invalid', transport=httpx.MockTransport(transport)) as client:
            api = Repliz(FixtureConfig(), client=client)
            first = asyncio.create_task(api.request('POST', '/first'))
            await entered.wait()
            second = asyncio.create_task(api.request('POST', '/second'))
            await asyncio.wait_for(api.request('GET', '/read'), 1)
            self.assertNotIn(('POST', '/second'), calls)
            release.set()
            await asyncio.gather(first, second)
            self.assertEqual(calls[-1], ('POST', '/second'))

    async def test_cooldown_extended_during_wait_is_respected(self):
        limiter = SharedRateLimiter(0)
        clock = [10.]
        limiter.next_at = 13.
        waits = []
        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 1:
                clock[0] = 11.
                limiter.defer(7)
            else:
                clock[0] += seconds
        with patch('fahra_automation.repliz.time.monotonic', side_effect=lambda: clock[0]), patch('fahra_automation.repliz.asyncio.sleep', side_effect=sleep):
            await limiter.wait()
        self.assertEqual(waits, [3., 7.])
        self.assertEqual(clock[0], 18.)

    async def test_sync_has_deadline_and_cannot_apply_partial_snapshot(self):
        config = FixtureConfig()
        config.section = lambda name: {'base_url':'https://deadline.invalid','sync_timeout_seconds':.02}
        async with httpx.AsyncClient() as client:
            api = Repliz(config, client=client)
            async def stalled(*args):
                await asyncio.Event().wait()
            api.pages = AsyncMock(side_effect=stalled)
            store = SimpleNamespace(begin_sync=Mock(return_value=1), apply_snapshot=Mock())
            with self.assertRaises(TimeoutError):
                await api.sync(store)
            store.apply_snapshot.assert_not_called()


class AccountIdentityTest(unittest.IsolatedAsyncioTestCase):
    setUp = foundation.PipelineTest.setUp
    ready = foundation.PipelineTest.ready

    async def test_page_and_channel_names_do_not_invalidate_matching_username(self):
        self.ready()
        async def account(aid, platform):
            return {'username':self.store.account_username(aid,identity=True) + ' ',
                    'name':'Different display name', 'type':platform}
        self.repliz.check_account.side_effect = account
        generation = self.store.snapshot_generation()
        for _ in range(2):
            await self.pipeline.handle(self.store.claim())
        self.assertEqual(self.repliz.create.await_count,2)
        self.assertEqual(self.store.snapshot_generation(),generation)
        self.assertTrue(self.store.snapshot_fresh())
        self.assertEqual(self.store.job(self.fb['id'])['status'],'scheduled')
        self.assertEqual(self.store.job(self.yt['id'])['status'],'scheduled')

    async def test_real_username_change_still_blocks_before_post(self):
        self.ready()
        self.repliz.check_account.return_value = {'username':'renamed_account'}
        await self.pipeline.handle(self.store.claim())
        self.repliz.create.assert_not_awaited()
        self.assertFalse(self.store.snapshot_fresh())
        self.assertIn('Identitas akun berubah',self.store.job(self.fb['id'])['last_error'])


class JournalResultTest(unittest.TestCase):
    setUp = operations.OperationTests.setUp
    delivery = operations.OperationTests.delivery

    def accepted(self):
        job, body = self.delivery()
        self.journal.begin(job['id'], body)
        self.journal.mark(job['id'], 'accepted', schedule_id='s1')
        self.store.checkpoint(job['id'], 'failed', schedule_id='s1', last_error='Provider error')
        return job, body

    def test_restart_preserves_remote_failure_and_quota(self):
        job, body = self.accepted()
        self.journal.reconcile_store(self.store)
        current = self.store.job(job['id'])
        self.assertEqual((current['status'],current['schedule_id'],current['counts_quota']), ('failed','s1',1))
        self.assertEqual(current['last_error'], 'Provider error')
        self.assertFalse(self.store.retry(job['id']))

    def test_snapshot_error_cannot_look_scheduled_and_success_still_needs_evidence(self):
        job, body = self.accepted()
        self.store.checkpoint(job['id'],'scheduled')
        row = {**body,'id':'s1','status':'error','errorMessage':'Timeout provider'}
        accounts = self.store.accounts()
        accounts = [dict(a, type=a['platform'],isConnected=True,publishEligible=True) for a in accounts]
        self.store.apply_snapshot(accounts,[row],'2026-10-15','2026-10-19')
        self.assertEqual(self.store.job(job['id'])['status'],'failed')
        self.journal.reconcile_store(self.store)
        self.assertEqual(self.store.job(job['id'])['status'],'failed')
        self.store.apply_snapshot(accounts,[dict(row,status='success')],'2026-10-15','2026-10-19')
        self.journal.reconcile_store(self.store)
        self.assertEqual(self.store.job(job['id'])['status'],'scheduled')

    def test_conflicting_schedule_ids_hold_recovery(self):
        job, body = self.accepted()
        self.store.checkpoint(job['id'], schedule_id='different')
        result = self.journal.reconcile_store(self.store)
        self.assertEqual(result['unresolved'],[job['id']])
        self.assertEqual(self.store.job(job['id'])['schedule_id'],'different')

    def test_identical_acceptance_does_not_grow_journal(self):
        job, body = self.accepted()
        before = len(list(self.journal.folder(job['id']).glob('*.json')))
        for _ in range(10):
            self.journal.mark(job['id'],'accepted',schedule_id='s1')
        self.assertEqual(len(list(self.journal.folder(job['id']).glob('*.json'))),before)


class WaitingHealthTest(unittest.IsolatedAsyncioTestCase):
    setUp = replies.ReplyTest.setUp

    async def test_temporary_snapshot_wait_does_not_trigger_error_alerts(self):
        for component, method, loop in (
                ('reply_poll','ingest','polling_loop'),
                ('reply_profiles','profile_once','profile_loop'),
                ('reply_worker','process_one','worker_loop')):
            self.store.set('reply_enabled',True)
            with patch.object(self.worker,method,AsyncMock(side_effect=ReplyWait('Snapshot sedang diperbarui.'))):
                for _ in range(3):
                    async def stop_wait(awaitable, **kwargs):
                        awaitable.close()
                        raise asyncio.CancelledError()
                    with patch('fahra_automation.replies.asyncio.sleep',AsyncMock(side_effect=asyncio.CancelledError())), \
                            patch('fahra_automation.replies.asyncio.wait_for', side_effect=stop_wait):
                        with self.assertRaises(asyncio.CancelledError):
                            await getattr(self.worker,loop)()
            with self.store.connect() as c:
                row=c.execute('SELECT state,failures FROM health WHERE component=?',(component,)).fetchone()
                self.assertEqual(tuple(row),('waiting',0))
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE kind='health_alert'").fetchone()[0],0)
