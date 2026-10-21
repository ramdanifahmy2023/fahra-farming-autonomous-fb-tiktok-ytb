#!/usr/bin/env python3
"""Offline scheduler measurements. Uses generated fixtures, never a production DB."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fahra_automation.db import Store, ZONE
from fahra_automation.scheduler import daily_slots, offset_for
from fahra_automation.util import iso

AT = datetime(2026, 10, 21, 7, tzinfo=ZONE)
DAY = AT.date()
END = (DAY + timedelta(days=366)).isoformat()


class SQLCounter:
    def __init__(self):
        self.statements = 0
        self.connections = 0
        self.categories = Counter()

    def trace(self, statement):
        self.statements += 1
        sql = ' '.join(statement.lower().split())
        patterns = {
            'quota_local_count': 'select count(*) from jobs where account_id=',
            'quota_external_count': 'select count(*) from external_schedules e where account_id=',
            'occupied_local_slots': 'select planned_at from jobs where account_id=',
            'occupied_external_slots': 'select scheduled_at from external_schedules where account_id=',
            'account_pool': 'select * from accounts where connected=1',
            'setting_read': 'select value from settings where key=',
            'snapshot_match': 'select * from jobs where account_id=',
            'published_proof': 'select 1 from jobs j join events e on e.job_id=j.id',
        }
        category = next((name for name, prefix in patterns.items() if sql.startswith(prefix)), sql.split()[0])
        self.categories[category] += 1


class ProbeStore(Store):
    counter = None

    def connect(self):
        connection = super().connect()
        if self.counter is not None:
            self.counter.connections += 1
            connection.set_trace_callback(self.counter.trace)
        return connection


def measure(store, operation, *, expected_error=None):
    counter = SQLCounter()
    store.counter = counter
    wall, cpu = time.perf_counter(), time.process_time()
    result = None
    error = None
    try:
        result = operation()
    except Exception as exc:
        if expected_error is None or not isinstance(exc, expected_error):
            raise
        error = type(exc).__name__ + ': ' + str(exc)
    finally:
        elapsed, consumed = time.perf_counter() - wall, time.process_time() - cpu
        store.counter = None
    return {
        'wall_ms': round(elapsed * 1000, 3), 'cpu_ms': round(consumed * 1000, 3),
        'sql_statements': counter.statements, 'connections': counter.connections,
        'query_categories': dict(counter.categories), 'expected_error': error,
    }, result


def accounts(count):
    return [
        {'id': f'fixture-{kind}-{number}', 'username': f'fixture{kind}{number}',
         'name': f'Fixture {kind} {number}', 'type': kind,
         'isConnected': True, 'publishEligible': True}
        for kind, size in [('tiktok', count - 18), ('facebook', 14), ('youtube', 4)]
        for number in range(size)
    ]


def remote_schedules(rows, count):
    schedules = []
    for number in range(count):
        account = rows[number % len(rows)]
        per_account = number // len(rows)
        day = DAY + timedelta(days=per_account // 20)
        slot = daily_slots(day, 20, offset_for(account['id'], 20))[per_account % 20]
        schedules.append({'id': f'fixture-remote-{number}', 'accountId': account['id'],
                          'type': 'reel' if account['type'] == 'facebook' else 'video',
                          'status': 'pending', 'title': 'Fixture remote', 'scheduleAt': iso(slot)})
    return schedules


def seed_jobs(store, count):
    """Normal enqueue/allocate, maximum 100 source URLs per simulated message."""
    metrics = []
    remaining, sequence = count, 1
    while remaining:
        deliveries = min(remaining, 300)
        full_sources, tail = divmod(deliveries, 3)
        if full_sources:
            urls = [(f'https://fixture.invalid/reel/{sequence}-{i}', f'fixture-{sequence}-{i}')
                    for i in range(full_sources)]
            metric, result = measure(store, lambda: store.enqueue(1, 1, sequence, urls))
            assert result['accepted'] == full_sources
            metric.update(deliveries=full_sources * 3, sources=full_sources)
            metrics.append(metric)
            sequence += 1
        if tail:
            metric, result = measure(store, lambda: store.enqueue(1, 1, sequence,
                [(f'https://fixture.invalid/reel/tail-{sequence}', f'fixture-tail-{sequence}')],
                required_destinations=('tiktok', 'facebook')[:tail]))
            assert result['accepted'] == 1
            metric.update(deliveries=tail, sources=1)
            metrics.append(metric)
            sequence += 1
        remaining -= deliveries
    return metrics


def total_metrics(metrics):
    categories = Counter()
    for metric in metrics:
        categories.update(metric['query_categories'])
    return {
        'wall_ms': round(sum(m['wall_ms'] for m in metrics), 3),
        'cpu_ms': round(sum(m['cpu_ms'] for m in metrics), 3),
        'sql_statements': sum(m['sql_statements'] for m in metrics),
        'max_single_message_wall_ms': max(m['wall_ms'] for m in metrics),
        'query_categories': dict(categories), 'messages': len(metrics),
    }


def read_loads(store, kind=None):
    with store.connect() as connection:
        return store.loads(connection, DAY.isoformat(), kind)


def query_plans(store):
    statements = {
        'local_quota': ("SELECT count(*) FROM jobs WHERE account_id=? AND slot_date=? AND counts_quota=1",
                        ('fixture-youtube-0', DAY.isoformat())),
        'external_quota': ("""SELECT count(*) FROM external_schedules e WHERE account_id=? AND slot_date=?
            AND NOT EXISTS(SELECT 1 FROM jobs j WHERE (j.schedule_id=e.id OR j.id=e.job_id)
            AND j.account_id=e.account_id AND j.slot_date=e.slot_date AND j.counts_quota=1)""",
            ('fixture-youtube-0', DAY.isoformat())),
        'external_occupied_slots': ("SELECT scheduled_at FROM external_schedules WHERE account_id=? AND slot_date=?",
                                    ('fixture-youtube-0', DAY.isoformat())),
        'snapshot_replace_range': ("DELETE FROM external_schedules WHERE slot_date BETWEEN ? AND ?",
                                   (DAY.isoformat(), END)),
        'snapshot_match': ("""SELECT * FROM jobs WHERE account_id=? AND
            (schedule_id=? OR json_extract(payload,'$.submission') IS NOT NULL)""",
            ('fixture-youtube-0', 'fixture-remote-0')),
        'published_proof': ("""SELECT 1 FROM jobs j JOIN events e ON e.job_id=j.id
            WHERE j.account_id=? AND j.status='published'
            AND json_extract(j.payload,'$.post_id') IS NOT NULL
            AND json_extract(j.payload,'$.post_url') IS NOT NULL
            AND e.kind='status_changed' AND json_extract(e.detail,'$.status')='published'
            AND e.at>=? AND e.at<=? LIMIT 1""",
            ('fixture-youtube-0', iso(AT - timedelta(days=7)), iso(AT))),
    }
    with store.connect() as connection:
        return {name: [dict(row) for row in connection.execute('EXPLAIN QUERY PLAN ' + sql, params)]
                for name, (sql, params) in statements.items()}


async def snapshot_event_loop_probe(store, rows, schedules):
    delays = []
    interval = .01
    async def tick():
        previous = time.perf_counter()
        while True:
            await asyncio.sleep(interval)
            current = time.perf_counter()
            delays.append(max(0, current - previous - interval) * 1000)
            previous = current
    task = asyncio.create_task(tick())
    await asyncio.sleep(interval * 2)
    metric, _ = measure(store, lambda: store.apply_snapshot(rows, schedules, DAY.isoformat(), END))
    await asyncio.sleep(interval * 2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    metric['heartbeat_interval_ms'] = interval * 1000
    metric['max_event_loop_lag_ms'] = round(max(delays, default=0), 3)
    return metric


def matrix_case(folder, count, account_count):
    rows = accounts(account_count)
    store = ProbeStore(folder / f'matrix-{count}-{account_count}.sqlite3')
    store.apply_snapshot(rows, [], DAY.isoformat(), END)
    enqueue_metrics = seed_jobs(store, count)
    with store.connect() as connection:
        assert connection.execute('SELECT count(*) FROM jobs').fetchone()[0] == count
    initial_load, _ = measure(store, lambda: read_loads(store))
    schedules = remote_schedules(rows, count)
    snapshot = asyncio.run(snapshot_event_loop_probe(store, rows, schedules))
    external_load, _ = measure(store, lambda: read_loads(store))
    youtube_load, _ = measure(store, lambda: read_loads(store, 'youtube'))
    new_batch, _ = measure(store, lambda: store.enqueue(1, 1, 90001,
        [(f'https://fixture.invalid/reel/new-{i}', f'fixture-new-{i}') for i in range(10)]))
    with store.connect() as connection:
        settings = {'journal_mode': connection.execute('PRAGMA journal_mode').fetchone()[0],
                    'synchronous': connection.execute('PRAGMA synchronous').fetchone()[0],
                    'db_bytes': store.path.stat().st_size}
    return {
        'existing_jobs': count, 'connected_accounts': account_count,
        'platform_counts': {'tiktok': account_count - 18, 'facebook': 14, 'youtube': 4},
        'enqueue_from_empty_no_external': total_metrics(enqueue_metrics),
        'enqueue_messages': enqueue_metrics, 'loads_all_no_external': initial_load,
        'apply_snapshot_external_rows_equal_jobs': snapshot,
        'loads_all_with_external': external_load, 'loads_youtube_with_external': youtube_load,
        'enqueue_10_more_sources_30_deliveries_with_external': new_batch,
        'query_plans': query_plans(store), 'sqlite': settings,
    }


def horizon_case(folder, full_days, candidate_index=False):
    rows = accounts(64)
    store = ProbeStore(folder / f'horizon-{full_days}-{int(candidate_index)}.sqlite3')
    store.apply_snapshot(rows, [], DAY.isoformat(), END)
    youtube = [row for row in rows if row['type'] == 'youtube']
    records = []
    for day_offset in range(full_days):
        day = DAY + timedelta(days=day_offset)
        for account in youtube:
            for number, stamp in enumerate(daily_slots(day, 20, offset_for(account['id'], 20))):
                records.append((f'fixture-full-{day_offset}-{account["id"]}-{number}',
                    account['id'], day.isoformat(), 'pending', 'Fixture full', iso(stamp), None))
    with store.tx() as connection:
        connection.executemany('INSERT INTO external_schedules VALUES(?,?,?,?,?,?,?)', records)
        if candidate_index:
            connection.execute('CREATE INDEX fixture_external_account_day ON external_schedules(account_id,slot_date)')
    store.enqueue(1, 1, 1, [('https://fixture.invalid/reel/probe', 'fixture-probe')],
                  can_allocate=False, required_destinations=['youtube'])
    with store.connect() as connection:
        job_id = connection.execute('SELECT id FROM jobs').fetchone()[0]
    def allocate():
        with store.tx() as connection:
            return store.allocate(connection, job_id, DAY)
    metric, allocated = measure(store, allocate, expected_error=RuntimeError if full_days == 366 else None)
    with store.connect() as connection:
        slot = connection.execute('SELECT slot_date FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
    if full_days < 366:
        assert allocated and slot == (DAY + timedelta(days=full_days)).isoformat()
    else:
        assert metric['expected_error'] and slot is None
    return {'full_days': full_days, 'external_rows': len(records), 'local_jobs': 1,
            'connected_accounts': 64, 'target_accounts': 4,
            'candidate_fixture_index_only': candidate_index, 'allocate': metric,
            'reserved_day': slot, 'query_plans': query_plans(store)}


def event_history_case(folder):
    store = ProbeStore(folder / 'events-fixture.sqlite3')
    rows = accounts(64)
    store.apply_snapshot(rows, [], DAY.isoformat(), END)
    seed_jobs(store, 1000)
    with store.tx() as connection:
        job_ids = [row[0] for row in connection.execute('SELECT id FROM jobs')]
        connection.execute("UPDATE jobs SET status='published',payload=?",
                           (json.dumps({'post_id': 'fixture-post', 'post_url': 'https://fixture.invalid/post'}),))
        connection.executemany('INSERT INTO events(at,job_id,kind,detail) VALUES(?,?,?,?)',
            [(iso(AT), job_ids[index % len(job_ids)], 'status_changed', '{"status":"queued"}')
             for index in range(40000)])
    baseline = asyncio.run(snapshot_event_loop_probe(store, rows, []))
    original_plan = query_plans(store)['published_proof']
    with store.tx() as connection:
        connection.execute('CREATE INDEX fixture_event_job_kind_at ON events(job_id,kind,at)')
    candidate = asyncio.run(snapshot_event_loop_probe(store, rows, []))
    return {
        'local_jobs': 1000, 'connected_accounts': 64, 'added_noise_events': 40000,
        'case': 'published jobs lack qualifying published event; forces proof fallback for every account',
        'baseline': baseline, 'baseline_plan': original_plan,
        'candidate_fixture_index_only': candidate,
        'candidate_plan': query_plans(store)['published_proof'],
        'limits': 'Deliberate worst-case missing proof, not a reconstruction of production event mix.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/verification/latency-deep/backend.json')
    parser.add_argument('--sizes', type=int, nargs='+', default=[100, 500, 1000])
    parser.add_argument('--accounts', type=int, nargs='+', choices=[63, 64], default=[63, 64])
    parser.add_argument('--horizon-days', type=int, nargs='*', default=[1, 7, 30, 365, 366])
    parser.add_argument('--index-experiment', action='store_true')
    parser.add_argument('--event-history-only', action='store_true')
    args = parser.parse_args()
    if any(size < 1 or size > 3000 for size in args.sizes) or any(day < 0 or day > 366 for day in args.horizon_days):
        parser.error('Fixture size outside the bounded audit range.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        'kind': 'offline_generated_fixture_only', 'production_code_modified': False,
        'production_database_read': False, 'network_used': False,
        'frozen_time': AT.isoformat(), 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
        'platform': platform.system() + '/' + platform.machine(),
        'method': {
            'filesystem': 'temporary fixtures under output directory; same volume as output',
            'repetitions': 1, 'trace_overhead': 'SQL trace enabled during each measured operation; PRAGMA setup excluded',
            'clock': 'wall perf_counter and process CPU process_time measured independently',
            'source_of_data': 'synthetic accounts, URLs, jobs and schedules only; no Config.load()',
            'matrix': 'seed N exact deliveries, then add N unmatched remote rows; final new intake is 30 deliveries',
            'index_experiment': 'separate disposable fixture only, production SQL/methods unchanged',
            'limits': 'One machine and storage load; not production latency, not network/AI/provider time; no p95 estimate',
        }, 'matrix': [], 'horizon': [], 'hypothetical_index': [],
    }
    if args.event_history_only and args.output.exists():
        existing = json.loads(args.output.read_text())
        if existing.get('kind') != 'offline_generated_fixture_only':
            parser.error('Refusing to append to an unrelated report.')
        report = existing
        report['method']['event_history_run_separately'] = True
    report.setdefault('invocations', []).append(sys.argv[1:])
    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    with tempfile.TemporaryDirectory(prefix='scheduler-fixture-', dir=args.output.parent) as temporary, ExitStack() as stack:
        stack.enter_context(patch('fahra_automation.db.now', new=lambda: AT))
        stack.enter_context(patch('fahra_automation.util.now', new=lambda: AT))
        folder = Path(temporary)
        for count in ([] if args.event_history_only else args.sizes):
            for account_count in args.accounts:
                item = matrix_case(folder, count, account_count)
                report['matrix'].append(item)
                save()
                print(json.dumps({'case': 'matrix', 'jobs': count, 'accounts': account_count,
                    'enqueue': item['enqueue_from_empty_no_external'],
                    'snapshot': item['apply_snapshot_external_rows_equal_jobs']}), flush=True)
        for full_days in ([] if args.event_history_only else args.horizon_days):
            item = horizon_case(folder, full_days)
            report['horizon'].append(item)
            save()
            print(json.dumps({'case': 'horizon', 'days': full_days, 'allocate': item['allocate']}), flush=True)
        if args.index_experiment and not args.event_history_only:
            for full_days in (30, 365):
                item = horizon_case(folder, full_days, candidate_index=True)
                report['hypothetical_index'].append(item)
                save()
                print(json.dumps({'case': 'fixture_index_only', 'days': full_days, 'allocate': item['allocate']}), flush=True)
        if args.index_experiment or args.event_history_only:
            report['event_history'] = event_history_case(folder)
            save()
            print(json.dumps({'case': 'event_history', **report['event_history']}), flush=True)
    report['fixture_databases_removed'] = True
    save()
    print(str(args.output), flush=True)


if __name__ == '__main__':
    main()
