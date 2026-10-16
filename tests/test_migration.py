import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fahra_automation.config import ConfigurationError
from fahra_automation.db import Store, ZONE
from fahra_automation.migration import import_snapshots, reconcile
from fahra_automation.reply_store import ReplyStore
from fahra_automation.scheduler import offset_for
from fahra_automation.util import digest_file, iso


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.at=datetime(2026,10,16,7,tzinfo=ZONE)
        for name in ('fahra_automation.db.now','fahra_automation.util.now','fahra_automation.reply_store.now'):
            timer=patch(name,return_value=self.at)
            timer.start()
            self.addCleanup(timer.stop)
        self.store=Store(self.root/'target/workflow.sqlite3')
        self.accounts=[]
        self.original_ids={}
        self.cutoff=iso(self.at-timedelta(days=1))
        manifest={'id':'migration-test','final':True,'sources':[]}
        for origin,platforms in [('tiktok',['tiktok']),('fb-ytb',['facebook','youtube'])]:
            path=self.root/(origin+'.sqlite3')
            source=Store(path)
            accounts=[{'id':p+'-account','type':p,'username':p+'user','name':p+' account','isConnected':True} for p in platforms]
            self.accounts.extend(accounts)
            source.apply_snapshot(accounts,[],self.at.date().isoformat(),(self.at.date()+timedelta(days=2)).isoformat())
            source.enqueue(10,10,1,[('https://www.instagram.com/reel/same/','same')],required_destinations=platforms)
            with source.tx() as c:
                for row in c.execute('SELECT * FROM jobs').fetchall():
                    body={'accountId':row['account_id'],'scheduleAt':row['planned_at'],'title':'pw:'+row['id'],
                          'description':'Legacy caption','type':'video','medias':[{'url':'https://example.invalid/'+origin+'.mp4'}]}
                    payload={'submission':body,'source_path':'/legacy/'+origin+'/source.mp4','source_sha256':origin+'-digest'}
                    c.execute("UPDATE jobs SET status='submitting',counts_quota=1,payload=? WHERE id=?",(json.dumps(payload),row['id']))
                    self.original_ids[row['platform']]=row['id']
                source.put_setting(c,'telegram_offset',998)
            if origin=='tiktok':
                source.enqueue(10,10,2,[('https://www.instagram.com/reel/archive/','archive')],required_destinations=['tiktok'])
                with source.connect() as c:
                    archived=source.decode(c.execute("SELECT * FROM jobs WHERE source_id='archive'").fetchone())
                self.archived_id=archived['id']
                source.checkpoint(archived['id'],'published',payload={'post_id':'archive-post'},schedule_id='legacy-published')
                replies=ReplyStore(source)
                with source.tx() as c:
                    c.execute('INSERT INTO video_contexts VALUES(?,?,?,?,?)',
                              (archived['id'],'tiktok-account','archive-post',json.dumps({'quality':'clear','facts':['Kucing.']}),iso()))
                    for n,state in enumerate(('sent','send_unknown')):
                        eid='legacy-event-'+str(n)
                        payload={'account_id':'tiktok-account','post_id':'archive-post','platform_id':'comment-'+str(n),'inbox_id':'inbox-'+str(n)}
                        c.execute('''INSERT INTO comment_events(id,account_id,post_id,platform_id,inbox_id,payload,job_id,status,
                            next_attempt,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                            (eid,'tiktok-account','archive-post',payload['platform_id'],payload['inbox_id'],json.dumps(payload),archived['id'],state,iso(),iso(),iso()))
                        c.execute('''INSERT INTO reply_attempts(event_id,account_id,day,status,text,started_at,updated_at,reply_id)
                            VALUES(?,?,?,?,?,?,?,?)''',(eid,'tiktok-account',self.at.date().isoformat(),state,'Balasan '+str(n),iso(),iso(),'reply-'+str(n) if state=='sent' else None))
                    source.put_setting(c,'reply_since',self.cutoff)
                    source.put_setting(c,'reply_account_since',{'tiktok-account':self.cutoff})
                    source.put_setting(c,'reply_account_ids',['tiktok-account'])
                    source.put_setting(c,'reply_owner_aliases',{'tiktok-account':['legacy-owner']})
                    source.put_setting(c,'cursor',0)
            with source.tx() as c:
                c.execute('PRAGMA user_version=0')
            if origin=='tiktok':
                with closing(sqlite3.connect(path)) as c:
                    columns=[r[1] for r in c.execute('PRAGMA table_info(jobs)') if r[1] not in {'asset_id','platform'}]
                    c.execute('CREATE TABLE jobs_copy AS SELECT '+','.join(columns)+' FROM jobs')
                    c.execute('DROP TABLE jobs')
                    c.execute('ALTER TABLE jobs_copy RENAME TO jobs')
                    c.execute('DROP TABLE assets')
                    c.execute('CREATE TABLE accounts_copy AS SELECT id,username,name,ordinal,connected,updated_at FROM accounts')
                    c.execute('DROP TABLE accounts')
                    c.execute('ALTER TABLE accounts_copy RENAME TO accounts')
                    c.commit()
            manifest['sources'].append({'origin':origin,'file':path.name,'sha256':digest_file(path)})
        self.manifest=manifest
        (self.root/'manifest.json').write_text(json.dumps(manifest))

    def test_import_preserves_destinations_unknown_and_source_dedupe(self):
        report=import_snapshots(self.store,self.root)
        self.assertEqual(report['jobs'],4)
        self.assertEqual(report['assets'],3)
        with self.store.connect() as c:
            assets=[json.loads(r[0]) for r in c.execute('SELECT required_destinations FROM assets')]
            self.assertEqual({tuple(v) for v in assets},{('tiktok',),('facebook','youtube')})
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE status='submit_unknown' AND counts_quota=1").fetchone()[0],3)
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
            self.assertTrue(all(r[0]==0 for r in c.execute('SELECT chat_id FROM batches')))
        result=self.store.enqueue(1,1,99,[('https://www.instagram.com/reel/same/','same')],can_allocate=False)
        self.assertEqual(result['accepted'],0)
        self.assertEqual(result['duplicates'],1)
        self.assertEqual(import_snapshots(self.store,self.root),report)
        for source in self.manifest['sources']:
            self.assertEqual(digest_file(self.root/source['file']),source['sha256'])
        self.assertFalse(self.store.get('reply_enabled'))
        self.assertIsNone(self.store.get('telegram_offset'))

    def test_account_ordinals_cursors_offsets_and_video_quota_survive_import(self):
        import_snapshots(self.store,self.root)
        self.assertEqual({a['id'] for a in self.store.accounts()}, {a['id'] for a in self.accounts})
        self.assertEqual([a['ordinal'] for a in self.store.accounts()], [0,1,2])
        self.assertTrue(all(a['slot_offset']==offset_for(a['id'],20,8,23) and not a['eligible'] for a in self.store.accounts()))
        self.assertEqual(self.store.get('cursor:tiktok'),0)
        for platform,jid in self.original_ids.items():
            job=self.store.job(jid)
            self.assertEqual((job['platform'],job['account_id'],job['counts_quota']), (platform,platform+'-account',1))
            self.assertEqual(job['payload']['source_path'],'/legacy/'+('tiktok' if platform=='tiktok' else 'fb-ytb')+'/source.mp4')
        self.store.apply_snapshot(self.accounts,[],self.at.date().isoformat(),(self.at.date()+timedelta(days=2)).isoformat())
        with self.store.connect() as c:
            loads=self.store.loads(c,self.at.date().isoformat())
            self.assertEqual({k:v[1] for k,v in loads.items()}, {'tiktok-account':2,'facebook-account':1,'youtube-account':1})

    def test_reply_history_unknown_quota_context_and_cutoff_survive_import(self):
        report=import_snapshots(self.store,self.root)
        self.assertEqual(report['reply_events'],2)
        replies=ReplyStore(self.store)
        self.assertEqual(self.store.get('reply_since'),self.cutoff)
        self.assertEqual(self.store.get('reply_account_since'),{'tiktok-account':self.cutoff})
        self.assertEqual(self.store.get('reply_account_ids'),['tiktok-account'])
        self.assertEqual(self.store.get('reply_owner_aliases'),{'tiktok-account':['legacy-owner']})
        with self.store.connect() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT status FROM reply_attempts ORDER BY id')],['sent','send_unknown'])
            self.assertEqual(c.execute('SELECT count(*) FROM reply_attempts WHERE day=? AND status!=?',(self.at.date().isoformat(),'failed')).fetchone()[0],2)
            context=json.loads(c.execute('SELECT context FROM video_contexts WHERE job_id=?',(self.archived_id,)).fetchone()[0])
            self.assertEqual(context['facts'],['Kucing.'])
        self.assertEqual(replies.get('legacy-event-1')['status'],'send_unknown')
        with self.store.tx() as c:
            c.execute("INSERT INTO comment_events(id,account_id,post_id,platform_id,inbox_id,payload,status,draft,next_attempt,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      ('new-comment','tiktok-account','archive-post','comment-new','inbox-new','{}','drafted',json.dumps({'text':'Baru'}),iso(),iso(),iso()))
        self.store.set('reply_enabled',True)
        self.assertFalse(replies.reserve_send(replies.get('new-comment'),2))

    def test_unknown_without_remote_id_blocks_migration_ready(self):
        import_snapshots(self.store,self.root)
        async def sync(store):
            store.apply_snapshot(self.accounts,[],self.at.date().isoformat(),(self.at.date()+timedelta(days=2)).isoformat())
        api=Mock(sync=AsyncMock(side_effect=sync))
        journal=Mock(reconcile_store=Mock(return_value={'repaired':[],'unresolved':[]}))
        with patch('fahra_automation.journal.AttemptJournal',return_value=journal):
            report=asyncio.run(reconcile(Mock(),self.store,api))
        self.assertFalse(report['ready'])
        self.assertFalse(self.store.get('migration_reconciled'))

    def test_checksum_change_rejected_without_partial_import(self):
        with (self.root/'tiktok.sqlite3').open('ab') as f:
            f.write(b'changed')
        with self.assertRaises(ValueError):
            import_snapshots(self.store,self.root)
        self.assertIsNone(self.store.get('migration_id'))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],0)

    def test_nonempty_target_rejected(self):
        self.store.enqueue(1,1,1,[('https://www.instagram.com/reel/new/','new')],can_allocate=False)
        with self.assertRaises(ConfigurationError):
            import_snapshots(self.store,self.root)
