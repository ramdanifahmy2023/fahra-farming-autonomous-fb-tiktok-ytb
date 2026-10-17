import copy
import json
import sqlite3
import tempfile
import threading
import shutil
from concurrent.futures import ThreadPoolExecutor
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fahra_automation.backup import create, restore, verify
from fahra_automation.config import Config
from fahra_automation.db import Store
from fahra_automation.health import report
from fahra_automation.journal import AttemptJournal, AttemptBlocked
from fahra_automation.reply_store import ReplyStore
from fahra_automation.util import digest_file, iso, now
import test_telegram_ui as telegram_fixture


class Operations(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = Config(self.root, copy.deepcopy(Config.load().data))
        self.store = Store(self.root / 'runtime/workflow.sqlite3')
        self.account = {'id': 'tt', 'type': 'tiktok', 'username': 'fixture', 'isConnected': True}

    def snapshot(self, accounts, schedules=()):
        return self.store.apply_snapshot(accounts, schedules, now().date().isoformat(),
                                         (now()+timedelta(days=365)).date().isoformat())

    def test_unknown_capability_waits_then_verified_history_enrolls_without_quota_reset(self):
        self.snapshot([self.account])
        self.assertFalse(self.store.accounts()[0]['eligible'])
        self.snapshot([self.account], [{'id':'s','accountId':'tt','type':'video','status':'success',
            'scheduleAt':iso(), 'postId':'123'}])
        self.assertTrue(self.store.accounts()[0]['eligible'])
        ordinal = self.store.accounts()[0]['ordinal']
        self.snapshot([{**self.account, 'isConnected':False}])
        self.assertFalse(self.store.accounts()[0]['eligible'])
        self.snapshot([self.account])
        self.assertFalse(self.store.accounts()[0]['eligible'])
        self.snapshot([{**self.account, 'publishEligible':True}])
        self.assertEqual(self.store.accounts()[0]['ordinal'], ordinal)
        self.snapshot([{**self.account, 'publishEligible':True, 'username':''}])
        self.assertFalse(self.store.accounts()[0]['eligible'])

    def test_cached_capability_expires_and_partial_connection_snapshot_rolls_back(self):
        self.snapshot([{**self.account, 'publishEligible':True}])
        before = self.store.accounts()
        with self.assertRaises(ValueError):
            self.snapshot([{'id':'tt','type':'tiktok','username':'fixture'}])
        self.assertEqual(self.store.accounts(), before)
        with self.store.tx() as c:
            c.execute('UPDATE account_capabilities SET expires_at=?', (iso(now()-timedelta(seconds=1)),))
        self.snapshot([self.account])
        self.assertFalse(self.store.accounts()[0]['eligible'])

    def test_readonly_report_has_no_writes_and_rejects_mutations(self):
        ReplyStore(self.store)
        from fahra_automation.report_store import ReportStore
        ReportStore(self.store)
        self.store.health('fixture', 'ok')
        before = digest_file(self.store.path)
        readonly = Store(self.store.path, readonly=True)
        report(readonly)
        ReplyStore(readonly).summary()
        ReportStore(readonly).summary()
        readonly.summary()
        with self.assertRaises(sqlite3.OperationalError):
            readonly.set('paused', True)
        self.assertEqual(digest_file(self.store.path), before)
        missing = self.root / 'missing/workflow.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            Store(missing, readonly=True)
        self.assertFalse(missing.parent.exists())

    def test_health_alert_is_deduplicated_and_waiting_is_not_failure(self):
        for _ in range(8):
            self.store.health('fixture', 'error', reason='fixture unavailable')
        self.assertEqual(len(self.store.notifications()), 1)
        item = report(self.store)['loops'][0]
        self.assertEqual(item['failures'], 8)
        self.assertIsNone(item['last_success'])
        self.store.health('fixture', 'waiting')
        self.assertEqual(report(self.store)['loops'][0]['failures'], 0)

    def test_bundle_restore_keeps_accepted_journal_and_is_shadow(self):
        journal = AttemptJournal(self.config)
        journal.begin('fixture-action', {'accountId':'tt'})
        journal.mark('fixture-action', 'accepted', schedule_id='schedule')
        from fahra_automation.account_reports import ReportJournal
        report_journal = ReportJournal(self.config)
        report_journal.begin('send:fixture', {'text':'fixture report'})
        report_journal.mark('send:fixture', 'accepted', schedule_id='message-1')
        folder = create(self.config, self.store, self.root / 'backups')
        self.assertEqual(verify(folder)['journal_states'], 2)
        target = restore(folder, self.root / 'restored')
        restored = Store(target / 'runtime/workflow.sqlite3', readonly=True)
        self.assertTrue(restored.get('paused'))
        self.assertFalse(restored.get('reply_enabled'))
        self.assertFalse((target / 'runtime/ownership.json').exists())
        config = Config(target, copy.deepcopy(self.config.data))
        self.assertEqual(ReportJournal(config).state('send:fixture')['schedule_id'], 'message-1')
        with self.assertRaises(AttemptBlocked):
            AttemptJournal(config).begin('fixture-action', {'accountId':'tt'})
        next((folder / 'attempts').glob('*/*.json')).unlink()
        with self.assertRaises(ValueError):
            verify(folder)

    def test_missing_intent_evidence_rejects_backup(self):
        self.store.enqueue(1,1,1,[('https://www.instagram.com/reel/test/', 'test')], can_allocate=False)
        job = self.store.queue()[0]
        self.store.checkpoint(job['id'], 'submit_unknown', payload={'submission':{'accountId':'tt'}})
        with self.assertRaises(ValueError):
            create(self.config, self.store, self.root / 'backups')

    def test_slow_bundle_copy_does_not_block_database_or_new_intents(self):
        journal = AttemptJournal(self.config)
        journal.begin('old-intent', {'id':'old'})
        entered, release = threading.Event(), threading.Event()
        original_copy = shutil.copyfile
        def slow_copy(source, target):
            if Path(source).suffix == '.json' and Path(source).parent.parent == journal.root:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('fixture copy')
            return original_copy(source,target)
        with ThreadPoolExecutor(max_workers=1) as executor, patch('fahra_automation.backup.shutil.copyfile', slow_copy):
            future = executor.submit(create,self.config,self.store,self.root/'backups')
            try:
                self.assertTrue(entered.wait(2))
                self.store.set('copied_after_boundary', True)
                journal.begin('new-intent', {'id':'new'})
            finally:
                release.set()
            folder = future.result(timeout=5)
        copied = Store(folder/'workflow.sqlite3',readonly=True)
        self.assertIsNone(copied.get('copied_after_boundary'))
        self.assertEqual(verify(folder)['journal_states'],1)

    def test_legacy_reply_gap_is_explicit_and_restore_keeps_it_held(self):
        self.store.enqueue(1,1,1,[('https://www.instagram.com/reel/legacy/', 'legacy')], can_allocate=False)
        job = self.store.queue()[0]
        self.store.checkpoint(job['id'], 'published', payload={'legacy_origin':'tiktok'})
        replies = ReplyStore(self.store)
        replies.ingest({'account_id':'tt','platform_id':'comment','inbox_id':'inbox','post_id':'post'}, 'sent_unverified')
        with self.store.tx() as c:
            identity = c.execute('SELECT id FROM comment_events').fetchone()[0]
            c.execute('UPDATE comment_events SET job_id=?', (job['id'],))
            c.execute("""INSERT INTO reply_attempts(event_id,account_id,day,status,text,started_at,updated_at)
                VALUES(?,'tt','2026-10-18','sent_unverified','old reply',?,?)""", (identity,iso(),iso()))
            c.execute('CREATE TABLE legacy_mapping(origin TEXT,kind TEXT,legacy_id TEXT,new_id TEXT)')
            c.execute("INSERT INTO legacy_mapping VALUES('tiktok','job',?,?)", (job['id'],job['id']))
        folder = create(self.config,self.store,self.root/'backups')
        self.assertEqual(verify(folder)['legacy_reply_gaps'],[identity])
        target = restore(folder,self.root/'restore-legacy')
        restored = Store(target/'runtime/workflow.sqlite3',readonly=True)
        self.assertTrue(restored.get('restore_requires_reconciliation'))
        self.assertEqual(ReplyStore(restored).get(identity)['status'],'sent_unverified')

    def test_three_senders_take_turns_and_thousand_sources_deduplicate(self):
        for sender, count in ((1,1000),(2,100),(3,20)):
            urls = [(f'https://www.instagram.com/reel/r{sender}_{n}/', f'r{sender}_{n}') for n in range(count)]
            self.store.enqueue(sender,sender,1,urls,can_allocate=False)
            duplicate = self.store.enqueue(sender,sender,2,urls,can_allocate=False)
            self.assertEqual(duplicate['accepted'], 0)
            self.assertEqual(duplicate['duplicates'], count)
        senders = []
        for _ in range(6):
            job = self.store.claim()
            senders.append(job['sender'])
            self.store.checkpoint(job['id'], leased=0, next_attempt=iso(now()+timedelta(days=1)))
            self.store.release_asset_lease(job['id'])
        self.assertEqual(senders, [1,2,3,1,2,3])
        self.assertEqual(self.store.summary()['reels'], 1120)
        self.assertEqual(self.store.summary()['statuses']['queued'], 3360)


class Privacy(unittest.IsolatedAsyncioTestCase):
    setUp = telegram_fixture.TelegramUITest.setUp

    async def test_group_receipt_contains_no_detail_and_progress_goes_to_sender(self):
        update = {'update_id':1001,'message':{'message_id':1001,'from':{'id':77},
            'chat':{'id':-100,'type':'supergroup'},'text':'https://www.instagram.com/reel/privatefixture/'}}
        await self.bot.process(update)
        with self.store.connect() as c:
            batch = c.execute('SELECT id FROM batches').fetchone()[0]
            group = [r['text'] for r in c.execute('SELECT * FROM outbox WHERE chat_id=-100')]
        self.assertTrue(group)
        self.assertTrue(all('privatefixture' not in text and batch not in text for text in group))
        self.assertEqual(self.store.progress_snapshot(batch)[0]['report_chat_id'], 77)
        await self.bot.update_progress(batch)
        calls = self.bot.send_ui.await_args_list
        self.assertTrue(calls)
        self.assertTrue(all(call.args[1] == 77 for call in calls))

    def test_schema_upgrade_drops_group_message_binding_and_quarantines_pending_details(self):
        self.store.enqueue(-100,77,9,[('https://www.instagram.com/reel/private/', 'private')], progress=True)
        with self.store.connect() as c:
            c.execute('UPDATE batch_progress SET message_id=123,done=1')
            c.execute("INSERT INTO outbox(dedupe_key,chat_id,text,created_at) VALUES('old',-100,'private details',?)", (iso(),))
            c.execute('PRAGMA user_version=1')
        self.assertEqual(Store(self.store.path, readonly=True).summary()['schema_version'],1)
        Store(self.store.path)
        with self.store.connect() as c:
            self.assertEqual(tuple(c.execute('SELECT message_id,done FROM batch_progress').fetchone()), (None,0))
            self.assertEqual(c.execute("SELECT sent FROM outbox WHERE dedupe_key='old'").fetchone()[0], -2)
