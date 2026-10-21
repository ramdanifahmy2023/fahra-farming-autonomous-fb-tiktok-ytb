from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import math
import re
import time
import weakref
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx

from .config import ConfigurationError, EXCLUDED_ACCOUNT_ID, PLATFORMS, allowed_account
from .db import ZONE
from .util import iso, now, safe_error
from .telemetry import record_timing, flush as flush_timings

SCHEDULE_STATUSES = {'pending', 'process', 'success', 'error'}
SCHEDULE_TYPES = {'video', 'reel', 'image', 'carousel', 'text', 'story', 'link'}
API_CLASSES = ('publish', 'sync', 'reply', 'monitor', 'background')
_api_class = ContextVar('repliz_api_class', default='background')


@contextmanager
def api_scope(priority):
    if priority not in API_CLASSES:
        raise ValueError('Kelas prioritas API tidak dikenal.')
    token = _api_class.set(priority)
    try:
        yield
    finally:
        _api_class.reset(token)


def account_id(value):
    account = value.get('accountId') or value.get('account')
    return account.get('id', account.get('_id')) if isinstance(account, dict) else account


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Waktu jadwal tidak valid.')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('Waktu jadwal harus memuat zona waktu.')
    return stamp


def publication_details(schedule, name, platform):
    if schedule.get('status') != 'success' or platform not in PLATFORMS:
        return {}
    post_id = str(schedule.get('postId') or '')
    url = schedule.get('postUrl') or ''
    hosts = {'youtube': {'youtube.com', 'www.youtube.com', 'youtu.be'},
             'facebook': {'facebook.com', 'www.facebook.com', 'fb.watch'},
             'tiktok': {'tiktok.com', 'www.tiktok.com'}}
    parsed = urlsplit(url)
    if not (parsed.scheme == 'https' and parsed.hostname in hosts[platform] and not parsed.username and not parsed.password):
        url = ''
    if not url:
        if platform == 'youtube' and re.fullmatch(r'[A-Za-z0-9_-]{11}', post_id):
            url = 'https://www.youtube.com/watch?v=' + post_id
        elif platform == 'facebook' and re.fullmatch(r'\d{1,30}', post_id):
            url = 'https://www.facebook.com/reel/' + post_id + '/'
        elif platform == 'facebook' and re.fullmatch(r'\d{1,30}_\d{1,30}', post_id):
            page, post = post_id.split('_')
            url = f'https://www.facebook.com/{page}/posts/{post}'
        elif platform == 'tiktok' and re.fullmatch(r'\d{10,30}', post_id) and re.fullmatch(r'[A-Za-z0-9_.]{1,40}', name.lstrip('@')):
            url = f'https://www.tiktok.com/@{name.lstrip("@")}/video/{post_id}'
    return {'post_id': post_id, 'published_username': name, 'post_url': url} if url and post_id else {}


class ReplizError(RuntimeError):
    def __init__(self, status, message, retry_after=0):
        self.status, self.retry_after = status, retry_after
        super().__init__(f'Repliz HTTP {status}: {safe_error(message)}')


class AccountUnavailable(ConfigurationError):
    pass


class MutationNotSent(ConfigurationError):
    pass


class SharedRateLimiter:
    """All Repliz clients on the same event loop share a workspace request budget."""
    _loops = weakref.WeakKeyDictionary()

    def __init__(self, interval=.65):
        self.interval = interval
        self.lock = asyncio.Lock()
        self.next_at = 0.0
        self.queues = {name: deque() for name in API_CLASSES}
        self.cycle = ('publish', 'sync', 'publish', 'sync', 'reply', 'monitor', 'publish', 'sync', 'reply', 'background')
        self.cursor = 0
        self.active = 0
        self.mutation_active = False

    def dispatch(self):
        while self.active < 2:
            selected = None
            for _ in self.cycle:
                name = self.cycle[self.cursor]
                self.cursor = (self.cursor + 1) % len(self.cycle)
                queue = self.queues[name]
                selected = next((item for item in queue if not item['future'].done()
                                 and not (item['mutation'] and self.mutation_active)), None)
                if selected is not None:
                    queue.remove(selected)
                    break
            if selected is None:
                return
            selected['granted'] = True
            self.active += 1
            if selected['mutation']:
                self.mutation_active = True
            selected['future'].set_result(None)

    @asynccontextmanager
    async def acquire(self, priority, mutation=False):
        item = {'future': asyncio.get_running_loop().create_future(), 'mutation': mutation, 'granted': False}
        self.queues[priority].append(item)
        self.dispatch()
        try:
            await item['future']
            yield
        finally:
            if item['granted']:
                self.active -= 1
                if mutation:
                    self.mutation_active = False
            else:
                self.queues[priority].remove(item)
            self.dispatch()

    @classmethod
    def shared(cls, key, interval=.65):
        pools = cls._loops.setdefault(asyncio.get_running_loop(), {})
        if key not in pools:
            pools[key] = cls(interval)
        return pools[key]

    async def wait(self):
        while self.next_at > time.monotonic():
            await asyncio.sleep(self.next_at - time.monotonic())
        self.next_at = time.monotonic() + self.interval

    def defer(self, seconds):
        self.next_at = max(self.next_at, time.monotonic() + seconds)


class Repliz:
    def __init__(self, config, client=None, limiter=None):
        self.config = config
        self.limiter = limiter
        self.observer = None
        self.client = client or httpx.AsyncClient(
            base_url=config.section('repliz')['base_url'], auth=config.repliz_credentials(), timeout=45)

    async def request(self, method, path, *, before_send=None, **kwargs):
        method = method.upper()
        mutation = method not in {'GET', 'HEAD', 'OPTIONS'}
        if mutation:
            try:
                self.config.require_live_action()
            except ConfigurationError as exc:
                raise MutationNotSent(str(exc)) from None
        if self.limiter is None:
            cfg = self.config.section('repliz')
            self.limiter = SharedRateLimiter.shared(cfg['base_url'], cfg.get('min_request_interval_seconds', .65))
        waiting = time.monotonic()
        scope = _api_class.get()
        async with self.limiter.acquire(scope, mutation):
            async with self.limiter.lock:
                await self.limiter.wait()
            record_timing(self.observer, 'repliz.limiter', (time.monotonic()-waiting)*1000)
            record_timing(self.observer, 'repliz.' + scope + '.wait', (time.monotonic()-waiting)*1000)
            if mutation:
                try:
                    self.config.require_live_action()
                except ConfigurationError as exc:
                    raise MutationNotSent(str(exc)) from None
            if before_send is not None:
                before_send()
            started = time.monotonic()
            try:
                response = await self.client.request(method, path, **kwargs)
            finally:
                elapsed = (time.monotonic()-started)*1000
                record_timing(self.observer, 'repliz.' + method, elapsed)
                record_timing(self.observer, 'repliz.' + scope + '.' + method, elapsed)
            if response.status_code == 429:
                try:
                    value = float(response.headers.get('retry-after', '60'))
                    retry_after = max(1, min(86400, value)) if math.isfinite(value) else 60
                except ValueError:
                    retry_after = 60
                self.limiter.defer(retry_after)
            else:
                retry_after = 0
        if not response.is_success:
            try:
                data = response.json()
                message = data.get('message', 'Request rejected') if isinstance(data, dict) else 'Request rejected'
            except ValueError:
                message = 'Response is not JSON'
            raise ReplizError(response.status_code, str(message), retry_after)
        if response.status_code == 204:
            return {}
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError('Respons Repliz bukan object JSON.')
        return result

    async def pages(self, path, params=None):
        rows, seen = [], set()
        expected_total = None
        expected_pages = None
        for page in range(1, 201):
            query = [('page', str(page)), ('limit', '100')]
            for key, value in (params or {}).items():
                if isinstance(value, list):
                    query.extend((f'{key}[{i}]', str(v)) for i, v in enumerate(value))
                else:
                    query.append((key, str(value)))
            result = await self.request('GET', path, params=query)
            data = result.get('data', result)
            if (not isinstance(data, dict) or not isinstance(data.get('docs'), list)
                    or type(data.get('hasNextPage')) is not bool or type(data.get('page')) is not int
                    or data['page'] != page or type(data.get('totalDocs')) is not int or data['totalDocs'] < 0):
                raise ValueError('Pagination Repliz tidak lengkap; snapshot tidak diterapkan.')
            total = data['totalDocs']
            if expected_total is not None and expected_total != total:
                raise ValueError('Jumlah record berubah selama pagination.')
            expected_total = total
            if 'totalPages' in data:
                pages = data['totalPages']
                if type(pages) is not int or pages < (1 if total else 0) or pages > 200:
                    raise ValueError('Jumlah halaman Repliz tidak valid.')
                if expected_pages is not None and expected_pages != pages:
                    raise ValueError('Jumlah halaman berubah selama pagination.')
                expected_pages = pages
                if data['hasNextPage'] != (page < pages) or page > max(1, pages):
                    raise ValueError('Penanda halaman terakhir tidak konsisten.')
            if 'nextPage' in data and data['nextPage'] != (page + 1 if data['hasNextPage'] else None):
                raise ValueError('Halaman lanjutan Repliz tidak konsisten.')
            for row in data['docs']:
                rid = row.get('id', row.get('_id')) if isinstance(row, dict) else None
                if not isinstance(rid, str) or not rid or rid in seen:
                    raise ValueError('ID snapshot Repliz kosong/berulang.')
                seen.add(rid)
                rows.append(row)
            if len(rows) > total:
                raise ValueError('Record snapshot melebihi total.')
            if not data['hasNextPage']:
                if len(rows) != total:
                    raise ValueError('Snapshot Repliz belum lengkap.')
                return rows
            if not data['docs'] or len(rows) >= total:
                raise ValueError('Halaman Repliz berulang atau kosong sebelum akhir.')
        raise ValueError('Pagination Repliz melampaui batas.')

    async def comment_inbox(self, account_ids, *, status='pending', max_pages=20, start_page=1):
        """Collect changing inbox pages without presenting them as a quota snapshot."""
        if (status not in {'pending', 'resolved', 'ignored'} or type(max_pages) is not int or not 2 <= max_pages <= 200
                or type(start_page) is not int or not 1 <= start_page <= 100000):
            raise ValueError('Filter/batas inbox komentar tidak valid.')
        ids = set(account_ids)
        if not ids or EXCLUDED_ACCOUNT_ID in ids:
            raise ValueError('Akun inbox kosong/dikecualikan.')
        rows = {}
        numbers = ([1] + list(range(start_page, start_page + max_pages - 1)) if start_page > 1
                   else range(1, max_pages + 1))
        requested = 0
        for page in numbers:
            requested += 1
            query = [('page', str(page)), ('limit', '100'), ('status', status)]
            query.extend((f'accountIds[{i}]', aid) for i, aid in enumerate(sorted(ids)))
            result = await self.request('GET', '/public/comment', params=query)
            data = result.get('data', result)
            if (not isinstance(data, dict) or not isinstance(data.get('docs'), list)
                    or type(data.get('hasNextPage')) is not bool or type(data.get('page')) is not int
                    or data['page'] != page or type(data.get('totalDocs')) is not int or data['totalDocs'] < 0):
                raise ValueError('Halaman inbox komentar tidak valid.')
            if 'nextPage' in data and data['nextPage'] != (page + 1 if data['hasNextPage'] else None):
                raise ValueError('Nomor halaman lanjutan inbox tidak valid.')
            previous = len(rows)
            for row in data['docs']:
                identity = row.get('id', row.get('_id')) if isinstance(row, dict) else None
                if (not isinstance(identity, str) or not identity or row.get('accountId') not in ids
                        or row.get('status') not in {'pending', 'resolved', 'ignored'}):
                    raise ValueError('Identitas/akun/status inbox tidak valid.')
                rows[identity] = row
            if not data['hasNextPage']:
                return {'rows': list(rows.values()), 'exhausted': True, 'pages': requested, 'next_page': 1}
            if len(rows) == previous:
                break
        return {'rows': list(rows.values()), 'exhausted': False, 'pages': requested,
                'next_page': min(100000, page + 1)}

    @staticmethod
    def validate_schedules(rows, ids):
        seen = set()
        for item in rows:
            rid = item.get('id', item.get('_id'))
            if not isinstance(rid, str) or not rid or rid in seen or account_id(item) not in ids:
                raise ValueError('ID/akun pada jadwal tidak sesuai snapshot.')
            if item.get('type') not in SCHEDULE_TYPES or item.get('status') not in SCHEDULE_STATUSES:
                raise ValueError('Tipe/status jadwal tidak dikenal; kuota lama dipertahankan.')
            timestamp(item.get('scheduleAt'))
            seen.add(rid)

    async def sync(self, store):
        generation = store.begin_sync()
        with api_scope('sync'):
            async with asyncio.timeout(self.config.section('workflow').get('sync_timeout_seconds', 120)):
                return await self.collect_snapshot(store, generation)

    async def collect_snapshot(self, store, generation):
        all_accounts = await self.pages('/public/account')
        if any(not isinstance(a.get('type'), str) or not a['type']
               or type(a.get('isConnected')) is not bool for a in all_accounts):
            raise ValueError('Identitas/status akun pada snapshot tidak lengkap.')
        accounts = [a for a in all_accounts if allowed_account(a)]
        ids = {a.get('id', a.get('_id')) for a in accounts}
        ids.update(a['id'] for a in store.accounts() if a.get('platform') in PLATFORMS and a['id'] != EXCLUDED_ACCOUNT_ID)
        ids = sorted(ids)
        start = now().astimezone(ZONE).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        end = start + timedelta(days=367)
        gate = asyncio.Semaphore(max(1, min(2, self.config.section('repliz').get('sync_concurrency', 2))))
        async def collect(batch_ids):
            async with gate:
                batch = await self.pages('/public/schedule', {
                    'accountIds': batch_ids, 'fromDate': iso(start), 'toDate': iso(end)})
                self.validate_schedules(batch, set(batch_ids))
                return batch
        tasks = [asyncio.create_task(collect(ids[offset:offset + 30])) for offset in range(0, len(ids), 30)]
        try:
            batches = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        schedules = [row for batch in batches for row in batch]
        self.validate_schedules(schedules, set(ids))
        return store.apply_snapshot(accounts, schedules, start.date().isoformat(), end.date().isoformat(),
                                    expected_generation=generation)

    async def check_account(self, aid, platform):
        if platform not in PLATFORMS or not aid or aid == EXCLUDED_ACCOUNT_ID:
            raise AccountUnavailable('Tujuan bukan akun TikTok, Facebook Page, atau YouTube yang diizinkan.')
        try:
            data = await self.request('GET', f'/public/account/{aid}')
        except ReplizError as exc:
            if exc.status == 404:
                raise AccountUnavailable('Akun tidak ditemukan di Repliz.') from None
            raise
        account = data.get('data', data)
        if (not isinstance(account, dict) or not allowed_account(account) or account.get('type') != platform
                or account.get('isConnected') is not True or account.get('id', account.get('_id')) != aid
                or not isinstance(account.get('username'), str) or not account['username'].strip()):
            raise AccountUnavailable('Akun tujuan terputus atau tidak sesuai platform.')
        return account

    @staticmethod
    def payload(job, when):
        from .attribution import credit_description, require_attribution
        platform = job.get('platform')
        if platform not in PLATFORMS or not job.get('account_id') or job['account_id'] == EXCLUDED_ACCOUNT_ID:
            raise ConfigurationError('Akun tujuan belum dialokasikan atau dikecualikan.')
        metadata = job['payload'].get('delivery_metadata')
        if not isinstance(metadata, dict) or metadata.get('account_id') != job['account_id'] or metadata.get('platform') != platform:
            raise ConfigurationError('Metadata persona belum terikat pada akun tujuan.')
        title = metadata.get('title')
        if title is None or isinstance(title, str) and not title.strip():
            generated = job['payload'].get('generated', {})
            hook = generated.get('hook') if isinstance(generated, dict) else None
            title = hook.strip()[:100] if platform in {'tiktok', 'facebook'} and isinstance(hook, str) else ''
        elif isinstance(title, str):
            title = title.strip()
        if not isinstance(title, str) or not title or len(title) > 100 or any(ch in title for ch in '<>\n\r'):
            raise ValueError('Judul video harus 1–100 karakter tanpa tag/baris baru.')
        hashtags = metadata.get('hashtags', [])
        if not isinstance(hashtags, list) or any(not isinstance(tag, str) or not tag.startswith('#') for tag in hashtags):
            raise ValueError('Hashtag publikasi tidak valid.')
        caption = metadata.get('caption', '')
        description = metadata.get('description') if platform == 'youtube' else None
        if description is None:
            description = caption + ('\n\n' + ' '.join(hashtags) if hashtags else '')
        if not isinstance(description, str) or not description.strip() or len(description) > (2200 if platform == 'tiktok' else 5000):
            raise ValueError('Caption/deskripsi melampaui batas platform atau kosong.')
        description = credit_description(description, require_attribution(job), 2200 if platform == 'tiktok' else 5000)
        info = {'targetCountries': ['ID'], 'isDraft': False}
        if platform == 'youtube':
            tags = metadata.get('tags', [])
            if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags) or len(','.join(tags)) > 500:
                raise ValueError('Tags YouTube tidak valid atau melebihi 500 karakter.')
            info['tags'] = tags
        return {'title': title, 'description': description,
                'type': 'reel' if platform == 'facebook' else 'video',
                'medias': [{'type': 'video', 'url': job['payload']['drive_url'], 'alt': 'fa:' + job['id']}],
                'accountId': job['account_id'], 'scheduleAt': iso(when), 'additionalInfo': info}

    async def create(self, payload, platform, *, before_send=None):
        if platform not in PLATFORMS or not payload.get('accountId') or payload['accountId'] == EXCLUDED_ACCOUNT_ID:
            raise ConfigurationError('Tujuan publikasi tidak diizinkan.')
        if payload.get('type') != ('reel' if platform == 'facebook' else 'video'):
            raise ValueError('Jenis media tidak sesuai adapter platform.')
        def check_time():
            if timestamp(payload.get('scheduleAt')) <= now():
                raise MutationNotSent('Slot jadwal sudah lewat selama menunggu budget API.')
            if before_send is not None:
                try:
                    before_send()
                except ConfigurationError as exc:
                    raise MutationNotSent(safe_error(exc)) from None
        data = await self.request('POST', '/public/schedule', json=payload, before_send=check_time)
        data = data.get('data', data)
        if not isinstance(data, dict) or not isinstance(data.get('scheduleId'), str) or not data['scheduleId']:
            raise RuntimeError('Respons create tanpa scheduleId; perlu rekonsiliasi.')
        return data['scheduleId']

    async def status(self, schedule_id):
        data = await self.request('GET', f'/public/schedule/{schedule_id}')
        result = data.get('data', data)
        if not isinstance(result, dict) or result.get('id', result.get('_id')) != schedule_id:
            raise ValueError('ID respons jadwal tidak sesuai permintaan.')
        return result

    @staticmethod
    def matches_submission(schedule, body):
        if account_id(schedule) != body.get('accountId'):
            return False
        if any(schedule.get(key) != body.get(key) for key in ('title', 'description', 'type')):
            return False
        if [m.get('url') for m in schedule.get('medias', [])] != [m.get('url') for m in body.get('medias', [])]:
            return False
        marker_matches = False
        for source, target in zip(schedule.get('medias', []), body.get('medias', [])):
            if source.get('alt', '').startswith('fa:') and source['alt'] != target.get('alt'):
                return False
            if re.fullmatch(r'fa:[0-9a-f]{32}', source.get('alt', '')) and source['alt'] == target.get('alt'):
                marker_matches = True
        if marker_matches or re.fullmatch(r'pw:[0-9a-f]{32}', body.get('title', '')):
            return True
        try:
            return abs((timestamp(schedule['scheduleAt']) - timestamp(body['scheduleAt'])).total_seconds()) < 1
        except (KeyError, TypeError, ValueError):
            return False

    async def reconcile(self, job):
        body = job['payload'].get('submission')
        if not isinstance(body, dict) or body.get('accountId') != job['account_id']:
            raise ConfigurationError('Intent publikasi belum lengkap; rekonsiliasi manual diperlukan.')
        when = timestamp(body['scheduleAt'])
        candidates = await self.pages('/public/schedule', {'accountIds': [job['account_id']],
            'fromDate': iso(when - timedelta(days=1)), 'toDate': iso(when + timedelta(days=1))})
        self.validate_schedules(candidates, {job['account_id']})
        matches = [s for s in candidates if self.matches_submission(s, body)]
        if len(matches) > 1:
            raise RuntimeError('Lebih dari satu jadwal cocok; perlu pemeriksaan operator.')
        return matches[0] if matches else None

    async def close(self):
        try:
            await flush_timings(self.observer)
        finally:
            await self.client.aclose()
