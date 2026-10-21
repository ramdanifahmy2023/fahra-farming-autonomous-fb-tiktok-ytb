"""Measure disposable SQLite telemetry; never open the operational database."""
import argparse
import asyncio
import json
import socket
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from fahra_automation.db import Store
from fahra_automation.repliz import Repliz, SharedRateLimiter


class FixtureConfig:
    def section(self, name):
        return {'base_url': 'https://audit.invalid'}


def summary(values):
    values = sorted(values)
    return {'n': len(values), 'median_ms': round(statistics.median(values), 3),
            'p90_ms': round(values[round((len(values) - 1) * .9)], 3),
            'max_ms': round(max(values), 3)}


def blocked(*args, **kwargs):
    raise RuntimeError('Network disabled for offline audit')


async def request_probe(store, count):
    lag = []
    stop = asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            start = time.perf_counter()
            await asyncio.sleep(.005)
            lag.append(max(0, (time.perf_counter() - start - .005) * 1000))

    async def transport(request):
        await asyncio.sleep(.005)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(base_url='https://audit.invalid',
                                transport=httpx.MockTransport(transport)) as client:
        api = Repliz(FixtureConfig(), client=client, limiter=SharedRateLimiter(0))
        api.observer = store
        ticker = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        start = time.perf_counter()
        for _ in range(count):
            await api.request('GET', '/fixture')
        elapsed = time.perf_counter() - start
        stop.set()
        await ticker
    return {'requests': count, 'transport_delay_ms': 5, 'limiter_interval_ms': 0,
            'elapsed_seconds': round(elapsed, 4), 'heartbeat_lag': summary(lag)}


async def measure(parent, count):
    with tempfile.TemporaryDirectory(prefix='audit-sqlite-', dir=parent) as temporary:
        store = Store(Path(temporary) / 'fixture.sqlite3')
        writes = []
        for i in range(count):
            start = time.perf_counter()
            store.timing('fixture', i)
            writes.append((time.perf_counter() - start) * 1000)
        with store.connect() as connection:
            journal = connection.execute('PRAGMA journal_mode').fetchone()[0]
            synchronous = connection.execute('PRAGMA synchronous').fetchone()[0]
        return {'path_parent': str(parent), 'journal_mode': journal,
                'synchronous': synchronous, 'telemetry_transactions': summary(writes),
                'mock_requests_with_observer': await request_probe(store, count)}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=20)
    args = parser.parse_args()
    if not 5 <= args.samples <= 100:
        parser.error('Use 5 to 100 small fixture transactions')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with patch.object(socket.socket, 'connect', blocked), patch.object(socket, 'create_connection', blocked):
        result = {'production_access': False, 'network_calls': 0,
                  'unobserved_mock_requests': await request_probe(None, args.samples),
                  'workspace_volume': await measure(args.output.parent, args.samples),
                  'system_temporary_volume': await measure(tempfile.gettempdir(), args.samples),
                  'limits': ['Disposable tiny database, not a clone of production.',
                             'Wall times depend on current storage and concurrent system load.',
                             'Mock request latency is controlled, not Repliz network latency.',
                             'Production journaling and durability are unchanged.']}
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
