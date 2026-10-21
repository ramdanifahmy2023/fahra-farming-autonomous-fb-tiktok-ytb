import asyncio
import json
import secrets
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from fahra_automation.config import ConfigurationError, EXCLUDED_ACCOUNT_ID
from fahra_automation.db import Store
from fahra_automation.repliz import Repliz, SharedRateLimiter
from fahra_automation.util import now
from fahra_automation.webhook import (WebhookReceiver, WebhookWorker, WebhookSettings,
                                     load_webhook_token, parse_event)
from fahra_automation.webhook_store import WebhookInbox, InboxFull


def schedule(status='success', account='account-fixture', remote='schedule-fixture'):
    return {'platform': 'repliz', 'type': 'schedule', 'accountId': account,
            'data': {'id': remote, 'status': status}}


def comment(post='post-fixture', platform='tiktok'):
    return {'platform': platform, 'type': 'comment', 'accountId': 'account-fixture',
            'data': {'_id': 'inbox-fixture', 'content': {'id': post},
                     'comment': {'id': 'comment-fixture', 'text': 'Text must not enter webhook storage.'}}}


class Fixture:
    def set_up_fixture(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'workflow.sqlite3')
        account = {'id': 'account-fixture', 'type': 'tiktok', 'username': 'fixture',
                   'name': 'Fixture', 'isConnected': True, 'publishEligible': True}
        day = now().date()
        self.store.apply_snapshot([account], [], day.isoformat(), (day + timedelta(days=1)).isoformat())
        self.store.enqueue(1, 2, 3, [('https://instagram.com/reel/fixture/', 'fixture')])
        self.job = next(job for job in self.store.queue() if job['platform'] == 'tiktok')
        self.store.checkpoint(self.job['id'], 'scheduled', schedule_id='schedule-fixture')
        self.clock = 1000000.0
        self.inbox = WebhookInbox(self.store, clock=lambda: self.clock)


class WebhookStoreTest(Fixture, unittest.TestCase):
    def setUp(self):
        self.set_up_fixture()

    def test_claimed_success_never_updates_job_and_dedup_survives_restart(self):
        self.assertTrue(self.inbox.accept(parse_event(schedule()))['queued'])
        reopened = WebhookInbox(self.store)
        self.assertTrue(reopened.accept(parse_event(schedule()))['duplicate'])
        self.assertEqual(self.store.job(self.job['id'])['status'], 'scheduled')
        self.assertEqual(reopened.summary(), {'pending': 1})

    def test_error_and_later_success_are_separate_hints(self):
        self.inbox.accept(parse_event(schedule('error')))
        self.inbox.accept(parse_event(schedule('success')))
        self.assertEqual(self.inbox.summary(), {'pending': 2})

    def test_concurrent_deliveries_have_one_durable_event(self):
        event = parse_event(schedule())
        with ThreadPoolExecutor(max_workers=4) as threads:
            results = list(threads.map(lambda _: self.inbox.accept(event), range(8)))
        self.assertEqual(sum(row['queued'] for row in results), 1)
        self.assertEqual(self.inbox.summary(), {'pending': 1})

    def test_unknown_or_excluded_account_schedule_never_queued(self):
        for payload in (schedule(account=EXCLUDED_ACCOUNT_ID), schedule(account='unknown'), schedule(remote='unmanaged')):
            self.assertTrue(self.inbox.accept(parse_event(payload))['ignored'])
        self.assertEqual(self.inbox.summary(), {})

    def test_managed_published_post_only_for_comments_and_no_text_stored(self):
        self.assertTrue(self.inbox.accept(parse_event(comment()))['ignored'])
        self.store.checkpoint(self.job['id'], 'published', payload={'post_id': 'post-fixture'})
        self.assertTrue(self.inbox.accept(parse_event(comment('not-managed')))['ignored'])
        self.assertTrue(self.inbox.accept(parse_event(comment(platform='facebook')))['ignored'])
        self.assertTrue(self.inbox.accept(parse_event(comment()))['queued'])
        with self.store.connect() as connection:
            row = dict(connection.execute('SELECT * FROM webhook_inbox').fetchone())
        self.assertNotIn('Text must not enter', json.dumps(row))
        self.assertNotIn('raw', row)

    def test_lease_recovery_and_stale_completion_cannot_overwrite_new_owner(self):
        self.inbox.accept(parse_event(schedule()))
        first = self.inbox.claim(lease_seconds=10)
        self.assertIsNone(self.inbox.claim())
        self.clock += 11
        second = WebhookInbox(self.store, clock=lambda: self.clock).claim()
        self.assertEqual(second['attempts'], 2)
        self.assertFalse(self.inbox.finish(first))
        self.assertTrue(self.inbox.finish(second))
        self.assertEqual(self.inbox.summary(), {'done': 1})

    def test_expired_retries_backoff_and_final_hold_preserve_polling(self):
        self.inbox.accept(parse_event(schedule()))
        for attempt in range(12):
            hint = self.inbox.claim()
            self.assertIsNotNone(hint)
            self.inbox.retry(hint, RuntimeError('Never persist request token or arbitrary response.'))
            self.assertIsNone(self.inbox.claim())
            self.clock += 4000
        self.assertEqual(self.inbox.summary(), {'held': 1})
        with self.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT last_error FROM webhook_inbox').fetchone()[0], 'RuntimeError')

    def test_capacity_rejection_and_testing_event_durable(self):
        limited = WebhookInbox(self.store, max_pending=1, max_records=10)
        limited.accept(parse_event(schedule()))
        with self.assertRaises(InboxFull):
            limited.accept(parse_event(schedule('error')))
        hint = limited.claim()
        limited.finish(hint)
        result = limited.accept(parse_event({'platform': 'testing', 'type': 'testing', 'accountId': 'testing', 'data': {}}))
        self.assertFalse(result['queued'])
        self.assertEqual(limited.summary(), {'done': 2})


class MemoryWriter:
    def __init__(self):
        self.data = b''
        self.closed = False
    def get_extra_info(self, key):
        return ('127.0.0.1', 12345)
    def write(self, data):
        self.data += data
    async def drain(self):
        pass
    def close(self):
        self.closed = True
    async def wait_closed(self):
        pass


class WebhookHTTPTest(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.set_up_fixture()
        self.token = secrets.token_urlsafe(32)
        self.receiver = WebhookReceiver(self.inbox, self.token, WebhookSettings(enabled=True, request_timeout_seconds=.1))

    def request(self, body=None, *, headers=None, method='POST', path='/repliz/webhook'):
        body = json.dumps(schedule()).encode() if body is None else body
        fields = {'Host': 'fixture.invalid', 'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                  'X-Token': self.token, 'X-Forwarded-Proto': 'https'}
        fields.update(headers or {})
        lines = [f'{key}: {value}' for key, value in fields.items() if value is not None]
        return (f'{method} {path} HTTP/1.1\r\n' + '\r\n'.join(lines) + '\r\n\r\n').encode() + body

    async def send(self, request, *, eof=True):
        reader, writer = asyncio.StreamReader(limit=8192), MemoryWriter()
        reader.feed_data(request)
        if eof:
            reader.feed_eof()
        await self.receiver._handle(reader, writer)
        self.assertTrue(writer.closed)
        self.assertNotIn(self.token.encode(), writer.data)
        return int(writer.data.split(b' ')[1]), writer.data

    async def test_authenticated_forwarded_request_acknowledged_after_commit(self):
        status, _ = await self.send(self.request())
        self.assertEqual(status, 202)
        self.assertEqual(self.inbox.summary(), {'pending': 1})
        self.assertEqual((await self.send(self.request()))[0], 202)
        self.assertEqual(self.inbox.summary(), {'pending': 1})

    async def test_bad_auth_protocol_and_framing_are_rejected(self):
        cases = [({'X-Token': None}, 401), ({'X-Token': 'incorrect'}, 401),
                 ({'X-Forwarded-Proto': None}, 403), ({'X-Forwarded-Proto': 'http'}, 403),
                 ({'Content-Type': 'text/plain'}, 415), ({'Content-Length': None}, 411),
                 ({'Content-Length': '65537'}, 413), ({'Content-Length': '-1'}, 411),
                 ({'Transfer-Encoding': 'chunked'}, 400), ({'Content-Encoding': 'gzip'}, 400),
                 ({'Host': None}, 400)]
        for headers, expected in cases:
            with self.subTest(headers=list(headers)):
                self.assertEqual((await self.send(self.request(headers=headers)))[0], expected)
        self.assertEqual((await self.send(self.request(method='GET')))[0], 405)
        self.assertEqual((await self.send(self.request(path='/other')))[0], 404)
        self.assertEqual(self.inbox.summary(), {})

    async def test_ambiguous_json_headers_and_malformed_payload_rejected(self):
        cases = [b'{}', b'[]', b'\xff', b'{"platform":NaN}',
                 b'{"platform":"repliz","platform":"testing"}',
                 json.dumps({**schedule(), 'data': {'id': '../escape', 'status': 'success'}}).encode(),
                 json.dumps({**schedule(), 'data': {'id': 'a', 'status': []}}).encode()]
        for body in cases:
            self.assertEqual((await self.send(self.request(body)))[0], 400)
        duplicated = self.request().replace(b'Content-Type:', b'Content-Length: 1\r\nContent-Type:')
        self.assertEqual((await self.send(duplicated))[0], 400)
        self.assertEqual((await self.send(b'P' * 9000 + b'\r\n\r\n'))[0], 431)

    async def test_slow_body_times_out_and_over_capacity_refused(self):
        self.assertEqual((await self.send(self.request(b'', headers={'Content-Length': '20'}), eof=False))[0], 408)
        self.receiver._active = self.receiver.settings.max_connections
        self.assertEqual((await self.send(self.request()))[0], 503)

    async def test_storage_failure_never_gets_success_and_test_event_does(self):
        with patch.object(self.inbox, 'accept', side_effect=OSError('private path and token')):
            status, body = await self.send(self.request())
        self.assertEqual(status, 503)
        self.assertNotIn(b'private', body)
        testing = {'platform': 'testing', 'type': 'testing', 'accountId': 'testing', 'data': {}}
        self.assertEqual((await self.send(self.request(json.dumps(testing).encode())))[0], 200)
        self.assertEqual(self.inbox.summary(), {'done': 1})

    async def test_disabled_server_does_not_bind(self):
        receiver = WebhookReceiver(self.inbox, self.token)
        with patch('asyncio.start_server', new_callable=AsyncMock) as start:
            self.assertIsNone(await receiver.start())
            start.assert_not_awaited()

    async def test_worker_only_targeted_get_and_ignores_claimed_success(self):
        self.inbox.accept(parse_event(schedule()))
        requests = []
        def handler(request):
            requests.append((request.method, request.url.path))
            return httpx.Response(200, json={'id': 'schedule-fixture', 'accountId': 'account-fixture', 'status': 'pending'})
        config = SimpleNamespace(section=lambda name: {'base_url': 'https://fixture.invalid'})
        async with httpx.AsyncClient(base_url='https://fixture.invalid', transport=httpx.MockTransport(handler)) as client:
            api = Repliz(config, client=client, limiter=SharedRateLimiter(0))
            async def reconcile(job_id):
                job = self.store.job(job_id)
                result = await api.status(job['schedule_id'])
                self.assertEqual(result['status'], 'pending')
            worker = WebhookWorker(self.inbox, on_schedule=reconcile)
            self.assertEqual(await worker.process_once(), 1)
        self.assertEqual(requests, [('GET', '/public/schedule/schedule-fixture')])
        self.assertEqual(self.store.job(self.job['id'])['status'], 'scheduled')
        self.assertEqual(self.inbox.summary(), {'done': 1})

    async def test_changed_mapping_is_ignored_before_callback(self):
        self.inbox.accept(parse_event(schedule()))
        self.store.checkpoint(self.job['id'], schedule_id='changed')
        callback = AsyncMock()
        await WebhookWorker(self.inbox, on_schedule=callback).process_once()
        callback.assert_not_awaited()
        self.assertEqual(self.inbox.summary(), {'ignored': 1})

    async def test_callback_failure_is_retried_without_new_send(self):
        self.inbox.accept(parse_event(schedule()))
        callback = AsyncMock(side_effect=TimeoutError())
        worker = WebhookWorker(self.inbox, on_schedule=callback)
        await worker.process_once()
        self.assertEqual(self.inbox.summary(), {'pending': 1})
        self.assertEqual(await worker.process_once(), 0)
        self.clock += 31
        await worker.process_once()
        self.assertEqual(callback.await_count, 2)

    async def test_comment_wakes_poll_without_ingesting_untrusted_body(self):
        self.store.checkpoint(self.job['id'], 'published', payload={'post_id': 'post-fixture'})
        self.inbox.accept(parse_event(comment()))
        callback = AsyncMock()
        await WebhookWorker(self.inbox, on_schedule=AsyncMock(), on_comment=callback).process_once()
        callback.assert_awaited_once_with('account-fixture', 'inbox-fixture')
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master WHERE name='comment_events'").fetchone()[0], 0)


class WebhookConfigTest(unittest.TestCase):
    def test_untrusted_bind_and_bad_settings_rejected(self):
        for values in ({'host': '0.0.0.0'}, {'host': 'localhost'}, {'path': '/path?query'},
                       {'max_body_bytes': True}, {'request_timeout_seconds': float('nan')}, {'enabled': 1}):
            with self.assertRaises(ConfigurationError):
                WebhookSettings(**values)

    def test_token_file_must_be_private_project_credential_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'Credential').mkdir()
            token = secrets.token_urlsafe(32)
            path = root / 'Credential/repliz-webhook-token.txt'
            path.write_text(token)
            config = SimpleNamespace(root=root, path=lambda value: root / value)
            self.assertEqual(load_webhook_token(config, WebhookSettings()), token)
            outside = root / 'outside.txt'; outside.write_text(token)
            with self.assertRaises(ConfigurationError):
                load_webhook_token(config, WebhookSettings(token_file='outside.txt'))
            path.write_text('short')
            with self.assertRaises(ConfigurationError):
                load_webhook_token(config, WebhookSettings())
