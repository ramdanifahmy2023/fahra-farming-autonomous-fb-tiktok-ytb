import asyncio
import copy
import hashlib
import tempfile
import threading
import unittest
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from fahra_automation.config import ConfigurationError
from fahra_automation.drive import Drive


class CountedBody(httpx.AsyncByteStream):
    def __init__(self, fixture, body):
        self.fixture, self.body = fixture, body
        self.bytes_read, self.closed = 0, False

    async def __aiter__(self):
        fixture = self.fixture
        fixture.active += 1
        fixture.maximum = max(fixture.maximum, fixture.active)
        if fixture.active == 2:
            fixture.two_started.set()
        try:
            if fixture.release_streams is not None:
                await fixture.release_streams.wait()
            for offset in range(0, len(self.body), 256 * 1024):
                chunk = self.body[offset:offset + 256 * 1024]
                self.bytes_read += len(chunk)
                yield chunk
        finally:
            fixture.active -= 1

    async def aclose(self):
        self.closed = True


class FixtureStore:
    def __init__(self, rows):
        self.rows = rows
        self.lock = threading.RLock()
        self.records = []

    def job(self, identity):
        with self.lock:
            return copy.deepcopy(self.rows[identity])

    def checkpoint(self, identity, *, payload):
        with self.lock:
            self.rows[identity]['payload'].update(payload)

    def record(self, kind, identity, **detail):
        with self.lock:
            self.records.append((kind, identity, detail))


class DrivePoolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bank = self.root / 'bank'; self.bank.mkdir()
        self.data = b'\x00\x00\x00\x18ftypisom' + b'x' * (2 * 1024 * 1024 + 41)
        self.remote_body = self.data
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.file = self.bank / 'video.mp4'; self.file.write_bytes(self.data)
        self.cfg = {'folder_id': 'fixture-root-123', 'mode': 'synced_public', 'sync_dir': 'bank', 'parallel_verifications': 2}
        self.config = SimpleNamespace(section=lambda section: self.cfg, path=lambda value: self.root / value)
        self.payload = {'drive_root_id': self.cfg['folder_id'], 'drive_relative_path': 'video.mp4',
                        'drive_file_id': 'fixture-video-123', 'drive_path_ids': ['fixture-video-123'],
                        'drive_url': Drive.media_url('fixture-video-123'), 'bank_path': str(self.file),
                        'render_sha256': self.digest, 'drive_verified_sha256': self.digest,
                        'drive_verified_size': len(self.data)}
        self.sync_calls, self.async_calls, self.streams = [], [], []
        self.sync_clients, self.async_clients = [], []
        self.active = self.maximum = 0
        self.two_started = asyncio.Event()
        self.release_streams = None
        original_sync, original_async = httpx.Client, httpx.AsyncClient

        def sync_factory(**kwargs):
            client = original_sync(transport=httpx.MockTransport(self.sync_handler), **kwargs)
            self.sync_clients.append(client)
            return client

        def async_factory(**kwargs):
            client = original_async(transport=httpx.MockTransport(self.async_handler), **kwargs)
            self.async_clients.append(client)
            return client

        for name, factory in (('Client', sync_factory), ('AsyncClient', async_factory)):
            mocked = patch('fahra_automation.drive.httpx.' + name, side_effect=factory)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.drive = Drive(self.config, pool_connections=True)

    async def asyncTearDown(self):
        await self.drive.close()

    def sync_handler(self, request):
        self.assertEqual(request.method, 'GET')
        self.sync_calls.append(request)
        if request.url.host == 'drive.google.com':
            self.assertEqual(request.headers['Cache-Control'], 'no-cache')
            return httpx.Response(200, text='<div class="flip-entries"><a href="https://drive.google.com/file/d/fixture-video-123">video.mp4</a></div>')
        self.assertEqual(request.url.host, 'drive.usercontent.google.com')
        return httpx.Response(200, content=self.remote_body, headers={'content-type': 'video/mp4'})

    async def async_handler(self, request):
        self.assertEqual(request.method, 'GET')
        self.assertEqual(request.url.host, 'drive.usercontent.google.com')
        self.assertEqual(request.headers['Cache-Control'], 'no-cache')
        self.async_calls.append(request)
        stream = CountedBody(self, self.remote_body)
        self.streams.append(stream)
        return httpx.Response(200, stream=stream, headers={'content-type': 'video/mp4'})

    async def test_same_client_three_deliveries_and_retry_still_read_entire_file(self):
        for platform in ('tiktok', 'facebook', 'youtube', 'youtube'):
            result = await self.drive.verify_delivery({'platform': platform, 'payload': self.payload})
            self.assertEqual(result['sha256'], self.digest)
        self.assertEqual(len(self.async_clients), 1)
        self.assertEqual(len(self.sync_clients), 1)
        self.assertEqual(len(self.async_calls), 4)
        self.assertEqual(len(self.sync_calls), 4)
        self.assertEqual([stream.bytes_read for stream in self.streams], [len(self.data)] * 4)
        self.assertTrue(all(stream.closed for stream in self.streams))
        self.assertFalse(self.async_clients[0].is_closed)
        await self.drive.close()
        self.assertTrue(self.async_clients[0].is_closed)
        self.assertTrue(self.sync_clients[0].is_closed)

    async def test_pooled_connection_does_not_cache_or_accept_changed_tail(self):
        await self.drive.verify_delivery({'payload': self.payload})
        self.remote_body = self.data[:-1] + b'y'
        with self.assertRaisesRegex(ConfigurationError, 'Checksum'):
            await self.drive.verify_delivery({'payload': self.payload})
        self.remote_body = self.data
        await self.drive.verify_delivery({'payload': self.payload})
        self.assertEqual(len(self.async_clients), 1)
        self.assertEqual(len(self.async_calls), 3)
        self.assertEqual([stream.bytes_read for stream in self.streams], [len(self.data)] * 3)
        self.assertTrue(all(stream.closed for stream in self.streams))

    async def test_async_close_failure_still_closes_sync_pool(self):
        await self.drive.verify_delivery({'payload': self.payload})
        with patch.object(self.async_clients[0], 'aclose', new=AsyncMock(side_effect=RuntimeError('fixture async close failed'))):
            with self.assertRaises(RuntimeError):
                await self.drive.close()
        self.assertTrue(self.sync_clients[0].is_closed)

    async def test_six_deliveries_share_two_verification_slots(self):
        self.release_streams = asyncio.Event()
        tasks = [asyncio.create_task(self.drive.verify_delivery({'payload': self.payload})) for _ in range(6)]
        try:
            await asyncio.wait_for(self.two_started.wait(), 3)
            self.assertEqual(self.active, 2)
            self.assertEqual(len(self.async_calls), 2)
            self.release_streams.set()
            results = await asyncio.gather(*tasks)
            self.assertEqual(len(results), 6)
            self.assertEqual(self.maximum, 2)
            self.assertEqual(len(self.async_clients), 1)
            self.assertEqual(len(self.async_calls), 6)
            self.assertTrue(all(stream.bytes_read == len(self.data) and stream.closed for stream in self.streams))
        finally:
            self.release_streams.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_failed_verification_releases_slot_for_next_delivery(self):
        self.remote_body = self.data[:-1] + b'z'
        results = await asyncio.gather(*(self.drive.verify_delivery({'payload': self.payload}) for _ in range(3)), return_exceptions=True)
        self.assertTrue(all(isinstance(result, ConfigurationError) for result in results))
        self.remote_body = self.data
        result = await asyncio.wait_for(self.drive.verify_delivery({'payload': self.payload}), 3)
        self.assertEqual(result['sha256'], self.digest)
        self.assertEqual(len(self.async_clients), 1)

    async def test_cancellation_drains_local_folder_request_before_client_close(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = self.drive.resolve_file

        def slow_folder(relative):
            entered.set()
            try:
                if not release.wait(3):
                    raise TimeoutError('Fixture folder request was not released.')
                self.assertFalse(self.sync_clients[0].is_closed)
                return original(relative)
            finally:
                finished.set()

        with patch.object(self.drive, 'resolve_file', side_effect=slow_folder):
            task = asyncio.create_task(self.drive.verify_delivery({'payload': self.payload}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(finished.is_set())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(finished.is_set())
        self.assertEqual(len(self.sync_calls), 1)
        self.assertEqual(self.async_calls, [])
        result = await asyncio.wait_for(self.drive.verify_delivery({'payload': self.payload}), 3)
        self.assertEqual(result['sha256'], self.digest)

    async def test_bank_allocation_is_serial_but_remote_downloads_can_overlap(self):
        fixed = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        prefix = self.bank / '2030/01/01/a'
        prefix.mkdir(parents=True)
        for index in range(7):
            (prefix / f'existing-{index}.mp4').write_bytes(self.data)
        identities = ['a' + '1' * 31, 'a' + '2' * 31]
        store = FixtureStore({identity: {'id': identity, 'payload': {'render_path': str(self.file), 'render_sha256': self.digest}} for identity in identities})
        remote_overlap = threading.Barrier(2)
        start = threading.Barrier(2)
        local_active = local_maximum = 0
        counts_lock = threading.Lock()
        original_bank = self.drive.bank_file

        def local(*args):
            nonlocal local_active, local_maximum
            with counts_lock:
                local_active += 1
                local_maximum = max(local_maximum, local_active)
            try:
                return original_bank(*args)
            finally:
                with counts_lock:
                    local_active -= 1

        def remote(relative):
            remote_overlap.wait(timeout=3)
            return 'fixture-video-123', ['fixture-video-123']

        def run(identity):
            start.wait(timeout=3)
            return self.drive.synced_file({'id': identity}, store)

        with patch('fahra_automation.drive.now', return_value=fixed), \
             patch.object(self.drive, 'bank_file', side_effect=local), \
             patch.object(self.drive, 'resolve_file', side_effect=remote):
            results = await asyncio.gather(*(asyncio.to_thread(run, identity) for identity in identities))
        self.assertEqual(local_maximum, 1)
        self.assertEqual(len(list(prefix.glob('*.mp4'))), 8)
        self.assertEqual(len(list(prefix.rglob('*.mp4'))), 9)
        self.assertEqual(len({Path(result['bank_path']).parent for result in results}), 2)
        self.assertEqual(len(self.sync_clients), 1)
        self.assertEqual(len(self.sync_calls), 2)
        self.assertEqual(len(store.records), 2)
        self.assertFalse(list(self.root.glob('.fa-*.partial')))
        for identity, result in zip(identities, results):
            self.assertEqual(Path(result['bank_path']).read_bytes(), self.data)
            self.assertEqual(store.job(identity)['payload']['drive_relative_path'], result['drive_relative_path'])


class ShutdownResourcesTest(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_and_close_errors_still_close_every_owned_client(self):
        from fahra_automation.cli import run
        config, store = Mock(), Mock()
        config.runtime = Path('/fixture/runtime')
        repliz = SimpleNamespace(close=AsyncMock())
        telegram = SimpleNamespace(client=SimpleNamespace(aclose=AsyncMock()))
        drive = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('fixture drive close failed')))
        receiver = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('fixture receiver close failed')))
        with ExitStack() as stack:
            stack.enter_context(patch('fahra_automation.cli.process_lock', return_value=nullcontext()))
            stack.enter_context(patch('fahra_automation.cli.legacy_locks', return_value=nullcontext()))
            for name in ('Media','Pipeline','Downloader','Analyzer','Cleaner','ReplyWorker'):
                stack.enter_context(patch('fahra_automation.cli.' + name))
            for name, value in (('Repliz', repliz), ('Telegram', telegram), ('Drive', drive)):
                stack.enter_context(patch('fahra_automation.cli.' + name, return_value=value))
            stack.enter_context(patch('fahra_automation.cli.start_webhook', new=AsyncMock(return_value=(receiver, object()))))
            stack.enter_context(patch('fahra_automation.account_reports.AccountReports', side_effect=RuntimeError('fixture startup failure')))
            with self.assertRaises(RuntimeError):
                await run(config, store)
        receiver.close.assert_awaited_once()
        drive.close.assert_awaited_once()
        store.record.assert_called_once_with('process_stopped')
        telegram.client.aclose.assert_awaited_once()
        repliz.close.assert_awaited_once()
