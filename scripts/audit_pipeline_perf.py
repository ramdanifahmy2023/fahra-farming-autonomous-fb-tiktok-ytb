#!/usr/bin/env python3
"""Offline latency probes; uses disposable SQLite and synthetic adapter delays only."""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import time
import tomllib
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fahra_automation.attribution import from_metadata
from fahra_automation.config import Config, ConfigurationError
from fahra_automation.db import Store
from fahra_automation.media import Media
from fahra_automation.personas import PersonaService
from fahra_automation.pipeline import Pipeline
from fahra_automation.repliz import Repliz
from fahra_automation.util import iso


PROBE = {'duration': 30, 'width': 1080, 'height': 1920, 'codec': 'h264',
         'pixel_format': 'yuv420p', 'audio': True, 'audio_codec': 'aac', 'fps': 30, 'size': 2048}
COPY = {'hook': 'Kucing mengintip kardus', 'caption': 'Kardusnya langsung diperiksa.', 'hashtags': ['#kucing']}


class Fixture:
    def __init__(self, folder, clock):
        self.clock, self.folder = clock, folder
        # Deliberately do not call Config.load(): it can read runtime credentials.
        data = tomllib.loads((ROOT / 'config.example.toml').read_text())
        data['workflow'].update(runtime_dir='runtime', journal_dir='runtime/attempts', min_free_disk_gb=0)
        self.config = Config(folder, data)
        self.config.require_live_action = lambda: None  # All remote adapters below are in-memory fixtures.
        self.store = Store(folder / 'runtime/fixture.sqlite3')
        self.accounts = [{'id': p + '-fixture', 'username': p + '_fixture', 'name': p,
                          'type': p, 'isConnected': True, 'publishEligible': True}
                         for p in ('tiktok', 'facebook', 'youtube')]
        self.snapshot()
        self.calls, self.events = Counter(), []
        self.delay = .02
        self.started = time.monotonic()
        self.invalidate_persona_once = False
        self.stop_at_drive = False
        self.block_source = None
        self.download_entered = asyncio.Event()
        self.download_release = asyncio.Event()
        self.ready_persona_entered = asyncio.Event()
        self.active_downloads = set()
        self.duplicate_download_overlap = False
        self.analyzer = SimpleNamespace(ready=lambda: None, analyze=self.analyze,
                                        delivery_metadata=self.metadata, store=self.store)
        self.remote = SimpleNamespace(check_account=self.account, payload=Repliz.payload, create=self.create)
        self.drive = SimpleNamespace(ready=lambda: None, upload=self.upload, verify_delivery=self.verify)
        self.media = SimpleNamespace(probe=lambda source: dict(PROBE),
            validate_shared=Media.validate_shared, validate_delivery=Media.validate_delivery, render=self.render)
        self.personas = PersonaService(self.config, self.store, self.analyzer)
        self.pipeline = Pipeline(self.config, self.store, self.remote,
            SimpleNamespace(download=self.download), self.media, self.analyzer, self.drive, personas=self.personas)

    def event(self, operation, subject):
        self.calls[operation] += 1
        self.events.append({'operation': operation, 'subject': subject,
                            'elapsed_ms': round((time.monotonic() - self.started) * 1000, 3)})

    def snapshot(self, schedules=()):
        self.store.apply_snapshot(self.accounts, list(schedules), '2026-10-20', '2027-10-22')

    def enqueue(self, source, message, *, destinations=None, ready=False):
        self.store.enqueue(1, 1, message, [(f'https://www.instagram.com/reel/{source}/', source)],
                           required_destinations=destinations)
        with self.store.connect() as connection:
            asset_id = connection.execute('SELECT id FROM assets WHERE source_id=?', (source,)).fetchone()[0]
        if ready:
            attribution = from_metadata(f'https://www.instagram.com/reel/{source}/',
                                       {'id': source, 'extractor_key': 'Instagram', 'channel': 'fixture_creator'})
            self.store.asset_checkpoint(asset_id, 'drive_ready', {
                **attribution, 'render_probe': dict(PROBE), 'generated': dict(COPY),
                'drive_url': 'https://drive.usercontent.google.com/download?id=fixturevideo',
                'caption_review': {'observations': ['Kucing mengintip kardus.'], 'confidence': .9, 'tone': 'ringan'}})
        return asset_id

    async def download(self, job, store):
        self.event('download_started', job['source_id'])
        if job['id'] in self.active_downloads:
            self.duplicate_download_overlap = True
        self.active_downloads.add(job['id'])
        try:
            if job['source_id'] == self.block_source:
                self.download_entered.set()
                await self.download_release.wait()
            await asyncio.sleep(self.delay)
            target = self.folder / (job['id'] + '.fixture')
            target.write_bytes(job['id'].encode())
            attribution = from_metadata(job['url'], {'id': job['source_id'],
                'extractor_key': 'Instagram', 'channel': 'fixture_creator'})
            self.event('download_finished', job['source_id'])
            return {'source_path': str(target), 'source_caption': 'Fixture, bukan media produksi.', **attribution}
        finally:
            self.active_downloads.remove(job['id'])

    async def analyze(self, job, folder, **kwargs):
        self.event('analyze', job['source_id'])
        await asyncio.sleep(self.delay)
        return {'generated': dict(COPY), 'caption_review': {
            'observations': ['Kucing mengintip kardus.'], 'confidence': .9, 'tone': 'ringan'}, 'transcript': ''}

    def render(self, source, target, generated, job_id):
        time.sleep(self.delay)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'fixture-render-' + job_id.encode())
        return dict(PROBE)

    def upload(self, job, store):
        time.sleep(self.delay)
        return {'drive_url': 'https://drive.usercontent.google.com/download?id=fixturevideo'}

    async def account(self, account_id, platform):
        self.event('account_preflight', account_id)
        await asyncio.sleep(self.delay)
        return next(a for a in self.accounts if a['id'] == account_id)

    async def metadata(self, job, binding, folder):
        self.event('persona', job['source_id'])
        if job['source_id'] == 'READY':
            self.ready_persona_entered.set()
        await asyncio.sleep(self.delay)
        if self.invalidate_persona_once:
            self.invalidate_persona_once = False
            self.store.begin_sync()
        return {'title': COPY['hook'], 'caption': COPY['caption'], 'description': COPY['caption'],
                'hashtags': ['#kucing'], 'tags': ['kucing']}

    async def verify(self, job):
        self.event('drive_preflight', job['source_id'])
        await asyncio.sleep(self.delay)
        if self.stop_at_drive:
            raise ConfigurationError('Fixture berhenti setelah persona tersimpan.')
        return {'sha256': 'a' * 64, 'size': 2048}

    async def create(self, body, platform, *, before_send=None):
        if before_send:
            before_send()
        self.event('fake_schedule', body['medias'][0]['alt'])
        await asyncio.sleep(self.delay)
        return 'fixture-schedule-' + body['medias'][0]['alt'][3:]

    async def consume(self):
        # Same claim/await progression as worker_loop, with a finite drain for this fixture.
        while (job := self.store.claim()) is not None:
            await self.pipeline.handle(job)

    def states(self):
        with self.store.connect() as connection:
            return Counter(dict(row)['status'] for row in connection.execute('SELECT status FROM jobs'))


async def head_of_line(folder, clock, workers):
    fixture = Fixture(folder, clock)
    fixture.block_source = 'SLOW'
    fixture.enqueue('SLOW', 1)
    fixture.enqueue('READY', 2, destinations=('tiktok',), ready=True)
    first = asyncio.create_task(fixture.consume())
    await asyncio.wait_for(fixture.download_entered.wait(), 2)
    others = [asyncio.create_task(fixture.consume()) for _ in range(workers - 1)]
    # While the first downloader is held, the second may complete ready preflight.
    if workers == 2:
        await asyncio.wait_for(fixture.ready_persona_entered.wait(), 30)
    else:
        await asyncio.sleep(.05)
    ready_before_release = any(e['operation'] == 'persona' and e['subject'] == 'READY' for e in fixture.events)
    duplicate_claim = fixture.duplicate_download_overlap
    fixture.download_release.set()
    await asyncio.wait_for(asyncio.gather(first, *others), 120)
    assert fixture.states() == {'scheduled': 4}, fixture.states()
    assert fixture.calls['download_started'] == 1
    assert not duplicate_claim
    assert ready_before_release == (workers == 2)
    return {'consumers': workers, 'ready_persona_before_slow_download_released': ready_before_release,
            'download_calls': fixture.calls['download_started'], 'duplicate_asset_download_overlap': duplicate_claim,
            'scheduled_fixture_deliveries': fixture.calls['fake_schedule'], 'events': fixture.events}


async def throughput(folder, clock, workers):
    fixture = Fixture(folder, clock)
    fixture.delay = .03
    for index in range(3):
        fixture.enqueue('ITEM' + str(index), index + 1)
    gaps, finished = [], False
    async def heartbeat():
        previous = time.monotonic()
        while not finished:
            await asyncio.sleep(.005)
            current = time.monotonic()
            gaps.append(current - previous)
            previous = current
    ticker = asyncio.create_task(heartbeat())
    started = time.monotonic()
    await asyncio.wait_for(asyncio.gather(*(fixture.consume() for _ in range(workers))), 120)
    elapsed = time.monotonic() - started
    finished = True
    await ticker
    assert fixture.states() == {'scheduled': 9}, fixture.states()
    assert fixture.calls['download_started'] == 3
    assert fixture.calls['analyze'] == 3
    assert fixture.calls['persona'] == 9
    assert fixture.calls['drive_preflight'] == 9
    assert not fixture.duplicate_download_overlap
    return {'consumers': workers, 'elapsed_ms': round(elapsed * 1000, 3), 'calls': dict(fixture.calls),
            'duplicate_asset_download_overlap': fixture.duplicate_download_overlap,
            'states': dict(fixture.states()), 'delay_per_fake_operation_ms': 30,
            'event_loop_max_heartbeat_gap_ms': round(max(gaps, default=0) * 1000, 3)}


async def future_day(folder, clock):
    fixture = Fixture(folder, clock)
    fixture.delay = 0
    today = clock[0].date().isoformat()
    external = [{'id': 'yt-busy-' + str(i), 'accountId': 'youtube-fixture', 'type': 'video',
                 'status': 'pending', 'scheduleAt': today + f'T02:{i:02d}:00+00:00'} for i in range(20)]
    fixture.snapshot(external)
    for index in range(40):
        fixture.enqueue('FUTURE' + str(index), index + 1, destinations=('youtube',), ready=True)
    fixture.enqueue('READYTT', 41, destinations=('tiktok',), ready=True)
    selects = Counter()
    original_connect = fixture.store.connect
    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(lambda query: selects.update(['select']) if query.lstrip().upper().startswith('SELECT') else None)
        return connection
    fixture.store.connect = connect
    claimed = []
    started = time.monotonic()
    while (job := fixture.store.claim()) is not None:
        claimed.append(job['source_id'])
        await fixture.pipeline.handle(job)
    elapsed = time.monotonic() - started
    assert len(claimed) == len(set(claimed)) == 41
    assert claimed[-1] == 'READYTT'
    with fixture.store.connect() as connection:
        future = [dict(row) for row in connection.execute("SELECT next_attempt,slot_date FROM jobs WHERE platform='youtube'")]
    assert all(row['next_attempt'] > iso(clock[0]) for row in future)
    assert fixture.store.claim() is None
    return {'future_youtube_rows': 40, 'claims_before_ready_tiktok': len(claimed) - 1,
            'repeat_claim_before_future_date': False, 'select_statements_in_drain': selects['select'],
            'fixture_drain_ms': round(elapsed * 1000, 3), 'next_attempt_examples': future[:2],
            'provider_fixture_calls': dict(fixture.calls)}


async def snapshot_timer(folder, clock):
    fixture = Fixture(folder, clock)
    fixture.enqueue('SNAPWAIT', 1)
    fixture.store.begin_sync()
    job = fixture.store.claim()
    await fixture.pipeline.handle(job)
    held = fixture.store.job(job['id'])
    assert held['attempts'] == 0 and held['next_attempt'] == iso(clock[0] + timedelta(seconds=60))
    clock[0] += timedelta(seconds=1)
    fixture.snapshot()
    claim_after_recovery = fixture.store.claim()
    assert claim_after_recovery is None
    clock[0] += timedelta(seconds=59)
    claim_at_timer = fixture.store.claim()
    assert claim_at_timer is not None
    fixture.store.checkpoint(claim_at_timer['id'], leased=0)
    return {'snapshot_recovered_after_seconds': 1, 'claim_at_recovery': False,
            'extra_wait_after_recovery_seconds': 59, 'claim_at_60_seconds': True,
            'download_calls_while_deferred': fixture.calls['download_started']}


async def lost_persona(folder, clock):
    fixture = Fixture(folder, clock)
    fixture.enqueue('PERSONA', 1, destinations=('tiktok',), ready=True)
    fixture.invalidate_persona_once = True
    job = fixture.store.claim()
    await fixture.pipeline.handle(job)
    first = fixture.store.job(job['id'])
    assert 'delivery_metadata' not in first['payload']
    assert fixture.calls['persona'] == 1
    clock[0] += timedelta(seconds=61)
    fixture.snapshot()
    fixture.stop_at_drive = True
    await fixture.pipeline.handle(fixture.store.claim())
    second = fixture.store.job(job['id'])
    assert second['payload'].get('delivery_metadata')
    assert fixture.calls['persona'] == 2
    clock[0] += timedelta(seconds=61)
    fixture.stop_at_drive = False
    await fixture.pipeline.handle(fixture.store.claim())
    assert fixture.calls['persona'] == 2
    assert fixture.states() == {'scheduled': 1}
    return {'persona_calls': 2, 'lost_results_when_generation_changed_during_ai': 1,
            'cached_after_drive_failure': True, 'persona_calls_on_third_attempt': 0,
            'fake_schedule_calls': fixture.calls['fake_schedule']}


async def local_stage_event_loop():
    ticks = 0
    finished = False
    async def heartbeat():
        nonlocal ticks
        while not finished:
            ticks += 1
            await asyncio.sleep(.005)
    ticker = asyncio.create_task(heartbeat())
    await Pipeline.local_stage(time.sleep, .08)
    finished = True
    await ticker
    assert ticks > 3
    return {'synchronous_fixture_stage_ms': 80, 'event_loop_heartbeat_ticks': ticks,
            'whole_event_loop_blocked': False, 'calling_consumer_still_waits': True}


async def probes(root, clock):
    result = {'schema': 1, 'evidence_kind': 'offline_fixture_actual_Store_and_Pipeline',
              'production_db_read': False, 'credential_read': False, 'network_calls': 0,
              'provider_publications': 0, 'real_media_rendered': False,
              'fixture_storage': 'Disposable SQLite under this worktree runtime; production DB is never opened.',
              'limits': ['Artificial adapter delays are not production speed estimates.',
                         'Two consumers are not a deployed worker design or a full concurrency proof.',
                         'Media CPU contention, provider quotas and external locks are not simulated.']}
    result['head_of_line'] = [await head_of_line(root / f'hol-{n}', clock, n) for n in (1, 2)]
    result['throughput'] = [await throughput(root / f'throughput-{n}', clock, n) for n in (1, 2)]
    result['future_day'] = await future_day(root / 'future', clock)
    result['snapshot_timer'] = await snapshot_timer(root / 'snapshot', clock)
    result['lost_persona'] = await lost_persona(root / 'persona', clock)
    result['local_stage'] = await local_stage_event_loop()
    result['assertions_passed'] = True
    return result


def main():
    output = ROOT / 'runtime/verification/latency-deep/worker.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    clock = [datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)]
    with tempfile.TemporaryDirectory(prefix='worker-fixture-', dir=output.parent) as temp, ExitStack() as stack:
        for target in ('fahra_automation.db.now', 'fahra_automation.pipeline.now', 'fahra_automation.util.now'):
            stack.enter_context(patch(target, side_effect=lambda: clock[0]))
        def no_network(*args, **kwargs):
            raise AssertionError('Network is forbidden in this audit fixture.')
        for target in ('socket.create_connection', 'socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex'):
            stack.enter_context(patch(target, side_effect=no_network))
        result = asyncio.run(probes(Path(temp), clock))
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'report': str(output), 'assertions_passed': result['assertions_passed'],
                      'evidence_kind': result['evidence_kind']}))


if __name__ == '__main__':
    main()
