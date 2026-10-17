import copy
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch

from fahra_automation.config import Config, ConfigurationError, credential_pairs, allowed_account
from fahra_automation.db import Store, ZONE
from fahra_automation.journal import AttemptJournal, AttemptBlocked
from fahra_automation.ownership import verify_ownership, activate
from fahra_automation.util import safe_error, iso


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = copy.deepcopy(Config.load().data)
        self.data['workflow']['journal_dir'] = 'runtime/attempts'
        self.config = Config(self.root,self.data)
        self.store = Store(self.root/'runtime/workflow.sqlite3')
        self.journal = AttemptJournal(self.config)
        self.at = datetime(2026, 10, 16, 7, tzinfo=ZONE)
        for name in ('fahra_automation.db.now', 'fahra_automation.util.now'):
            timer = patch(name, return_value=self.at)
            timer.start()
            self.addCleanup(timer.stop)

    def delivery(self):
        accounts = [{'id':f'tt-{n}', 'type':'tiktok', 'username':f'user{n}',
                     'name':f'User {n}', 'isConnected':True, 'publishEligible':True} for n in range(2)]
        self.store.apply_snapshot(accounts, [], self.at.date().isoformat(),
                                  (self.at.date()+timedelta(days=2)).isoformat())
        self.store.enqueue(1,1,1,[('https://www.instagram.com/reel/restore/','restore')], required_destinations=['tiktok'])
        job = self.store.job(self.store.queue()[0]['id'])
        body = {'accountId':job['account_id'], 'scheduleAt':job['planned_at'], 'type':'video',
                'title':'original', 'description':'Bukti fixture.',
                'medias':[{'url':'https://example.invalid/video.mp4', 'alt':'fa:'+job['id']}]}
        return job, body

    def test_hyphen_keys_conflict_and_empty_timeout(self):
        credential = self.root/'test.md'
        credential.write_text('base-url=http://localhost/v1\napi-key=fixture\nmodel-combo=combo\n')
        self.assertEqual(credential_pairs(credential)['api_key'],'fixture')
        credential.write_text('api-key=one\napi_key=two\n')
        with self.assertRaises(ConfigurationError):
            credential_pairs(credential)
        self.assertEqual(safe_error(TimeoutError()),'TimeoutError')

    def test_excluded_tiktok_name_but_same_facebook_name_allowed(self):
        for identity in ('id','_id'):
            self.assertFalse(allowed_account({identity:'other','type':'tiktok','username':'@IQ7FansPage'}))
            self.assertTrue(allowed_account({identity:'page','type':'facebook','username':'iq7fanspage'}))

    def test_worktree_root_override_cannot_enable_writes(self):
        self.config.data['workflow'].update(mode='live',publish_enabled=True)
        with self.assertRaises(ConfigurationError):
            self.config.require_live_action()
        (self.root/'.git').write_text('gitdir: fixture')
        with patch('fahra_automation.config.CODE_ROOT',self.root), self.assertRaises(ConfigurationError):
            self.config.require_live_action()

    def test_journal_rejects_unknown_and_accepted_replays(self):
        self.journal.begin('one',{'title':'test'})
        self.journal.mark('one','unknown')
        with self.assertRaises(AttemptBlocked):
            AttemptJournal(self.config).begin('one',{'title':'test'})
        self.journal.mark('one','accepted',schedule_id='s1')
        with self.assertRaises(AttemptBlocked):
            self.journal.mark('one','not_sent')
        with self.assertRaises(AttemptBlocked):
            self.journal.begin('one',{})
        self.assertEqual(len(list(self.journal.folder('one').glob('*.json'))),3)

    def test_known_not_sent_can_retry(self):
        self.journal.begin('one',{})
        self.journal.mark('one','not_sent')
        self.journal.begin('one',{'retry':1})
        self.assertEqual(self.journal.state('one')['body'],{'retry':1})

    def test_restore_old_db_recovers_intent_and_holds_orphans(self):
        job, body = self.delivery()
        self.journal.begin(job['id'],body)
        result=self.journal.reconcile_store(self.store)
        self.assertEqual(result['repaired'],[job['id']])
        recovered=self.store.job(job['id'])
        self.assertEqual(recovered['status'],'submit_unknown')
        self.assertEqual(recovered['payload']['submission']['title'],'original')
        self.journal.begin('missingjob',{})
        self.assertEqual(self.journal.reconcile_store(self.store)['unresolved'],['missingjob'])

    def test_restore_accepted_recovers_remote_id(self):
        job, body = self.delivery()
        self.journal.begin(job['id'],body)
        self.journal.mark(job['id'],'accepted',schedule_id='remote')
        self.journal.reconcile_store(self.store)
        self.assertEqual(self.store.job(job['id'])['schedule_id'],'remote')

    def test_restore_after_reallocation_recovers_durable_account_day_and_quota(self):
        job, body = self.delivery()
        moved = {**body, 'accountId':'tt-1', 'scheduleAt':iso(self.at + timedelta(days=1, hours=3))}
        self.journal.begin(job['id'], moved)
        self.journal.mark(job['id'], 'accepted', schedule_id='remote-after-backup')
        self.journal.reconcile_store(self.store)
        restored = self.store.job(job['id'])
        tomorrow = (self.at.date()+timedelta(days=1)).isoformat()
        self.assertEqual((restored['account_id'], restored['slot_date'], restored['planned_at']),
                         ('tt-1', tomorrow, moved['scheduleAt']))
        self.assertEqual(restored['payload']['submission'], moved)
        self.assertEqual(restored['status'], 'scheduled')
        with self.store.connect() as c:
            self.assertEqual(self.store.loads(c, self.at.date().isoformat())['tt-0'][1], 0)
            self.assertEqual(self.store.loads(c, tomorrow)['tt-1'][1], 1)
        with self.assertRaises(AttemptBlocked):
            self.journal.begin(job['id'], moved)

    def test_restore_missing_or_foreign_account_and_naive_date_remain_unresolved(self):
        job, body = self.delivery()
        for changes in ({'accountId':None}, {'accountId':'unknown'}, {'scheduleAt':'2026-10-16T10:00:00'}):
            with self.subTest(changes=changes):
                self.journal.begin(job['id'], {**body, **changes})
                result = self.journal.reconcile_store(self.store)
                self.assertEqual(result['unresolved'], [job['id']])
                self.assertEqual(self.store.job(job['id'])['status'], job['status'])
                self.journal.mark(job['id'], 'not_sent')

    def test_journal_does_not_undo_fresh_remote_schedule_date(self):
        job, body = self.delivery()
        self.journal.begin(job['id'], body)
        self.journal.mark(job['id'], 'accepted', schedule_id='remote-moved')
        self.store.checkpoint(job['id'],'scheduled',payload={'submission':body},schedule_id='remote-moved')
        remote_time=iso(self.at+timedelta(days=1,hours=3))
        remote={**body,'id':'remote-moved','status':'pending','scheduleAt':remote_time}
        accounts=[{'id':f'tt-{n}','type':'tiktok','username':f'user{n}','isConnected':True, 'publishEligible':True} for n in range(2)]
        self.store.apply_snapshot(accounts,[remote],self.at.date().isoformat(),(self.at.date()+timedelta(days=2)).isoformat())
        self.journal.reconcile_store(self.store)
        restored=self.store.job(job['id'])
        self.assertEqual(restored['planned_at'],remote_time)
        self.assertEqual(restored['slot_date'],(self.at.date()+timedelta(days=1)).isoformat())
        with self.store.connect() as c:
            self.assertEqual(self.store.loads(c,self.at.date().isoformat())[job['account_id']][1],0)
            self.assertEqual(self.store.loads(c,restored['slot_date'])[job['account_id']][1],1)

    def test_ownership_rejects_old_service_or_database_restore(self):
        self.store.set('migration_id','new')
        self.store.set('migration_reconciled',True)
        self.config.data['workflow']['legacy_roots']=[]
        with patch.object(Config,'require_production_checkout'), patch('fahra_automation.ownership.legacy_services',return_value=[]):
            activate(self.config,self.store)
            verify_ownership(self.config)
            self.store.set('migration_id','oldbackup')
            with self.assertRaises(ConfigurationError):
                verify_ownership(self.config)
        with patch('fahra_automation.ownership.legacy_services',return_value=[{'loaded':True}]):
            with self.assertRaises(ConfigurationError):
                verify_ownership(self.config)

    def test_fixture_runtime_never_uses_production_journal_on_main_checkout(self):
        (self.root/'.git').mkdir()
        self.config.data['workflow']['runtime_dir']='fixture-runtime'
        self.config.data['workflow']['journal_dir']='production-journal'
        with patch('fahra_automation.journal.CODE_ROOT',self.root):
            isolated=AttemptJournal(self.config)
        self.assertEqual(isolated.root,self.root/'fixture-runtime/attempts')
        self.assertFalse((self.root/'production-journal').exists())
