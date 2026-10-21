"""Loopback HTTP ingress behind an HTTPS reverse proxy, authenticated by Repliz x-token.

Contract: https://docs.repliz.com/tutorial/webhook (checked 2026-10-21).
Callbacks reconcile through the existing authenticated GET paths; hints grant no send permission.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import re
from dataclasses import dataclass
from http import HTTPStatus

from .config import ConfigurationError
from .webhook_store import InboxFull, WebhookEvent


@dataclass(frozen=True)
class WebhookSettings:
    enabled: bool = False
    host: str = '127.0.0.1'
    port: int = 8769
    path: str = '/repliz/webhook'
    token_file: str = 'Credential/repliz-webhook-token.txt'
    max_body_bytes: int = 65536
    request_timeout_seconds: float = 5
    max_connections: int = 8

    def __post_init__(self):
        try:
            loopback = isinstance(self.host, str) and ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ConfigurationError('Webhook hanya boleh bind ke alamat loopback numerik.')
        if type(self.enabled) is not bool or type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ConfigurationError('Status/port webhook tidak valid.')
        if not isinstance(self.path, str) or not re.fullmatch(r'/[A-Za-z0-9/_-]{1,120}', self.path):
            raise ConfigurationError('Path webhook tidak valid.')
        if not isinstance(self.token_file, str) or not self.token_file:
            raise ConfigurationError('Path file token webhook tidak valid.')
        if type(self.max_body_bytes) is not int or not 256 <= self.max_body_bytes <= 1048576:
            raise ConfigurationError('Batas body webhook harus 256 sampai 1048576 byte.')
        if type(self.request_timeout_seconds) not in {int, float} or not .05 <= self.request_timeout_seconds <= 30:
            raise ConfigurationError('Batas waktu webhook tidak valid.')
        if type(self.max_connections) is not int or not 1 <= self.max_connections <= 64:
            raise ConfigurationError('Batas koneksi webhook tidak valid.')

    @classmethod
    def from_config(cls, config):
        return cls(**config.data.get('webhook', {}))


def load_webhook_token(config, settings):
    path = config.path(settings.token_file)
    credential_root = config.root / 'Credential'
    if (not path.resolve().is_relative_to(credential_root.resolve()) or credential_root.is_symlink()
            or any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(credential_root))):
        raise ConfigurationError('Token webhook harus berasal dari Credential proyek ini.')
    try:
        token = path.read_text().strip()
    except (OSError, UnicodeError):
        raise ConfigurationError('File token webhook belum tersedia.') from None
    validate_token(token)
    return token


def validate_token(token):
    if not isinstance(token, str) or not re.fullmatch(r'[\x21-\x7e]{32,512}', token):
        raise ConfigurationError('Token webhook memerlukan 32–512 karakter ASCII tanpa spasi.')


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', value):
        raise ValueError('Identitas event webhook tidak valid.')
    return value


def parse_event(payload):
    if not isinstance(payload, dict) or not all(key in payload for key in ('platform','type','accountId','data')):
        raise ValueError('Envelope webhook tidak lengkap.')
    kind, platform, account, data = (payload[k] for k in ('type','platform','accountId','data'))
    if not isinstance(kind, str) or not isinstance(platform, str) or not isinstance(data, dict):
        raise ValueError('Jenis field webhook tidak valid.')
    account = _identity(account)
    if kind == 'testing':
        if platform != 'testing' or account != 'testing' or data:
            raise ValueError('Event testing tidak sesuai kontrak Repliz.')
        return WebhookEvent('testing', 'testing', 'testing')
    if kind not in {'comment','schedule'}:
        return None
    if data.get('accountId', account) != account:
        raise ValueError('Akun envelope dan data tidak cocok.')
    if kind == 'schedule':
        if platform != 'repliz' or not isinstance(data.get('status'), str) or data['status'] not in {'success','error'}:
            raise ValueError('Event schedule tidak sesuai kontrak Repliz.')
        return WebhookEvent(kind, platform, account, _identity(data.get('id')), claimed_status=data['status'])
    if platform not in {'facebook','tiktok'}:
        return None  # Repliz does not deliver YouTube comment webhooks; polling remains enabled.
    if not isinstance(data.get('content'), dict) or not isinstance(data.get('comment'), dict):
        raise ValueError('Identitas posting/komentar belum tersedia.')
    remote = _identity(data.get('_id', data.get('id')))
    if data.get('id', remote) != remote:
        raise ValueError('Identitas komentar bertentangan.')
    _identity(data['comment'].get('id'))
    return WebhookEvent(kind, platform, account, remote, _identity(data['content'].get('id')))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Field JSON ganda.')
        result[key] = value
    return result


class HTTPFailure(Exception):
    def __init__(self, status):
        self.status = status


class WebhookReceiver:
    def __init__(self, inbox, token, settings=None, *, on_accepted=None):
        self.inbox, self.settings = inbox, settings or WebhookSettings()
        validate_token(token)
        self._token = token
        self.on_accepted = on_accepted
        self.server = None
        self._active = 0
        self._clients = set()

    async def start(self):
        if not self.settings.enabled:
            return None
        if self.server is not None:
            raise RuntimeError('Receiver webhook sudah berjalan.')
        self.server = await asyncio.start_server(self._handle, self.settings.host, self.settings.port, limit=8192)
        return self.server

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        clients = tuple(self._clients)
        for task in clients:
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)

    async def _receive(self, reader):
        block = await reader.readuntil(b'\r\n\r\n')
        if len(block) > 8192:
            raise HTTPFailure(431)
        try:
            lines = block.decode('ascii').split('\r\n')
            method, target, version = lines[0].split(' ')
        except (UnicodeError, ValueError):
            raise HTTPFailure(400) from None
        if method != 'POST':
            raise HTTPFailure(405)
        if target != self.settings.path:
            raise HTTPFailure(404)
        if version != 'HTTP/1.1' or len(lines) > 68:
            raise HTTPFailure(400)
        headers = {}
        for line in lines[1:-2]:
            if ':' not in line:
                raise HTTPFailure(400)
            key, value = line.split(':', 1)
            key, value = key.lower(), value.strip()
            if not re.fullmatch(r'[a-z0-9-]+', key) or key in headers or not re.fullmatch(r'[\x20-\x7e]*', value):
                raise HTTPFailure(400)
            headers[key] = value
        if not hmac.compare_digest(headers.get('x-token', ''), self._token):
            raise HTTPFailure(401)
        if headers.get('x-forwarded-proto') != 'https':
            raise HTTPFailure(403)
        if 'transfer-encoding' in headers or 'content-encoding' in headers or not headers.get('host'):
            raise HTTPFailure(400)
        if headers.get('content-type', '').lower().split(';')[0].strip() != 'application/json':
            raise HTTPFailure(415)
        size = headers.get('content-length', '')
        if not re.fullmatch(r'[0-9]{1,9}', size):
            raise HTTPFailure(411)
        size = int(size)
        if size > self.settings.max_body_bytes:
            raise HTTPFailure(413)
        body = await reader.readexactly(size)
        try:
            def invalid_constant(value):
                raise ValueError('JSON non-finite.')
            event = parse_event(json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=invalid_constant))
        except (ValueError, UnicodeError, RecursionError, TypeError):
            raise HTTPFailure(400) from None
        if event is None:
            return 202, {'accepted': True, 'queued': False, 'ignored': True}
        result = await asyncio.to_thread(self.inbox.accept, event)
        if result['queued'] and self.on_accepted:
            self.on_accepted()
        return (200 if event.kind == 'testing' else 202), result

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._clients.add(task)
        admitted = self._active < self.settings.max_connections
        if admitted:
            self._active += 1
        try:
            try:
                peer = writer.get_extra_info('peername')
                if not peer or not ipaddress.ip_address(peer[0]).is_loopback:
                    raise HTTPFailure(403)
                if not admitted:
                    raise HTTPFailure(503)
                async with asyncio.timeout(self.settings.request_timeout_seconds):
                    status, response = await self._receive(reader)
            except HTTPFailure as exc:
                status, response = exc.status, {'accepted': False}
            except asyncio.LimitOverrunError:
                status, response = 431, {'accepted': False}
            except (TimeoutError, asyncio.IncompleteReadError):
                status, response = 408, {'accepted': False}
            except Exception:
                # Never include token, request body, database path, or provider text in HTTP responses.
                status, response = 503, {'accepted': False}
            data = json.dumps(response, separators=(',', ':')).encode()
            writer.write(f'HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n'.encode() + data)
            with contextlib.suppress(ConnectionError, TimeoutError):
                await asyncio.wait_for(writer.drain(), 1)
        finally:
            if admitted:
                self._active -= 1
            writer.close()
            with contextlib.suppress(ConnectionError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)
            self._clients.discard(task)


class WebhookWorker:
    def __init__(self, inbox, *, on_schedule, on_comment=None, callback_timeout_seconds=60):
        self.inbox = inbox
        self.on_schedule, self.on_comment = on_schedule, on_comment
        self.callback_timeout_seconds = callback_timeout_seconds
        self.wake = asyncio.Event()

    async def process_once(self, *, limit=20):
        processed = 0
        for _ in range(limit):
            hint = await asyncio.to_thread(self.inbox.claim, lease_seconds=max(120, self.callback_timeout_seconds + 30))
            if not hint:
                break
            try:
                target = await asyncio.to_thread(self.inbox.target, hint)
                ignored = not target or (hint['kind'] == 'schedule' and target['status'] == 'published')
                if not ignored:
                    async with asyncio.timeout(self.callback_timeout_seconds):
                        if hint['kind'] == 'schedule':
                            await self.on_schedule(target['id'])
                        elif self.on_comment:
                            await self.on_comment(hint['account_id'], hint['remote_id'])
                        else:
                            ignored = True
                await asyncio.to_thread(self.inbox.finish, hint, ignored=ignored)
            except asyncio.CancelledError:
                raise  # The durable lease expires after a crash/cancellation; no acknowledgement is lost.
            except Exception as exc:
                await asyncio.to_thread(self.inbox.retry, hint, exc)
            processed += 1
        return processed

    async def run(self):
        while True:
            self.wake.clear()
            if await self.process_once():
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), 5)
