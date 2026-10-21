#!/usr/bin/env python3
"""Measure implemented selectors with disposable data and all sockets disabled."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from audit_pipeline_perf import Fixture, ROOT
from audit_scheduler_perf import matrix_case, horizon_case
from fahra_automation.util import iso


async def consume(fixture, stage, done):
    while not done.is_set():
        job = fixture.store.claim(stage=stage)
        if job:
            await fixture.pipeline.handle(job)
        else:
            await asyncio.sleep(.005)
        states = fixture.states()
        if states and set(states) == {'scheduled'}:
            done.set()
        if states.keys() & {'failed', 'submit_unknown', 'needs_review'}:
            raise AssertionError(dict(states))


async def drain(fixture, stages, *, timeout=45):
    done = asyncio.Event()
    tasks = [asyncio.create_task(consume(fixture, stage, done)) for stage in stages]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def blocked_download(folder, clock, separated):
    fixture = Fixture(folder, clock)
    fixture.block_source = 'SLOW'
    fixture.enqueue('SLOW', 1)
    fixture.enqueue('READY', 2, destinations=('tiktok',), ready=True)
    done = asyncio.Event()
    tasks = [asyncio.create_task(consume(fixture, 'prepare' if separated else None, done))]
    try:
        await asyncio.wait_for(fixture.download_entered.wait(), 10)
        if separated:
            tasks.append(asyncio.create_task(consume(fixture, 'send', done)))
            await asyncio.wait_for(fixture.ready_persona_entered.wait(), 15)
        else:
            await asyncio.sleep(.05)
        before_release = fixture.ready_persona_entered.is_set()
        assert before_release == separated
        fixture.download_release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 45)
    finally:
        fixture.download_release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert fixture.states() == {'scheduled': 4}
    assert fixture.calls['download_started'] == 1 and not fixture.duplicate_download_overlap
    return {'stages': ['prepare', 'send'] if separated else [None],
            'ready_persona_before_slow_download_released': before_release,
            'download_calls': fixture.calls['download_started'],
            'fixture_schedule_calls': fixture.calls['fake_schedule'],
            'duplicate_download_overlap': fixture.duplicate_download_overlap}


async def throughput(folder, clock, stages):
    fixture = Fixture(folder, clock)
    fixture.delay = .03
    for index in range(3):
        fixture.enqueue('ITEM' + str(index), index + 1)
    started, cpu = time.perf_counter(), time.process_time()
    await drain(fixture, stages)
    wall_ms, cpu_ms = (time.perf_counter() - started) * 1000, (time.process_time() - cpu) * 1000
    assert fixture.states() == {'scheduled': 9}
    assert fixture.calls['download_started'] == fixture.calls['analyze'] == 3
    assert fixture.calls['persona'] == fixture.calls['drive_preflight'] == fixture.calls['fake_schedule'] == 9
    assert not fixture.duplicate_download_overlap
    return {'stages': stages, 'wall_ms': round(wall_ms, 3), 'cpu_ms': round(cpu_ms, 3),
            'fake_operation_delay_ms': 30, 'calls': dict(fixture.calls), 'states': dict(fixture.states()),
            'duplicate_download_overlap': fixture.duplicate_download_overlap}


async def future_dates(folder, clock):
    fixture = Fixture(folder, clock)
    fixture.delay = 0
    day = clock[0].date().isoformat()
    fixture.snapshot([{'id': 'busy-' + str(i), 'accountId': 'youtube-fixture', 'type': 'video',
                       'status': 'pending', 'scheduleAt': day + f'T02:{i:02d}:00+00:00'} for i in range(20)])
    for index in range(40):
        fixture.enqueue('FUTURE' + str(index), index + 1, destinations=('youtube',), ready=True)
    fixture.enqueue('READYTT', 41, destinations=('tiktok',), ready=True)
    statements = Counter()
    original = fixture.store.connect

    def connect():
        connection = original()
        connection.set_trace_callback(lambda query: statements.update(['SELECT'])
                                     if query.lstrip().upper().startswith('SELECT') else None)
        return connection

    fixture.store.connect = connect
    claimed = []
    started, cpu = time.perf_counter(), time.process_time()
    while (job := fixture.store.claim(stage='send')) is not None:
        claimed.append(job['source_id'])
        await fixture.pipeline.handle(job)
    result = {'future_youtube_rows': 40, 'claimed_sources': claimed,
              'claims_before_ready_tiktok': len(claimed) - 1, 'select_statements_in_drain': statements['SELECT'],
              'wall_ms': round((time.perf_counter() - started) * 1000, 3),
              'cpu_ms': round((time.process_time() - cpu) * 1000, 3)}
    assert claimed == ['READYTT']
    assert fixture.calls['fake_schedule'] == 1
    return result


async def wakeup_and_persona(folder, clock):
    fixture = Fixture(folder / 'snapshot', clock)
    fixture.enqueue('WAIT', 1)
    fixture.store.begin_sync()
    await fixture.pipeline.handle(fixture.store.claim(stage='prepare'))
    clock[0] += timedelta(seconds=1)
    fixture.snapshot()
    ready = fixture.store.claim(stage='prepare')
    assert ready is not None and ready['attempts'] == 0
    fixture.store.checkpoint(ready['id'], leased=0)
    persona = Fixture(folder / 'persona', clock)
    persona.enqueue('PERSONA', 1, destinations=('tiktok',), ready=True)
    persona.invalidate_persona_once = True
    job = persona.store.claim(stage='send')
    await persona.pipeline.handle(job)
    assert persona.store.job(job['id'])['payload'].get('delivery_candidate')
    assert persona.calls['persona'] == 1
    clock[0] += timedelta(seconds=1)
    persona.snapshot()
    await persona.pipeline.handle(persona.store.claim(stage='send'))
    assert persona.states() == {'scheduled': 1}
    assert persona.calls['persona'] == 1 and persona.calls['fake_schedule'] == 1
    return {'snapshot_recovered_after_seconds': 1, 'claimed_immediately_after_snapshot': True,
            'extra_wait_after_snapshot_seconds': 0, 'persona_calls_across_snapshot_retry': 1,
            'candidate_reused': True, 'fixture_schedule_calls': 1}


async def pipeline_cases(folder, clock):
    results = {'head_of_line': [], 'throughput': []}
    for separated in (False, True):
        item = await blocked_download(folder / ('blocked-' + str(separated)), clock, separated)
        results['head_of_line'].append(item)
        print(json.dumps({'case': 'blocked_download', **item}), flush=True)
    for index, stages in enumerate(([None], ['prepare', 'send'], ['prepare', 'prepare', 'send'])):
        item = await throughput(folder / ('throughput-' + str(index)), clock, stages)
        results['throughput'].append(item)
        print(json.dumps({'case': 'throughput', **item}), flush=True)
    results['future_dates'] = await future_dates(folder / 'future', clock)
    results['wakeup_and_persona'] = await wakeup_and_persona(folder / 'recovery', clock)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/verification/latency-implementation/perf.json')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to(ROOT / 'runtime'):
        parser.error('Use a new output file inside this worktree runtime.')
    output.parent.mkdir(parents=True, exist_ok=True)
    clock = [datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)]
    report = {'result': 'RUNNING', 'evidence_kind': 'offline_fixture_implemented_code',
              'network_calls': 0, 'production_database_read': False, 'credential_read': False,
              'publication_requests': 0, 'real_media_rendered': False,
              'method': {'repetitions': 1, 'fixture_storage': 'worktree runtime volume',
                         'consumer': 'actual Store.claim(stage) and Pipeline.handle; finite fixture drain',
                         'comparison': 'one mixed consumer vs one/two preparers plus one sender on current code',
                         'limits': ['Synthetic adapters and frozen clock; no production throughput promise.',
                                    'Wall time may overlap other verification work; query counts are deterministic.',
                                    'Snapshot microprobe calls Store directly, not the threaded Repliz integration.',
                                    'No media CPU contention, external MCP lock, or live provider quotas modeled.']}}

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    def no_network(*args, **kwargs):
        raise AssertionError('Network is forbidden in the performance fixture.')

    with tempfile.TemporaryDirectory(prefix='latency-after-', dir=output.parent) as temporary, ExitStack() as stack:
        for target in ('fahra_automation.db.now', 'fahra_automation.pipeline.now', 'fahra_automation.util.now'):
            stack.enter_context(patch(target, side_effect=lambda: clock[0]))
        for target in ('socket.create_connection', 'socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex'):
            stack.enter_context(patch(target, side_effect=no_network))
        folder = Path(temporary)
        report['pipeline'] = asyncio.run(pipeline_cases(folder / 'pipeline', clock))
        clock[0] = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
        report['scheduler'] = []
        save()
        for jobs in (100, 500, 1000):
            for accounts in (63, 64):
                item = matrix_case(folder, jobs, accounts)
                report['scheduler'].append(item)
                save()
                print(json.dumps({'case': 'scheduler', 'jobs': jobs, 'accounts': accounts,
                                  'enqueue': item['enqueue_from_empty_no_external'],
                                  'loads': item['loads_all_no_external']}), flush=True)
        report['horizon_365_days_full'] = horizon_case(folder, 365)
    report.update(result='PASS', fixture_databases_removed=True, assertions_passed=True, completed_at=iso())
    save()
    print(json.dumps({'result': 'PASS', 'report': str(output)}), flush=True)


if __name__ == '__main__':
    main()
