"""Daily read-only Repliz reports and durable, single-owner Telegram delivery."""
from __future__ import annotations

import asyncio
import base64
import os
import re
from datetime import date, datetime, time, timedelta

from .config import allowed_account
from .journal import AttemptJournal
from .report_store import CHAT_ID, ReportStore, decode_photo, digest, sending_body
from .report_views import ZONE, render_report
from .report_chart import render_chart
from .report_history import daily_series
from .util import iso, now


class ReportStopped(RuntimeError):
    pass


def counter(value):
    return type(value) is int and 0 <= value <= 10**15


def parse_statistics(payload, at):
    data = payload.get('data', payload)
    if not isinstance(data, dict) or not counter(data.get('followersCount')):
        raise ValueError('followersCount harus integer nonnegatif.')
    result = {'version': 1, 'followers': data['followersCount'],
              'likes': data.get('totalLikes') if counter(data.get('totalLikes')) else None,
              'videos': data.get('videosCount') if counter(data.get('videosCount')) else None,
              'daily': [], 'warnings': []}
    warnings, days, conflicts = set(), {}, set()
    metrics = data.get('metrics', [])
    if not isinstance(metrics, list):
        metrics = []
        warnings.add('histori harian tidak valid')
    fields = ('followersCount', 'dailyNewFollowers', 'dailyLostFollowers', 'dailyTotalFollowers',
              'videoViews', 'profileViews', 'likes', 'comments', 'shares')
    for raw in metrics:
        try:
            day = raw['date']
            if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
                raise ValueError()
            parsed = date.fromisoformat(day)
            if parsed > at.astimezone(ZONE).date() or not counter(raw.get('followersCount')):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            warnings.add('tanggal/nilai histori tidak valid; baris diabaikan')
            continue
        normalized = {key: raw.get(key) for key in fields if type(raw.get(key)) is int}
        if day in days and days[day] != normalized:
            conflicts.add(day)
        days[day] = normalized
    if conflicts:
        warnings.add('tanggal histori ganda bertentangan; baris diabaikan')
    latest = max(days, default=None)
    for day, item in sorted(days.items()):
        if day in conflicts:
            continue
        if day == latest and result['followers'] > 0 and all(key in item and item[key] == 0 for key in fields):
            warnings.add(f'{day} serba nol; belum terverifikasi')
            continue
        result['daily'].append({'day': day, 'followers': item['followersCount']})
    if latest and latest < (at.astimezone(ZONE).date() - timedelta(days=1)).isoformat():
        warnings.add(f'histori terbaru bertanggal {latest}')
    result['warnings'] = sorted(warnings)
    result['payload_hash'] = digest({key: data.get(key) for key in ('followersCount', 'totalLikes', 'videosCount', 'metrics')})
    return result


class ReportJournal(AttemptJournal):
    def __init__(self, config):
        super().__init__(config)
        self.root = self.root / 'account-reports'
        self.root.mkdir(mode=0o700, exist_ok=True)

    def begin(self, action_id, body):
        item = super().begin(action_id, body)
        # Persist newly created action/report directory entries before transport.
        for directory in (self.root, self.root.parent):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return item

    def checked_states(self):
        states = self.all_states()
        for item in states:
            if digest(item['body']) != item['body_sha256']:
                raise ValueError('Checksum jurnal laporan tidak cocok.')
        return states


class AccountReports:
    def __init__(self, config, store, repliz, telegram=None, *, clock=now):
        self.config, self.store, self.repliz, self.telegram = config, store, repliz, telegram
        self.db, self.journal, self.clock = ReportStore(store), ReportJournal(config), clock
        self.collection = None
        self.collecting_id = None
        self.recovered = False
        self.destination_checked = False

    def enabled(self):
        return self.store.get('account_report_enabled', self.config.data.get('account_report', {}).get('enabled', False)) is True

    def guard(self):
        if not self.enabled():
            raise ReportStopped('Laporan dinonaktifkan.')
        self.config.require_live_action()

    async def check_destination(self):
        if not self.telegram:
            raise ReportStopped('Transport Telegram belum tersedia.')
        bot = await self.telegram.call('getMe')
        chat = await self.telegram.call('getChat', chat_id=CHAT_ID)
        if bot.get('username', '').lower() != 'fahra_autonomousbot' or chat.get('id') != CHAT_ID or chat.get('type') != 'private':
            raise ReportStopped('Laporan hanya untuk bot Fahra dan chat pribadi admin.')
        self.destination_checked = True

    def recover(self):
        states = self.journal.checked_states()
        for item in states:
            if item['action_id'].startswith('manifest:'):
                body = item['body']
                if item['action_id'] != 'manifest:' + body['run']['id']:
                    raise ValueError('Identitas manifest jurnal tidak cocok.')
                self.db.restore(body)
        for item in states:
            if not item['action_id'].startswith('send:'):
                continue
            identity = item['action_id'][5:]
            part = self.db.part(identity)
            if not part or item['body'] != sending_body(part):
                raise ValueError('Jurnal pengiriman tidak cocok dengan manifest.')
            state = item['state']
            if state == 'accepted':
                if type(item.get('schedule_id')) is not int or item['schedule_id'] <= 0:
                    raise ValueError('message_id jurnal tidak valid.')
                self.db.part_state(identity, 'sent', message_id=item['schedule_id'], at=datetime.fromisoformat(item['at']))
            elif state in {'intended', 'unknown'}:
                self.db.part_state(identity, 'send_unknown', error='hasil transport belum terverifikasi')
            elif state == 'rejected':
                error = item.get('error') or ''
                if error.startswith('retry_at='):
                    retry_at = error.split('=', 1)[1]
                    datetime.fromisoformat(retry_at)
                    self.db.part_state(identity, 'queued', next_attempt=retry_at)
                else:
                    self.db.part_state(identity, 'failed', error=error)
        # A database intent without its external journal is not safe to repeat.
        with self.store.tx() as c:
            c.execute("UPDATE account_report_parts SET state='send_unknown',last_error='intent DB tanpa hasil jurnal' WHERE state='sending'")
        self.recovered = True

    async def collect(self, run):
        identity = run['id']
        deadline = datetime.fromisoformat(run['deadline'])
        try:
            raw_accounts = await self.fetch_inventory(deadline)
            if any(a.get('type') != 'tiktok' for a in raw_accounts):
                raise ValueError('Filter inventaris TikTok tidak dipenuhi.')
            selected = [a for a in raw_accounts if a.get('type') == 'tiktok' and allowed_account({**a, 'username': str(a.get('username', '')).strip()})]
            accounts = []
            for account in selected:
                key = account.get('id', account.get('_id'))
                if (not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', key) or not isinstance(account.get('username'), str)
                        or not account['username'].strip() or type(account.get('isConnected')) is not bool):
                    raise ValueError('Identitas/koneksi akun tidak lengkap.')
                accounts.append({'id': key, 'username': account['username'], 'connected': account['isConnected']})
            self.db.inventory(identity, accounts, len(raw_accounts) - len(selected))
        except Exception as exc:
            self.db.inventory_error(identity, error_label(exc))
            return
        retry = []
        for account in accounts:
            if account['connected'] and not await self.collect_account(run, account):
                retry.append(account)
        for account in retry:
            if (deadline - self.clock()).total_seconds() > 25:
                await self.collect_account(run, account)

    async def fetch_inventory(self, deadline):
        for attempt in range(2):
            try:
                remaining = (deadline - self.clock()).total_seconds()
                if remaining <= 0:
                    raise TimeoutError()
                async with asyncio.timeout(min(120, remaining)):
                    return await self.repliz.pages('/public/account', {'types': ['tiktok']})
            except Exception:
                if attempt or (deadline - self.clock()).total_seconds() <= 5:
                    raise
                await asyncio.sleep(1)

    async def collect_account(self, run, account):
        remaining = (datetime.fromisoformat(run['deadline']) - self.clock()).total_seconds()
        if remaining <= 0:
            return False
        try:
            async with asyncio.timeout(min(25, remaining)):
                value = await self.repliz.request('GET', f"/public/account/{account['id']}/statistic")
            at = self.clock()
            parsed = parse_statistics(value, at)
            self.db.metric(run['id'], account['id'], data=parsed, fetched=at)
            return True
        except Exception as exc:
            self.db.metric(run['id'], account['id'], error=error_label(exc))
            return False

    def freeze(self, run, at):
        action = 'manifest:' + run['id']
        existing = self.journal.state(action)
        if existing:
            if digest(existing['body']) != existing['body_sha256']:
                raise ValueError('Checksum manifest tidak cocok.')
            self.db.restore(existing['body'])
            return
        with self.store.tx() as c:
            c.execute("UPDATE account_metric_snapshots SET status='stale' WHERE run_id=? AND status='ok' AND (fetched_at<? OR fetched_at>?)",
                      (run['id'], iso(at - timedelta(minutes=20)), iso(at)))
        run = self.db.run(run['id'])
        run['frozen_at'] = iso(at)
        rows = self.db.rows(run['id'])
        series = daily_series(self.store, run['day'], current_run=run, current_rows=rows)
        parts = render_report(run, rows, self.db.baseline(run['day'], 1), self.db.baseline(run['day'], 7),
                              self.db.history(run['day']), missed=self.db.missed(run),
                              previous_connections=self.db.previous_connections(run['day']), series=series)
        chart = render_chart(run, rows, series)
        label = 'Laporan harian' if run['kind'] == 'daily' else 'Preview' if run['kind'] == 'preview' else 'Uji laporan'
        caption = (f'<b>{label} TikTok · target 600 followers</b>\n'
                   'Ringkasan capaian, akun terdekat ke target, dan histori snapshot harian.\n'
                   'Daftar lengkap, angka, serta waktu pengambilan ada pada pesan berikutnya.')
        parts.insert(0, {'text': caption, 'photo': base64.b64encode(chart).decode('ascii')})
        body = self.db.manifest(run, parts, at)
        self.journal.begin(action, body)
        self.db.restore(body)
        self.store.record('account_report_frozen', run_id=run['id'], parts=len(parts), valid=sum(r['status'] == 'ok' for r in rows))

    async def cancel_collection(self):
        if self.collection:
            self.collection.cancel()
            await asyncio.gather(self.collection, return_exceptions=True)
        self.collection, self.collecting_id = None, None

    async def tick(self):
        at = self.clock()
        if not self.recovered:
            self.recover()
        if not self.enabled():
            await self.cancel_collection()
            return
        local = at.astimezone(ZONE)
        day = local.date()
        due = datetime.combine(day, time(8), ZONE)
        cutoff = datetime.combine(day, time(12), ZONE)
        start = self.store.get('account_report_checked_day', self.store.get('account_report_activation_day', day.isoformat()))
        cursor = date.fromisoformat(start)
        while cursor < day:
            past_due = datetime.combine(cursor, time(8), ZONE)
            old = self.db.create('daily', cursor.isoformat(), past_due, past_due, at)
            if old['state'] == 'collecting':
                with self.store.tx() as c:
                    c.execute("UPDATE account_report_runs SET state='missed' WHERE id=? AND state='collecting'", (old['id'],))
            cursor += timedelta(days=1)
        self.store.set('account_report_checked_day', day.isoformat())
        pilot = self.store.get('account_report_pilot_day') == day.isoformat()
        daily = self.db.run(f'daily:{day.isoformat()}:{CHAT_ID}')
        if due - timedelta(minutes=10) <= local < cutoff and (not daily or daily['state'] == 'collecting'):
            pilot = False
        if pilot:
            run = self.db.create('pilot', day.isoformat(), at, at + timedelta(minutes=10), at)
            if run['state'] == 'ready':
                self.store.set('account_report_pilot_day', None)
                return
        elif local < due - timedelta(minutes=10):
            return
        else:
            deadline = due if local < due else min(at + timedelta(minutes=10), cutoff)
            run = self.db.create('daily', day.isoformat(), due, deadline, at)
            if local >= cutoff:
                with self.store.tx() as c:
                    c.execute("UPDATE account_report_runs SET state='missed' WHERE id=? AND state='collecting'", (run['id'],))
                await self.cancel_collection()
                return
        if run['state'] != 'collecting':
            return
        if at > datetime.fromisoformat(run['deadline']) + timedelta(minutes=20):
            await self.cancel_collection()
            deadline = min(at + timedelta(minutes=10), cutoff) if run['kind'] == 'daily' else at + timedelta(minutes=10)
            with self.store.tx() as c:
                c.execute("UPDATE account_report_runs SET deadline=?,started_at=? WHERE id=? AND state='collecting'",
                          (iso(deadline), iso(at), run['id']))
            run = self.db.run(run['id'])
        if self.collecting_id != run['id']:
            await self.cancel_collection()
            self.collecting_id = run['id']
            self.collection = asyncio.create_task(self.collect(run))
        complete = self.collection.done()
        deadline_reached = at >= datetime.fromisoformat(run['deadline'])
        due_reached = at >= datetime.fromisoformat(run['due_at'])
        if deadline_reached or (due_reached and complete):
            await self.cancel_collection()
            self.freeze(run, at)

    async def loop(self):
        try:
            while True:
                try:
                    await self.tick()
                    self.store.set('account_report_health', {'at': iso(self.clock()), 'ok': True, 'enabled': self.enabled(), 'collecting': self.collecting_id})
                except Exception as exc:
                    self.store.set('account_report_health', {'at': iso(self.clock()), 'ok': False, 'error': error_label(exc)})
                await asyncio.sleep(1)
        finally:
            await self.cancel_collection()

    async def flush_one(self):
        if not self.enabled():
            return
        if not self.recovered:
            self.recover()
        at = self.clock()
        part = self.db.pending(at)
        if not part:
            return
        local = at.astimezone(ZONE)
        if part['day'] != local.date().isoformat() or (part['kind'] == 'daily' and local.hour >= 12):
            self.db.part_state(part['id'], 'failed', error='jendela kirim sudah terlewat')
            return
        self.guard()
        if not self.destination_checked:
            await self.check_destination()
        action = 'send:' + part['id']
        body = sending_body(part)
        photo = decode_photo(part['photo']) if part.get('photo') is not None else None
        attempted = False
        def before_send():
            nonlocal attempted
            self.guard()
            current = self.clock().astimezone(ZONE)
            if current.date().isoformat() != part['day'] or (part['kind'] == 'daily' and current.hour >= 12):
                raise ReportStopped('Jendela pengiriman sudah lewat.')
            self.journal.begin(action, body)
            attempted = True
            self.db.part_state(part['id'], 'sending')
        try:
            if photo is None:
                result = await self.telegram.send_ui('sendMessage', CHAT_ID, text=part['text'], parse_mode='HTML', before_send=before_send)
            else:
                result = await self.telegram.send_ui('sendPhoto', CHAT_ID, photo=photo, caption=part['text'],
                                                   parse_mode='HTML', before_send=before_send)
            message_id = result.get('message_id') if isinstance(result, dict) else None
            if type(message_id) is not int or message_id <= 0:
                raise ValueError('Telegram tidak mengembalikan message_id.')
            self.journal.mark(action, 'accepted', schedule_id=message_id)
            self.db.part_state(part['id'], 'sent', message_id=message_id, at=self.clock())
            self.store.record('account_report_sent', part_id=part['id'], message_id=message_id)
            self.store.set('account_report_delivery_health', {'ok': True, 'at': iso(self.clock()), 'part_id': part['id']})
        except BaseException as exc:
            if attempted:
                prior = self.journal.state(action)
                if prior['state'] == 'accepted':
                    self.db.part_state(part['id'], 'sent', message_id=prior['schedule_id'], at=datetime.fromisoformat(prior['at']))
                elif getattr(exc, 'code', None) == 429:
                    retry = iso(self.clock() + timedelta(seconds=max(1, getattr(exc, 'retry_after', 0))))
                    self.journal.mark(action, 'rejected', error='retry_at=' + retry)
                    self.db.part_state(part['id'], 'queued', next_attempt=retry, error='Telegram 429')
                elif getattr(exc, 'code', None) in {400, 403, 404}:
                    self.journal.mark(action, 'rejected', error=error_label(exc))
                    self.db.part_state(part['id'], 'failed', error=error_label(exc))
                else:
                    self.journal.mark(action, 'unknown', error=error_label(exc))
                    self.db.part_state(part['id'], 'send_unknown', error=error_label(exc))
            self.store.set('account_report_delivery_health', {'ok': False, 'at': iso(self.clock()), 'error': error_label(exc), 'part_id': part['id']})
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not attempted:
                raise


def error_label(exc):
    status = getattr(exc, 'status', getattr(exc, 'code', None))
    return type(exc).__name__ + (f' HTTP {status}' if status else '')
