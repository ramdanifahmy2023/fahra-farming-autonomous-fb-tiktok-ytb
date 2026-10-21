"""Durable references from Repliz; webhook bodies never become publication evidence."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass

from .config import EXCLUDED_ACCOUNT_ID, allowed_account


class InboxFull(RuntimeError):
    pass


@dataclass(frozen=True)
class WebhookEvent:
    kind: str
    platform: str
    account_id: str
    remote_id: str = ''
    post_id: str = ''
    claimed_status: str = ''

    @property
    def key(self):
        # A later success must still wake reconciliation after an earlier error.
        fields = (self.kind, self.platform, self.account_id, self.remote_id, self.claimed_status)
        return hashlib.sha256(json.dumps(fields, separators=(',', ':')).encode()).hexdigest()


class WebhookInbox:
    def __init__(self, store, *, max_pending=5000, max_records=100000, clock=time.time, initialize=True):
        self.store, self.clock = store, clock
        self.max_pending, self.max_records = max_pending, max_records
        if not 1 <= max_pending <= max_records:
            raise ValueError('Batas inbox webhook tidak valid.')
        if initialize:
            with store.tx() as connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS webhook_inbox(
                    event_key TEXT PRIMARY KEY, kind TEXT NOT NULL, platform TEXT NOT NULL,
                    account_id TEXT NOT NULL, remote_id TEXT NOT NULL, post_id TEXT NOT NULL,
                    job_id TEXT, claimed_status TEXT NOT NULL,
                    received_at REAL NOT NULL, updated_at REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','processing','done','ignored','held')),
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
                    lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0, last_error TEXT)""")
                connection.execute('CREATE INDEX IF NOT EXISTS webhook_due ON webhook_inbox(status,next_attempt,lease_until)')
                connection.execute('CREATE INDEX IF NOT EXISTS webhook_retention ON webhook_inbox(status,updated_at)')

    @staticmethod
    def _target(connection, event):
        if event.kind == 'testing':
            return None
        if event.account_id == EXCLUDED_ACCOUNT_ID:
            return None
        row = connection.execute('SELECT * FROM accounts WHERE id=?', (event.account_id,)).fetchone()
        if not row or not allowed_account({**dict(row), 'type': row['platform']}):
            return None
        if event.kind == 'schedule':
            jobs = connection.execute("""SELECT id,status FROM jobs WHERE account_id=? AND schedule_id=?
                AND status IN ('scheduled','submitting','submit_unknown','failed','published') LIMIT 2""",
                (event.account_id, event.remote_id)).fetchall()
        elif event.kind == 'comment' and row['connected'] and event.platform == row['platform']:
            jobs = connection.execute("""SELECT id,status FROM jobs WHERE account_id=? AND status='published'
                AND json_extract(payload,'$.post_id')=? LIMIT 2""", (event.account_id, event.post_id)).fetchall()
        else:
            return None
        return dict(jobs[0]) if len(jobs) == 1 else None

    def accept(self, event):
        stamp = self.clock()
        with self.store.tx() as connection:
            if event.kind != 'testing' and not (target := self._target(connection, event)):
                return {'accepted': True, 'queued': False, 'ignored': True, 'duplicate': False}
            if connection.execute('SELECT 1 FROM webhook_inbox WHERE event_key=?', (event.key,)).fetchone():
                return {'accepted': True, 'queued': False, 'ignored': False, 'duplicate': True}
            # Retain a month of completed dedup keys; replay remains GET-only after expiry.
            connection.execute("""DELETE FROM webhook_inbox WHERE event_key IN (
                SELECT event_key FROM webhook_inbox WHERE status IN ('done','ignored') AND updated_at<? LIMIT 500)""",
                (stamp - 30 * 86400,))
            total = connection.execute('SELECT count(*) FROM webhook_inbox').fetchone()[0]
            pending = connection.execute("SELECT count(*) FROM webhook_inbox WHERE status IN ('pending','processing')").fetchone()[0]
            if total >= self.max_records or pending >= self.max_pending:
                raise InboxFull('Inbox webhook penuh; polling cadangan tetap diperlukan.')
            testing = event.kind == 'testing'
            connection.execute("""INSERT INTO webhook_inbox(event_key,kind,platform,account_id,remote_id,
                post_id,job_id,claimed_status,received_at,updated_at,status,next_attempt)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (event.key, event.kind, event.platform, event.account_id,
                event.remote_id, event.post_id, None if testing else target['id'], event.claimed_status,
                stamp, stamp, 'done' if testing else 'pending', stamp))
        return {'accepted': True, 'queued': not testing, 'ignored': False, 'duplicate': False}

    def claim(self, *, lease_seconds=120):
        stamp = self.clock()
        with self.store.tx() as connection:
            row = connection.execute("""SELECT * FROM webhook_inbox WHERE
                (status='pending' AND next_attempt<=?) OR (status='processing' AND lease_until<=?)
                ORDER BY received_at,event_key LIMIT 1""", (stamp, stamp)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            connection.execute("""UPDATE webhook_inbox SET status='processing',lease_token=?,lease_until=?,
                attempts=attempts+1,updated_at=? WHERE event_key=?""", (token, stamp + lease_seconds, stamp, row['event_key']))
            return {**dict(row), 'lease_token': token, 'attempts': row['attempts'] + 1}

    def target(self, hint):
        event = WebhookEvent(*(hint[k] for k in ('kind','platform','account_id','remote_id','post_id','claimed_status')))
        with self.store.connect() as connection:
            target = self._target(connection, event)
        return target if target and target['id'] == hint['job_id'] else None

    def finish(self, hint, *, ignored=False):
        with self.store.tx() as connection:
            return connection.execute("""UPDATE webhook_inbox SET status=?,updated_at=?,lease_until=0,
                lease_token=NULL,last_error=NULL WHERE event_key=? AND status='processing' AND lease_token=?""",
                ('ignored' if ignored else 'done', self.clock(), hint['event_key'], hint['lease_token'])).rowcount == 1

    def retry(self, hint, error):
        stamp = self.clock()
        with self.store.tx() as connection:
            connection.execute("""UPDATE webhook_inbox SET status=?,updated_at=?,next_attempt=?,lease_until=0,
                lease_token=NULL,last_error=? WHERE event_key=? AND status='processing' AND lease_token=?""",
                ('held' if hint['attempts'] >= 12 else 'pending', stamp,
                 stamp + min(3600, 30 * 2 ** min(hint['attempts'] - 1, 7)), type(error).__name__[:80],
                 hint['event_key'], hint['lease_token']))

    def summary(self):
        with self.store.connect() as connection:
            return {row['status']: row['count'] for row in connection.execute(
                'SELECT status,count(*) AS count FROM webhook_inbox GROUP BY status')}
