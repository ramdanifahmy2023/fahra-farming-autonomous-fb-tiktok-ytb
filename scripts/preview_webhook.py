"""Exercise the real loopback HTTP receiver with fixture DB and mock provider GET only."""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import secrets
import socket
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from fahra_automation.db import Store
from fahra_automation.repliz import Repliz, SharedRateLimiter
from fahra_automation.util import now
from fahra_automation.webhook import WebhookReceiver, WebhookSettings, WebhookWorker
from fahra_automation.webhook_store import WebhookInbox


async def preview():
    with tempfile.TemporaryDirectory(prefix='fahra-webhook-preview-') as temporary:
        store = Store(Path(temporary) / 'fixture.sqlite3')
        account = {'id': 'fixture-account', 'type': 'tiktok', 'username': 'fixture',
                   'name': 'Fixture', 'isConnected': True, 'publishEligible': True}
        day = now().date()
        store.apply_snapshot([account], [], day.isoformat(), (day + timedelta(days=1)).isoformat())
        store.enqueue(1, 2, 3, [('https://instagram.com/reel/fixture/', 'fixture')])
        job = next(row for row in store.queue() if row['platform'] == 'tiktok')
        store.checkpoint(job['id'], 'scheduled', schedule_id='fixture-schedule')
        inbox = WebhookInbox(store)
        token = secrets.token_urlsafe(32)
        calls = []
        def provider(request):
            calls.append({'method': request.method, 'path': request.url.path})
            assert request.method == 'GET'
            return httpx.Response(200, json={'id': 'fixture-schedule', 'accountId': 'fixture-account', 'status': 'pending'})
        config = SimpleNamespace(section=lambda section: {'base_url': 'https://provider.invalid'})
        async with httpx.AsyncClient(base_url='https://provider.invalid', transport=httpx.MockTransport(provider)) as client:
            api = Repliz(config, client=client, limiter=SharedRateLimiter(0))
            async def reconcile(job_id):
                current = await asyncio.to_thread(store.job, job_id)
                result = await api.status(current['schedule_id'])
                assert result['accountId'] == current['account_id'] and result['id'] == current['schedule_id']
                assert result['status'] == 'pending'
            worker = WebhookWorker(inbox, on_schedule=reconcile)
            receiver = WebhookReceiver(inbox, token, WebhookSettings(enabled=True, port=0), on_accepted=worker.wake.set)
            await receiver.start()
            port = receiver.server.sockets[0].getsockname()[1]
            async def request(payload, secret):
                body = json.dumps(payload).encode()
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.write(f'POST /repliz/webhook HTTP/1.1\r\nHost: fixture.invalid\r\nX-Token: {secret}\r\nX-Forwarded-Proto: https\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
                await writer.drain()
                response = await reader.read()
                writer.close()
                await writer.wait_closed()
                assert token.encode() not in response
                return int(response.split(b' ')[1])
            try:
                payload = {'platform': 'repliz', 'type': 'schedule', 'accountId': account['id'],
                           'data': {'id': 'fixture-schedule', 'status': 'success'}}
                statuses = [await request(payload, 'invalid'), await request(payload, token), await request(payload, token)]
                before = await asyncio.to_thread(inbox.summary)
                assert calls == [] and before == {'pending': 1}
                await worker.process_once()
                assert store.job(job['id'])['status'] == 'scheduled'
                assert statuses == [401, 202, 202]
                assert calls == [{'method': 'GET', 'path': '/public/schedule/fixture-schedule'}]
                return {'result': 'PASS', 'fixture': True, 'external_network_requests': 0,
                        'local_HTTP_requests': 3, 'HTTP_statuses': statuses, 'before_consumer': before,
                        'after_consumer': inbox.summary(), 'provider_fixture_requests': calls,
                        'claimed_status': 'success', 'verified_provider_status': 'pending',
                        'local_job_status': 'scheduled', 'real_publication_or_reply': False,
                        'production_access': False, 'credentials_read': False}
            finally:
                await receiver.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Output sudah ada; gunakan nama baru.')
    original_connect = socket.socket.connect
    def local_only(sock, address):
        if not isinstance(address, tuple) or not ipaddress.ip_address(address[0]).is_loopback:
            raise AssertionError('Preview hanya mengizinkan socket loopback.')
        return original_connect(sock, address)
    with patch('socket.socket.connect', local_only), patch('socket.socket.connect_ex', side_effect=AssertionError('No connect_ex')):
        report = asyncio.run(preview())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'result': report['result'], 'output': str(args.output)}))


if __name__ == '__main__':
    main()
