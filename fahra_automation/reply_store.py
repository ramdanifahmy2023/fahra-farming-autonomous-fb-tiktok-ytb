from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from .config import EXCLUDED_ACCOUNT_ID
from .db import ZONE
from .util import iso, now


class ReplyStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as c:
            c.executescript(f"""
                CREATE TABLE IF NOT EXISTS video_contexts(
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id), account_id TEXT NOT NULL,
                    post_id TEXT NOT NULL, context TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(account_id,post_id), CHECK(account_id != '{EXCLUDED_ACCOUNT_ID}')
                );
                CREATE TABLE IF NOT EXISTS comment_events(
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, post_id TEXT NOT NULL,
                    platform_id TEXT NOT NULL, inbox_id TEXT NOT NULL, payload TEXT NOT NULL,
                    job_id TEXT, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    draft TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    leased INTEGER NOT NULL DEFAULT 0,
                    next_attempt TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(account_id,platform_id), UNIQUE(account_id,inbox_id),
                    CHECK(account_id != '{EXCLUDED_ACCOUNT_ID}')
                );
                CREATE INDEX IF NOT EXISTS comments_ready ON comment_events(status,next_attempt);
                CREATE TABLE IF NOT EXISTS reply_attempts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL REFERENCES comment_events(id),
                    account_id TEXT NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL,
                    text TEXT NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    reply_id TEXT, error TEXT
                );
            """)
            if 'leased' not in {r[1] for r in c.execute('PRAGMA table_info(comment_events)')}:
                c.execute('ALTER TABLE comment_events ADD COLUMN leased INTEGER NOT NULL DEFAULT 0')

    def enroll_accounts(self, account_ids=None, *, enable=False):
        with self.store.tx() as c:
            stamp = iso()
            since = self.store.setting(c, 'reply_since') or stamp
            previous = self.store.setting(c, 'reply_account_ids', [])
            cutoffs = self.store.setting(c, 'reply_account_since', {})
            # Preserve migrated cutoffs before replacing even an empty/disconnected inventory.
            for identity in previous:
                cutoffs.setdefault(identity, since)
            current = sorted(set(previous if account_ids is None else account_ids))
            for identity in current:
                cutoffs.setdefault(identity, stamp)
            self.store.put_setting(c, 'reply_account_since', cutoffs)
            self.store.put_setting(c, 'reply_account_ids', current)
            if enable:
                self.store.put_setting(c, 'reply_since', since)
                self.store.put_setting(c, 'reply_enabled', True)
        return {'accounts': len(current), 'since': since}

    def recover(self):
        with self.store.tx() as c:
            c.execute("UPDATE comment_events SET status='queued' WHERE status='processing'")
            c.execute("UPDATE comment_events SET status='send_unknown' WHERE status='sending'")
            c.execute("UPDATE reply_attempts SET status='send_unknown' WHERE status='sending'")
            c.execute('UPDATE comment_events SET leased=0')

    @staticmethod
    def decode(row):
        if row is None:
            return None
        row = dict(row)
        row['payload'] = json.loads(row['payload'])
        row['draft'] = json.loads(row['draft']) if row['draft'] else None
        return row

    def ingest(self, item, status='queued', reason=''):
        if item['account_id'] == EXCLUDED_ACCOUNT_ID:
            raise ValueError('Akun reply dikecualikan.')
        identity = hashlib.sha256((item['account_id'] + ':' + item['platform_id']).encode()).hexdigest()
        stamp = iso()
        with self.store.tx() as c:
            count = c.execute("""INSERT OR IGNORE INTO comment_events
                (id,account_id,post_id,platform_id,inbox_id,payload,status,reason,next_attempt,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (identity, item['account_id'], item['post_id'], item['platform_id'],
                item['inbox_id'], json.dumps(item, ensure_ascii=False), status, reason, stamp, stamp, stamp)).rowcount
        return count

    def get(self, identity):
        with self.store.connect() as c:
            return self.decode(c.execute('SELECT * FROM comment_events WHERE id=?', (identity,)).fetchone())

    def claim(self):
        with self.store.tx() as c:
            row = c.execute("""SELECT * FROM comment_events WHERE status IN ('queued','drafted')
                AND leased=0 AND next_attempt<=? ORDER BY created_at LIMIT 1""", (iso(),)).fetchone()
            if not row:
                return None
            if row['status'] == 'queued':
                c.execute("UPDATE comment_events SET status='processing' WHERE id=?", (row['id'],))
            c.execute("UPDATE comment_events SET leased=1,updated_at=? WHERE id=?", (iso(), row['id']))
            return self.decode(c.execute('SELECT * FROM comment_events WHERE id=?', (row['id'],)).fetchone())

    def release(self, identity):
        with self.store.tx() as c:
            c.execute('UPDATE comment_events SET leased=0 WHERE id=?', (identity,))

    def failure(self, identity):
        with self.store.tx() as c:
            c.execute('UPDATE comment_events SET attempts=attempts+1 WHERE id=?', (identity,))
            return c.execute('SELECT attempts FROM comment_events WHERE id=?', (identity,)).fetchone()[0]

    def update(self, identity, status, reason='', draft=None, job_id=None, delay=0):
        with self.store.tx() as c:
            c.execute("""UPDATE comment_events SET status=?,reason=?,updated_at=?,next_attempt=?,
                draft=coalesce(?,draft),job_id=coalesce(?,job_id) WHERE id=?""",
                (status, reason[:800], iso(), iso(now() + timedelta(seconds=delay)),
                 json.dumps(draft, ensure_ascii=False) if draft is not None else None, job_id, identity))
            self.store.event(c, 'reply_' + status, job_id, event_id=identity, reason=reason[:300])

    def job_for(self, account_id, post_id):
        with self.store.connect() as c:
            rows = c.execute("""SELECT id FROM jobs WHERE account_id=? AND status='published'
                AND json_extract(payload,'$.post_id')=?""", (account_id, post_id)).fetchall()
        return self.store.job(rows[0]['id']) if len(rows) == 1 else None

    def context(self, job):
        with self.store.connect() as c:
            r = c.execute('SELECT * FROM video_contexts WHERE job_id=? AND account_id=? AND post_id=?',
                          (job['id'], job['account_id'], job['payload']['post_id'])).fetchone()
            return json.loads(r['context']) if r else None

    def save_context(self, job, context):
        with self.store.tx() as c:
            c.execute('INSERT OR REPLACE INTO video_contexts VALUES(?,?,?,?,?)',
                      (job['id'], job['account_id'], job['payload']['post_id'], json.dumps(context, ensure_ascii=False), iso()))
            self.store.event(c, 'reply_context_saved', job['id'], quality=context['quality'], context_hash=context['hash'])

    def recent(self, account_id):
        with self.store.connect() as c:
            return [r[0] for r in c.execute("""SELECT text FROM reply_attempts WHERE account_id=?
                AND status IN ('sent','sent_unverified','send_unknown','sending') ORDER BY id DESC LIMIT 8""", (account_id,))]

    def reserve_send(self, event, daily_limit):
        day = now().astimezone(ZONE).date().isoformat()
        with self.store.tx() as c:
            # The pause and quota checks share the transaction with the irreversible-send intent.
            if not self.store.setting(c, 'reply_enabled', False):
                return False
            row = c.execute('SELECT status,draft FROM comment_events WHERE id=?', (event['id'],)).fetchone()
            if not row or row['status'] != 'drafted':
                return False
            used = c.execute("""SELECT count(*) FROM reply_attempts WHERE account_id=? AND day=?
                AND status!='failed'""", (event['account_id'], day)).fetchone()[0]
            if used >= daily_limit:
                c.execute("UPDATE comment_events SET next_attempt=? WHERE id=?",
                          (iso(now() + timedelta(minutes=15)), event['id']))
                return False
            text = json.loads(row['draft'])['text']
            previous = c.execute('SELECT status FROM reply_attempts WHERE event_id=?', (event['id'],)).fetchone()
            if previous and previous['status'] != 'failed':
                return False
            c.execute("""INSERT INTO reply_attempts(event_id,account_id,day,status,text,started_at,updated_at)
                VALUES(?,?,?,'sending',?,?,?) ON CONFLICT(event_id) DO UPDATE SET
                day=excluded.day,status='sending',text=excluded.text,started_at=excluded.started_at,
                updated_at=excluded.updated_at,reply_id=NULL,error=NULL""",
                      (event['id'], event['account_id'], day, text, iso(), iso()))
            c.execute("UPDATE comment_events SET status='sending',updated_at=? WHERE id=?", (iso(), event['id']))
            self.store.event(c, 'reply_sending', event['job_id'], event_id=event['id'])
        return True

    def finish_send(self, event_id, status, reply_id=None, error=None):
        with self.store.tx() as c:
            c.execute("UPDATE reply_attempts SET status=?,reply_id=coalesce(?,reply_id),error=?,updated_at=? WHERE event_id=?",
                      (status, reply_id, error, iso(), event_id))
            c.execute("UPDATE comment_events SET status=?,reason=?,next_attempt=?,updated_at=? WHERE id=?",
                      (status, error or '', iso(now() + timedelta(seconds=120)), iso(), event_id))
            self.store.event(c, 'reply_' + status, event_id=event_id, reply_id=reply_id, error=error)

    def restore_send(self, event, record):
        body = record.get('body', {})
        if (body.get('account_id') != event['account_id'] or body.get('post_id') != event['post_id'] or
                body.get('comment_id') != event['platform_id'] or not isinstance(body.get('text'), str)):
            self.update(event['id'], 'needs_review', 'Jurnal kirim tidak lengkap/cocok; kirim ulang ditahan.')
            return
        status = 'sent_unverified' if record.get('schedule_id') else 'send_unknown'
        with self.store.tx() as c:
            c.execute('''INSERT INTO reply_attempts
                (event_id,account_id,day,status,text,started_at,updated_at,reply_id)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET
                day=excluded.day,status=excluded.status,text=excluded.text,started_at=excluded.started_at,
                updated_at=excluded.updated_at,reply_id=excluded.reply_id,error=NULL''', (event['id'], event['account_id'], now().astimezone(ZONE).date().isoformat(),
                    status, body['text'], body.get('started_at', event['created_at']), iso(), record.get('schedule_id')))
            c.execute('UPDATE comment_events SET status=?,job_id=?,next_attempt=?,updated_at=? WHERE id=?',
                      (status, event.get('job_id'), iso(), iso(), event['id']))

    def unresolved(self):
        with self.store.connect() as c:
            return [(self.decode(r), dict(c.execute('SELECT * FROM reply_attempts WHERE event_id=?', (r['id'],)).fetchone()))
                    for r in c.execute("""SELECT * FROM comment_events
                        WHERE status IN ('send_unknown','sent_unverified') AND next_attempt<=? LIMIT 5""", (iso(),)).fetchall()]

    def summary(self):
        with self.store.connect() as c:
            return {'enabled': self.store.setting(c, 'reply_enabled', False),
                    'since': self.store.setting(c, 'reply_since'),
                    'poll_at': self.store.setting(c, 'reply_poll_at'),
                    'poll_error': self.store.setting(c, 'reply_poll_error'),
                    'worker_at': self.store.setting(c, 'reply_worker_at'),
                    'accounts': len(self.store.setting(c, 'reply_account_ids', [])),
                    'contexts': dict(c.execute("SELECT json_extract(context,'$.quality'),count(*) FROM video_contexts GROUP BY 1")),
                    'comments': dict(c.execute('SELECT status,count(*) FROM comment_events GROUP BY status'))}
