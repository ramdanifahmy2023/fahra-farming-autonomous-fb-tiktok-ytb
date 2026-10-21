import asyncio
import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from fahra_automation.comment_profiles import ProfileSweep, custom_reply_config
from fahra_automation.db import Store
from fahra_automation.repliz import Repliz, SharedRateLimiter, ReplizError, api_scope
from fahra_automation.telemetry import TimingBuffer, record_timing, flush
from test_repliz import FixtureConfig, page
import test_comment_profiles as profile_tests
import test_replies as reply_tests
from test_replies import raw_comment


class PriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_weighted_queue_has_two_lanes_and_no_class_starves(self):
        gate, full = asyncio.Event(), asyncio.Event()
        order, active, maximum = [], 0, 0
        async def handler(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            name = request.url.path.strip('/')
            order.append(name)
            try:
                if name.startswith('hold'):
                    if active == 2:
                        full.set()
                    await gate.wait()
                await asyncio.sleep(.001)
                return httpx.Response(200, json={})
            finally:
                active -= 1
        limiter = SharedRateLimiter(0)
        async with httpx.AsyncClient(base_url='https://fixture.invalid', transport=httpx.MockTransport(handler)) as client:
            api = Repliz(FixtureConfig(), client=client, limiter=limiter)
            async def request(scope, path):
                with api_scope(scope):
                    await api.request('GET', '/' + path)
            held = [asyncio.create_task(request('background', 'hold' + str(i))) for i in range(2)]
            await full.wait()
            work = [asyncio.create_task(request(scope, scope)) for scope, count in
                    [('background', 2), ('monitor', 2), ('reply', 4), ('sync', 10), ('publish', 10)] for _ in range(count)]
            await asyncio.sleep(0)
            gate.set()
            await asyncio.wait_for(asyncio.gather(*held, *work), 2)
        self.assertEqual(maximum, 2)
        self.assertEqual(order[2:4], ['publish', 'sync'])
        self.assertLess(order[2:].index('reply'), 10)
        self.assertLess(order[2:].index('background'), 12)
        self.assertEqual(limiter.active, 0)
        self.assertFalse(any(limiter.queues.values()))

    async def test_cancel_queued_and_granted_tickets_returns_capacity(self):
        limiter = SharedRateLimiter(0)
        entered, release = asyncio.Event(), asyncio.Event()
        async def hold():
            async with limiter.acquire('publish', True):
                entered.set()
                await release.wait()
        first = asyncio.create_task(hold())
        await entered.wait()
        waiting = asyncio.create_task(hold())
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        async with limiter.acquire('reply', True):
            self.assertEqual(limiter.active, 1)
        self.assertEqual(limiter.active, 0)
        self.assertFalse(limiter.mutation_active)

    async def test_429_cooldown_is_global_across_priorities_and_clients(self):
        limiter = SharedRateLimiter(0)
        calls = []
        async def handler(request):
            calls.append(time.monotonic())
            return (httpx.Response(429, headers={'retry-after': '1'}, json={}) if len(calls) == 1
                    else httpx.Response(200, json={}))
        async with httpx.AsyncClient(base_url='https://fixture.invalid', transport=httpx.MockTransport(handler)) as client:
            first = Repliz(FixtureConfig(), client=client, limiter=limiter)
            second = Repliz(FixtureConfig(), client=client, limiter=limiter)
            with api_scope('background'), self.assertRaises(ReplizError):
                await first.request('GET', '/limited')
            with api_scope('publish'):
                await second.request('GET', '/important')
        self.assertGreaterEqual(calls[1] - calls[0], .95)


class ParallelSyncTests(unittest.IsolatedAsyncioTestCase):
    async def run_snapshot(self, fail=False, cancel=False):
        accounts = [{'id': 'a' + str(i), 'username': 'fixture', 'type': 'tiktok',
                     'isConnected': True, 'publishEligible': True} for i in range(65)]
        started, release = asyncio.Event(), asyncio.Event()
        active = maximum = stopped = 0
        async def pages(path, params=None):
            nonlocal active, maximum, stopped
            if path == '/public/account':
                return accounts
            active += 1
            maximum = max(maximum, active)
            try:
                if active == 2:
                    started.set()
                await release.wait()
                if fail and params['accountIds'][0] == 'a0':
                    raise ValueError('Incomplete provider page')
                return [{'id': 's-' + params['accountIds'][0], 'accountId': params['accountIds'][0],
                         'type': 'video', 'status': 'pending', 'scheduleAt': '2026-10-21T10:00:00+07:00'}]
            finally:
                active -= 1
                stopped += 1
        store = SimpleNamespace(begin_sync=Mock(return_value=1), accounts=Mock(return_value=[]), apply_snapshot=Mock())
        async with httpx.AsyncClient() as client:
            api = Repliz(FixtureConfig(), client=client)
            api.pages = pages
            task = asyncio.create_task(api.sync(store))
            await asyncio.wait_for(started.wait(), 1)
            store.apply_snapshot.assert_not_called()
            if cancel:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                if fail:
                    with self.assertRaises(ValueError):
                        await task
                else:
                    await task
        self.assertEqual(maximum, 2)
        self.assertEqual(active, 0)
        if fail or cancel:
            store.apply_snapshot.assert_not_called()
        else:
            store.apply_snapshot.assert_called_once()
            self.assertEqual(len(store.apply_snapshot.call_args.args[1]), 3)
        return stopped

    async def test_complete_groups_apply_once_after_all_three_finish(self):
        self.assertEqual(await self.run_snapshot(), 3)

    async def test_failure_and_cancellation_never_apply_partial_groups(self):
        await self.run_snapshot(fail=True)
        await self.run_snapshot(cancel=True)


class InboxTests(unittest.IsolatedAsyncioTestCase):
    def row(self, identity):
        return {'id': identity, 'accountId': 'a', 'status': 'pending'}

    async def test_total_changes_and_overlap_deduplicate_without_relaxing_quota_pages(self):
        responses = [page([self.row('1'), self.row('2')], 3, 1, True),
                     page([self.row('2'), self.row('3')], 4, 2, True), page([self.row('4')], 2, 3)]
        async with httpx.AsyncClient() as client:
            api = Repliz(FixtureConfig(), client=client)
            api.request = AsyncMock(side_effect=copy.deepcopy(responses))
            result = await api.comment_inbox(['a'])
            self.assertEqual([row['id'] for row in result['rows']], ['1', '2', '3', '4'])
            self.assertTrue(result['exhausted'])
            self.assertEqual(dict(api.request.call_args_list[0].kwargs['params'])['status'], 'pending')
            api.request = AsyncMock(side_effect=copy.deepcopy(responses))
            with self.assertRaises(ValueError):
                await api.pages('/public/schedule')

    async def test_bounded_page_window_rechecks_head_then_continues_documented_page(self):
        requested = []
        async with httpx.AsyncClient() as client:
            api = Repliz(FixtureConfig(), client=client)
            async def request(method, path, params):
                number = int(dict(params)['page'])
                requested.append(number)
                return page([self.row(str(number))], 500, number, True)
            api.request = request
            first = await api.comment_inbox(['a'], max_pages=2)
            second = await api.comment_inbox(['a'], max_pages=2, start_page=first['next_page'])
        self.assertEqual(requested, [1, 2, 1, 3])
        self.assertFalse(first['exhausted'])
        self.assertEqual(second['next_page'], 4)

    async def test_foreign_account_and_bad_shape_are_rejected(self):
        async with httpx.AsyncClient() as client:
            api = Repliz(FixtureConfig(), client=client)
            for result in [page([{**self.row('1'), 'accountId': 'other'}]), {'docs': []}]:
                api.request = AsyncMock(return_value=result)
                with self.assertRaises(ValueError):
                    await api.comment_inbox(['a'])


class ParentRetentionTests(unittest.IsolatedAsyncioTestCase):
    setUp = reply_tests.ReplyTest.setUp
    events = reply_tests.ReplyTest.events

    async def test_malformed_comment_does_not_hide_valid_inbox_rows(self):
        valid = raw_comment()
        self.api.comment_inbox = AsyncMock(return_value={'rows': [
            {**valid, 'id': 'invalid', '_id': 'invalid', 'comment': 'malformed'}, valid],
            'exhausted': True, 'pages': 1, 'next_page': 1})
        await self.worker.ingest()
        self.assertEqual(len(self.events()), 1)

    async def test_webhook_wakeup_interrupts_idle_poll_only_for_managed_account(self):
        first, second = asyncio.Event(), asyncio.Event()
        calls = 0
        async def ingest():
            nonlocal calls
            calls += 1
            (first if calls == 1 else second).set()
            return {'inserted': 0, 'exhausted': True}
        self.worker.ingest = ingest
        task = asyncio.create_task(self.worker.polling_loop())
        try:
            await asyncio.wait_for(first.wait(), 1)
            self.assertFalse(self.worker.wake_poll('foreign-account'))
            self.assertTrue(self.worker.wake_poll('account-a'))
            await asyncio.wait_for(second.wait(), 1)
            self.assertEqual(calls, 2)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_pending_filter_retains_resolved_parent_without_replying_to_it(self):
        parent = raw_comment('parent')
        parent['status'] = 'resolved'
        child = raw_comment('child')
        child['comment']['parentId'] = parent['comment']['id']
        self.api.rows = {'inbox-child': child, 'inbox-parent': parent}
        async def collect(ids, *, status='pending', **kwargs):
            return {'rows': [copy.deepcopy(child if status == 'pending' else parent)],
                    'exhausted': True, 'pages': 1, 'next_page': 1}
        self.api.comment_inbox = AsyncMock(side_effect=collect)
        await self.worker.ingest()
        events = self.events()
        self.assertEqual(len(events), 2)
        self.assertEqual(next(e for e in events if e['platform_id'] == 'comment-parent')['status'], 'skipped')
        await self.worker.process_one()
        self.assertEqual(next(e for e in self.events() if e['platform_id'] == 'comment-child')['status'], 'needs_review')
        self.assertEqual(self.api.posts, [])
        self.assertEqual([c.kwargs.get('status', 'pending') for c in self.api.comment_inbox.call_args_list], ['pending', 'resolved'])


class SweepTests(unittest.IsolatedAsyncioTestCase):
    setUp = profile_tests.CommentProfileTest.setUp

    async def test_matching_posts_reuse_immutable_gets_within_sweep_only(self):
        account = {**self.account, 'isConnected': True}
        wanted = custom_reply_config(account['config'], self.profile, 'tiktok')
        rows = [{'id': 'override-' + str(i), 'accountId': account['id'], 'contentId': 'post' + str(i),
                 'config': copy.deepcopy(wanted)} for i in range(2)]
        self.api.request = AsyncMock(return_value=account)
        self.api.pages = AsyncMock(return_value=rows)
        sweep = ProfileSweep()
        for i in range(2):
            job = {'id': 'job' + str(i), 'status': 'published', 'account_id': account['id'],
                   'payload': {'post_id': 'post' + str(i)}}
            self.assertTrue((await self.manager.ensure_post(job, sweep=sweep))['verified'])
        self.assertEqual(self.api.request.await_count, 1)
        self.assertEqual(self.api.pages.await_count, 1)
        returned = await sweep.account(self.api, account['id'])
        returned['config']['reply']['isActive'] = True
        self.assertFalse((await sweep.account(self.api, account['id']))['config']['reply']['isActive'])
        await self.manager.ensure_post(job, sweep=ProfileSweep())
        self.assertEqual(self.api.request.await_count, 2)

    async def test_new_override_rechecks_account_config_before_journal_and_post(self):
        account = {**self.account, 'isConnected': True}
        changed = copy.deepcopy(account)
        changed['config']['message']['text'] = 'Operator changed this module'
        self.api.request = AsyncMock(side_effect=[account, changed])
        self.api.pages = AsyncMock(return_value=[])
        job = {'id': 'job', 'status': 'published', 'account_id': account['id'], 'payload': {'post_id': 'post'}}
        with self.assertRaisesRegex(RuntimeError, 'berubah sebelum create'):
            await self.manager.ensure_post(job)
        self.assertEqual([c.args[0] for c in self.api.request.call_args_list], ['GET', 'GET'])

    async def test_mutation_outside_sweep_invalidates_previous_shared_gets(self):
        account = {**self.account, 'isConnected': True}
        wanted = custom_reply_config(account['config'], self.profile, 'tiktok')
        rows = [{'id': 'override0', 'accountId': account['id'], 'contentId': 'post0', 'config': wanted}]
        async def request(method, path, **kwargs):
            if method == 'GET':
                return copy.deepcopy(account)
            rows.append({'id': 'override1', 'accountId': account['id'],
                         'contentId': kwargs['json']['contentId'], 'config': kwargs['json']['config']})
            return rows[-1]
        self.api.request = AsyncMock(side_effect=request)
        self.api.pages = AsyncMock(side_effect=lambda *args: copy.deepcopy(rows))
        def job(index):
            return {'id': 'job' + str(index), 'status': 'published', 'account_id': account['id'],
                    'payload': {'post_id': 'post' + str(index)}}
        sweep = ProfileSweep()
        await self.manager.ensure_post(job(0), sweep=sweep)
        await self.manager.ensure_post(job(1))
        await self.manager.ensure_post(job(0), sweep=sweep)
        self.assertEqual(self.api.pages.await_count, 4)
        self.assertEqual([c.args[0] for c in self.api.request.call_args_list], ['GET', 'GET', 'GET', 'POST', 'GET'])


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_preserves_aggregate_and_does_not_write_during_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'fixture.sqlite3')
            for value in (2, 7, 3):
                record_timing(store, 'repliz.GET', value)
            with store.connect() as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM timings').fetchone()[0], 0)
            await flush(store)
            with store.connect() as connection:
                row = dict(connection.execute('SELECT * FROM timings').fetchone())
            self.assertEqual((row['samples'], row['total_ms'], row['max_ms'], row['last_ms']), (3, 12, 7, 3))

    async def test_failed_flush_merges_new_samples_and_retry_writes_once(self):
        class Sink:
            def __init__(self):
                self.calls = []
            def timings_batch(self, batch):
                self.calls.append(copy.deepcopy(batch))
                if len(self.calls) == 1:
                    raise OSError('fixture full')
        store = Sink()
        buffer = TimingBuffer(store)
        buffer.add('stage.queued', 4)
        with self.assertRaises(OSError):
            await buffer.flush()
        buffer.add('stage.queued', 8)
        await buffer.flush()
        self.assertEqual(store.calls[-1]['stage.queued'], {'samples': 2, 'total_ms': 12, 'max_ms': 8, 'last_ms': 8})
        self.assertEqual(buffer.samples, {})

    async def test_flush_cancellation_waits_for_commit_and_never_replays_it(self):
        started, release = threading.Event(), threading.Event()
        class Sink:
            calls = 0
            def timings_batch(self, batch):
                self.calls += 1
                started.set()
                release.wait(2)
        store = Sink()
        buffer = TimingBuffer(store)
        buffer.add('repliz.GET', 2)
        task = asyncio.create_task(buffer.flush())
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(.01)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await buffer.flush()
        self.assertEqual(store.calls, 1)
