"""Fsynced append-only attempt records live outside SQLite backups."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigurationError, CODE_ROOT
from .util import iso, safe_error


class AttemptBlocked(ConfigurationError):
    pass


class AttemptJournal:
    def __init__(self, config, *, readonly=False):
        production_runtime = (config.root.resolve() == CODE_ROOT and (config.root / '.git').is_dir()
                              and config.runtime.resolve() == CODE_ROOT / 'runtime')
        self.root = (config.path(config.section('workflow').get('journal_dir', 'runtime/attempts'))
                     if production_runtime else config.runtime / 'attempts')
        self.barrier_root = self.root
        if not readonly:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def barrier(self, exclusive=False):
        with (self.barrier_root / '.backup.lock').open('a+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def folder(self, action_id):
        return self.root / hashlib.sha256(str(action_id).encode()).hexdigest()

    @contextmanager
    def locked(self, action_id):
        folder = self.folder(action_id)
        folder.mkdir(exist_ok=True, mode=0o700)
        with self.barrier(), (folder / '.lock').open('a+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield folder
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def state(self, action_id):
        folder = self.folder(action_id)
        events = sorted(folder.glob('*.json'))
        if not events:
            return None
        item = json.loads(events[-1].read_text())
        if item.get('action_id') != str(action_id):
            raise AttemptBlocked('Identitas attempt journal tidak cocok.')
        return item

    def write(self, folder, item):
        target = folder / (f'{time.time_ns():020d}-' + uuid.uuid4().hex + '.json')
        data = json.dumps(item, ensure_ascii=False, sort_keys=True).encode()
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            descriptor = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            # An incomplete record blocks subsequent sends until reviewed.
            raise
        return item

    def begin(self, action_id, body):
        with self.locked(action_id) as folder:
            existing = self.state(action_id)
            if existing and existing['state'] not in {'not_sent', 'rejected'}:
                raise AttemptBlocked('Pengiriman sebelumnya belum aman untuk diulang; rekonsiliasi diperlukan.')
            encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
            return self.write(folder, {'action_id': str(action_id), 'state': 'intended', 'at': iso(),
                                      'body': body, 'body_sha256': hashlib.sha256(encoded).hexdigest()})

    def mark(self, action_id, state, schedule_id=None, error=None):
        if state not in {'not_sent', 'rejected', 'unknown', 'accepted'}:
            raise ValueError('Status attempt journal tidak valid.')
        with self.locked(action_id) as folder:
            existing = self.state(action_id)
            if existing is None:
                raise AttemptBlocked('Intent durable belum tersedia.')
            if existing['state'] == 'accepted' and (state != 'accepted' or str(schedule_id) != str(existing.get('schedule_id'))):
                raise AttemptBlocked('Hasil diterima tidak boleh diturunkan menjadi aman retry.')
            if (existing['state'] == state and existing.get('schedule_id') == schedule_id
                    and existing.get('error') == (safe_error(error) if error else None)):
                return existing
            return self.write(folder, {**existing, 'state': state, 'at': iso(),
                                      'schedule_id': schedule_id, 'error': safe_error(error) if error else None})

    def all_states(self):
        result = []
        if not self.root.exists():
            return result
        for folder in self.root.iterdir():
            if folder.is_dir():
                files = sorted(folder.glob('*.json'))
                if files:
                    item = json.loads(files[-1].read_text())
                    if self.folder(item['action_id']) != folder:
                        raise AttemptBlocked('Folder attempt journal tidak cocok.')
                    result.append(item)
        return result

    def reconcile_store(self, store):
        repaired, unresolved = [], []
        for item in self.all_states():
            action_id = item['action_id']
            if ':' in action_id or item['state'] in {'not_sent', 'rejected'}:
                continue
            job = store.job(action_id)
            if not job:
                unresolved.append(action_id)
                continue
            if job.get('schedule_id') and item.get('schedule_id') and str(job['schedule_id']) != str(item['schedule_id']):
                unresolved.append(action_id)
                continue
            if job.get('status') == 'published':
                if item.get('schedule_id') and str(job.get('schedule_id')) != str(item['schedule_id']):
                    unresolved.append(action_id)
                continue
            body = item['body']
            account = body.get('accountId')
            from .scheduler import parse_time
            from .db import ZONE
            try:
                if not isinstance(account,str) or not account:
                    raise ValueError('Account ID absent')
                when = parse_time(body['scheduleAt'])
            except (ValueError, TypeError, KeyError):
                unresolved.append(action_id)
                continue
            remote = None
            with store.tx() as connection:
                registered = connection.execute('SELECT platform FROM accounts WHERE id=?',(account,)).fetchone()
                if not registered or registered['platform'] != job['platform']:
                    unresolved.append(action_id)
                    continue
                schedule_id = item.get('schedule_id') or job.get('schedule_id')
                if schedule_id and store._snapshot_fresh(connection):
                    remote = connection.execute('SELECT * FROM external_schedules WHERE id=?',(schedule_id,)).fetchone()
                    if remote:
                        if remote['account_id'] != account or not remote['scheduled_at']:
                            unresolved.append(action_id)
                            continue
                        when = parse_time(remote['scheduled_at'])
                connection.execute('UPDATE jobs SET account_id=?,slot_date=?,planned_at=?,counts_quota=1 WHERE id=?',
                    (account,when.astimezone(ZONE).date().isoformat(),iso(when),action_id))
            payload = {'submission': body, 'journal_restored': True, 'submitted_at': item['at']}
            if item['state'] == 'accepted' and item.get('schedule_id'):
                failed = remote['status'] == 'error' if remote else job['status'] == 'failed'
                payload['next_poll'] = ''
                if failed:
                    payload['failure_kind'] = 'provider'
                store.checkpoint(action_id, 'failed' if failed else 'scheduled', payload=payload,
                                 schedule_id=item['schedule_id'], counts_quota=1, leased=0,
                                 last_error=(job.get('last_error') or 'Provider melaporkan kiriman gagal.')
                                 if failed else None)
            elif job.get('status') == 'scheduled' and job.get('schedule_id'):
                store.checkpoint(action_id, payload=payload, counts_quota=1, leased=0)
            else:
                store.checkpoint(action_id, 'submit_unknown', payload=payload, counts_quota=1, leased=0)
            repaired.append(action_id)
        store.set('journal_restore_unresolved', unresolved)
        if repaired:
            store.record('journal_restore_reconciled', repaired=repaired)
        return {'repaired': repaired, 'unresolved': unresolved}
