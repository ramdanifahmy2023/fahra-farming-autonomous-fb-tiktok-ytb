import asyncio
import copy
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import httpx
import test_replies as baseline

from fahra_automation.comment_profiles import CommentProfiles
from fahra_automation.config import ConfigurationError
from fahra_automation.replies import ReplyWorker
from fahra_automation.reply_store import ReplyWait
from fahra_automation.repliz import Repliz, SharedRateLimiter
from fahra_automation.util import iso, now


class ReplySnapshotWaitTest(unittest.IsolatedAsyncioTestCase):
    setUp = baseline.ReplyTest.setUp
    events = baseline.ReplyTest.events
    prepare = baseline.ReplyTest.prepare

    def snapshot(self):
        day = now().date()
        return self.store.apply_snapshot(
            [self.api.account] + self.api.extra_accounts, [], day.isoformat(),
            (day + timedelta(days=366)).isoformat())

    async def drafted(self):
        await self.prepare()
        event = self.events()[0]
        self.worker.db.update(event['id'], 'drafted', draft=copy.deepcopy(baseline.DRAFT),
                              job_id=self.job_id)
        with self.store.tx() as c:
            c.execute('UPDATE comment_events SET attempts=2 WHERE id=?', (event['id'],))
        return self.events()[0]

    def enrollment(self):
        with self.store.connect() as c:
            personas = {
                table: [tuple(row) for row in c.execute(f'SELECT * FROM {table} ORDER BY 1,2')]
                for table in ('persona_accounts', 'persona_versions')
            }
        return {
            'accounts': copy.deepcopy(self.worker.accounts),
            'owners': set(self.worker.own_ids),
            'generation': self.worker.inventory_generation,
            'settings': {key: self.store.get(key) for key in
                         ('reply_account_ids', 'reply_account_since', 'reply_since', 'reply_enabled')},
            'personas': personas,
        }

    def no_intent(self):
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM reply_attempts').fetchone()[0], 0)
        self.assertIsNone(self.worker.journal.state('reply:account-a:comment-1'))
        self.assertFalse(self.api.posts)

    def unchanged_draft(self, before):
        current = self.events()[0]
        for key in ('status', 'draft', 'attempts', 'job_id'):
            self.assertEqual(current[key], before[key], key)
        self.assertEqual(current['leased'], 0)
        self.no_intent()

    async def test_stale_startup_does_not_claim_or_spend_draft_retry_budget(self):
        before = await self.drafted()
        self.worker = ReplyWorker(self.config, self.store, self.api, self.ai)
        self.worker.own_ids = {'owner-1'}
        self.store.begin_sync()
        for _ in range(4):
            await self.worker.process_one()
        self.assertEqual(self.events()[0], before)
        self.assertEqual(self.worker.accounts, {})
        self.no_intent()
        self.ai.context.assert_not_awaited()
        self.ai.draft.assert_not_awaited()

    async def test_fresh_recovery_sends_existing_draft_once_without_regenerating_or_changing_attempts(self):
        before = await self.drafted()
        with self.store.tx() as c:
            c.execute('UPDATE comment_events SET attempts=43 WHERE id=?', (before['id'],))
        before = self.events()[0]
        self.worker.accounts = {}
        self.worker.inventory_generation = None
        self.worker.own_ids = {'owner-1'}
        self.store.begin_sync()
        await self.worker.process_one()
        self.assertEqual(self.events()[0], before)
        self.no_intent()
        self.snapshot()
        await self.worker.process_one()
        current = self.events()[0]
        self.assertEqual(current['status'], 'sent_unverified')
        self.assertEqual(current['attempts'], 43)
        self.assertEqual(current['draft'], before['draft'])
        self.assertEqual(current['leased'], 0)
        self.assertEqual(self.worker.inventory_generation, self.store.snapshot_generation())
        self.assertIn('account-a', self.worker.accounts)
        self.ai.context.assert_not_awaited()
        self.ai.draft.assert_not_awaited()
        await self.worker.process_one()
        self.assertEqual(len(self.api.posts), 1)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM reply_attempts').fetchone()[0], 1)

    async def test_inventory_recovery_keeps_46_legacy_cutoffs_and_enrolls_18_new_accounts(self):
        self.api.extra_accounts = [
            {**copy.deepcopy(self.api.account), 'id': f'{platform}-{index}', 'type': platform,
             'username': f'{platform}{index}', 'generatedId': f'owner-{platform}-{index}'}
            for platform, count in [('tiktok', 45), ('facebook', 14), ('youtube', 4)]
            for index in range(count)
        ]
        legacy_ids = ['account-a'] + [a['id'] for a in self.api.extra_accounts if a['type'] == 'tiktok']
        new_ids = [a['id'] for a in self.api.extra_accounts if a['type'] != 'tiktok']
        legacy_cutoff = iso(now() - timedelta(days=1))
        fresh_cutoff = iso()
        self.store.set('reply_since', legacy_cutoff)
        self.store.set('reply_account_ids', legacy_ids)
        self.store.set('reply_account_since', {})
        before = self.enrollment()
        self.store.begin_sync()
        with self.assertRaises(ReplyWait):
            await self.worker.inventory()
        self.assertEqual(self.enrollment(), before)
        self.snapshot()
        with patch('fahra_automation.reply_store.iso', return_value=fresh_cutoff):
            await self.worker.inventory()
        cutoffs = self.store.get('reply_account_since')
        self.assertEqual(len(self.worker.accounts), 64)
        self.assertEqual(set(self.store.get('reply_account_ids')), set(legacy_ids + new_ids))
        self.assertEqual({cutoffs[identity] for identity in legacy_ids}, {legacy_cutoff})
        self.assertEqual({cutoffs[identity] for identity in new_ids}, {fresh_cutoff})
        self.assertEqual(self.store.get('reply_since'), legacy_cutoff)
        self.assertEqual(len(self.enrollment()['personas']['persona_accounts']), 64)
        with patch('fahra_automation.reply_store.iso', return_value=iso(now() + timedelta(hours=1))):
            await self.worker.inventory()
        self.assertEqual(self.store.get('reply_account_since'), cutoffs)

    async def test_inventory_stale_before_get_preserves_memory_cutoffs_and_personas(self):
        await self.worker.inventory()
        before = self.enrollment()
        self.store.begin_sync()
        original = self.api.pages
        self.api.pages = AsyncMock(side_effect=original)
        with self.assertRaises(ReplyWait):
            await self.worker.inventory()
        self.assertEqual(self.enrollment(), before)
        self.api.pages.assert_not_awaited()

    async def test_inventory_invalidated_during_get_does_not_commit_partial_state(self):
        await self.worker.inventory()
        before = self.enrollment()
        original = self.api.pages
        async def pages(path, params=None):
            result = await original(path, params)
            self.store.begin_sync()
            return result
        self.api.pages = pages
        with self.assertRaises(ReplyWait):
            await self.worker.inventory()
        self.assertEqual(self.enrollment(), before)

    async def test_new_complete_generation_of_65_accounts_cannot_replace_old_inventory(self):
        self.api.extra_accounts = [
            {**copy.deepcopy(self.api.account), 'id': f'account-{index}',
             'username': f'account{index}', 'generatedId': f'owner-{index + 10}'}
            for index in range(63)
        ]
        self.snapshot()
        await self.worker.inventory()
        before = self.enrollment()
        self.assertEqual(len(before['accounts']), 64)
        original = self.api.pages
        async def pages(path, params=None):
            self.api.extra_accounts.append({**copy.deepcopy(self.api.account),
                'id': 'new-65', 'username': 'new65', 'generatedId': 'owner-65'})
            self.snapshot()
            return await original(path, params)
        self.api.pages = pages
        with self.assertRaises(ReplyWait):
            await self.worker.inventory()
        self.assertEqual(len(self.store.eligible_accounts()), 65)
        self.assertEqual(self.enrollment(), before)

    async def test_expired_snapshot_without_generation_change_waits_before_claim(self):
        before = await self.drafted()
        generation = self.store.snapshot_generation()
        self.store.set('repliz_synced_at', iso(now() - timedelta(seconds=self.store.snapshot_ttl + 1)))
        with self.assertRaises(ReplyWait):
            await self.worker.inventory()
        await self.worker.process_one()
        self.assertEqual(self.store.snapshot_generation(), generation)
        self.assertEqual(self.events()[0], before)
        self.no_intent()

    async def test_claim_and_send_reservation_reject_replaced_generation_atomically(self):
        before = await self.drafted()
        generation = self.store.snapshot_generation()
        self.snapshot()
        with self.assertRaises(ReplyWait):
            self.worker.db.claim(generation=generation)
        with self.assertRaises(ReplyWait):
            self.worker.db.reserve_send(self.worker.db.get(before['id']), 20, generation=generation)
        self.assertEqual(self.events()[0], before)
        self.no_intent()

    async def test_comment_read_from_replaced_generation_is_not_ingested(self):
        await self.worker.inventory()
        original = self.api.pages
        async def pages(path, params=None):
            result = await original(path, params)
            if path == '/public/comment':
                self.snapshot()
            return result
        self.api.pages = pages
        with self.assertRaises(ReplyWait):
            await self.worker.ingest()
        self.assertEqual(self.events(), [])
        self.no_intent()

    async def test_sync_begins_during_fresh_account_get_preserves_draft(self):
        before = await self.drafted()
        with self.store.tx() as c:
            c.execute('UPDATE comment_events SET attempts=43 WHERE id=?', (before['id'],))
        before = self.events()[0]
        original = self.api.request
        async def request(method, path, **kwargs):
            result = await original(method, path, **kwargs)
            if method == 'GET' and path == '/public/account/account-a':
                self.store.begin_sync()
            return result
        self.api.request = request
        await self.worker.process_one()
        self.unchanged_draft(before)
        self.ai.draft.assert_not_awaited()

    async def test_complete_generation_changes_during_thread_get_preserves_draft(self):
        before = await self.drafted()
        original = self.api.request
        async def request(method, path, **kwargs):
            result = await original(method, path, **kwargs)
            if method == 'GET' and path.startswith('/public/content/'):
                self.snapshot()
            return result
        self.api.request = request
        await self.worker.process_one()
        self.unchanged_draft(before)
        self.ai.draft.assert_not_awaited()

    async def test_snapshot_change_during_ai_draft_waits_without_intent_or_failure(self):
        await self.prepare()
        async def draft(*args):
            self.store.begin_sync()
            return copy.deepcopy(baseline.DRAFT)
        self.ai.draft.side_effect = draft
        await self.worker.process_one()
        event = self.events()[0]
        self.assertIn(event['status'], {'queued', 'drafted'})
        self.assertEqual(event['attempts'], 0)
        self.assertEqual(event['leased'], 0)
        self.no_intent()

    async def test_profile_response_from_replaced_generation_does_not_change_job_or_draft(self):
        before = await self.drafted()
        with self.store.tx() as c:
            c.execute('UPDATE comment_events SET attempts=45 WHERE id=?', (before['id'],))
        before = self.events()[0]
        original_payload = self.store.job(self.job_id)['payload']
        async def ensure(*args, **kwargs):
            self.snapshot()
            return {'verified': True, 'owner': 'custom'}
        self.worker.profiles.ensure_post.side_effect = ensure
        await self.worker.process_one()
        self.assertEqual(self.store.job(self.job_id)['payload'], original_payload)
        self.unchanged_draft(before)

    async def test_actual_profile_guard_stops_mutation_after_inventory_becomes_stale(self):
        before = await self.drafted()
        with self.store.tx() as c:
            c.execute('UPDATE comment_events SET attempts=45 WHERE id=?', (before['id'],))
        before = self.events()[0]
        original_payload = self.store.job(self.job_id)['payload']
        self.worker.profiles.ensure_post = CommentProfiles.ensure_post.__get__(self.worker.profiles)
        original = self.api.pages
        async def pages(path, params=None):
            result = await original(path, params)
            if path == '/public/automation':
                self.store.begin_sync()
            return result
        self.api.pages = pages
        await self.worker.process_one()
        self.assertEqual(self.store.job(self.job_id)['payload'], original_payload)
        self.unchanged_draft(before)
        state = self.worker.journal.state('automation:account-a:post-1')
        if state is not None:
            self.assertEqual(state['state'], 'not_sent')

    async def test_limiter_snapshot_change_prevents_transport_and_send_intent(self):
        before = await self.drafted()
        with self.store.tx() as c:
            c.execute('UPDATE comment_events SET attempts=44 WHERE id=?', (before['id'],))
        before = self.events()[0]
        worker_store = self.store
        class InvalidatingLimiter(SharedRateLimiter):
            def __init__(self):
                super().__init__(interval=0)
                self.waited = False
            async def wait(self):
                self.waited = True
                worker_store.begin_sync()
            def defer(self, seconds):
                raise AssertionError('No response should reach the limiter.')
        limiter = InvalidatingLimiter()
        transport = AsyncMock(side_effect=AssertionError('Snapshot wait must not call transport.'))
        client = httpx.AsyncClient(base_url='https://fixture.invalid')
        client.request = transport
        wire = Repliz(self.config, client=client, limiter=limiter)
        original = self.api.request
        async def request(method, path, **kwargs):
            if method == 'POST':
                return await wire.request(method, path, **kwargs)
            return await original(method, path, **kwargs)
        self.api.request = request
        try:
            await self.worker.process_one()
        finally:
            await client.aclose()
        self.assertTrue(limiter.waited)
        transport.assert_not_awaited()
        self.unchanged_draft(before)

    async def test_transport_live_guard_before_callback_does_not_create_journal_or_fail_draft(self):
        before = await self.drafted()
        with self.store.tx() as c:
            c.execute('UPDATE comment_events SET attempts=43 WHERE id=?', (before['id'],))
        before = self.events()[0]
        self.config.require_live_action.side_effect = [None, None, ConfigurationError('fixture disabled')]
        client = httpx.AsyncClient(base_url='https://fixture.invalid')
        transport = AsyncMock(side_effect=AssertionError('Disabled guard must not call transport.'))
        client.request = transport
        wire = Repliz(self.config, client=client, limiter=Mock())
        original = self.api.request
        callbacks = []
        async def request(method, path, **kwargs):
            if method == 'POST':
                callback = Mock(side_effect=kwargs['before_send'])
                callbacks.append(callback)
                return await wire.request(method, path, **{**kwargs, 'before_send': callback})
            return await original(method, path, **kwargs)
        self.api.request = request
        try:
            await self.worker.process_one()
        finally:
            await client.aclose()
        self.assertEqual(len(callbacks), 1)
        callbacks[0].assert_not_called()
        transport.assert_not_awaited()
        self.unchanged_draft(before)

    async def test_true_disconnection_holds_existing_draft_without_send(self):
        before = await self.drafted()
        self.api.account['isConnected'] = False
        self.snapshot()
        await self.worker.process_one()
        current = self.events()[0]
        self.assertEqual(current['status'], 'needs_review')
        self.assertEqual(current['attempts'], 3)
        self.assertEqual(current['draft'], before['draft'])
        self.assertEqual(current['leased'], 0)
        self.no_intent()
        self.ai.draft.assert_not_awaited()

    async def test_unknown_journal_remains_guarded_after_snapshot_wait_and_recovery(self):
        await self.drafted()
        action = 'reply:account-a:comment-1'
        self.worker.journal.begin(action, {'account_id': 'account-a', 'post_id': 'post-1',
            'comment_id': 'comment-1', 'text': baseline.DRAFT['text'], 'started_at': iso()})
        self.worker.journal.mark(action, 'unknown')
        self.store.begin_sync()
        await self.worker.process_one()
        self.assertEqual(self.worker.journal.state(action)['state'], 'unknown')
        self.snapshot()
        with self.store.tx() as c:
            c.execute("UPDATE comment_events SET next_attempt=''")
        await self.worker.process_one()
        self.assertEqual(self.events()[0]['status'], 'send_unknown')
        self.assertEqual(self.worker.journal.state(action)['state'], 'unknown')
        self.assertFalse(self.api.posts)
        self.ai.context.assert_not_awaited()
        self.ai.draft.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
