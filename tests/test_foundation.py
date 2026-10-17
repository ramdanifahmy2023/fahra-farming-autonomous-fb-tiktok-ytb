import copy
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from fahra_automation.attribution import from_metadata
from fahra_automation.config import Config, ConfigurationError, EXCLUDED_ACCOUNT_ID
from fahra_automation.db import Store, AssetStore, ZONE
from fahra_automation.pipeline import Pipeline
from fahra_automation.repliz import Repliz, AccountUnavailable, account_id, publication_details
from fahra_automation.util import iso, normalize_url


def now():
    return datetime(2030, 1, 16, 7, tzinfo=ZONE)


def freeze(test):
    for name in ("fahra_automation.db.now", "fahra_automation.util.now", "fahra_automation.pipeline.now", "fahra_automation.repliz.now"):
        clock = patch(name, side_effect=now)
        clock.start()
        test.addCleanup(clock.stop)


def sync_store(store, rows):
    day = now().astimezone(ZONE).date()
    return store.apply_snapshot(rows, [], day.isoformat(), (day + timedelta(days=366)).isoformat())


def accounts(facebook=3, youtube=2):
    return [{'id': f'{p}-{i}', 'name': f'{p} {i}', 'username': f'user-{i}', 'type': p,
             'isConnected': True, 'publishEligible': True} for p, n in [('facebook', facebook), ('youtube', youtube)] for i in range(n)]


def config(root):
    data = copy.deepcopy(Config.load().data)
    data['workflow']['runtime_dir'] = 'runtime'
    data['workflow']['publish_enabled'] = True
    return Config(root, data)


class StoreTest(unittest.TestCase):
    def setUp(self):
        freeze(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'workflow.sqlite3', daily_limit=5)
        sync_store(self.store, accounts())
        self.day = now().astimezone(ZONE).date()

    def enqueue(self, n=1, message=1):
        return self.store.enqueue(1, 1, message, [(f'https://instagram.com/reel/r{message}_{i}/', f'r{message}_{i}') for i in range(n)], self.day, required_destinations=['facebook', 'youtube'])

    def rows(self):
        with self.store.connect() as c:
            return [self.store.decode(r) for r in c.execute('SELECT * FROM jobs ORDER BY rowid')]

    def test_one_source_two_deliveries_and_independent_persistent_rotation(self):
        result = self.enqueue(2)
        self.assertEqual(result['accepted'], 2)
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(len(set(j['asset_id'] for j in self.rows())), 2)
        self.store = Store(self.store.path, daily_limit=5)
        self.enqueue(1, 2)
        for p, expected in [('facebook', ['facebook-0', 'facebook-1', 'facebook-2']),
                            ('youtube', ['youtube-0', 'youtube-1', 'youtube-0'])]:
            self.assertEqual([j['account_id'] for j in self.rows() if j['platform'] == p], expected)

    def test_duplicate_reel_and_replayed_message_create_no_extra_deliveries(self):
        self.enqueue(2)
        self.assertTrue(self.enqueue(2)['replayed'])
        result = self.store.enqueue(1, 1, 99, [('https://instagram.com/reel/r1_0/', 'r1_0')])
        self.assertEqual(result['duplicates'], 1)
        self.assertEqual(len(self.rows()), 4)

    def test_capacity_and_overflow_are_separate_per_platform(self):
        sync_store(self.store, accounts(14, 4))
        self.enqueue(71)
        for platform, today in [('facebook', 70), ('youtube', 20)]:
            rows = [j for j in self.rows() if j['platform'] == platform]
            self.assertEqual(sum(j['slot_date'] == self.day.isoformat() for j in rows), today)
            self.assertGreater(rows[today]['slot_date'], self.day.isoformat())
        self.assertEqual(self.store.summary()['platforms']['youtube']['daily_capacity'], 20)

    def test_concurrent_batches_never_exceed_five_per_account_day(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.enqueue(12, i), range(1, 5)))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0], 96)
            self.assertFalse(c.execute('SELECT account_id,slot_date,count(*) n FROM jobs GROUP BY account_id,slot_date HAVING n>5').fetchall())

    def test_disconnected_and_missing_accounts_reallocate_only_unsent_jobs(self):
        self.enqueue(2)
        first, yt, next_fb, _ = self.rows()
        self.store.checkpoint(next_fb['id'], 'submitting', payload={'submission': {'accountId': next_fb['account_id']}}, leased=1)
        updated = [a for a in accounts() if a['id'] != 'facebook-0']
        updated[0]['isConnected'] = False
        changes = sync_store(self.store, updated)
        self.assertIn('facebook-0', changes['disconnected'])
        self.assertIsNone(self.store.job(first['id'])['account_id'])
        self.store.ensure_slot(first['id'], self.day)
        self.assertEqual(self.store.job(first['id'])['account_id'], 'facebook-2')
        self.store.recover()
        self.store.ensure_slot(next_fb['id'], self.day + timedelta(days=1))
        self.assertEqual(self.store.job(next_fb['id'])['account_id'], 'facebook-1')
        self.assertEqual(self.store.job(next_fb['id'])['status'], 'submit_unknown')
        self.assertEqual(self.store.job(yt['id'])['account_id'], yt['account_id'])

    def test_new_account_pulls_waiting_job_into_today_and_reconnect_preserves_ordinal(self):
        sync_store(self.store, accounts(1, 1))
        self.enqueue(6)
        last = [j for j in self.rows() if j['platform'] == 'facebook'][-1]
        self.assertGreater(last['slot_date'], self.day.isoformat())
        sync_store(self.store, accounts(2, 1))
        self.assertEqual(self.store.ensure_slot(last['id'], self.day), self.day.isoformat())
        before = {a['id']: a['ordinal'] for a in self.store.accounts()}
        sync_store(self.store, accounts(0, 1))
        changes = sync_store(self.store, accounts(2, 1))
        self.assertIn('facebook-0', changes['reconnected'])
        self.assertEqual(before, {a['id']: a['ordinal'] for a in self.store.accounts()})

    def test_zero_platform_accounts_wait_without_using_other_platform(self):
        sync_store(self.store, accounts(1, 0))
        self.enqueue()
        fb, yt = self.rows()
        self.assertEqual(fb['account_id'], 'facebook-0')
        self.assertIsNone(yt['account_id'])
        self.assertIsNone(self.store.ensure_slot(yt['id'], self.day))
        sync_store(self.store, accounts(1, 1))
        self.store.ensure_slot(yt['id'], self.day)
        self.assertEqual(self.store.job(yt['id'])['account_id'], 'youtube-0')

    def test_invalid_snapshot_rolls_back_and_only_excluded_tiktok_is_blocked(self):
        before = self.store.accounts()
        with self.assertRaises(ValueError):
            sync_store(self.store, [{**accounts()[0], 'isConnected': 'false'}])
        self.assertEqual(before, self.store.accounts())
        sync_store(self.store, accounts() + [{'id': EXCLUDED_ACCOUNT_ID, 'type': 'tiktok', 'isConnected': True, 'publishEligible': True},
                                             {'id': 'other-tiktok', 'type': 'tiktok', 'isConnected': True, 'publishEligible': True}])
        self.assertEqual(len(self.store.accounts()), 6)
        before = self.store.accounts()
        with self.assertRaises(ValueError):
            sync_store(self.store, [{**accounts()[0], 'type': 'youtube'}])
        self.assertEqual(before, self.store.accounts())

    def test_external_reels_and_videos_consume_quota_without_double_counting_known_schedule(self):
        self.enqueue()
        fb = self.rows()[0]
        self.store.checkpoint(fb['id'], 'scheduled', schedule_id='ours')
        base = {'accountId': 'facebook-0', 'type': 'reel', 'status': 'pending', 'scheduleAt': iso(), 'title': 'Judul'}
        schedules = [{**base, 'id': 'ours'}] + [{**base, 'id': f'outside-{i}'} for i in range(4)]
        self.store.sync_external(schedules, self.day.isoformat(), self.day.isoformat())
        with self.store.connect() as c:
            self.assertEqual(self.store.loads(c, self.day.isoformat())['facebook-0'][1], 5)
        self.assertFalse(self.store.retry(fb['id']))
        self.assertFalse(self.store.cancel(fb['id']))

    def test_media_checkpoint_shared_without_overwriting_sent_delivery(self):
        self.enqueue()
        fb, yt = self.rows()
        self.store.asset_checkpoint(fb['asset_id'], 'analyzed', {'generated': {'caption': 'contoh'}})
        self.assertEqual(self.store.job(yt['id'])['status'], 'analyzed')
        self.store.checkpoint(fb['id'], 'scheduled', schedule_id='r1', payload={'submission': {'title': 'judul'}})
        self.store.asset_checkpoint(fb['asset_id'], 'drive_ready', {'drive_url': 'https://example.test/video'})
        self.assertEqual(self.store.job(fb['id'])['status'], 'scheduled')
        self.assertEqual(self.store.job(fb['id'])['payload']['submission']['title'], 'judul')
        self.assertEqual(self.store.job(yt['id'])['status'], 'drive_ready')

    def test_backup_is_restorable_and_events_append_only(self):
        self.enqueue(2)
        backup = self.store.backup(self.root / 'backups')
        restored = Store(backup)
        self.assertEqual(restored.summary()['reels'], 2)
        self.assertEqual(restored.get('cursor:facebook'), self.store.get('cursor:facebook'))
        with self.assertRaises(sqlite3.IntegrityError), self.store.tx() as c:
            c.execute('DELETE FROM events')

    def test_partial_media_checkpoint_does_not_reset_failure_budget(self):
        self.enqueue()
        job = self.rows()[0]
        self.store.checkpoint(job['id'], attempts=3)
        AssetStore(self.store).checkpoint(job['asset_id'], payload={'mcp_job_id': 'retry'})
        self.assertEqual(self.store.job(job['id'])['attempts'], 3)
        self.store.asset_checkpoint(job['asset_id'], 'downloaded', {'source_path': '/fixture'})
        self.assertEqual(self.store.job(job['id'])['attempts'], 0)

    def test_retry_with_uncertain_intent_is_refused(self):
        self.enqueue()
        job = self.rows()[0]
        self.store.checkpoint(job['id'], 'failed', payload={'submission': {'accountId': job['account_id']}, 'resume_status': 'drive_ready'})
        self.assertFalse(self.store.retry(job['id']))

    def test_old_tiktok_database_is_rejected(self):
        old = self.root / 'old.sqlite3'
        with sqlite3.connect(old) as c:
            c.execute('CREATE TABLE jobs(id TEXT)')
        c.close()
        with self.assertRaisesRegex(ValueError, 'Database workflow lama'):
            Store(old)


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        freeze(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = config(self.root)
        self.config.require_live_action = Mock()
        self.store = Store(self.root / 'workflow.sqlite3', daily_limit=5)
        sync_store(self.store, accounts())
        self.store.set('repliz_synced_at', iso())
        self.store.enqueue(1, 1, 1, [('https://instagram.com/reel/abc/', 'abc')], required_destinations=['facebook', 'youtube'])
        self.fb, self.yt = [self.store.job(j['id']) for j in self.store.queue()]
        self.repliz = Mock(payload=Repliz.payload, check_account=AsyncMock(), create=AsyncMock(side_effect=['remote-fb', 'remote-yt']), reconcile=AsyncMock(return_value=None))
        self.repliz.status = AsyncMock(side_effect=lambda sid: {'id': sid, 'status': 'success',
            'accountId': 'facebook-0' if sid == 'remote-fb' else 'youtube-0',
            'postId': '12345' if sid == 'remote-fb' else 'abcdefghijk',
            'scheduleAt': self.store.job(self.fb['id'] if sid == 'remote-fb' else self.yt['id'])['planned_at']})
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'fixture source')
        self.attribution = from_metadata(self.fb['url'], {'id': 'abc', 'extractor_key': 'Instagram', 'channel': 'fixture_creator'})
        self.downloader = Mock(download=AsyncMock(return_value={'source_path': str(self.source), 'source_caption': 'Kucing', **self.attribution}))
        self.generated = {'hook': 'Kucing penasaran', 'caption': 'Matanya langsung melotot.', 'hashtags': ['#kucing']}
        self.analyzer = Mock(analyze=AsyncMock(return_value={'generated': self.generated}))
        self.media = Mock()
        self.media.probe.return_value = {'duration': 10}
        def render(source, target, generated, sid):
            target.write_bytes(b'render fixture')
            return {'duration': 10}
        self.media.render.side_effect = render
        self.drive = Mock(verify_url=AsyncMock(), verify_delivery=AsyncMock(return_value={'verified': True}))
        self.drive.upload.return_value = {'drive_url': 'https://drive.example/video', 'drive_file_id': 'd1'}
        async def metadata(job):
            return {'account_id': job['account_id'], 'platform': job['platform'], 'title': self.generated['hook'],
                    'caption': self.generated['caption'], 'hashtags': self.generated['hashtags'], 'tags': ['kucing']}
        self.personas = Mock(prepare_delivery=AsyncMock(side_effect=metadata))
        self.pipeline = Pipeline(self.config, self.store, self.repliz, self.downloader, self.media, self.analyzer, self.drive,
                                 personas=self.personas, journal=Mock())

    def ready(self):
        self.store.asset_checkpoint(self.fb['asset_id'], 'drive_ready', {**self.attribution, 'generated': self.generated, 'drive_url': 'https://drive.example/video', 'render_probe': {'duration': 10}})

    async def test_one_download_render_and_drive_then_two_posts_and_share_links(self):
        for _ in range(6):
            await self.pipeline.handle(self.store.claim())
        self.downloader.download.assert_awaited_once()
        self.analyzer.analyze.assert_awaited_once()
        self.media.render.assert_called_once()
        self.drive.upload.assert_called_once()
        self.assertEqual(self.repliz.create.await_count, 2)
        payloads = [call.args[0] for call in self.repliz.create.call_args_list]
        self.assertEqual([p['type'] for p in payloads], ['reel', 'video'])
        self.assertEqual(payloads[0]['medias'][0]['url'], payloads[1]['medias'][0]['url'])
        self.assertNotEqual(payloads[0]['medias'][0]['alt'], payloads[1]['medias'][0]['alt'])
        self.assertEqual(payloads[1]['title'], self.generated['hook'])
        self.assertNotIn('pw:', payloads[1]['title'])
        for j in (self.fb, self.yt):
            self.store.checkpoint(j['id'], payload={'next_poll': ''})
        await self.pipeline.monitor()
        links = [n['text'] for n in self.store.notifications() if n['dedupe_key'].startswith('published:')]
        self.assertEqual(len(links), 2)
        self.assertTrue(any('facebook.com/reel/12345' in text for text in links))
        self.assertTrue(any('youtube.com/watch?v=abcdefghijk' in text for text in links))
        await self.pipeline.monitor()
        self.assertEqual(len([n for n in self.store.notifications() if n['dedupe_key'].startswith('published:')]), 2)
        self.assertIsNone(self.store.claim())

    async def test_timeout_keeps_original_account_while_other_destination_continues(self):
        self.ready()
        self.repliz.create.side_effect = [httpx.ReadTimeout('uncertain'), 'remote-yt']
        await self.pipeline.handle(self.store.claim())
        self.assertEqual(self.store.job(self.fb['id'])['status'], 'submit_unknown')
        sync_store(self.store, accounts()[1:])
        self.store.recover()
        await self.pipeline.handle(self.store.claim())
        self.assertEqual(self.store.job(self.yt['id'])['status'], 'scheduled')
        self.store.checkpoint(self.fb['id'], payload={'next_poll': ''})
        await self.pipeline.monitor()
        self.assertEqual(self.repliz.create.await_count, 2)
        self.assertEqual(self.store.job(self.fb['id'])['account_id'], 'facebook-0')
        self.assertFalse(self.store.retry(self.fb['id']))

    async def test_preflight_disconnect_reassigns_before_post(self):
        self.ready()
        self.repliz.check_account.side_effect = [AccountUnavailable('terputus'), None]
        await self.pipeline.handle(self.store.claim())
        self.assertIsNone(self.store.job(self.fb['id'])['account_id'])
        self.repliz.create.assert_not_awaited()
        self.store.checkpoint(self.fb['id'], next_attempt=iso())
        await self.pipeline.handle(self.store.claim())
        self.assertNotEqual(self.repliz.create.call_args.args[0]['accountId'], 'facebook-0')
        self.assertEqual(self.repliz.create.call_args.args[1], 'facebook')

    async def test_no_youtube_account_does_not_block_facebook(self):
        sync_store(self.store, accounts(1, 0))
        self.ready()
        await self.pipeline.handle(self.store.claim())
        await self.pipeline.handle(self.store.claim())
        self.assertEqual(self.store.job(self.fb['id'])['status'], 'scheduled')
        self.assertEqual(self.store.job(self.yt['id'])['status'], 'drive_ready')
        self.assertEqual(self.repliz.create.await_count, 1)

    async def test_disabled_publish_and_stale_sync_do_not_submit(self):
        self.ready()
        self.config.require_live_action.side_effect = ConfigurationError('shadow')
        await self.pipeline.handle(self.store.claim())
        self.repliz.create.assert_not_awaited()
        self.config.require_live_action.side_effect = None
        self.store.set('repliz_synced_at', None)
        await self.pipeline.handle(self.store.claim())
        self.repliz.create.assert_not_awaited()

    async def test_wrong_account_status_cannot_mark_published(self):
        self.store.checkpoint(self.fb['id'], 'scheduled', schedule_id='r1')
        self.repliz.status.side_effect = None
        self.repliz.status.return_value = {'status': 'success', 'accountId': 'wrong', 'postId': '123'}
        await self.pipeline.monitor()
        self.assertEqual(self.store.job(self.fb['id'])['status'], 'scheduled')

    async def test_duplicate_hash_skips_both_destinations(self):
        self.store.asset_checkpoint(self.fb['asset_id'], 'downloaded', {'source_sha256': __import__('hashlib').sha256(self.source.read_bytes()).hexdigest()})
        self.store.enqueue(1, 1, 2, [('https://instagram.com/reel/other/', 'other')], required_destinations=['facebook', 'youtube'])
        with self.store.connect() as c:
            row = self.store.decode(c.execute("SELECT * FROM jobs WHERE source_id='other' LIMIT 1").fetchone())
        await self.pipeline.handle(row)
        with self.store.connect() as c:
            self.assertEqual([r[0] for r in c.execute("SELECT status FROM jobs WHERE source_id='other'")], ['duplicate', 'duplicate'])


class ReplizTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        freeze(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = config(self.root)
        self.client = Repliz(self.config, client=Mock())
        self.client.request = AsyncMock()

    async def test_pagination_collects_all_and_uses_array_parameters(self):
        self.client.request.side_effect = [
            {'docs': accounts(1, 0), 'page': 1, 'hasNextPage': True, 'totalDocs': 2},
            {'docs': accounts(0, 1), 'page': 2, 'hasNextPage': False, 'totalDocs': 2}]
        rows = await self.client.pages('/public/account', {'types': ['facebook', 'youtube']})
        self.assertEqual(len(rows), 2)
        self.assertIn(('types[1]', 'youtube'), self.client.request.call_args.kwargs['params'])

    async def test_partial_or_malformed_snapshot_never_disables_existing_accounts(self):
        store = Store(self.root / 'state.db')
        sync_store(store, accounts())
        store.set('repliz_synced_at', iso())
        before = store.accounts()
        self.client.request.return_value = {'docs': [], 'hasNextPage': False, 'totalDocs': 1}
        with self.assertRaises(ValueError):
            await self.client.sync(store)
        self.assertEqual(store.accounts(), before)
        self.assertIsNone(store.get('repliz_synced_at'))
        for bad in ({}, {'docs': []}, {'docs': [], 'hasNextPage': 'false'}):
            self.client.request.return_value = bad
            with self.assertRaises(ValueError):
                await self.client.pages('/public/account')

    async def test_complete_empty_snapshot_removes_pool_but_keeps_history(self):
        store = Store(self.root / 'empty.db')
        sync_store(store, accounts())
        self.client.request.return_value = {'docs': [], 'page': 1, 'hasNextPage': False, 'totalDocs': 0}
        changes = await self.client.sync(store)
        self.assertEqual(len(changes['disconnected']), 5)
        self.assertEqual(store.summary()['accounts'], 0)
        self.assertEqual(len(store.accounts()), 5)
        self.assertTrue(store.get('repliz_synced_at'))

    async def test_snapshot_without_connection_flag_preserves_old_pool(self):
        store = Store(self.root / 'malformed.db')
        sync_store(store, accounts())
        before = store.accounts()
        self.client.request.return_value = {'docs': [{'id': 'facebook-0', 'type': 'facebook'}], 'hasNextPage': False, 'totalDocs': 1}
        with self.assertRaises(ValueError):
            await self.client.sync(store)
        self.assertEqual(store.accounts(), before)

    async def test_create_blocks_wrong_platform_before_any_post(self):
        self.client.request.return_value = {'id': 'bad', 'type': 'tiktok', 'isConnected': True, 'publishEligible': True}
        with self.assertRaises(AccountUnavailable):
            await self.client.check_account('bad', 'facebook')
        self.assertEqual([c.args[0] for c in self.client.request.call_args_list], ['GET'])

    async def test_disabled_or_worktree_create_never_calls_api(self):
        self.client.request = Repliz.request.__get__(self.client)
        self.client.client.request = AsyncMock()
        self.config.data['workflow']['publish_enabled'] = False
        with self.assertRaises(ConfigurationError):
            await self.client.create({'accountId': 'a', 'type': 'video'}, 'youtube')
        self.client.client.request.assert_not_awaited()
        self.config.data['workflow']['publish_enabled'] = True
        (self.root / '.git').write_text('gitdir: fixture')
        with self.assertRaises(ConfigurationError):
            await self.client.create({'accountId': 'a', 'type': 'video'}, 'youtube')
        self.client.client.request.assert_not_awaited()

    async def test_reconcile_requires_matching_media_account_text_and_time(self):
        body = {'accountId': 'a', 'title': 'Judul manusia', 'description': 'Caption', 'type': 'video',
                'medias': [{'url': 'https://drive.test/unique'}], 'scheduleAt': iso(), 'status': 'pending'}
        job = {'account_id': 'a', 'payload': {'submission': body}}
        self.client.pages = AsyncMock(return_value=[{**body, 'id': 'match'}, {**body, 'id': 'other', 'title': 'Different'}])
        self.assertEqual((await self.client.reconcile(job))['id'], 'match')
        self.client.pages.return_value = [{**body, 'id': 'match'}, {**body, 'id': 'duplicate'}]
        with self.assertRaises(RuntimeError):
            await self.client.reconcile(job)
        self.assertFalse(Repliz.matches_submission({**body, 'medias': [{'url': 'different'}]}, body))

    def test_platform_share_links_and_reject_untrusted_hosts(self):
        self.assertIn('watch?v=abcdefghijk', publication_details({'status': 'success', 'postId': 'abcdefghijk'}, 'YT', 'youtube')['post_url'])
        self.assertEqual(publication_details({'status': 'success', 'postId': '123_456'}, 'Page', 'facebook')['post_url'], 'https://www.facebook.com/123/posts/456')
        for value in ({'status': 'pending', 'postId': '123'}, {'status': 'success', 'postUrl': 'https://facebook.com.evil.test/post'}, {'status': 'success', 'postId': '../123'}):
            self.assertEqual(publication_details(value, 'Page', 'facebook'), {})

    def test_old_bot_identity_is_rejected_even_if_token_was_rotated(self):
        old = self.root / 'old.txt'
        new = self.root / 'new.txt'
        old.write_text('123456789:' + 'A' * 30)
        new.write_text('123456789:' + 'B' * 30)
        self.config.data['telegram'].update(token_file=str(new), forbidden_token_files=[str(old)])
        with self.assertRaises(ConfigurationError):
            self.config.telegram_token()
        new.write_text('987654321:' + 'C' * 30)
        self.assertTrue(self.config.telegram_token().startswith('987654321:'))

    def test_instagram_url_rejects_other_hosts(self):
        self.assertEqual(normalize_url('https://instagram.com/reel/abc/?tracking=1')[1], 'abc')
        for url in ('https://instagram.com.evil.test/reel/abc/', 'https://instagram.com@evil.test/reel/abc/', 'file:///abc'):
            with self.assertRaises(ValueError):
                normalize_url(url)
