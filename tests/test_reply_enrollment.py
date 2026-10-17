import asyncio
import copy
import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import AsyncMock, Mock, patch

from fahra_automation import cli, replies
from fahra_automation.config import Config
from fahra_automation.db import Store
from fahra_automation.reply_store import ReplyStore, ReplyWait
from fahra_automation.util import iso, now


class ReplyEnrollmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'workflow.sqlite3')
        self.db = ReplyStore(self.store)
        self.config = Config.load()
        self.config.data = copy.deepcopy(self.config.data)
        self.config.data['workflow']['runtime_dir'] = self.temp.name
        self.config.require_live_action = Mock()
        self.at = now()
        self.legacy_since = iso(self.at - timedelta(days=1))
        self.enrolled_at = iso(self.at)
        self.later = iso(self.at + timedelta(hours=1))
        self.accounts = [
            {'id': f'{platform}-{number:02d}', 'type': platform,
             'username': f'{platform}{number}', 'name': f'{platform} {number}',
             'isConnected': True, 'publishEligible': True}
            for platform, count in [('tiktok', 46), ('facebook', 14), ('youtube', 4)]
            for number in range(count)
        ]
        self.legacy_ids = [a['id'] for a in self.accounts[:46]]
        self.new_ids = [a['id'] for a in self.accounts[46:]]
        self.api = Mock()
        self.api.pages = AsyncMock(return_value=self.accounts)
        self.api.request = AsyncMock(return_value={'docs': []})
        self.api.close = AsyncMock()
        self.snapshot()

    def snapshot(self):
        day = self.at.date()
        self.store.apply_snapshot(self.accounts, [], day.isoformat(),
                                  (day + timedelta(days=366)).isoformat())

    def migrated(self):
        self.store.set('reply_since', self.legacy_since)
        self.store.set('reply_account_ids', self.legacy_ids)
        self.store.set('reply_account_since', {})
        self.store.set('reply_enabled', False)

    def secondary(self, action):
        with patch.object(replies.Config, 'load', return_value=self.config), \
                patch.object(replies, 'Store', return_value=self.store), \
                patch.object(replies, 'Repliz', return_value=self.api), \
                patch('sys.argv', ['replies', action]), redirect_stdout(io.StringIO()):
            replies.main()

    def primary(self, action):
        with patch.object(cli.Config, 'load', return_value=self.config), \
                patch.object(cli, 'initialize', return_value=self.store), \
                patch.object(cli, 'Repliz') as api, \
                patch('sys.argv', ['fahra_automation', 'reply', action]), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(), 0)
            api.assert_not_called()

    def worker(self):
        return replies.ReplyWorker(self.config, self.store, self.api, ai=Mock())

    def settings(self):
        return {key: self.store.get(key) for key in
                ('reply_enabled', 'reply_since', 'reply_account_ids', 'reply_account_since')}

    def test_secondary_resume_and_activate_preserve_46_legacy_and_enroll_18_new(self):
        for action in ('resume', 'activate'):
            with self.subTest(action=action):
                self.migrated()
                with patch('fahra_automation.reply_store.iso', return_value=self.enrolled_at):
                    self.secondary(action)
                cutoffs = self.store.get('reply_account_since')
                self.assertEqual(len(cutoffs), 64)
                self.assertEqual({cutoffs[a] for a in self.legacy_ids}, {self.legacy_since})
                self.assertEqual({cutoffs[a] for a in self.new_ids}, {self.enrolled_at})
                self.assertEqual(self.store.get('reply_since'), self.legacy_since)
                self.assertTrue(self.store.get('reply_enabled'))
                with patch('fahra_automation.reply_store.iso', return_value=self.later):
                    self.secondary(action)
                    asyncio.run(self.worker().inventory())
                self.assertEqual(self.store.get('reply_account_since'), cutoffs)
        self.assertTrue(all(call.args[0] == 'GET' for call in self.api.request.call_args_list))

    def test_secondary_first_activation_uses_one_cutoff_after_read_preflight(self):
        def preflight(*args, **kwargs):
            self.assertIsNone(self.store.get('reply_since'))
            self.assertFalse(self.store.get('reply_enabled', False))
            self.assertIsNone(self.store.get('reply_account_since'))
            return {'docs': []}
        self.api.request.side_effect = preflight
        with patch('fahra_automation.reply_store.iso', return_value=self.enrolled_at):
            self.secondary('activate')
        self.assertEqual(self.store.get('reply_since'), self.enrolled_at)
        self.assertEqual(set(self.store.get('reply_account_since').values()), {self.enrolled_at})
        self.api.request.assert_awaited_once()
        self.api.close.assert_awaited_once()

    def test_secondary_failed_preflight_leaves_reply_settings_unchanged(self):
        self.migrated()
        before = self.settings()
        self.api.request.side_effect = RuntimeError('fixture read denied')
        with self.assertRaisesRegex(RuntimeError, 'fixture read denied'):
            self.secondary('resume')
        self.assertEqual(self.settings(), before)
        self.api.close.assert_awaited_once()

    def test_secondary_resume_still_requires_first_activation(self):
        with self.assertRaisesRegex(ValueError, 'Gunakan activate'):
            self.secondary('resume')
        self.assertIsNone(self.store.get('reply_since'))
        self.api.pages.assert_not_awaited()

    def test_secondary_preflight_generation_change_does_not_enable_or_enroll(self):
        self.migrated()
        before = self.settings()
        self.api.request.side_effect = lambda *args, **kwargs: self.store.begin_sync()
        with self.assertRaises(ReplyWait):
            self.secondary('resume')
        self.assertEqual(self.settings(), before)
        self.api.close.assert_awaited_once()

    def test_primary_first_resume_and_later_inventory_keep_join_time(self):
        with patch('fahra_automation.reply_store.iso', return_value=self.enrolled_at):
            self.primary('resume')
        self.assertEqual(self.store.get('reply_since'), self.enrolled_at)
        self.assertEqual(self.store.get('reply_account_ids'), [])
        with patch('fahra_automation.reply_store.iso', return_value=self.later):
            asyncio.run(self.worker().inventory())
        self.assertEqual(set(self.store.get('reply_account_since').values()), {self.later})
        cutoffs = self.store.get('reply_account_since')
        self.primary('pause')
        self.assertFalse(self.store.get('reply_enabled'))
        self.primary('resume')
        self.assertEqual(self.store.get('reply_account_since'), cutoffs)
        self.assertEqual(self.store.get('reply_since'), self.enrolled_at)

    def test_primary_resume_seeds_migrated_cutoffs_without_preflight(self):
        self.migrated()
        self.primary('resume')
        self.assertEqual(self.store.get('reply_account_ids'), self.legacy_ids)
        self.assertEqual(set(self.store.get('reply_account_since').values()), {self.legacy_since})
        with patch('fahra_automation.reply_store.iso', return_value=self.enrolled_at):
            asyncio.run(self.worker().inventory())
        self.assertEqual({self.store.get('reply_account_since')[a] for a in self.new_ids},
                         {self.enrolled_at})

    def test_stale_inventory_and_disconnect_reconnect_preserve_cutoffs(self):
        self.migrated()
        explicit = {self.legacy_ids[0]: iso(self.at - timedelta(hours=12))}
        self.store.set('reply_account_since', explicit)
        self.store.begin_sync()
        worker = self.worker()
        with self.assertRaises(ReplyWait):
            asyncio.run(worker.inventory())
        self.assertEqual(self.store.get('reply_account_ids'), self.legacy_ids)
        cutoffs = self.store.get('reply_account_since')
        self.assertEqual(cutoffs, explicit)
        self.assertEqual(cutoffs[self.legacy_ids[0]], explicit[self.legacy_ids[0]])
        self.assertFalse(self.store.get('reply_enabled'))
        self.snapshot()
        with patch('fahra_automation.reply_store.iso', return_value=self.enrolled_at):
            asyncio.run(worker.inventory())
        cutoffs = self.store.get('reply_account_since')
        self.assertEqual(cutoffs[self.legacy_ids[1]], self.legacy_since)
        self.accounts[-1]['isConnected'] = False
        asyncio.run(worker.inventory())
        self.assertNotIn(self.new_ids[-1], self.store.get('reply_account_ids'))
        self.accounts[-1]['isConnected'] = True
        with patch('fahra_automation.reply_store.iso', return_value=self.later):
            asyncio.run(worker.inventory())
        self.assertEqual(self.store.get('reply_account_since'), cutoffs)
        self.assertFalse(self.store.get('reply_enabled'))

    def test_new_account_comments_before_cutoff_skip_but_boundary_and_legacy_queue(self):
        self.migrated()
        with patch('fahra_automation.reply_store.iso', return_value=self.enrolled_at):
            self.secondary('resume')
        rows = []
        for index, (account_id, stamp) in enumerate([
                (self.new_ids[0], iso(self.at - timedelta(seconds=1))),
                (self.new_ids[0], self.enrolled_at),
                (self.new_ids[0], iso(self.at + timedelta(seconds=1))),
                (self.legacy_ids[0], iso(self.at - timedelta(hours=1)))]):
            rows.append({'id': f'inbox-{index}', 'accountId': account_id, 'status': 'pending',
                         'content': {'id': 'post'}, 'comment': {'id': f'comment-{index}',
                         'type': 'text', 'text': 'kok matanya begitu?', 'owner': {'id': 'viewer'},
                         'medias': [], 'replies': [], 'createdAt': stamp}})
        async def pages(path, params=None):
            return self.accounts if path == '/public/account' else rows
        self.api.pages.side_effect = pages
        with patch('fahra_automation.reply_store.iso', return_value=self.later):
            asyncio.run(self.worker().ingest())
        with self.store.connect() as c:
            events = c.execute('SELECT status,reason FROM comment_events ORDER BY inbox_id').fetchall()
        self.assertEqual([row['status'] for row in events], ['skipped', 'queued', 'queued', 'queued'])
        self.assertEqual(events[0]['reason'], 'Sebelum aktivasi.')

    def test_concurrent_enrollment_keeps_both_cutoff_additions(self):
        self.migrated()
        barrier = Barrier(2)
        def enroll(identity):
            barrier.wait(timeout=5)
            self.db.enroll_accounts([identity])
        with patch('fahra_automation.reply_store.iso', return_value=self.enrolled_at):
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(enroll, self.new_ids[:2]))
        cutoffs = self.store.get('reply_account_since')
        self.assertEqual(len(cutoffs), 48)
        self.assertEqual({cutoffs[a] for a in self.legacy_ids}, {self.legacy_since})
        self.assertEqual({cutoffs[a] for a in self.new_ids[:2]}, {self.enrolled_at})


if __name__ == '__main__':
    unittest.main()
