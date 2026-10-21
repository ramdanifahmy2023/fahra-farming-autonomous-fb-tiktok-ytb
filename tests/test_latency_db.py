import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fahra_automation.config import ConfigurationError
from fahra_automation.db import Store, ZONE
from fahra_automation.scheduler import parse_time
from fahra_automation.schema import VERSION
from fahra_automation.util import iso


def account(kind, number=0):
    return {'id': f'{kind}-{number}', 'username': f'user{kind}{number}', 'name': f'{kind} {number}',
            'type': kind, 'isConnected': True, 'publishEligible': True}


class LatencyDBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.at = datetime(2026, 10, 21, 7, tzinfo=ZONE)
        for name in ('fahra_automation.db.now', 'fahra_automation.util.now'):
            clock = patch(name, new=lambda: self.at)
            clock.start()
            self.addCleanup(clock.stop)
        self.store = Store(Path(self.tmp.name) / 'fixture.sqlite3')
        self.accounts = [account('tiktok'), account('facebook'), account('youtube')]
        self.message = 0
        self.sync()

    def sync(self, accounts=None, schedules=()):
        return self.store.apply_snapshot(accounts or self.accounts, list(schedules),
            (self.at.date() - timedelta(days=1)).isoformat(), (self.at.date() + timedelta(days=366)).isoformat())

    def enqueue(self, count=1, destinations=None, **kwargs):
        self.message += 1
        result = self.store.enqueue(1, 1, self.message,
            [(f'https://fixture.invalid/reel/{self.message}-{i}', f'fixture-{self.message}-{i}') for i in range(count)],
            required_destinations=destinations, **kwargs)
        with self.store.connect() as c:
            return [self.store.decode(r) for r in c.execute('SELECT * FROM jobs WHERE batch_id=? ORDER BY rowid',
                                                           (result['batch_id'],))]

    def ready(self, kind='youtube'):
        job = self.enqueue(destinations=[kind])[0]
        self.store.asset_checkpoint(job['asset_id'], 'drive_ready')
        return self.store.claim(stage='send')

    @staticmethod
    def body(job):
        return {'accountId': job['account_id'], 'scheduleAt': job['planned_at'], 'type': 'video',
                'title': 'Fixture', 'description': 'Fixture caption', 'medias': [{'url': 'https://fixture.invalid/video'}]}

    def scheduled(self, kind='youtube'):
        job = self.ready(kind)
        self.store.begin_submission(job['id'], self.body(job), self.store.snapshot_generation())
        self.store.checkpoint(job['id'], 'scheduled', schedule_id='remote-' + job['id'], leased=0)
        return self.store.job(job['id'])

    def remote(self, job, error='uploadLimitExceeded'):
        return {'id': job['schedule_id'], 'accountId': job['account_id'], 'scheduleAt': job['planned_at'],
                'type': 'reel' if job['platform'] == 'facebook' else 'video', 'status': 'error', 'errorMessage': error}

    def test_prepare_and_send_claims_are_separate_and_asset_lease_is_shared(self):
        preparing = self.enqueue()
        ready = self.enqueue(destinations=['tiktok'])[0]
        self.store.asset_checkpoint(ready['asset_id'], 'drive_ready')
        first = self.store.claim(stage='prepare')
        self.assertEqual(first['asset_id'], preparing[0]['asset_id'])
        self.assertIsNone(self.store.claim(stage='prepare'))
        sent = self.store.claim(stage='send')
        self.assertEqual(sent['id'], ready['id'])
        self.assertIsNone(self.store.claim(stage='send'))
        with self.assertRaises(ValueError):
            self.store.claim(stage='invalid')

    def test_parallel_preparers_never_claim_the_same_asset(self):
        self.enqueue(4)
        barrier = Barrier(4)
        def claim(_):
            barrier.wait(timeout=5)
            return self.store.claim(stage='prepare')
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed = list(pool.map(claim, range(4)))
        self.assertEqual(len({j['asset_id'] for j in claimed}), 4)
        self.assertIsNone(self.store.claim(stage='prepare'))

    def test_send_skips_future_without_blocking_shared_asset_preparation(self):
        future = self.enqueue(destinations=['youtube'])[0]
        self.store.asset_checkpoint(future['asset_id'], 'drive_ready')
        with self.store.connect() as c:
            c.execute('UPDATE jobs SET slot_date=?,planned_at=? WHERE id=?',
                      ((self.at.date() + timedelta(days=1)).isoformat(), iso(self.at + timedelta(days=1, hours=2)), future['id']))
        ready = self.ready('tiktok')
        self.assertEqual(ready['platform'], 'tiktok')
        self.assertIsNone(self.store.claim(stage='send'))
        shared = self.enqueue()
        with self.store.connect() as c:
            c.execute('UPDATE jobs SET slot_date=? WHERE asset_id=?',
                      ((self.at.date() + timedelta(days=1)).isoformat(), shared[0]['asset_id']))
        self.assertEqual(self.store.claim(stage='prepare')['asset_id'], shared[0]['asset_id'])

    def test_new_account_rechecks_future_delivery_once_and_keeps_saved_copy(self):
        jobs = self.enqueue(21, destinations=['youtube'])
        target = jobs[-1]
        self.store.asset_checkpoint(target['asset_id'], 'drive_ready')
        self.store.checkpoint(target['id'], payload={'delivery_metadata': {'account_id': target['account_id'], 'caption': 'Saved'}})
        self.assertIsNone(self.store.claim(stage='send'))
        self.sync(self.accounts + [account('youtube', 1)])
        claimed = self.store.claim(stage='send')
        self.assertEqual(claimed['id'], target['id'])
        self.assertEqual(self.store.ensure_slot(claimed['id'], self.at.date()), self.at.date().isoformat())
        moved = self.store.job(claimed['id'])
        self.assertEqual(moved['account_id'], 'youtube-1')
        self.assertNotIn('delivery_metadata', moved['payload'])
        self.assertNotIn('allocation_recheck', moved['payload'])

    def test_snapshot_wait_survives_restart_and_wakes_only_after_complete_snapshot(self):
        job = self.enqueue()[0]
        self.store.begin_sync()
        self.assertEqual(self.store.defer_dependency(job['id'], 'Menunggu snapshot'), 3)
        self.store = Store(self.store.path)
        self.assertIsNone(self.store.claim(stage='prepare'))
        with self.assertRaises(ValueError):
            self.sync([{**self.accounts[0], 'isConnected': None}])
        self.assertIsNone(self.store.claim(stage='prepare'))
        self.sync()
        self.assertEqual(self.store.claim(stage='prepare')['asset_id'], job['asset_id'])
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM job_waits').fetchone()[0], 0)

    def test_snapshot_wake_does_not_touch_drive_wait_transient_retry_or_pause(self):
        snapshot, drive, transient = self.enqueue(3, destinations=['tiktok'])
        self.store.begin_sync()
        self.store.defer_dependency(snapshot['id'], 'Snapshot', dependency='snapshot')
        self.store.defer_dependency(drive['id'], 'Drive', dependency='drive')
        retry_at = iso(self.at + timedelta(minutes=8))
        self.store.checkpoint(transient['id'], attempts=2, next_attempt=retry_at)
        self.store.set('paused', True)
        self.sync()
        self.assertIsNone(self.store.claim(stage='prepare'))
        self.assertEqual(self.store.job(drive['id'])['next_attempt'], iso(self.at + timedelta(seconds=60)))
        self.assertEqual(self.store.job(transient['id'])['next_attempt'], retry_at)
        self.assertEqual(self.store.job(transient['id'])['attempts'], 2)
        self.store.set('paused', False)
        self.assertEqual(self.store.claim(stage='prepare')['id'], snapshot['id'])
        self.assertEqual(self.store.wake_dependency('drive'), 1)
        self.assertEqual(self.store.claim(stage='prepare')['id'], drive['id'])

    def test_snapshot_recovery_racing_deferral_never_loses_wakeup(self):
        job = self.enqueue(destinations=['tiktok'])[0]
        self.store.begin_sync()
        barrier = Barrier(2)
        def defer():
            barrier.wait(timeout=5)
            return self.store.defer_dependency(job['id'], 'Snapshot changed')
        def recover():
            barrier.wait(timeout=5)
            return self.sync()
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(defer), pool.submit(recover)]
            for call in calls:
                call.result()
        self.assertEqual(self.store.job(job['id'])['next_attempt'], iso())
        self.assertEqual(self.store.claim(stage='prepare')['id'], job['id'])

    def test_dependency_cannot_requeue_intent_or_release_reserved_quota(self):
        job = self.scheduled()
        before = self.store.job(job['id'])
        self.assertEqual(self.store.defer_dependency(job['id'], 'Wait'), 0)
        with self.store.tx() as c:
            c.execute('INSERT INTO job_waits VALUES(?,?,?,?)', (job['id'], 'snapshot', 'Stale fixture', iso()))
        self.assertEqual(self.store.wake_dependency('snapshot'), 0)
        self.assertEqual(self.store.job(job['id']), before)

    def test_least_loaded_account_uses_earliest_slot_then_preserves_fairness(self):
        self.sync(self.accounts + [account('tiktok', 1)])
        with self.store.connect() as c:
            c.execute("UPDATE accounts SET slot_offset=2100 WHERE id='tiktok-0'")
            c.execute("UPDATE accounts SET slot_offset=300 WHERE id='tiktok-1'")
        first = self.enqueue(destinations=['tiktok'])[0]
        second = self.enqueue(destinations=['tiktok'])[0]
        self.assertEqual(first['account_id'], 'tiktok-1')
        self.assertEqual(second['account_id'], 'tiktok-0')
        self.assertLess(first['planned_at'], second['planned_at'])

    def test_configured_lead_reallocates_too_close_slot_and_rechecks_after_limiter(self):
        self.at = self.at.replace(hour=7, minute=58)
        self.store = Store(self.store.path, schedule_lead_seconds=300, snapshot_ttl=6000)
        self.sync()
        with self.store.tx() as c:
            c.execute("UPDATE accounts SET slot_offset=0 WHERE platform='tiktok'")
        job = self.enqueue(destinations=['tiktok'])[0]
        self.assertEqual(parse_time(job['planned_at']).astimezone(ZONE).strftime('%H:%M'), '08:45')
        metadata = {'account_id': job['account_id'], 'caption': 'Frozen fixture'}
        self.store.checkpoint(job['id'], planned_at=iso(self.at.replace(hour=8, minute=0)),
                              payload={'delivery_metadata': metadata})
        self.store.ensure_slot(job['id'], self.at.date())
        job = self.store.job(job['id'])
        self.assertEqual(parse_time(job['planned_at']).astimezone(ZONE).strftime('%H:%M'), '08:45')
        self.assertEqual(job['payload']['delivery_metadata'], metadata)
        self.store.asset_checkpoint(job['asset_id'], 'drive_ready')
        self.store.claim(stage='send')
        body = self.body(job)
        generation = self.store.snapshot_generation()
        self.store.begin_submission(job['id'], body, generation)
        self.at = self.at.replace(hour=8, minute=41)
        with self.assertRaises(ConfigurationError):
            self.store.validate_submission(job['id'], body, generation)
        self.assertEqual(self.store.job(job['id'])['payload']['submission'], body)

    def test_aggregated_loads_use_three_queries_and_do_not_double_count_remote_matches(self):
        job = self.scheduled('tiktok')
        with self.store.tx() as c:
            records = [(job['schedule_id'], job['account_id'], job['slot_date'], 'pending', '', job['planned_at'], job['id']),
                       ('unmatched', job['account_id'], job['slot_date'], 'error', '', job['planned_at'], None)]
            c.executemany('INSERT INTO external_schedules VALUES(?,?,?,?,?,?,?)', records)
        with self.store.connect() as c:
            statements = []
            c.set_trace_callback(statements.append)
            loads = self.store.loads(c, self.at.date().isoformat())
        self.assertEqual(len(statements), 3)
        self.assertEqual(loads[job['account_id']][1], 2)
        with self.store.connect() as c:
            c.execute('UPDATE jobs SET counts_quota=0 WHERE id=?', (job['id'],))
            self.assertEqual(self.store.loads(c, self.at.date().isoformat())[job['account_id']][1], 2)

    def test_parallel_enqueue_preserves_daily_quota_unique_slots_and_legacy_destinations(self):
        barrier = Barrier(4)
        def enqueue(number):
            barrier.wait(timeout=5)
            return self.store.enqueue(1, number + 1, number + 1,
                [(f'https://fixture.invalid/{number}-{i}', f'race-{number}-{i}') for i in range(10)],
                required_destinations=['facebook'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(enqueue, range(4)))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0], 40)
            self.assertEqual({r[0] for r in c.execute('SELECT platform FROM jobs')}, {'facebook'})
            self.assertFalse(c.execute('SELECT slot_date,count(*) n FROM jobs GROUP BY slot_date HAVING n>20').fetchall())
            self.assertFalse(c.execute('SELECT planned_at,count(*) n FROM jobs GROUP BY planned_at HAVING n>1').fetchall())

    def test_monitor_prioritizes_unknown_and_near_due_but_reserves_background_turn(self):
        jobs = self.enqueue(15, destinations=['tiktok'])
        for index, job in enumerate(jobs):
            self.store.checkpoint(job['id'], 'submit_unknown' if index < 12 else 'failed',
                schedule_id=None if index < 12 else 'remote-' + job['id'], payload={'next_poll': ''})
        chosen = self.store.pending_publications()
        self.assertEqual(len(chosen), 10)
        self.assertEqual(sum(job['status'] == 'submit_unknown' for job in chosen), 9)
        self.assertEqual(sum(job['status'] == 'failed' for job in chosen), 1)
        self.store.checkpoint(jobs[12]['id'], payload={'next_poll': iso(self.at + timedelta(hours=1))})
        self.assertNotIn(jobs[12]['id'], {job['id'] for job in self.store.pending_publications()})

    def test_known_upload_limit_is_durable_deduped_and_preserves_failed_quota(self):
        job = self.scheduled()
        remote = self.remote(job)
        first_until = iso(self.at + timedelta(hours=6))
        self.store.apply_remote_schedule(job['id'], remote)
        failed = self.store.job(job['id'])
        self.assertEqual(failed['payload']['failure_category'], 'provider_upload_limit')
        self.assertEqual(failed['counts_quota'], 1)
        self.assertEqual(failed['payload']['submission'], job['payload']['submission'])
        self.at += timedelta(hours=1)
        self.store = Store(self.store.path)
        self.sync(schedules=[remote])
        account_row = next(a for a in self.store.accounts() if a['id'] == job['account_id'])
        self.assertEqual(account_row['blocked_until'], first_until)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM provider_limits').fetchone()[0], 1)
        other = self.enqueue(destinations=['youtube'])[0]
        self.assertGreaterEqual(parse_time(other['planned_at']), parse_time(first_until))
        self.store.asset_checkpoint(other['asset_id'], 'drive_ready')
        self.assertIsNone(self.store.claim(stage='send'))

    def test_historical_failure_does_not_restart_cooldown_at_deployment(self):
        job = self.scheduled()
        self.store.checkpoint(job['id'], 'failed', leased=0)
        first_seen = iso()
        self.at += timedelta(days=1)
        self.store.apply_remote_schedule(job['id'], self.remote(job, 'The user has exceeded the number of videos they may upload.'))
        failed = self.store.job(job['id'])
        self.assertEqual(failed['payload']['provider_limit_first_seen'], first_seen)
        self.assertEqual(failed['counts_quota'], 1)
        self.assertIsNone(next(a['blocked_until'] for a in self.store.accounts() if a['id'] == job['account_id']))

    def test_snapshot_does_not_postpone_existing_failed_monitor_deadline(self):
        job = self.scheduled()
        remote = self.remote(job)
        self.store.apply_remote_schedule(job['id'], remote)
        deadline = iso(self.at + timedelta(hours=3))
        self.store.checkpoint(job['id'], payload={'next_poll': deadline, 'provider_poll_failures': 3})
        self.at += timedelta(minutes=5)
        self.sync(schedules=[remote])
        payload = self.store.job(job['id'])['payload']
        self.assertEqual(payload['next_poll'], deadline)
        self.assertEqual(payload['provider_poll_failures'], 3)

    def test_generic_limits_and_other_platforms_do_not_trigger_youtube_hold(self):
        for kind, error in [('youtube', 'A request limit may exist'), ('facebook', 'uploadLimitExceeded')]:
            with self.subTest(kind=kind):
                job = self.scheduled(kind)
                self.store.apply_remote_schedule(job['id'], self.remote(job, error))
                self.assertNotIn('failure_category', self.store.job(job['id'])['payload'])
                self.assertIsNone(next(a['blocked_until'] for a in self.store.accounts() if a['id'] == job['account_id']))

    def test_migration_from_v2_preserves_existing_routes_progress_and_payload(self):
        job = self.enqueue()[0]
        self.store.checkpoint(job['id'], payload={'legacy_origin': 'fixture', 'delivery_metadata': {'caption': 'Frozen'}})
        with self.store.tx() as c:
            c.execute('UPDATE batches SET chat_id=-1 WHERE id=?', (job['batch_id'],))
            c.execute('INSERT INTO batch_progress(batch_id,message_id,signature,page) VALUES(?,?,?,?)',
                      (job['batch_id'], 789, 'preserve', 2))
            c.execute("INSERT INTO outbox(dedupe_key,chat_id,text,created_at) VALUES('legacy',-1,'Fixture',?)", (iso(),))
            c.execute('PRAGMA user_version=2')
            c.execute('DROP TABLE job_waits')
            c.execute('DROP TABLE provider_limits')
            c.execute('DROP INDEX external_account_day')
            c.execute('DROP INDEX events_job_kind_at')
            c.execute('DROP INDEX jobs_account_schedule')
            c.execute('DROP INDEX jobs_published_account_post')
        before = self.store.job(job['id'])['payload']
        reopened = Store(self.store.path)
        self.assertEqual(reopened.job(job['id'])['payload'], before)
        with reopened.connect() as c:
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], VERSION)
            self.assertEqual(tuple(c.execute('SELECT message_id,signature,page FROM batch_progress').fetchone()), (789, 'preserve', 2))
            self.assertEqual(c.execute('SELECT sent FROM outbox').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT count(*) FROM provider_limits').fetchone()[0], 0)
            schedule_plan = c.execute('EXPLAIN QUERY PLAN SELECT id FROM jobs WHERE account_id=? AND schedule_id=?',
                                      ('fixture-account', 'fixture-schedule')).fetchall()
            post_plan = c.execute("""EXPLAIN QUERY PLAN SELECT id FROM jobs WHERE account_id=?
                AND status='published' AND json_extract(payload,'$.post_id')=?""",
                ('fixture-account', 'fixture-post')).fetchall()
            self.assertTrue(any('jobs_account_schedule' in row['detail'] for row in schedule_plan))
            self.assertTrue(any('jobs_published_account_post' in row['detail'] for row in post_plan))

    def test_telegram_timestamp_is_durable_and_replay_does_not_add_intake(self):
        received = iso(self.at - timedelta(seconds=9))
        jobs = self.enqueue(received_at=received)
        self.store.enqueue(1, 1, self.message, [], received_at=received)
        with self.store.connect() as c:
            events = c.execute("SELECT detail FROM events WHERE kind='batch_received'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0][0]), {'batch_id': jobs[0]['batch_id'], 'received_at': received, 'persisted_at': iso()})

    def test_timings_batch_merges_statistics_atomically(self):
        self.store.timing('fixture', 20)
        self.store.timings_batch({'fixture': {'samples': 2, 'total_ms': 30, 'max_ms': 20, 'last_ms': 10}})
        with self.store.connect() as c:
            row = c.execute('SELECT samples,total_ms,max_ms,last_ms FROM timings').fetchone()
        self.assertEqual(tuple(row), (3, 50, 20, 10))
        with self.assertRaises(ValueError):
            self.store.timings_batch({'fixture': {'samples': 1, 'total_ms': float('nan'), 'max_ms': 1, 'last_ms': 1}})


if __name__ == '__main__':
    unittest.main()
