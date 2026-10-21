import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fahra_automation.config import ConfigurationError, EXCLUDED_ACCOUNT_ID, PLATFORMS
from fahra_automation.db import SCHEMA_VERSION, Store, ZONE
from fahra_automation.scheduler import parse_time
from fahra_automation.util import iso


def account(platform, number=0, **changes):
    return {'id': f'{platform}-{number}', 'username': f'user{number}', 'name': f'{platform} {number}',
            'type': platform, 'isConnected': True, 'publishEligible': True, **changes}


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.at = datetime(2026, 10, 16, 7, tzinfo=ZONE)
        for name in ('fahra_automation.db.now', 'fahra_automation.util.now'):
            timer = patch(name, side_effect=lambda: self.at)
            timer.start()
            self.addCleanup(timer.stop)
        self.store = Store(self.root / 'fixture.sqlite3')
        self.accounts = [account(p) for p in PLATFORMS]
        self.sync()

    def sync(self, accounts=None, schedules=()):
        return self.store.apply_snapshot(self.accounts if accounts is None else accounts, list(schedules),
                                         (self.at.date() - timedelta(days=2)).isoformat(),
                                         (self.at.date() + timedelta(days=366)).isoformat())

    def enqueue(self, count=1, message=1, **kwargs):
        return self.store.enqueue(1, 1, message,
            [(f'https://instagram.com/reel/r{message}_{i}/', f'r{message}_{i}') for i in range(count)], **kwargs)

    def jobs(self, platform=None):
        with self.store.connect() as c:
            return [self.store.decode(r) for r in c.execute('SELECT * FROM jobs ORDER BY rowid')
                    if platform is None or r['platform'] == platform]

    def ready(self):
        self.enqueue()
        first = self.jobs()[0]
        self.store.asset_checkpoint(first['asset_id'], 'drive_ready', {'generated': {'caption': 'Kucing.'}})
        return self.store.claim()

    def body(self, job):
        return {'accountId': job['account_id'], 'scheduleAt': job['planned_at'], 'title': 'fahra:' + job['id']}

    def schedule(self, job, **changes):
        return {'id': 'remote-1', 'accountId': job['account_id'], 'scheduleAt': job['planned_at'],
                'title': 'fahra:' + job['id'], 'type': 'video', 'status': 'pending', **changes}

    def test_default_three_deliveries_and_explicit_legacy_destinations(self):
        self.enqueue()
        self.assertEqual({j['platform'] for j in self.jobs()}, set(PLATFORMS))
        self.assertEqual(self.store.asset(self.jobs()[0]['asset_id'])['required_destinations'], list(PLATFORMS))
        self.enqueue(message=2, required_destinations=['tiktok'])
        self.assertEqual(len(self.jobs()), 4)
        self.enqueue(message=3, required_destinations=['facebook', 'youtube'])
        self.assertEqual(len(self.jobs()), 6)
        with self.assertRaises(ValueError):
            self.enqueue(message=4, required_destinations=['tiktok', 'tiktok'])

    def test_schema_version_and_reopening_keep_required_destinations(self):
        self.enqueue(required_destinations=['tiktok'])
        reopened = Store(self.store.path)
        self.assertEqual(reopened.get('schema_version'), SCHEMA_VERSION)
        self.assertEqual(reopened.asset(self.jobs()[0]['asset_id'])['required_destinations'], ['tiktok'])
        with reopened.connect() as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)

    def test_twenty_slots_spread_in_window_and_twenty_first_rolls_over(self):
        self.enqueue(21)
        for platform in PLATFORMS:
            rows = self.jobs(platform)
            stamps = [parse_time(j['planned_at']).astimezone(ZONE) for j in rows]
            self.assertEqual([s.date() for s in stamps[:20]], [self.at.date()] * 20)
            self.assertEqual(stamps[20].date(), self.at.date() + timedelta(days=1))
            self.assertTrue(all(8 <= s.hour < 23 for s in stamps))
            self.assertEqual({(b - a).total_seconds() for a, b in zip(stamps[:19], stamps[1:20])}, {2700})
        self.assertEqual(self.store.summary()['daily_capacity'], 60)

    def test_other_account_capacity_used_before_next_day(self):
        self.sync(self.accounts + [account('tiktok', 1)])
        self.enqueue(40)
        rows = self.jobs('tiktok')
        self.assertTrue(all(j['slot_date'] == self.at.date().isoformat() for j in rows))
        self.assertEqual(sum(j['account_id'] == 'tiktok-0' for j in rows), 20)
        self.assertEqual(sum(j['account_id'] == 'tiktok-1' for j in rows), 20)
        self.assertEqual(sum(j['slot_date'] == self.at.date().isoformat() for j in self.jobs('youtube')), 20)

    def test_configurable_smaller_limit_and_invalid_limit(self):
        self.store = Store(self.store.path, daily_limit=2)
        self.sync()
        self.enqueue(3)
        self.assertEqual([j['slot_date'] for j in self.jobs('tiktok')],
                         [self.at.date().isoformat()] * 2 + [(self.at.date() + timedelta(days=1)).isoformat()])
        with self.assertRaises(ValueError):
            Store(self.root / 'invalid.db', daily_limit=21)

    def test_late_arrival_does_not_burst_missed_slots(self):
        self.at = self.at.replace(hour=22, minute=59)
        self.sync()
        self.enqueue(3)
        self.assertTrue(all(j['slot_date'] > self.at.date().isoformat() for j in self.jobs()))

    def test_concurrent_batches_do_not_overbook_or_duplicate_planned_times(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda number: self.enqueue(12, number), range(1, 5)))
        with self.store.connect() as c:
            self.assertFalse(c.execute('''SELECT account_id,slot_date,count(*) n FROM jobs
                GROUP BY account_id,slot_date HAVING n>20''').fetchall())
            self.assertFalse(c.execute('''SELECT account_id,planned_at,count(*) n FROM jobs
                GROUP BY account_id,planned_at HAVING n>1''').fetchall())

    def test_rotation_and_stable_offset_survive_reopen_and_reconnect(self):
        accounts = self.accounts + [account('tiktok', 1), account('tiktok', 2)]
        self.sync(accounts)
        with self.store.connect() as c:
            c.execute("UPDATE accounts SET slot_offset=0 WHERE platform='tiktok'")
        self.enqueue(2)
        offsets = {a['id']: a['slot_offset'] for a in self.store.accounts()}
        self.store = Store(self.store.path)
        self.enqueue(message=2)
        self.assertEqual([j['account_id'] for j in self.jobs('tiktok')], ['tiktok-0', 'tiktok-1', 'tiktok-2'])
        self.sync([a for a in accounts if a['id'] != 'tiktok-0'])
        changes = self.sync(accounts)
        self.assertIn('tiktok-0', changes['reconnected'])
        self.assertEqual(offsets, {a['id']: a['slot_offset'] for a in self.store.accounts()})

    def test_new_account_pulls_future_unsent_reservation_into_today(self):
        self.enqueue(21)
        job = self.jobs('tiktok')[-1]
        self.assertGreater(job['slot_date'], self.at.date().isoformat())
        self.sync(self.accounts + [account('tiktok', 1)])
        self.assertEqual(self.store.ensure_slot(job['id'], self.at.date()), self.at.date().isoformat())
        self.assertEqual(self.store.job(job['id'])['account_id'], 'tiktok-1')

    def prepared_caption(self, job):
        metadata = {'account_id': job['account_id'], 'platform': job['platform'],
                    'caption': 'Caption yang sudah disiapkan.', 'persona_version': 1,
                    'evidence_hash': 'fixture-evidence'}
        self.store.checkpoint(job['id'], payload={'delivery_metadata': metadata,
            'persona': {'name': 'Lama'}, 'persona_version': 1,
            'delivery_generated': {'caption': 'Draft lama.'}})
        return metadata

    def test_expired_slot_same_account_preserves_prepared_caption_and_persona(self):
        job = self.ready()
        metadata = self.prepared_caption(job)
        before = self.store.job(job['id'])['payload']
        self.at = parse_time(job['planned_at']).astimezone(ZONE) + timedelta(seconds=60)
        self.sync()
        self.assertEqual(self.store.ensure_slot(job['id'], self.at.date()), self.at.date().isoformat())
        current = self.store.job(job['id'])
        self.assertEqual(current['account_id'], job['account_id'])
        self.assertNotEqual(current['planned_at'], job['planned_at'])
        self.assertEqual(current['payload'], before)
        self.assertEqual(current['payload']['delivery_metadata'], metadata)
        self.assertEqual(current['counts_quota'], 1)

    def test_reassignment_to_different_account_clears_only_delivery_copy(self):
        job = self.ready()
        self.prepared_caption(job)
        self.sync([item for item in self.accounts if item['id'] != job['account_id']] + [account(job['platform'], 1)])
        self.assertEqual(self.store.ensure_slot(job['id'], self.at.date()), self.at.date().isoformat())
        current = self.store.job(job['id'])
        self.assertEqual(current['account_id'], job['platform'] + '-1')
        for key in ('persona', 'persona_version', 'delivery_generated', 'delivery_metadata'):
            self.assertNotIn(key, current['payload'])
        self.assertEqual(current['payload']['generated'], {'caption': 'Kucing.'})
        self.assertEqual(current['counts_quota'], 1)

    def test_unavailable_then_reconnected_account_keeps_pinned_caption_across_reopen(self):
        job = self.ready()
        metadata = self.prepared_caption(job)
        self.sync([item for item in self.accounts if item['type'] != job['platform']])
        self.assertIsNone(self.store.ensure_slot(job['id'], self.at.date()))
        waiting = self.store.job(job['id'])
        self.assertIsNone(waiting['account_id'])
        self.assertEqual(waiting['counts_quota'], 0)
        self.assertEqual(waiting['payload']['delivery_metadata'], metadata)
        self.assertNotIn('delivery_generated', waiting['payload'])
        self.store = Store(self.store.path)
        self.assertIsNone(self.store.ensure_slot(job['id'], self.at.date()))
        self.assertEqual(self.store.job(job['id'])['payload']['delivery_metadata'], metadata)
        self.sync()
        self.assertEqual(self.store.ensure_slot(job['id'], self.at.date()), self.at.date().isoformat())
        current = self.store.job(job['id'])
        self.assertEqual(current['account_id'], metadata['account_id'])
        self.assertEqual(current['payload']['delivery_metadata'], metadata)
        self.assertEqual(current['counts_quota'], 1)

    def test_unavailable_then_different_account_discards_old_pinned_caption(self):
        job = self.ready()
        self.prepared_caption(job)
        others = [item for item in self.accounts if item['type'] != job['platform']]
        self.sync(others)
        self.assertIsNone(self.store.ensure_slot(job['id'], self.at.date()))
        self.sync(others + [account(job['platform'], 1)])
        self.assertEqual(self.store.ensure_slot(job['id'], self.at.date()), self.at.date().isoformat())
        current = self.store.job(job['id'])
        self.assertEqual(current['account_id'], job['platform'] + '-1')
        self.assertNotIn('delivery_metadata', current['payload'])

    def test_submission_intent_preserves_slot_caption_and_account_even_if_unavailable(self):
        job = self.ready()
        self.prepared_caption(job)
        self.store.begin_submission(job['id'], self.body(job), self.store.snapshot_generation())
        self.store.checkpoint(job['id'], 'drive_ready')
        self.at = parse_time(job['planned_at']).astimezone(ZONE) + timedelta(days=1)
        self.sync([item for item in self.accounts if item['type'] != job['platform']])
        before = self.store.job(job['id'])
        self.assertEqual(self.store.ensure_slot(job['id'], self.at.date()), job['slot_date'])
        self.assertEqual(self.store.job(job['id']), before)

    def test_unavailable_platform_does_not_take_other_platform_account(self):
        self.sync([account('tiktok'), account('facebook')])
        self.enqueue()
        youtube = self.jobs('youtube')[0]
        self.assertIsNone(youtube['account_id'])
        self.assertIsNone(self.store.ensure_slot(youtube['id'], self.at.date()))
        self.assertTrue(all(j['account_id'] for j in self.jobs() if j['platform'] != 'youtube'))

    def test_exclusion_preserves_facebook_page_named_iq7(self):
        self.sync(self.accounts + [account('tiktok', 3, username='@IQ7FANSPAGE'),
                                  account('tiktok', 4, id=EXCLUDED_ACCOUNT_ID), account('facebook', 3, name='IQ7')])
        self.assertEqual(len(self.store.accounts()), 4)
        self.assertIn('facebook-3', {a['id'] for a in self.store.eligible_accounts()})

    def test_incomplete_sync_cannot_allocate_new_account_or_submit_old_snapshot(self):
        job = self.ready()
        generation = self.store.snapshot_generation()
        before = self.store.accounts()
        self.store.begin_sync()
        self.assertEqual(self.store.accounts(), before)
        self.enqueue(message=2)
        self.assertTrue(all(j['account_id'] is None for j in self.jobs() if j['source_id'] == 'r2_0'))
        with self.assertRaises(ConfigurationError):
            self.store.begin_submission(job['id'], self.body(job), generation)
        self.assertEqual(self.store.job(job['id'])['status'], 'drive_ready')

    def test_invalid_combined_snapshot_rolls_back_pool_and_schedule(self):
        before = self.store.accounts()
        token = self.store.snapshot_generation()
        with self.assertRaises(ValueError):
            self.sync(self.accounts + [account('tiktok', 5)], [{'id': 'bad', 'accountId': 'tiktok-5', 'type': 'video',
                'status': 'pending', 'scheduleAt': '2026-10-16T09:00:00'}])
        self.assertEqual(self.store.accounts(), before)
        self.assertEqual(self.store.snapshot_generation(), token)

    def test_duplicate_account_and_changed_platform_snapshot_rejected(self):
        with self.assertRaises(ValueError):
            self.sync(self.accounts + [self.accounts[0]])
        with self.assertRaises(ValueError):
            self.sync([{**self.accounts[0], 'type': 'youtube'}])

    def test_legacy_two_call_sync_never_grants_rights_between_calls(self):
        self.store.sync_accounts(self.accounts + [account('tiktok', 1)])
        self.assertFalse(self.store.snapshot_fresh())
        self.enqueue()
        self.assertTrue(all(j['account_id'] is None for j in self.jobs()))
        self.store.sync_external([], self.at.date().isoformat(), (self.at.date() + timedelta(days=366)).isoformat())
        self.assertTrue(self.store.snapshot_fresh())
        self.store.ensure_slot(self.jobs()[0]['id'], self.at.date())
        self.assertIsNotNone(self.store.job(self.jobs()[0]['id'])['account_id'])

    def test_remote_moved_day_counts_once_and_prevents_twenty_first(self):
        job = self.ready()
        self.store.checkpoint(job['id'], 'scheduled', schedule_id='remote-1', slot_date=(self.at.date()-timedelta(days=1)).isoformat())
        self.sync(schedules=[self.schedule(job)])
        with self.store.connect() as c:
            self.assertEqual(self.store.loads(c, self.at.date().isoformat())['tiktok-0'][1], 1)
        self.enqueue(20, message=2)
        self.assertEqual(sum(j['slot_date'] == self.at.date().isoformat() for j in self.jobs('tiktok')), 20)

    def test_remote_error_retains_quota_and_hash_dedupe(self):
        job = self.ready()
        self.store.asset_checkpoint(job['asset_id'], payload={'source_sha256': 'a' * 64})
        self.store.checkpoint(job['id'], 'failed', schedule_id='remote-1')
        self.sync(schedules=[self.schedule(job, status='error')])
        self.enqueue(message=2)
        self.assertTrue(self.store.duplicate_hash(self.jobs()[-1]['asset_id'], 'a' * 64))
        self.assertEqual(self.store.job(job['id'])['counts_quota'], 1)

    def test_delayed_success_uses_published_day_and_unknown_keeps_original_day(self):
        job = self.ready()
        self.store.begin_submission(job['id'], self.body(job), self.store.snapshot_generation())
        self.store.checkpoint(job['id'], 'submit_unknown')
        day = self.store.job(job['id'])['slot_date']
        self.assertEqual(self.store.ensure_slot(job['id'], self.at.date() + timedelta(days=1)), day)
        published = self.at + timedelta(days=1, hours=2)
        self.store.apply_remote_schedule(job['id'], self.schedule(job, status='success', publishedAt=iso(published)))
        self.assertEqual(self.store.job(job['id'])['slot_date'], published.date().isoformat())

    def test_generation_change_and_disconnect_block_intent_without_submission(self):
        job = self.ready()
        token = self.store.snapshot_generation()
        self.sync()
        with self.assertRaises(ConfigurationError):
            self.store.begin_submission(job['id'], self.body(job), token)
        self.store.disconnect(job['account_id'])
        with self.assertRaises(ConfigurationError):
            self.store.begin_submission(job['id'], self.body(job), self.store.snapshot_generation())
        self.assertNotIn('submission', self.store.job(job['id'])['payload'])

    def test_submission_is_atomic_unique_and_crash_becomes_unknown(self):
        job = self.ready()
        token = self.store.snapshot_generation()
        self.store.begin_submission(job['id'], self.body(job), token)
        with self.assertRaises(ConfigurationError):
            self.store.begin_submission(job['id'], self.body(job), token)
        self.store.recover()
        current = self.store.job(job['id'])
        self.assertEqual(current['status'], 'submit_unknown')
        self.assertFalse(self.store.cancel(job['id']))
        self.assertFalse(self.store.retry(job['id']))
        self.assertFalse(self.store.reject_submission(job['id'], 'cannot prove rejection'))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM submission_attempts').fetchone()[0], 1)

    def test_definite_rejection_keeps_journal_and_allows_safe_new_attempt(self):
        job = self.ready()
        token = self.store.snapshot_generation()
        self.store.begin_submission(job['id'], self.body(job), token)
        self.assertTrue(self.store.reject_submission(job['id'], 'rejected'))
        self.store.begin_submission(job['id'], self.body(job), token)
        with self.store.connect() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT status FROM submission_attempts ORDER BY id')], ['rejected', 'submitting'])

    def test_changed_time_body_or_missing_lease_blocks_submission(self):
        job = self.ready()
        for changes in ({'accountId': 'facebook-0'}, {'scheduleAt': iso(self.at - timedelta(seconds=1))}):
            with self.assertRaises(ConfigurationError):
                self.store.begin_submission(job['id'], {**self.body(job), **changes}, self.store.snapshot_generation())
        self.store.checkpoint(job['id'], leased=0)
        with self.assertRaises(ConfigurationError):
            self.store.begin_submission(job['id'], self.body(job), self.store.snapshot_generation())

    def test_asset_lease_serializes_shared_media_and_releases_on_checkpoint(self):
        self.enqueue(2)
        first = self.store.claim()
        second = self.store.claim()
        self.assertNotEqual(first['asset_id'], second['asset_id'])
        self.assertIsNone(self.store.claim())
        self.store.asset_checkpoint(first['asset_id'], 'downloaded', {'source_path': '/fixture'})
        self.store.checkpoint(first['id'], leased=0)
        next_job = self.store.claim()
        self.assertEqual(next_job['asset_id'], first['asset_id'])
        self.assertEqual(next_job['status'], 'downloaded')

    def test_media_checkpoint_preserves_destination_specific_metadata(self):
        self.enqueue()
        job = self.jobs()[0]
        self.store.checkpoint(job['id'], payload={'persona_version': 'one', 'delivery_generated': {'caption': 'Tujuan A'}})
        self.store.asset_checkpoint(job['asset_id'], 'analyzed', {'generated': {'caption': 'Dasar'}})
        payload = self.store.job(job['id'])['payload']
        self.assertEqual(payload['persona_version'], 'one')
        self.assertEqual(payload['delivery_generated']['caption'], 'Tujuan A')

    def test_future_wait_on_one_platform_does_not_block_other_ready_delivery(self):
        self.enqueue()
        first = self.jobs()[0]
        self.store.asset_checkpoint(first['asset_id'], 'drive_ready')
        youtube = self.jobs('youtube')[0]
        self.store.checkpoint(youtube['id'], next_attempt=iso(self.at + timedelta(days=1)))
        claims = [self.store.claim(), self.store.claim()]
        self.assertEqual({j['platform'] for j in claims}, {'tiktok', 'facebook'})
        self.assertIsNone(self.store.claim())

    def test_stale_snapshot_has_no_eligible_reply_or_submission_accounts(self):
        self.at += timedelta(seconds=601)
        self.assertFalse(self.store.snapshot_fresh())
        self.assertEqual(self.store.eligible_accounts(), [])
        with self.assertRaises(ConfigurationError):
            self.store.snapshot_token()

    def test_older_concurrent_sync_cannot_replace_new_snapshot(self):
        first = self.store.begin_sync()
        second = self.store.begin_sync()
        day = self.at.date().isoformat()
        result = self.store.apply_snapshot(self.accounts + [account('tiktok', 2)], [], day, day,
                                           expected_generation=second)
        with self.assertRaises(ConfigurationError):
            self.store.apply_snapshot(self.accounts + [account('tiktok', 1)], [], day, day,
                                      expected_generation=first)
        self.assertEqual(self.store.snapshot_generation(), result['generation'])
        self.assertEqual({a['id'] for a in self.store.eligible_accounts('tiktok')}, {'tiktok-0', 'tiktok-2'})

    def test_provider_cooldown_blocks_other_jobs_on_same_account(self):
        job = self.ready()
        until = self.at + timedelta(hours=3)
        self.store.defer_account(job['account_id'], iso(until), 'provider429')
        self.store.defer_account(job['account_id'], iso(self.at + timedelta(hours=1)))
        with self.assertRaises(ConfigurationError):
            self.store.begin_submission(job['id'], self.body(job), self.store.snapshot_generation())
        self.store.checkpoint(job['id'], leased=0)
        other = self.store.claim()
        self.assertNotEqual(other['account_id'], job['account_id'])
        self.enqueue(message=2, required_destinations=[job['platform']])
        later = self.jobs(job['platform'])[-1]
        self.assertGreaterEqual(parse_time(later['planned_at']), until)
        self.assertEqual(self.store.summary()['daily_limit'], 20)
        reopened = Store(self.store.path)
        self.assertEqual(next(a['blocked_until'] for a in reopened.accounts() if a['id'] == job['account_id']), iso(until))

    def test_uncertain_download_holds_unsent_destinations_only(self):
        job = self.ready()
        self.store.begin_submission(job['id'], self.body(job), self.store.snapshot_generation())
        self.store.checkpoint(job['id'], 'submit_unknown', leased=0)
        self.store.hold_asset(job['asset_id'], 'MCP start ambigu')
        self.assertEqual(self.store.asset(job['asset_id'])['status'], 'needs_review')
        rows = self.store.asset_deliveries(job['asset_id'])
        self.assertEqual([(j['status'], j['counts_quota']) for j in rows if j['id'] == job['id']], [('submit_unknown', 1)])
        self.assertTrue(all(j['status'] == 'needs_review' and j['counts_quota'] == 0 and not j['leased']
                            for j in rows if j['id'] != job['id']))
        self.assertIsNone(self.store.claim())

    def test_legacy_canonical_source_is_not_backfilled_and_chat_zero_is_silent(self):
        self.enqueue(required_destinations=['tiktok'])
        job = self.jobs()[0]
        with self.store.tx() as c:
            c.execute("UPDATE assets SET source_id='legacy:tiktok:r1_0' WHERE id=?", (job['asset_id'],))
            c.execute('UPDATE batches SET chat_id=0,user_id=0')
        result = self.store.enqueue(9, 9, 2, [('https://instagram.com/reel/r1_0/', 'r1_0')])
        self.assertEqual((result['accepted'], result['duplicates'], len(self.jobs())), (0, 1, 1))
        self.store.checkpoint(job['id'], 'published', payload={'post_url': 'https://www.tiktok.com/@user0/video/123'}, schedule_id='r')
        self.store.notify('unknown:' + job['id'], 0, 'held')
        self.assertFalse(self.store.notify_publication(job['id']))
        self.assertEqual(self.store.publications_to_report(), [])
        self.store.batch_reports()
        self.store.track_active_batches()
        self.assertEqual(self.store.notifications(), [])
        self.assertEqual(self.store.progress_due(), [])

    def test_unknown_marker_requires_intended_media_and_text_before_binding(self):
        job = self.ready()
        body = {**self.body(job), 'title': 'Kucing bermain', 'description': 'Sore santai.', 'type': 'video',
                'medias': [{'url': 'https://example.invalid/movie.mp4', 'alt': 'fa:' + job['id']}]}
        self.store.begin_submission(job['id'], body, self.store.snapshot_generation())
        self.store.checkpoint(job['id'], 'submit_unknown', leased=0)
        wrong = {**body, 'id': 'remote-1', 'status': 'pending', 'description': 'Berbeda.'}
        self.sync(schedules=[wrong])
        self.assertIsNone(self.store.job(job['id'])['schedule_id'])
        with self.store.connect() as c:
            self.assertEqual(self.store.loads(c, job['slot_date'])[job['account_id']][1], 2)
        moved = {**body, 'id': 'remote-1', 'status': 'pending',
                 'scheduleAt': iso(parse_time(job['planned_at']) + timedelta(days=1))}
        self.sync(schedules=[moved])
        matched = self.store.job(job['id'])
        self.assertEqual(matched['schedule_id'], 'remote-1')
        self.assertEqual(matched['status'], 'submit_unknown')
        self.assertEqual(matched['slot_date'], (self.at.date() + timedelta(days=1)).isoformat())
        with self.store.connect() as c:
            self.assertEqual(self.store.loads(c, matched['slot_date'])[job['account_id']][1], 1)

    def test_no_allocation_beyond_complete_snapshot_range(self):
        day = self.at.date().isoformat()
        self.store.apply_snapshot(self.accounts, [], day, day)
        self.enqueue(21, required_destinations=['tiktok'])
        rows = self.jobs()
        self.assertTrue(all(j['counts_quota'] for j in rows[:20]))
        self.assertFalse(rows[-1]['counts_quota'])
        self.assertIsNone(rows[-1]['account_id'])
        job = rows[0]
        self.store.asset_checkpoint(job['asset_id'], 'drive_ready')
        job = self.store.claim()
        self.store.checkpoint(job['id'], slot_date=(self.at.date() + timedelta(days=1)).isoformat(),
                              planned_at=iso(parse_time(job['planned_at']) + timedelta(days=1)))
        job = self.store.job(job['id'])
        with self.assertRaises(ConfigurationError):
            self.store.begin_submission(job['id'], self.body(job), self.store.snapshot_generation())

    def test_shared_asset_hydration_preserves_delivery_override(self):
        self.enqueue()
        job = self.jobs()[0]
        with self.store.tx() as c:
            c.execute('UPDATE assets SET payload=? WHERE id=?',
                      (json.dumps({'generated': {'caption': 'Dasar'}, 'source_path': '/fixture'}), job['asset_id']))
        self.store.checkpoint(job['id'], payload={'generated': {'caption': 'Lokal'}, 'persona_version': 'one'})
        self.assertEqual(self.store.job(job['id'])['payload']['generated']['caption'], 'Lokal')
        deliveries = self.store.asset_deliveries(job['asset_id'])
        self.assertTrue(all(j['payload']['source_path'] == '/fixture' for j in deliveries))
        self.assertEqual(deliveries[1]['payload']['generated']['caption'], 'Dasar')

    def test_tiktok_share_link_uses_username_not_display_name(self):
        self.assertEqual(self.store.account_username('tiktok-0'), 'user0')
        self.assertEqual(self.store.account_username('facebook-0'), 'facebook 0')
        self.enqueue(required_destinations=['tiktok'])
        job = self.jobs()[0]
        self.store.checkpoint(job['id'], 'published', schedule_id='remote-1')
        self.assertEqual(self.store.publications_to_report()[0]['username'], 'user0')
