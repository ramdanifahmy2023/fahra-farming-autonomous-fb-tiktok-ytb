"""Offline request/byte audit. Optional production summaries use SQLite mode=ro only."""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import math
import socket
import sqlite3
import sys
import tempfile
import time
import tomllib
from collections import Counter
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from fahra_automation.config import Config, allowed_account
from fahra_automation.db import Store, ZONE
from fahra_automation.drive import Drive
from fahra_automation.replies import ReplyWorker
from fahra_automation.repliz import Repliz, SharedRateLimiter
from fahra_automation.util import iso, now


def deny_network(*args, **kwargs):
    raise AssertionError('This audit forbids network sockets; every HTTP response must be a fixture.')


def fixture_config(folder):
    data = tomllib.loads((ROOT / 'config.example.toml').read_text())
    data['workflow'].update(runtime_dir=str(folder), journal_dir=str(folder / 'journals'),
                            backup_dir=str(folder / 'backup'), mode='shadow', publish_enabled=False)
    data['repliz']['base_url'] = 'https://fixture.invalid'
    data['repliz']['min_request_interval_seconds'] = 0
    config = Config(ROOT, data)
    config.require_live_action = lambda: None  # Fixture guards: transport rejects every mutation instead.
    return config


def account(identity='fixture-account'):
    return {'id': identity, 'type': 'tiktok', 'username': identity, 'generatedId': 'fixture-owner',
            'isConnected': True, 'publishEligible': True, 'config': {'reply': {'isActive': False}}}


def paginated(rows, number):
    return {'docs': rows[(number - 1) * 100:number * 100], 'page': number,
            'totalDocs': len(rows), 'hasNextPage': number * 100 < len(rows),
            'totalPages': math.ceil(len(rows) / 100)}


async def sync_benchmark(parallel, latency, interval=.008):
    active = maximum = 0
    calls, starts = [], []
    accounts = [account(f'fixture-{index:03d}') for index in range(91)]
    async def handler(request):
        nonlocal active, maximum
        assert request.method == 'GET'
        active += 1
        maximum = max(maximum, active)
        calls.append(request.url.path)
        starts.append(time.monotonic())
        try:
            await asyncio.sleep(latency)
            page = int(request.url.params.get('page', '1'))
            if request.url.path == '/public/account':
                rows = accounts
            else:
                ids = [value for key, value in request.url.params.multi_items() if key.startswith('accountIds[')]
                rows = [{'id': ids[0] + '-schedule-' + str(i), 'accountId': ids[i % len(ids)],
                         'type': 'video', 'status': 'pending', 'scheduleAt': iso(now() + timedelta(days=1))} for i in range(210)]
            return httpx.Response(200, json=paginated(rows, page))
        finally:
            active -= 1
    store = SimpleNamespace(begin_sync=lambda: 1, accounts=lambda: [])
    applied = []
    store.apply_snapshot = lambda a, s, *args, **kw: applied.append((len(a), len(s)))
    config = SimpleNamespace(section=lambda name: {'base_url': 'https://fixture.invalid', 'sync_timeout_seconds': 10})
    async with httpx.AsyncClient(base_url='https://fixture.invalid', transport=httpx.MockTransport(handler)) as client:
        api = Repliz(config, client=client, limiter=SharedRateLimiter(interval))
        start = time.perf_counter()
        if not parallel:
            await api.sync(store)
        else:
            generation = store.begin_sync()
            rows = await api.pages('/public/account')
            ids = sorted(a['id'] for a in rows if allowed_account(a))
            beginning = now().astimezone(ZONE).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
            ending = beginning + timedelta(days=367)
            slots = asyncio.Semaphore(2)
            async def fetch(batch):
                async with slots:
                    schedules = await api.pages('/public/schedule', {'accountIds': batch, 'fromDate': iso(beginning), 'toDate': iso(ending)})
                    api.validate_schedules(schedules, set(batch))
                    return schedules
            batches = await asyncio.gather(*(fetch(ids[i:i + 30]) for i in range(0, len(ids), 30)))
            schedules = [row for batch in batches for row in batch]
            api.validate_schedules(schedules, set(ids))
            store.apply_snapshot(rows, schedules, beginning.date().isoformat(), ending.date().isoformat(), expected_generation=generation)
        elapsed = time.perf_counter() - start
    assert applied == [(91, 840)] and len(calls) == 13 and maximum <= 2
    return {'implementation': 'candidate bounded parallel group wrapper' if parallel else 'current Repliz.sync/collect_snapshot',
            'elapsed_seconds': elapsed, 'fixture_latency_seconds': latency, 'limiter_interval_seconds': interval,
            'requests': len(calls), 'max_in_flight': maximum, 'snapshot_applied_once': len(applied) == 1,
            'minimum_start_gap_seconds': min(b - a for a, b in zip(starts, starts[1:]))}


async def drive_bytes(folder):
    source = folder / 'source.mp4'
    data = b'\x00\x00\x00\x18ftypisom' + b'x' * (2 * 1024 * 1024 - 12)
    source.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    bank = folder / 'bank'
    bank.mkdir()
    relative = '2026/10/21/a/fixture.mp4'
    parts = Path(relative).parts
    root_id, file_id = 'fixture-folder-root', 'fixture-video-file'
    ids = [root_id, 'fixture-year-folder', 'fixture-month-folder', 'fixture-day-folder', 'fixture-prefix-folder', file_id]
    listing = {ids[i]: (parts[i], ids[i + 1]) for i in range(len(parts))}
    cfg = SimpleNamespace(section=lambda key: {'mode': 'synced_public', 'folder_id': root_id, 'sync_dir': str(bank)}, path=lambda value: Path(value))
    drive = Drive(cfg)
    payload = {'render_path': str(source), 'render_sha256': digest, 'drive_relative_path': relative}
    store = SimpleNamespace(job=lambda identity: {'payload': payload}, record=lambda *a, **kw: None)
    def checkpoint(identity, **kwargs):
        payload.update(kwargs.get('payload', {}))
    store.checkpoint = checkpoint
    counts = Counter()
    original_async = httpx.AsyncClient
    def handler(request):
        assert request.method == 'GET'
        if request.url.host == 'drive.google.com':
            counts['folder_GET'] += 1
            name, child = listing[request.url.params['id']]
            html = f'<div class="flip-entries"><a href="https://drive.google.com/file/d/{child}">{name}</a></div>'
            return httpx.Response(200, text=html)
        counts['full_file_GET'] += 1
        counts['fixture_response_bytes'] += len(data)
        return httpx.Response(200, content=data, headers={'content-type': 'video/mp4'})
    snapshots = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as sync_client:
        with patch('fahra_automation.drive.httpx.get', side_effect=sync_client.get), \
             patch('fahra_automation.drive.httpx.stream', side_effect=sync_client.stream), \
             patch('fahra_automation.drive.httpx.AsyncClient', side_effect=lambda **kw: original_async(transport=httpx.MockTransport(handler), **kw)):
            uploaded = drive.synced_file({'id': 'asset-fixture'}, store)
            payload.update(uploaded)
            snapshots.append({'stage': 'initial_bank_sync', **counts})
            for platform in ('tiktok', 'facebook', 'youtube'):
                checked = await drive.verify_delivery({'platform': platform, 'payload': payload})
                assert checked['sha256'] == digest
            snapshots.append({'stage': 'after_three_deliveries', **counts})
            await drive.verify_delivery({'platform': 'youtube', 'payload': payload})
            snapshots.append({'stage': 'after_one_repeated_preflight', **counts})
    assert counts == {'folder_GET': 25, 'full_file_GET': 5, 'fixture_response_bytes': 5 * len(data)}
    return {'functions': ['Drive.synced_file', 'Drive.resolve_file', 'Drive.verify_delivery', 'Drive.verify_url'],
            'fixture_file_bytes': len(data), 'fixture_is_mp4_prefix_bytes_not_real_video': True,
            'folder_depth': len(parts), 'cumulative': snapshots,
            'contract_note': 'Each full SHA-256 result was checked. Reuse of unchecked stale Drive content is not a proposed optimization.'}


class ReplyFixture:
    def __init__(self, folder, comments=205, ambiguous=False):
        self.config = fixture_config(folder)
        self.store = Store(folder / 'fixture.sqlite3')
        self.account = account()
        day = now().date()
        self.store.apply_snapshot([self.account], [], day.isoformat(), (day + timedelta(days=366)).isoformat())
        self.store.set('reply_enabled', True)
        self.store.set('reply_since', iso(now() - timedelta(days=1)))
        self.store.set('reply_account_since', {self.account['id']: iso(now() - timedelta(days=1))})
        self.store.set('reply_account_ids', [self.account['id']])
        self.calls = []
        self.changed_total = False
        self.jobs = []
        for index in range(5 if ambiguous else 1):
            code = 'fixture' + str(index)
            self.store.enqueue(1, 2, index + 1, [('https://instagram.com/reel/' + code + '/', code)])
            job = next(row for row in self.store.queue() if row['platform'] == 'tiktok' and self.store.job(row['id'])['source_id'] == code)
            self.store.checkpoint(job['id'], 'published', payload={'post_id': 'post-' + str(index)})
            self.jobs.append(job['id'])
        self.rows = [{'id': 'inbox-' + str(i), 'accountId': self.account['id'], 'status': 'pending', 'content': {'id': 'post-0'},
                     'comment': {'id': 'comment-' + str(i), 'type': 'text', 'text': 'Lucu sekali',
                                 'owner': {'id': 'viewer'}, 'createdAt': iso(now()), 'medias': []}} for i in range(comments)]
        self.automations = [{'id': 'automation-' + str(i), 'accountId': self.account['id'],
                             'contentId': 'post-' + str(i // 2) if i < 10 else 'other-' + str(i), 'config': {}} for i in range(250)] if ambiguous else []
        self.client = httpx.AsyncClient(base_url='https://fixture.invalid', transport=httpx.MockTransport(self.handle))
        self.api = Repliz(self.config, client=self.client, limiter=SharedRateLimiter(0))
        self.worker = ReplyWorker(self.config, self.store, self.api, ai=object())

    def handle(self, request):
        assert request.method == 'GET', 'Fixture forbids social provider mutations.'
        self.calls.append(request.url.path)
        page = int(request.url.params.get('page', '1'))
        if request.url.path == '/public/account':
            rows = [self.account]
        elif request.url.path.startswith('/public/account/'):
            return httpx.Response(200, json=self.account)
        elif request.url.path == '/public/comment':
            rows = self.rows
        elif request.url.path == '/public/automation':
            rows = self.automations
        else:
            raise AssertionError(request.url.path)
        body = paginated(rows, page)
        if self.changed_total and request.url.path == '/public/comment' and page == 2:
            body['totalDocs'] += 1
        return httpx.Response(200, json=body)

    def event_count(self):
        with self.store.connect() as connection:
            return connection.execute('SELECT count(*) FROM comment_events').fetchone()[0]

    def last_poll(self):
        with self.store.connect() as connection:
            detail = connection.execute("SELECT detail FROM events WHERE kind='reply_poll' ORDER BY seq DESC LIMIT 1").fetchone()[0]
            return json.loads(detail)


async def comments_audit(folder):
    fixture = ReplyFixture(folder)
    try:
        polls = []
        for _ in range(2):
            before = len(fixture.calls)
            await fixture.worker.ingest()
            polls.append({'GET': len(fixture.calls) - before, 'stored_comments': fixture.event_count(), 'last_event': fixture.last_poll()})
        fixture.changed_total = True
        failures = []
        for _ in range(2):
            before = len(fixture.calls)
            try:
                await fixture.worker.ingest()
            except ValueError as exc:
                failures.append({'GET': len(fixture.calls) - before, 'reason': str(exc), 'stored_comments': fixture.event_count()})
        assert [row['GET'] for row in polls] == [4, 4]
        assert [row['GET'] for row in failures] == [3, 3]
        assert all(row['stored_comments'] == 205 for row in polls + failures)
        return {'function': 'ReplyWorker.ingest + Repliz.pages', 'comment_count': 205,
                'unchanged_two_polls': polls, 'changed_total_two_failures': failures,
                'all_comment_rows_retrieved_each_successful_poll': True, 'no_cursor_saved': True}
    finally:
        await fixture.client.aclose()


async def adaptive_poll_audit(folder):
    results = []
    for adaptive in (False, True):
        fixture = ReplyFixture(folder / ('adaptive' if adaptive else 'fixed'), comments=5)
        ticks, elapsed, idle = [], 0, 0
        try:
            while elapsed < 3600:
                before = fixture.event_count()
                await fixture.worker.ingest()
                inserted = fixture.event_count() - before
                ticks.append(elapsed)
                idle = 0 if inserted else idle + 1
                delay = min(300, 120 * 2 ** idle) if adaptive else 120
                elapsed += delay
            results.append({'policy': 'candidate idle backoff 120→240→300s' if adaptive else 'current fixed120s',
                'virtual_poll_seconds': ticks, 'polls': len(ticks), 'actual_function_GET': len(fixture.calls),
                'stored_comments': fixture.event_count()})
        finally:
            await fixture.client.aclose()
    assert results[0]['stored_comments'] == results[1]['stored_comments'] == 5
    return {'function': 'ReplyWorker.ingest called at virtual schedule points; no wall-clock sleeps',
            'fixture': 'No new comments after initial five. Reset-on-new-comment tested by first ingest.', 'results': results,
            'tradeoff': 'Candidate delays discovery up to300s on idle accounts vs120s; not deployed, and cursor filtering is not assumed supported.'}


class SweepCache:
    """Candidate read-only cache limited to one sweep; actual ensure_post still evaluates every job."""
    def __init__(self, api):
        self.api, self.cache = api, {}
    async def pages(self, path, params=None):
        key = ('pages', path, json.dumps(params, sort_keys=True))
        if key not in self.cache:
            self.cache[key] = await self.api.pages(path, params)
        return copy.deepcopy(self.cache[key])
    async def request(self, method, path, **kwargs):
        assert method == 'GET'
        key = ('GET', path)
        if key not in self.cache:
            self.cache[key] = await self.api.request(method, path, **kwargs)
        return copy.deepcopy(self.cache[key])


async def profiles_audit(folder):
    results = []
    for cached in (False, True):
        fixture = ReplyFixture(folder / ('candidate' if cached else 'current'), comments=0, ambiguous=True)
        reference = now()
        cycles = []
        try:
            for seconds in (0, 60, 301):
                if cached:
                    candidate = SweepCache(fixture.api)
                    fixture.worker.api = candidate
                    fixture.worker.profiles.api = candidate
                before = len(fixture.calls)
                with patch('fahra_automation.replies.now', return_value=reference + timedelta(seconds=seconds)):
                    await fixture.worker.profile_once()
                jobs = [fixture.store.job(identity) for identity in fixture.jobs]
                cycles.append({'virtual_seconds': seconds, 'GET': len(fixture.calls) - before,
                    'profile_failures': [job['payload'].get('comment_profile_failures', 0) for job in jobs],
                    'profile_retry_delay_seconds': [(now_from(job['payload']['comment_profile_retry_at']) - (reference + timedelta(seconds=seconds))).total_seconds() for job in jobs]})
            results.append({'policy': 'candidate one-sweep immutable GET reuse' if cached else 'current per-post GET', 'cycles': cycles,
                            'path_counts': dict(Counter(fixture.calls)), 'remote_mutations': 0})
        finally:
            await fixture.client.aclose()
    assert [cycle['GET'] for cycle in results[0]['cycles']] == [21, 1, 21]
    assert [cycle['GET'] for cycle in results[1]['cycles']] == [5, 1, 5]
    assert results[0]['cycles'][-1]['profile_failures'] == results[1]['cycles'][-1]['profile_failures'] == [2] * 5
    return {'function': 'ReplyWorker.profile_once/ensure_profile + CommentProfiles.ensure_post + Repliz.pages',
            'fixture': 'Five published posts on one account;250 automation rows;two matching overrides per post, no comments.',
            'results': results, 'finding': 'Backoff exists and suppresses pre-deadline retries. Expired retries repeat identical account/automation scans.',
            'candidate_limit': 'Read-only batch cache must be scoped to account/snapshot generation and invalidated after mutations; never reuse it as final send guard.'}


def now_from(value):
    from datetime import datetime
    return datetime.fromisoformat(value)


def readonly_summary(path):
    uri = path.resolve(strict=True).as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        profiles = dict(connection.execute("""SELECT count(*) AS published_posts,
            sum(json_extract(payload,'$.comment_profile.checked_at') IS NULL) AS never_checked,
            sum(json_extract(payload,'$.comment_profile_failures')>0) AS with_failures,
            sum(julianday(json_extract(payload,'$.comment_profile_retry_at'))<=julianday('now')) AS expired_retry,
            sum(julianday(json_extract(payload,'$.comment_profile_retry_at'))>julianday('now')) AS future_retry,
            sum(julianday(created_at)<julianday('now','-2 days')) AS posts_older_48h,
            max(json_extract(payload,'$.comment_profile_failures')) AS max_failures
            FROM jobs WHERE status='published' AND json_extract(payload,'$.post_id') IS NOT NULL""").fetchone())
        polls = [dict(row) for row in connection.execute("""SELECT at,json_extract(detail,'$.comments') AS comments,
            json_extract(detail,'$.inserted') AS inserted,json_extract(detail,'$.accounts') AS accounts
            FROM events WHERE kind='reply_poll' ORDER BY seq DESC LIMIT 20""")]
        poll_errors = [dict(row) for row in connection.execute("""SELECT substr(json_extract(detail,'$.error'),1,160) AS reason,count(*) AS count
            FROM events WHERE kind='reply_poll_failed' AND julianday(at)>julianday('now','-1 day') GROUP BY reason ORDER BY count DESC LIMIT 5""")]
        statuses = [dict(row) for row in connection.execute('SELECT status,count(*) AS count FROM comment_events GROUP BY status')]
        health = [dict(row) for row in connection.execute("SELECT component,state,checked_at,duration_ms,json_extract(details,'$.checked') AS checked,json_extract(details,'$.failed') AS failed FROM health WHERE component IN ('reply_poll','reply_profiles','account_sync')")]
        connection.rollback()
    return {'access': 'sqlite mode=ro, read transaction, aggregate counts/timestamps only; no DB copy',
            'profiles': profiles, 'last20_successful_polls': polls, 'poll_error_counts_last24h': poll_errors,
            'comment_states': statuses, 'health': health}


async def run(args):
    output = args.output.resolve()
    if output.exists():
        raise ValueError('Output already exists; choose a new evidence file.')
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'scope': 'Offline MockTransport and isolated fixture SQLite. Production snapshot optional and read-only.', 'network_calls': 0,
              'source_code_modified': False, 'fixture_result': 'PASS'}
    with tempfile.TemporaryDirectory(prefix='io-fixture-', dir=output.parent) as temp:
        folder = Path(temp)
        pairs = []
        for label, latency in [('limiter_bound', .001), ('response_latency_bound', .025)]:
            current = await sync_benchmark(False, latency)
            candidate = await sync_benchmark(True, latency)
            pairs.append({'scenario': label, 'current': current, 'candidate': candidate,
                          'measured_speedup': current['elapsed_seconds'] / candidate['elapsed_seconds']})
        report['sync'] = {'clock_scale': 'Fixture interval0.008s versus production default0.65s; not a production latency forecast.', 'scenarios': pairs}
        drive_folder = folder / 'drive'; drive_folder.mkdir()
        report['drive'] = await drive_bytes(drive_folder)
        report['comments'] = await comments_audit(folder / 'comments')
        report['adaptive_comment_poll'] = await adaptive_poll_audit(folder / 'adaptive-comments')
        report['profiles'] = await profiles_audit(folder / 'profiles')
    if args.read_only_db:
        report['production_readonly'] = readonly_summary(args.read_only_db)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'fixture_result': report['fixture_result'], 'network_calls': 0,
                      'production_readonly_summary': bool(args.read_only_db), 'output': str(args.output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('runtime/verification/latency-deep/io.json'))
    parser.add_argument('--read-only-db', type=Path)
    arguments = parser.parse_args()
    with patch('socket.socket.connect', deny_network), patch('socket.socket.connect_ex', deny_network), patch('socket.create_connection', deny_network):
        asyncio.run(run(arguments))
