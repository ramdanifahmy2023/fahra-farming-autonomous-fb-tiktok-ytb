"""Reporting history is independent from publishing inventory and quota snapshots."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from .util import iso

CHAT_ID = 265732518


def date_from_day(value):
    return datetime.fromisoformat(value).date()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class ReportStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS account_report_runs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, day TEXT NOT NULL,
                    chat_id INTEGER NOT NULL, due_at TEXT NOT NULL, deadline TEXT NOT NULL,
                    state TEXT NOT NULL, started_at TEXT NOT NULL, frozen_at TEXT,
                    inventory_complete INTEGER NOT NULL DEFAULT 0,
                    inventory_error TEXT, excluded INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(kind, day, chat_id));
                CREATE TABLE IF NOT EXISTS account_metric_snapshots (
                    run_id TEXT NOT NULL, account_id TEXT NOT NULL, username TEXT NOT NULL,
                    status TEXT NOT NULL, fetched_at TEXT, data TEXT, error TEXT,
                    PRIMARY KEY(run_id, account_id),
                    FOREIGN KEY(run_id) REFERENCES account_report_runs(id));
                CREATE TABLE IF NOT EXISTS account_report_parts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, part_no INTEGER NOT NULL,
                    text TEXT NOT NULL, content_hash TEXT NOT NULL, state TEXT NOT NULL,
                    message_id INTEGER, next_attempt TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    sent_at TEXT, last_error TEXT, UNIQUE(run_id, part_no),
                    FOREIGN KEY(run_id) REFERENCES account_report_runs(id));
            ''')

    def create(self, kind, day, due, deadline, at):
        identity = f'{kind}:{day}:{CHAT_ID}'
        with self.store.tx() as c:
            c.execute('INSERT OR IGNORE INTO account_report_runs '
                      '(id,kind,day,chat_id,due_at,deadline,state,started_at) VALUES (?,?,?,?,?,?,?,?)',
                      (identity, kind, day, CHAT_ID, iso(due), iso(deadline), 'collecting', iso(at)))
        return self.run(identity)

    def run(self, identity):
        with self.store.connect() as c:
            row = c.execute('SELECT * FROM account_report_runs WHERE id=?', (identity,)).fetchone()
        return dict(row) if row else None

    def rows(self, identity):
        with self.store.connect() as c:
            rows = c.execute('SELECT * FROM account_metric_snapshots WHERE run_id=? ORDER BY account_id', (identity,)).fetchall()
        return [{**dict(r), 'data': json.loads(r['data']) if r['data'] else None} for r in rows]

    def inventory(self, identity, accounts, excluded):
        with self.store.tx() as c:
            if c.execute('SELECT state FROM account_report_runs WHERE id=?', (identity,)).fetchone()['state'] != 'collecting':
                return
            c.execute('UPDATE account_report_runs SET inventory_complete=1,inventory_error=NULL,excluded=? WHERE id=?', (excluded, identity))
            c.execute('DELETE FROM account_metric_snapshots WHERE run_id=?', (identity,))
            c.executemany('INSERT INTO account_metric_snapshots (run_id,account_id,username,status) VALUES (?,?,?,?)',
                          [(identity, a['id'], a['username'], 'pending' if a['connected'] else 'disconnected') for a in accounts])

    def inventory_error(self, identity, error):
        with self.store.tx() as c:
            c.execute("UPDATE account_report_runs SET inventory_error=? WHERE id=? AND state='collecting'", (error, identity))

    def metric(self, identity, account, *, data=None, fetched=None, error=None):
        with self.store.tx() as c:
            c.execute("UPDATE account_metric_snapshots SET status=?,data=?,fetched_at=?,error=? "
                      "WHERE run_id=? AND account_id=? AND EXISTS "
                      "(SELECT 1 FROM account_report_runs WHERE id=? AND state='collecting')",
                      ('ok' if data is not None else 'error', encode(data) if data is not None else None,
                       iso(fetched) if fetched else None, error, identity, account, identity))

    def baseline(self, day, offset):
        target = (datetime.fromisoformat(day).date() - timedelta(days=offset)).isoformat()
        with self.store.connect() as c:
            rows = c.execute("SELECT s.* FROM account_metric_snapshots s JOIN account_report_runs r ON r.id=s.run_id "
                             "WHERE r.kind='daily' AND r.day=? AND r.state='ready' AND s.status='ok'", (target,)).fetchall()
        return {r['account_id']: {**dict(r), 'data': json.loads(r['data'])} for r in rows}

    def previous_connections(self, day):
        target = (date_from_day(day) - timedelta(days=1)).isoformat()
        with self.store.connect() as c:
            run = c.execute("SELECT id FROM account_report_runs WHERE kind='daily' AND day=? AND state='ready' AND inventory_complete=1", (target,)).fetchone()
        return {r['account_id']: r['status'] for r in self.rows(run['id'])} if run else None

    def history(self, day):
        with self.store.connect() as c:
            rows = c.execute("SELECT s.*,r.day FROM account_metric_snapshots s JOIN account_report_runs r ON r.id=s.run_id "
                             "WHERE r.kind='daily' AND r.day<? AND r.state='ready' AND s.status='ok' ORDER BY r.day", (day,)).fetchall()
        return {r['account_id']: {**dict(r), 'data': json.loads(r['data'])} for r in rows}

    def missed(self, run):
        with self.store.connect() as c:
            previous = c.execute("SELECT MAX(r.day) FROM account_report_runs r WHERE r.kind='daily' AND r.day<? "
                                 "AND EXISTS (SELECT 1 FROM account_report_parts p WHERE p.run_id=r.id AND p.state='sent')", (run['day'],)).fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM account_report_runs r WHERE r.kind='daily' AND r.day<? AND r.day>? "
                             "AND (r.state='missed' OR (r.state='ready' AND NOT EXISTS "
                             "(SELECT 1 FROM account_report_parts p WHERE p.run_id=r.id AND p.state IN ('sent','sending','send_unknown'))))",
                             (run['day'], previous or '')).fetchone()[0]

    def manifest(self, run, parts, at):
        return {'run': {**run, 'state': 'ready', 'frozen_at': iso(at)}, 'snapshots': self.rows(run['id']),
                'parts': [{'id': f"{run['id']}:{i}", 'run_id': run['id'], 'part_no': i,
                           'text': text, 'content_hash': digest(text)} for i, text in enumerate(parts, 1)]}

    def restore(self, body):
        run = body['run']
        if run['chat_id'] != CHAT_ID or run['id'] != f"{run['kind']}:{run['day']}:{CHAT_ID}":
            raise ValueError('Manifest laporan memiliki tujuan/identitas tidak valid.')
        for i, p in enumerate(body['parts'], 1):
            if (p['content_hash'] != digest(p['text']) or p['run_id'] != run['id'] or p['part_no'] != i
                    or p['id'] != f"{run['id']}:{i}"):
                raise ValueError('Hash/identitas bagian laporan tidak valid.')
        with self.store.tx() as c:
            old = c.execute('SELECT state FROM account_report_runs WHERE id=?', (run['id'],)).fetchone()
            if old and old['state'] == 'ready':
                existing = [dict(r) for r in c.execute('SELECT id,run_id,part_no,text,content_hash FROM account_report_parts WHERE run_id=? ORDER BY part_no', (run['id'],))]
                if existing != body['parts']:
                    raise ValueError('Laporan beku tidak boleh berubah.')
                return
            columns = list(run)
            c.execute(f"INSERT INTO account_report_runs ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(id) DO UPDATE SET " + ','.join(f"{col}=excluded.{col}" for col in columns if col != 'id'), list(run.values()))
            c.execute('DELETE FROM account_metric_snapshots WHERE run_id=?', (run['id'],))
            for row in body['snapshots']:
                if row['run_id'] != run['id']:
                    raise ValueError('Snapshot salah laporan.')
                row = {**row, 'data': encode(row['data']) if row['data'] is not None else None}
                c.execute(f"INSERT INTO account_metric_snapshots ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
            for part in body['parts']:
                c.execute('INSERT OR IGNORE INTO account_report_parts (id,run_id,part_no,text,content_hash,state) VALUES (?,?,?,?,?,?)',
                          (part['id'], part['run_id'], part['part_no'], part['text'], part['content_hash'], 'queued'))

    def part_state(self, identity, state, *, message_id=None, next_attempt=None, error=None, at=None):
        with self.store.tx() as c:
            c.execute('UPDATE account_report_parts SET state=?,message_id=COALESCE(?,message_id),next_attempt=?,last_error=?,sent_at=COALESCE(?,sent_at),attempts=attempts+? WHERE id=?',
                      (state, message_id, next_attempt, error, iso(at) if at else None, int(state == 'sending'), identity))

    def pending(self, at):
        with self.store.connect() as c:
            row = c.execute("SELECT p.*,r.chat_id,r.day,r.kind FROM account_report_parts p JOIN account_report_runs r ON r.id=p.run_id "
                            "WHERE p.state='queued' AND (p.next_attempt IS NULL OR p.next_attempt<=?) ORDER BY r.day,p.part_no LIMIT 1", (iso(at),)).fetchone()
        return dict(row) if row else None

    def summary(self):
        with self.store.connect() as c:
            runs = [dict(r) for r in c.execute('SELECT * FROM account_report_runs ORDER BY started_at DESC LIMIT 5')]
            parts = [dict(r) for r in c.execute('SELECT run_id,state,COUNT(*) count FROM account_report_parts GROUP BY run_id,state ORDER BY run_id DESC LIMIT 20')]
        return {'runs': runs, 'parts': parts}
