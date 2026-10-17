from __future__ import annotations

import json
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ConfigurationError, EXCLUDED_ACCOUNT_ID, PLATFORMS, allowed_account
from .scheduler import daily_slots, offset_for, parse_time
from .util import iso, now, safe_error
from .schema import VERSION as SCHEMA_VERSION, upgrade

ZONE = ZoneInfo("Asia/Jakarta")
TERMINAL = {"published", "failed", "cancelled", "duplicate"}
PREPARE = {"queued", "downloaded", "analyzed", "rendered", "drive_ready"}


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    def __init__(self, path: Path, daily_limit=20, *, window_start=8, window_end=23, snapshot_ttl=600, readonly=False):
        if type(daily_limit) is not int or not 1 <= daily_limit <= 20:
            raise ValueError('Batas video harus antara satu dan 20 per akun/hari.')
        if not (type(window_start) is int and type(window_end) is int and 0 <= window_start < window_end <= 24):
            raise ValueError('Jendela jadwal tidak valid.')
        self.path = Path(path)
        self.daily_limit, self.window_start, self.window_end = daily_limit, window_start, window_end
        self.snapshot_ttl = snapshot_ttl
        self.readonly = readonly
        if readonly:
            with self.connect() as c:
                if c.execute('PRAGMA user_version').fetchone()[0] not in {1, SCHEMA_VERSION}:
                    raise ValueError('Database belum diinisialisasi atau versi tidak didukung.')
            return
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            existing = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
            version = c.execute('PRAGMA user_version').fetchone()[0]
            if existing and version not in {1, SCHEMA_VERSION}:
                raise ValueError('Database workflow lama/versi lain harus melalui importer, bukan dibuka langsung.')
            c.executescript(f"""
                PRAGMA journal_mode=DELETE;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts(
                    id TEXT PRIMARY KEY CHECK(id != '{EXCLUDED_ACCOUNT_ID}'),
                    username TEXT NOT NULL, platform TEXT NOT NULL CHECK(platform IN ('tiktok','facebook','youtube')),
                    name TEXT NOT NULL, ordinal INTEGER UNIQUE NOT NULL, connected INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,eligible INTEGER NOT NULL DEFAULT 1,slot_offset INTEGER NOT NULL DEFAULT 0,
                    blocked_until TEXT,block_reason TEXT,
                    CHECK(platform != 'tiktok' OR lower(ltrim(username,'@')) != 'iq7fanspage')
                );
                CREATE TABLE IF NOT EXISTS batches(
                    id TEXT PRIMARY KEY,chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,created_at TEXT NOT NULL,
                    UNIQUE(chat_id,message_id)
                );
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES batches(id),
                    source_id TEXT NOT NULL,url TEXT NOT NULL,
                    asset_id TEXT NOT NULL REFERENCES assets(id),
                    platform TEXT NOT NULL CHECK(platform IN ('tiktok','facebook','youtube')),
                    account_id TEXT REFERENCES accounts(id),slot_date TEXT,
                    planned_at TEXT,snapshot_generation INTEGER,
                    counts_quota INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'queued',
                    payload TEXT NOT NULL DEFAULT '{{}}',schedule_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,next_attempt TEXT NOT NULL,
                    leased INTEGER NOT NULL DEFAULT 0,last_error TEXT,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    UNIQUE(source_id,platform)
                );
                CREATE TABLE IF NOT EXISTS assets(
                    id TEXT PRIMARY KEY,source_id TEXT UNIQUE NOT NULL,url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',payload TEXT NOT NULL DEFAULT '{{}}',
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    required_destinations TEXT NOT NULL DEFAULT '["tiktok","facebook","youtube"]',leased_job_id TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status,next_attempt,leased);
                CREATE INDEX IF NOT EXISTS jobs_quota ON jobs(account_id,slot_date,counts_quota);
                CREATE TABLE IF NOT EXISTS external_schedules(
                    id TEXT PRIMARY KEY,account_id TEXT NOT NULL,slot_date TEXT NOT NULL,
                    status TEXT NOT NULL,title TEXT NOT NULL,scheduled_at TEXT,job_id TEXT
                );
                CREATE TABLE IF NOT EXISTS submission_attempts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL REFERENCES jobs(id),
                    generation INTEGER NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,error TEXT
                );
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,
                    job_id TEXT,kind TEXT NOT NULL,detail TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT,'Events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT,'Events are append-only'); END;
                CREATE TABLE IF NOT EXISTS outbox(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,dedupe_key TEXT UNIQUE NOT NULL,
                    chat_id INTEGER NOT NULL,text TEXT NOT NULL,sent INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_progress(
                    batch_id TEXT PRIMARY KEY REFERENCES batches(id),message_id INTEGER,
                    signature TEXT NOT NULL DEFAULT '',page INTEGER NOT NULL DEFAULT 0,
                    receipt TEXT NOT NULL DEFAULT '{{}}',next_poll TEXT NOT NULL DEFAULT '',
                    done INTEGER NOT NULL DEFAULT 0
                );
            """)
            columns = {r["name"] for r in c.execute("PRAGMA table_info(outbox)")}
            if "options" not in columns:
                c.execute("ALTER TABLE outbox ADD COLUMN options TEXT NOT NULL DEFAULT '{}'")
            upgrade(c, version)
            self.put_setting(c, 'schema_version', SCHEMA_VERSION)
            self.put_setting(c, 'daily_limit', daily_limit)

    def connect(self):
        c = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro' if self.readonly else self.path,
                            uri=self.readonly, timeout=30, isolation_level=None, factory=Connection)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        if self.readonly:
            c.execute('PRAGMA query_only=ON')
        return c

    @contextmanager
    def tx(self):
        c = self.connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def event(c, kind, job_id=None, **detail):
        c.execute("INSERT INTO events(at,job_id,kind,detail) VALUES(?,?,?,?)",
                  (iso(), job_id, kind, json.dumps(detail, ensure_ascii=False)))

    def health(self, component, state, **detail):
        from .health import record
        record(self, component, state, **detail)

    def timing(self, name, milliseconds):
        with self.tx() as c:
            c.execute("""INSERT INTO timings VALUES(?,1,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                samples=samples+1,total_ms=total_ms+excluded.last_ms,max_ms=max(max_ms,excluded.last_ms),
                last_ms=excluded.last_ms,updated_at=excluded.updated_at""",
                      (name, milliseconds, milliseconds, milliseconds, iso()))

    def capability(self, account_id, operation, state, evidence, reason=''):
        with self.tx() as c:
            account = c.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
            if not account:
                raise ValueError('Akun capability tidak ditemukan.')
            c.execute('INSERT OR REPLACE INTO account_capabilities VALUES(?,?,?,?,?,?,?,?)',
                      (account_id, operation, state, evidence,
                       json.dumps([account['platform'],account['username']]), iso(),
                       iso(now()+timedelta(days=7)), reason[:800]))
            if operation == 'publish' and state == 'held':
                c.execute('UPDATE accounts SET eligible=0 WHERE id=?', (account_id,))
                self.event(c, 'account_publish_held', account_id=account_id, reason=reason[:800])

    def record(self, kind, job_id=None, **detail):
        with self.tx() as c:
            self.event(c, kind, job_id, **detail)

    @staticmethod
    def setting(c, key, default=None):
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put_setting(c, key, value):
        c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))

    def get(self, key, default=None):
        with self.connect() as c:
            return self.setting(c, key, default)

    def set(self, key, value):
        with self.tx() as c:
            self.put_setting(c, key, value)

    def recover(self):
        with self.tx() as c:
            c.execute("UPDATE jobs SET leased=0")
            c.execute("UPDATE assets SET leased_job_id=NULL")
            c.execute("UPDATE jobs SET status='submit_unknown' WHERE status='submitting'")
            c.execute("UPDATE submission_attempts SET status='submit_unknown',updated_at=? WHERE status='submitting'", (iso(),))
            self.event(c, "process_started")

    def backup(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / ("workflow-" + now().strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3")
        partial = target.with_suffix(".partial")
        with self.connect() as source, sqlite3.connect(partial) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Pemeriksaan backup SQLite gagal.")
        destination.close()
        os.chmod(partial, 0o600)
        partial.replace(target)
        for old in sorted(folder.glob("workflow-*.sqlite3"))[:-30]:
            old.unlink()
        self.record("backup_created", path=str(target))
        return target

    def begin_sync(self):
        with self.tx() as c:
            generation = self.setting(c, 'snapshot_generation', 0) + 1
            self.put_setting(c, 'snapshot_generation', generation)
            self.put_setting(c, 'sync_in_progress', True)
            self.put_setting(c, 'repliz_synced_at', None)
            return generation

    invalidate_snapshot = begin_sync

    def snapshot_generation(self):
        return self.get('snapshot_generation', 0)

    def _snapshot_fresh(self, c):
        stamp = self.setting(c, 'repliz_synced_at')
        if not stamp or self.setting(c, 'sync_in_progress', False):
            return False
        age = (now() - parse_time(stamp)).total_seconds()
        return 0 <= age < self.snapshot_ttl and self.setting(c, 'snapshot_generation', 0) > 0

    def snapshot_fresh(self):
        with self.connect() as c:
            return self._snapshot_fresh(c)

    def snapshot_token(self):
        with self.connect() as c:
            if not self._snapshot_fresh(c):
                raise ConfigurationError('Snapshot akun/kuota belum lengkap atau kedaluwarsa.')
            return self.setting(c, 'snapshot_generation', 0)

    def eligible_accounts(self, platform=None):
        with self.connect() as c:
            if not self._snapshot_fresh(c):
                return []
            return [dict(r) for r in c.execute('SELECT * FROM accounts WHERE connected=1 AND eligible=1 ORDER BY ordinal')
                    if platform is None or r['platform'] == platform]

    def _sync_accounts(self, c, accounts, schedules=()):
        selected = [a for a in accounts if allowed_account(a)]
        ids = [a.get("id", a.get("_id")) for a in selected]
        if len(ids) != len(set(ids)) or any(not isinstance(i, str) or not i for i in ids) or any(type(a.get("isConnected")) is not bool for a in selected):
            raise ValueError("Snapshot akun tidak lengkap/valid; daftar lama dipertahankan.")
        before = {r['id']: dict(r) for r in c.execute('SELECT * FROM accounts')}
        c.execute('UPDATE accounts SET connected=0')
        ordinal = c.execute('SELECT coalesce(max(ordinal),-1)+1 FROM accounts').fetchone()[0]
        for a in sorted(selected, key=lambda a: (a['type'], (a.get('name') or '').casefold(), a.get('id', a.get('_id')))):
            aid = a.get('id', a.get('_id'))
            old = before.get(aid)
            if old and old['platform'] != a['type']:
                raise ValueError('Platform akun berubah; snapshot ditolak.')
            order = old['ordinal'] if old else ordinal
            if not old:
                ordinal += 1
            explicit = a.get('publishEligible')
            if explicit is not None and type(explicit) is not bool:
                raise ValueError('Kesiapan akun harus boolean.')
            username = str(a.get('username') or '').strip()
            identity = json.dumps([a['type'], username, a.get('generatedId')], separators=(',', ':'))
            evidence = 'provider' if explicit is not None else ''
            prior = c.execute("SELECT * FROM account_capabilities WHERE account_id=? AND operation='publish'", (aid,)).fetchone()
            proof_since = max(iso(now()-timedelta(days=7)), prior['observed_at']
                              if prior and prior['evidence'] == 'provider_denied' else '')
            success = any(self._remote_account(s) == aid and s.get('status') == 'success'
                          and s.get('postId') and parse_time(proof_since) <= self._remote_time(s) <= now()
                          for s in schedules)
            if not success and old and old['username'] == username and username:
                success = bool(c.execute("""SELECT 1 FROM jobs j JOIN events e ON e.job_id=j.id
                    WHERE j.account_id=? AND j.status='published'
                    AND json_extract(j.payload,'$.post_id') IS NOT NULL
                    AND json_extract(j.payload,'$.post_url') IS NOT NULL
                    AND e.kind='status_changed' AND json_extract(e.detail,'$.status')='published'
                    AND e.at>=? AND e.at<=? LIMIT 1""", (aid, proof_since, iso())).fetchone())
            if not evidence and success:
                evidence = 'provider_success'
            if not evidence and prior and prior['identity'] == identity and prior['expires_at'] > iso() and prior['state'] == 'ready':
                evidence = 'cached:' + prior['evidence'].removeprefix('cached:')
            eligible = bool(username and a['isConnected'] and (explicit is True or (explicit is None and evidence)))
            reason = '' if eligible else ('Identitas akun belum lengkap.' if not username else
                     'Akun terputus.' if not a['isConnected'] else
                     'Provider menolak posting.' if explicit is False else 'Bukti kemampuan posting belum tersedia.')
            c.execute('''INSERT INTO accounts(id,username,platform,name,ordinal,connected,updated_at,eligible,slot_offset)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,
                name=excluded.name,connected=excluded.connected,eligible=excluded.eligible,updated_at=excluded.updated_at''',
                (aid, a.get('username') or '', a['type'], a.get('name') or '', order, int(a['isConnected']), iso(),
                 int(eligible), offset_for(aid, self.daily_limit, self.window_start, self.window_end)))
            expires = prior['expires_at'] if evidence.startswith('cached:') else iso(now() + timedelta(days=7))
            denied = prior and prior['evidence'] == 'provider_denied' and not eligible
            c.execute("""INSERT OR REPLACE INTO account_capabilities VALUES(?,?,?,?,?,?,?,?)""",
                      (aid, 'publish', 'ready' if eligible else 'held', 'provider_denied' if denied else evidence or 'missing',
                       identity, prior['observed_at'] if denied else iso(), expires, prior['reason'] if denied else reason))
        after = {r['id']: dict(r) for r in c.execute('SELECT * FROM accounts')}
        changes = {'added': sorted(set(after) - set(before)),
                   'disconnected': sorted(k for k in before if before[k]['connected'] and not after[k]['connected']),
                   'reconnected': sorted(k for k in before if not before[k]['connected'] and after[k]['connected'])}
        released = c.execute('''UPDATE jobs SET account_id=NULL,slot_date=NULL,planned_at=NULL,
            counts_quota=0,next_attempt=?,updated_at=? WHERE status IN ('queued','downloaded','analyzed','rendered','drive_ready')
            AND schedule_id IS NULL AND json_extract(payload,'$.submission') IS NULL
            AND account_id IN (SELECT id FROM accounts WHERE connected=0 OR eligible=0)''', (iso(), iso())).rowcount
        if any(changes.values()):
            c.execute("UPDATE jobs SET next_attempt=? WHERE status='drive_ready'", (iso(),))
            c.execute('UPDATE batch_progress SET done=0')
            self.event(c, 'account_pool_changed', **changes, reservations_released=released)
        self.event(c, 'accounts_synced', count=sum(a['connected'] for a in after.values()))
        return changes

    def sync_accounts(self, accounts):
        # Legacy two-call API is retained, but cannot grant fresh scheduling rights on its own.
        with self.tx() as c:
            changes = self._sync_accounts(c, accounts)
            self.put_setting(c, 'repliz_synced_at', None)
            self.put_setting(c, 'sync_in_progress', True)
            self.put_setting(c, 'snapshot_generation', self.setting(c, 'snapshot_generation', 0) + 1)
            return changes

    def disconnect(self, account_id):
        with self.tx() as c:
            c.execute("UPDATE accounts SET connected=0,updated_at=? WHERE id=?", (iso(), account_id))
            c.execute("""UPDATE jobs SET account_id=NULL,slot_date=NULL,planned_at=NULL,counts_quota=0,next_attempt=?,updated_at=?
                WHERE account_id=? AND status IN ('queued','downloaded','analyzed','rendered','drive_ready')
                AND schedule_id IS NULL AND json_extract(payload,'$.submission') IS NULL""",
                (iso(), iso(), account_id))
            self.event(c, "account_disconnected_preflight", account_id=account_id)

    def defer_account(self, account_id, until, error=''):
        stamp = iso(parse_time(until))
        with self.tx() as c:
            old = c.execute('SELECT blocked_until FROM accounts WHERE id=?', (account_id,)).fetchone()
            if not old:
                raise ValueError('Akun tidak ditemukan.')
            if old['blocked_until'] and old['blocked_until'] > stamp:
                stamp = old['blocked_until']
            c.execute('UPDATE accounts SET blocked_until=?,block_reason=?,updated_at=? WHERE id=?',
                      (stamp, safe_error(error), iso(), account_id))
            c.execute('''UPDATE jobs SET next_attempt=max(next_attempt,?) WHERE account_id=?
                AND status='drive_ready' AND schedule_id IS NULL AND json_extract(payload,'$.submission') IS NULL''',
                      (stamp, account_id))
            self.event(c, 'account_deferred', account_id=account_id, until=stamp, error=safe_error(error))

    @staticmethod
    def _remote_account(schedule):
        value = schedule.get('accountId') or schedule.get('account')
        return value.get('id', value.get('_id')) if isinstance(value, dict) else value

    @staticmethod
    def _remote_time(schedule):
        actual = schedule.get('publishedAt') if schedule.get('status') == 'success' else None
        return parse_time(actual or schedule['scheduleAt'])

    def _sync_external(self, c, schedules, from_day, to_day):
        datetime.fromisoformat(from_day)
        datetime.fromisoformat(to_day)
        if from_day > to_day:
            raise ValueError('Rentang snapshot terbalik.')
        c.execute('DELETE FROM external_schedules WHERE slot_date BETWEEN ? AND ?', (from_day, to_day))
        seen = set()
        for s in schedules:
            sid, aid = s.get('id', s.get('_id')), self._remote_account(s)
            if not isinstance(sid, str) or not sid or sid in seen:
                raise ValueError('ID jadwal kosong atau berulang.')
            seen.add(sid)
            if s.get('status') not in {'pending', 'process', 'success', 'error'}:
                raise ValueError('Status jadwal tidak dikenal; snapshot ditahan.')
            stamp = self._remote_time(s)
            if not c.execute('SELECT 1 FROM accounts WHERE id=?', (aid,)).fetchone():
                if aid == EXCLUDED_ACCOUNT_ID:
                    continue
                raise ValueError('Jadwal di luar inventaris akun.')
            if s.get('type') not in {'video', 'reel'}:
                continue
            day = stamp.astimezone(ZONE).date().isoformat()
            markers = {m.get('alt') for m in s.get('medias', []) if isinstance(m, dict)}
            markers.add(s.get('title', ''))
            matches = []
            for job in c.execute('''SELECT * FROM jobs WHERE account_id=? AND
                (schedule_id=? OR json_extract(payload,'$.submission') IS NOT NULL)''', (aid, sid)):
                if job['schedule_id'] == sid:
                    matches.append(job)
                    continue
                body = json.loads(job['payload']).get('submission')
                identified = any(prefix + job['id'] in markers for prefix in ('fa:', 'pw:', 'fahra:'))
                if (identified and isinstance(body, dict) and body.get('type') in {'video', 'reel'}
                        and all(body.get(key) == s.get(key) for key in ('title', 'description', 'type'))
                        and body.get('medias') and [m.get('url') for m in body['medias']] ==
                        [m.get('url') for m in s.get('medias', [])]):
                    matches.append(job)
            if len(matches) > 1:
                raise ValueError('Jadwal cocok dengan beberapa delivery.')
            jid = matches[0]['id'] if matches else None
            if matches:
                self._record_remote(c, matches[0], s)
            c.execute('''INSERT INTO external_schedules(id,account_id,slot_date,status,title,scheduled_at,job_id)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,
                slot_date=excluded.slot_date,status=excluded.status,title=excluded.title,
                scheduled_at=excluded.scheduled_at,job_id=excluded.job_id''',
                (sid, aid, day, s['status'], s.get('title', ''), iso(stamp), jid))
        self.event(c, 'schedules_synced', count=len(schedules), from_day=from_day, to_day=to_day)

    def apply_snapshot(self, accounts, schedules, from_day, to_day, expected_generation=None):
        with self.tx() as c:
            if expected_generation is not None and expected_generation != self.setting(c, 'snapshot_generation', 0):
                raise ConfigurationError('Snapshot sync lama sudah digantikan; hasil tidak diterapkan.')
            changes = self._sync_accounts(c, accounts, schedules)
            self._sync_external(c, schedules, from_day, to_day)
            generation = self.setting(c, 'snapshot_generation', 0) + 1
            self.put_setting(c, 'snapshot_generation', generation)
            self.put_setting(c, 'snapshot_from_day', from_day)
            self.put_setting(c, 'snapshot_to_day', to_day)
            self.put_setting(c, 'repliz_synced_at', iso())
            self.put_setting(c, 'sync_in_progress', False)
            return {**changes, 'generation': generation}

    def sync_external(self, schedules, from_day, to_day):
        with self.tx() as c:
            self._sync_external(c, schedules, from_day, to_day)
            self.put_setting(c, 'snapshot_generation', self.setting(c, 'snapshot_generation', 0) + 1)
            self.put_setting(c, 'snapshot_from_day', from_day)
            self.put_setting(c, 'snapshot_to_day', to_day)
            self.put_setting(c, 'repliz_synced_at', iso())
            self.put_setting(c, 'sync_in_progress', False)

    @staticmethod
    def loads(c, day, platform=None):
        result = {}
        for a in c.execute("SELECT * FROM accounts WHERE connected=1 AND eligible=1 ORDER BY ordinal"):
            if platform and a["platform"] != platform:
                continue
            local = c.execute("SELECT count(*) FROM jobs WHERE account_id=? AND slot_date=? AND counts_quota=1",
                              (a["id"], day)).fetchone()[0]
            external = c.execute("""SELECT count(*) FROM external_schedules e WHERE account_id=? AND slot_date=?
                AND NOT EXISTS(SELECT 1 FROM jobs j WHERE (j.schedule_id=e.id OR j.id=e.job_id)
                    AND j.account_id=e.account_id AND j.slot_date=e.slot_date AND j.counts_quota=1)""",
                (a["id"], day)).fetchone()[0]
            result[a["id"]] = (dict(a), local + external)
        return result

    def allocate(self, c, job_id, start_day):
        if not self._snapshot_fresh(c):
            return False
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job["status"] not in PREPARE or job["schedule_id"] or json.loads(job["payload"]).get("submission"):
            return False
        platform = job["platform"]
        day = start_day
        for _ in range(366):
            if not (self.setting(c, 'snapshot_from_day', '') <= day.isoformat() <= self.setting(c, 'snapshot_to_day', '')):
                return False
            loads = self.loads(c, day.isoformat(), platform)
            eligible = [(a, count, self._available_time(c, a, day)) for a, count in loads.values() if count < self.daily_limit]
            eligible = [(a, count, stamp) for a, count, stamp in eligible if stamp is not None]
            if not loads:
                return False
            if eligible:
                minimum = min(count for a, count, stamp in eligible)
                cursor = self.setting(c, "cursor:" + platform, 0)
                maximum = c.execute("SELECT max(ordinal)+1 FROM accounts").fetchone()[0]
                a, _, stamp = min((v for v in eligible if v[1] == minimum),
                                  key=lambda v: (v[0]['ordinal'] - cursor) % maximum)
                c.execute('''UPDATE jobs SET account_id=?,slot_date=?,planned_at=?,snapshot_generation=?,counts_quota=1 WHERE id=?''',
                          (a['id'], day.isoformat(), iso(stamp), self.setting(c, 'snapshot_generation'), job_id))
                self.put_setting(c, "cursor:" + platform, (a["ordinal"] + 1) % maximum)
                self.event(c, "slot_reserved", job_id, account_id=a["id"], day=day.isoformat())
                return True
            day += timedelta(days=1)
        raise RuntimeError("Antrean melampaui kapasitas satu tahun.")

    def _available_time(self, c, account, day, ignore_job=None):
        occupied = {r[0] for r in c.execute('''SELECT planned_at FROM jobs WHERE account_id=? AND slot_date=?
            AND counts_quota=1 AND id!=? AND planned_at IS NOT NULL''', (account['id'], day.isoformat(), ignore_job or ''))}
        occupied.update(r[0] for r in c.execute('SELECT scheduled_at FROM external_schedules WHERE account_id=? AND slot_date=?',
                                               (account['id'], day.isoformat())) if r[0])
        earliest = now() + timedelta(seconds=60)
        if account.get('blocked_until'):
            earliest = max(earliest, parse_time(account['blocked_until']))
        for stamp in daily_slots(day, self.daily_limit, account['slot_offset'], self.window_start, self.window_end):
            if stamp >= earliest and iso(stamp) not in occupied:
                return stamp
        return None

    def enqueue(self, chat_id, user_id, message_id, urls, start_day=None, can_allocate=True, progress=False, invalid=0,
                required_destinations=None):
        destinations = tuple(PLATFORMS if required_destinations is None else required_destinations)
        if not destinations or len(set(destinations)) != len(destinations) or set(destinations) - set(PLATFORMS):
            raise ValueError('Tujuan wajib media tidak valid.')
        day = start_day or now().astimezone(ZONE).date()
        with self.tx() as c:
            old = c.execute("SELECT id FROM batches WHERE chat_id=? AND message_id=?", (chat_id, message_id)).fetchone()
            if old:
                return {"batch_id": old[0], "replayed": True, "accepted": 0, "duplicates": 0}
            batch_id = uuid.uuid4().hex
            c.execute("INSERT INTO batches VALUES(?,?,?,?,?)", (batch_id, chat_id, user_id, message_id, iso()))
            c.execute('INSERT INTO batch_routes VALUES(?,?)',
                      (batch_id, user_id if chat_id < 0 and user_id > 0 else chat_id))
            accepted = duplicates = 0
            for url, source_id in urls:
                if (c.execute("SELECT 1 FROM assets WHERE source_id=?", (source_id,)).fetchone()
                        or c.execute('SELECT 1 FROM jobs WHERE source_id=? LIMIT 1', (source_id,)).fetchone()):
                    duplicates += 1
                    continue
                asset_id = uuid.uuid4().hex
                c.execute("INSERT INTO assets(id,source_id,url,created_at,updated_at,required_destinations) VALUES(?,?,?,?,?,?)",
                          (asset_id, source_id, url, iso(), iso(), json.dumps(destinations)))
                for platform in destinations:
                    jid = uuid.uuid4().hex
                    c.execute("""INSERT INTO jobs(id,batch_id,source_id,url,asset_id,platform,next_attempt,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""", (jid, batch_id, source_id, url, asset_id, platform, iso(), iso(), iso()))
                    if can_allocate:
                        self.allocate(c, jid, day)
                    self.event(c, "accepted", jid, batch_id=batch_id, source_id=source_id, platform=platform, asset_id=asset_id)
                accepted += 1
            if progress:
                c.execute("INSERT INTO batch_progress(batch_id,receipt) VALUES(?,?)",
                          (batch_id, json.dumps({"duplicates": duplicates, "invalid": invalid})))
            return {"batch_id": batch_id, "replayed": False, "accepted": accepted, "duplicates": duplicates}

    def asset(self, asset_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            return self.decode(row) if row else None

    def hold_asset(self, asset_id, reason):
        with self.tx() as c:
            asset = c.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
            if not asset:
                raise ValueError('Media tidak ditemukan.')
            payload = json.loads(asset['payload'])
            payload['hold_reason'] = safe_error(reason)
            c.execute("UPDATE assets SET status='needs_review',payload=?,leased_job_id=NULL,updated_at=? WHERE id=?",
                      (json.dumps(payload, ensure_ascii=False), iso(), asset_id))
            for job in c.execute('''SELECT * FROM jobs WHERE asset_id=?
                AND status IN ('queued','downloaded','analyzed','rendered','drive_ready')
                AND schedule_id IS NULL AND json_extract(payload,'$.submission') IS NULL''', (asset_id,)).fetchall():
                c.execute("UPDATE jobs SET status='needs_review',counts_quota=0,leased=0,last_error=?,updated_at=? WHERE id=?",
                          (safe_error(reason), iso(), job['id']))
                c.execute('UPDATE batch_progress SET done=0 WHERE batch_id=?', (job['batch_id'],))
                self.event(c, 'status_changed', job['id'], previous=job['status'], status='needs_review', reason=safe_error(reason))

    def fail_asset(self, asset_id, error, *, max_attempts=4, kind='transient'):
        with self.tx() as c:
            asset = c.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
            if not asset or asset['status'] not in PREPARE - {'drive_ready'}:
                return
            payload = json.loads(asset['payload'])
            attempts = payload.get('failure_attempts', 0) + 1
            terminal = isinstance(error, ValueError) or attempts >= max_attempts
            message = safe_error(error)
            payload.update(failure_attempts=attempts, failure_kind=kind, failure_reason=message,
                           resume_status=asset['status'])
            c.execute('UPDATE assets SET status=?,payload=?,leased_job_id=NULL,updated_at=? WHERE id=?',
                      ('failed' if terminal else asset['status'], json.dumps(payload, ensure_ascii=False), iso(), asset_id))
            retry_at = iso(now() + timedelta(seconds=min(1800, 30 * 2 ** attempts)))
            for row in c.execute("""SELECT * FROM jobs WHERE asset_id=? AND status IN
                ('queued','downloaded','analyzed','rendered') AND schedule_id IS NULL
                AND json_extract(payload,'$.submission') IS NULL""", (asset_id,)).fetchall():
                data = json.loads(row['payload'])
                data.update(failure_kind=kind, failure_reason=message, resume_status=asset['status'])
                state = 'failed' if terminal else row['status']
                c.execute("""UPDATE jobs SET status=?,payload=?,attempts=?,next_attempt=?,leased=0,
                    last_error=?,counts_quota=CASE WHEN ? THEN 0 ELSE counts_quota END,updated_at=? WHERE id=?""",
                    (state, json.dumps(data, ensure_ascii=False), attempts, retry_at, message, terminal, iso(), row['id']))
                c.execute('UPDATE batch_progress SET done=0 WHERE batch_id=?', (row['batch_id'],))
                if terminal:
                    self.event(c, 'status_changed', row['id'], previous=row['status'], status=state)
            self.event(c, 'asset_failed' if terminal else 'asset_retry_waiting', asset_id=asset_id,
                       stage=asset['status'], attempt=attempts, kind_code=kind, error=message)

    def defer_asset(self, asset_id, reason, seconds=60):
        with self.tx() as c:
            c.execute('UPDATE assets SET leased_job_id=NULL WHERE id=?', (asset_id,))
            c.execute("""UPDATE jobs SET next_attempt=?,leased=0,last_error=? WHERE asset_id=?
                AND status IN ('queued','downloaded','analyzed','rendered')
                AND schedule_id IS NULL AND json_extract(payload,'$.submission') IS NULL""",
                (iso(now() + timedelta(seconds=seconds)), safe_error(reason), asset_id))

    def asset_checkpoint(self, asset_id, status=None, payload=None):
        with self.tx() as c:
            old = c.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not old:
                raise ValueError("Media tidak ditemukan.")
            state = status or old["status"]
            if state not in PREPARE | {"duplicate"}:
                raise ValueError("Status media tidak valid.")
            merged = json.loads(old["payload"])
            merged.update(payload or {})
            if status and state != old['status']:
                merged['failure_attempts'] = 0
            encoded = json.dumps(merged, ensure_ascii=False)
            c.execute("UPDATE assets SET status=?,payload=?,updated_at=? WHERE id=?", (state, encoded, iso(), asset_id))
            for job in c.execute("SELECT * FROM jobs WHERE asset_id=?", (asset_id,)).fetchall():
                if job["status"] not in PREPARE:
                    continue
                delivery_payload = json.loads(job['payload'])
                delivery_payload.update(payload or {})
                c.execute("""UPDATE jobs SET status=?,payload=?,updated_at=?,
                    attempts=CASE WHEN ? THEN 0 ELSE attempts END WHERE id=?""",
                    (state, json.dumps(delivery_payload, ensure_ascii=False), iso(), bool(status and state != old["status"]), job["id"]))
                if state == "duplicate":
                    c.execute("UPDATE jobs SET counts_quota=0,leased=0 WHERE id=?", (job["id"],))
                    c.execute('UPDATE assets SET leased_job_id=NULL WHERE id=?', (asset_id,))
                c.execute("UPDATE batch_progress SET done=0 WHERE batch_id=?", (job["batch_id"],))
                if job["status"] != state:
                    self.event(c, "status_changed", job["id"], previous=job["status"], status=state, asset_id=asset_id)

    def job(self, job_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self._hydrate(c, row) if row else None

    def asset_deliveries(self, asset_id):
        with self.connect() as c:
            return [self._hydrate(c, row) for row in c.execute(
                'SELECT * FROM jobs WHERE asset_id=? ORDER BY rowid', (asset_id,)).fetchall()]

    def _hydrate(self, c, row):
        job = self.decode(row)
        asset = c.execute('SELECT payload FROM assets WHERE id=?', (job['asset_id'],)).fetchone()
        if asset:
            job['payload'] = {**json.loads(asset['payload']), **job['payload']}
        return job

    def recent_captions(self, exclude_job_id, limit=8):
        with self.connect() as c:
            rows = c.execute("""SELECT json_extract(payload,'$.generated') AS generated FROM assets
                WHERE id != ? AND status != 'duplicate'
                AND json_type(payload,'$.generated.caption') = 'text'
                ORDER BY created_at DESC, rowid DESC LIMIT ?""", (exclude_job_id, min(8, max(0, limit)))).fetchall()
        return [{k: json.loads(r["generated"]).get(k, "") for k in ("hook", "caption")} for r in rows]

    @staticmethod
    def decode(row):
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        if 'required_destinations' in value:
            value['required_destinations'] = json.loads(value['required_destinations'])
        return value

    def find_job(self, prefix):
        if len(prefix) < 6 or not all(x in "0123456789abcdef" for x in prefix):
            return None
        with self.connect() as c:
            rows = c.execute("SELECT * FROM jobs WHERE id LIKE ? LIMIT 2", (prefix + "%",)).fetchall()
        return self.decode(rows[0]) if len(rows) == 1 else None

    def claim(self):
        with self.tx() as c:
            if self.setting(c, "paused", False):
                return None
            row = c.execute("""SELECT j.*,b.user_id AS sender FROM jobs j JOIN assets a ON a.id=j.asset_id
                JOIN batches b ON b.id=j.batch_id LEFT JOIN sender_fairness f ON f.user_id=b.user_id WHERE j.status IN
                ('queued','downloaded','analyzed','rendered','drive_ready')
                AND j.leased=0 AND j.next_attempt<=? AND
                (j.status='drive_ready' OR a.leased_job_id IS NULL)
                AND (j.status!='drive_ready' OR NOT EXISTS(SELECT 1 FROM accounts target
                    WHERE target.id=j.account_id AND target.blocked_until>?))
                ORDER BY coalesce(f.last_claimed,''),j.created_at,j.rowid LIMIT 1""", (iso(), iso())).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET leased=1 WHERE id=?", (row["id"],))
            c.execute('INSERT OR REPLACE INTO sender_fairness VALUES(?,?)', (row['sender'], iso()))
            if row['status'] != 'drive_ready':
                c.execute('UPDATE assets SET leased_job_id=? WHERE id=?', (row['id'], row['asset_id']))
            return self.decode(row)

    def ensure_slot(self, job_id, day):
        with self.tx() as c:
            job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job["status"] not in PREPARE or job["schedule_id"] or json.loads(job["payload"]).get("submission"):
                return job["slot_date"]
            if not self._snapshot_fresh(c):
                return None
            a = c.execute("SELECT * FROM accounts WHERE id=?", (job["account_id"],)).fetchone()
            valid = (a and a['connected'] and a['eligible'] and a['platform'] == job['platform']
                     and job['slot_date'] and job['slot_date'] >= day.isoformat()
                     and job['planned_at'] and parse_time(job['planned_at']) >= now() + timedelta(seconds=60))
            if valid and job["slot_date"] > day.isoformat():
                valid = not any(n < self.daily_limit and self._available_time(c, other, day) is not None
                                for other, n in self.loads(c, day.isoformat(), job['platform']).values())
            if valid:
                valid = self.loads(c, job["slot_date"])[job["account_id"]][1] <= self.daily_limit
            if valid:
                c.execute('UPDATE jobs SET snapshot_generation=? WHERE id=?', (self.setting(c, 'snapshot_generation'), job_id))
                return job["slot_date"]
            payload = json.loads(job['payload'])
            for key in ('persona', 'persona_version', 'delivery_generated', 'delivery_metadata'):
                payload.pop(key, None)
            c.execute("UPDATE jobs SET counts_quota=0,account_id=NULL,slot_date=NULL,planned_at=NULL,payload=? WHERE id=?",
                      (json.dumps(payload, ensure_ascii=False), job_id))
            if self.allocate(c, job_id, day):
                return c.execute("SELECT slot_date FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            return None

    def checkpoint(self, job_id, status=None, payload=None, **updates):
        allowed = {"attempts", "next_attempt", "last_error", "schedule_id", "counts_quota", "leased", "slot_date", "planned_at"}
        if set(updates) - allowed:
            raise ValueError("Unknown job column")
        with self.tx() as c:
            old = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not old:
                raise ValueError("Unknown job")
            values = {"updated_at": iso(), **updates}
            if status is not None:
                values["status"] = status
            if payload is not None:
                merged = json.loads(old["payload"])
                merged.update(payload)
                values["payload"] = json.dumps(merged, ensure_ascii=False)
            c.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", (*values.values(), job_id))
            if updates.get('leased') == 0:
                c.execute('UPDATE assets SET leased_job_id=NULL WHERE leased_job_id=?', (job_id,))
            if status in {'submit_unknown', 'scheduled', 'published', 'failed'}:
                c.execute('''UPDATE submission_attempts SET status=?,updated_at=? WHERE job_id=?
                    AND id=(SELECT max(id) FROM submission_attempts WHERE job_id=?)''', (status, iso(), job_id, job_id))
            c.execute("UPDATE batch_progress SET done=0 WHERE batch_id=?", (old["batch_id"],))
            if status and status != old["status"]:
                self.event(c, "status_changed", job_id, previous=old["status"], status=status)

    def release_asset_lease(self, job_id):
        with self.tx() as c:
            c.execute('UPDATE assets SET leased_job_id=NULL WHERE leased_job_id=?', (job_id,))

    def begin_submission(self, job_id, body, generation):
        with self.tx() as c:
            if not self._snapshot_fresh(c) or generation != self.setting(c, 'snapshot_generation', 0):
                raise ConfigurationError('Snapshot berubah/kedaluwarsa sebelum intent; ulang preflight.')
            job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job or job['status'] != 'drive_ready' or not job['leased'] or job['schedule_id']:
                raise ConfigurationError('Delivery tidak siap atau sudah memiliki intent/jadwal.')
            payload = json.loads(job['payload'])
            if payload.get('submission'):
                raise ConfigurationError('Hasil pengiriman sebelumnya harus direkonsiliasi.')
            account = c.execute('SELECT * FROM accounts WHERE id=?', (job['account_id'],)).fetchone()
            if not account or not account['connected'] or not account['eligible'] or account['platform'] != job['platform']:
                raise ConfigurationError('Akun tidak lagi eligible.')
            if not c.execute("""SELECT 1 FROM account_capabilities WHERE account_id=?
                AND operation='publish' AND state='ready' AND expires_at>?""", (job['account_id'],iso())).fetchone():
                raise ConfigurationError('Bukti kemampuan posting belum tersedia atau kedaluwarsa.')
            if account['blocked_until'] and parse_time(account['blocked_until']) > now():
                raise ConfigurationError('Akun menunggu batas provider sampai ' + account['blocked_until'])
            if body.get('accountId') != job['account_id'] or not job['counts_quota'] or not job['planned_at']:
                raise ConfigurationError('Tujuan atau reservasi berubah sebelum intent.')
            if not (self.setting(c, 'snapshot_from_day', '') <= job['slot_date'] <= self.setting(c, 'snapshot_to_day', '')):
                raise ConfigurationError('Tanggal tujuan berada di luar snapshot kuota.')
            stamp = parse_time(body.get('scheduleAt'))
            if (stamp != parse_time(job['planned_at']) or stamp.astimezone(ZONE).date().isoformat() != job['slot_date']
                    or stamp <= now()):
                raise ConfigurationError('Waktu jadwal tidak cocok atau sudah lewat.')
            if self.loads(c, job['slot_date'], job['platform'])[job['account_id']][1] > self.daily_limit:
                raise ConfigurationError('Kuota berubah sebelum intent.')
            if self.setting(c, 'paused', False):
                raise ConfigurationError('Antrean dijeda sebelum intent.')
            payload.update(submission=body, submitted_at=iso(), submission_generation=generation)
            c.execute('''INSERT INTO submission_attempts(job_id,generation,body,status,created_at,updated_at)
                VALUES(?,?,?,'submitting',?,?)''', (job_id, generation, json.dumps(body, ensure_ascii=False), iso(), iso()))
            c.execute("UPDATE jobs SET status='submitting',payload=?,snapshot_generation=?,updated_at=? WHERE id=?",
                      (json.dumps(payload, ensure_ascii=False), generation, iso(), job_id))
            self.event(c, 'status_changed', job_id, previous='drive_ready', status='submitting')
            return self.decode(c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def prepare_submission(self, job_id, body, expected_generation, expected_account_id=None):
        if expected_account_id is not None and body.get('accountId') != expected_account_id:
            raise ConfigurationError('Akun preflight berubah.')
        return self.begin_submission(job_id, body, expected_generation)

    def validate_submission(self, job_id, body, generation):
        with self.connect() as c:
            c.execute('BEGIN')
            if not self._snapshot_fresh(c) or generation != self.setting(c, 'snapshot_generation'):
                raise ConfigurationError('Snapshot berubah saat menunggu pengiriman; ulang pemeriksaan.')
            job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job or job['status'] != 'submitting' or not job['leased'] or job['schedule_id']:
                raise ConfigurationError('Intent kiriman berubah sebelum request dikirim.')
            payload = json.loads(job['payload'])
            if payload.get('submission') != body or payload.get('submission_generation') != generation:
                raise ConfigurationError('Isi intent berubah sebelum request dikirim.')
            pilot = self.setting(c, 'pilot_source_id')
            if pilot and pilot != job['source_id']:
                raise ConfigurationError('Pilihan pilot berubah sebelum pengiriman.')
            account = c.execute('SELECT * FROM accounts WHERE id=?', (job['account_id'],)).fetchone()
            if (not account or not account['connected'] or not account['eligible']
                    or account['platform'] != job['platform'] or body.get('accountId') != account['id']):
                raise ConfigurationError('Akun tidak lagi siap untuk dikirim.')
            if not c.execute("""SELECT 1 FROM account_capabilities WHERE account_id=?
                AND operation='publish' AND state='ready' AND expires_at>?""", (job['account_id'],iso())).fetchone():
                raise ConfigurationError('Bukti kemampuan posting kedaluwarsa saat menunggu limiter.')
            if account['blocked_until'] and parse_time(account['blocked_until']) > now():
                raise ConfigurationError('Akun sedang menunggu batas provider.')
            if (not job['counts_quota'] or not job['planned_at']
                    or parse_time(job['planned_at']) != parse_time(body.get('scheduleAt'))
                    or parse_time(job['planned_at']) <= now()
                    or not self.setting(c, 'snapshot_from_day', '') <= job['slot_date'] <= self.setting(c, 'snapshot_to_day', '')
                    or self.loads(c, job['slot_date'], job['platform'])[account['id']][1] > self.daily_limit):
                raise ConfigurationError('Jadwal atau kuota berubah sebelum pengiriman.')

    def reject_submission(self, job_id, error=''):
        with self.tx() as c:
            job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job or job['status'] != 'submitting' or job['schedule_id']:
                return False
            payload = json.loads(job['payload'])
            payload.update(submission=None, submitted_at=None)
            c.execute("UPDATE jobs SET status='drive_ready',payload=?,last_error=?,updated_at=? WHERE id=?",
                      (json.dumps(payload, ensure_ascii=False), safe_error(error), iso(), job_id))
            c.execute("UPDATE submission_attempts SET status='rejected',error=?,updated_at=? WHERE job_id=? AND status='submitting'",
                      (safe_error(error), iso(), job_id))
            self.event(c, 'submission_rejected', job_id)
            return True

    def _record_remote(self, c, job, schedule):
        sid = schedule.get('id', schedule.get('_id')) or job['schedule_id']
        if self._remote_account(schedule) != job['account_id'] or not sid or (job['schedule_id'] and job['schedule_id'] != sid):
            raise ValueError('Identitas jadwal remote tidak cocok dengan delivery.')
        stamp = self._remote_time(schedule)
        day = stamp.astimezone(ZONE).date().isoformat()
        if schedule.get('status') not in {'pending', 'process', 'success', 'error'}:
            raise ValueError('Status jadwal tidak dikenal.')
        c.execute('UPDATE jobs SET slot_date=?,planned_at=?,counts_quota=1,schedule_id=?,updated_at=? WHERE id=?',
                  (day, iso(stamp), sid, iso(), job['id']))
        if job['status'] == 'failed' and schedule['status'] in {'pending', 'process', 'success'}:
            payload = json.loads(job['payload'])
            payload['next_poll'] = ''
            c.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), job['id']))
        if job['slot_date'] != day:
            self.event(c, 'remote_slot_reconciled', job['id'], previous_day=job['slot_date'], day=day, schedule_id=sid)

    def apply_remote_schedule(self, job_id, schedule):
        with self.tx() as c:
            job = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job:
                raise ValueError('Delivery tidak ditemukan.')
            self._record_remote(c, job, schedule)
            sid = schedule.get('id', schedule.get('_id')) or job['schedule_id']
            # A known schedule may have moved outside the last snapshot range.
            c.execute('UPDATE external_schedules SET slot_date=?,scheduled_at=?,status=?,job_id=? WHERE id=?',
                      (self._remote_time(schedule).astimezone(ZONE).date().isoformat(), iso(self._remote_time(schedule)),
                       schedule['status'], job_id, sid))
            return self.decode(c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def pending_publications(self):
        with self.connect() as c:
            return [self.decode(r) for r in c.execute("""SELECT * FROM jobs WHERE
                (status IN ('submitting','submit_unknown','scheduled') OR (status='failed' AND schedule_id IS NOT NULL))
                AND coalesce(json_extract(payload,'$.next_poll'),'')<=?
                ORDER BY coalesce(json_extract(payload,'$.next_poll'),''),updated_at LIMIT 100""", (iso(),))]

    def duplicate_hash(self, job_id, digest):
        with self.connect() as c:
            own = c.execute('SELECT asset_id FROM jobs WHERE id=?', (job_id,)).fetchone()
            asset_id = own[0] if own else job_id
            return c.execute("""SELECT a.id FROM assets a WHERE a.id!=? AND
                (json_extract(a.payload,'$.source_sha256')=? OR EXISTS(
                    SELECT 1 FROM jobs j WHERE j.asset_id=a.id AND json_extract(j.payload,'$.source_sha256')=?))
                AND (a.status!='duplicate' OR EXISTS(SELECT 1 FROM jobs j WHERE j.asset_id=a.id
                    AND (j.schedule_id IS NOT NULL OR json_extract(j.payload,'$.submission') IS NOT NULL)))
                LIMIT 1""", (asset_id, digest, digest)).fetchone() is not None

    def accounts(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM accounts ORDER BY ordinal")]

    def summary(self):
        day = now().astimezone(ZONE).date().isoformat()
        with self.connect() as c:
            counts = dict(c.execute("SELECT status,count(*) FROM jobs GROUP BY status").fetchall())
            loads = self.loads(c, day)
            platforms = {p: {"accounts": sum(a["platform"] == p for a, n in loads.values()),
                             "daily_capacity": self.daily_limit * sum(a["platform"] == p for a, n in loads.values()),
                             "reserved_or_published": sum(n for a, n in loads.values() if a["platform"] == p)} for p in PLATFORMS}
            return {"date": day, "accounts": len(loads), "reserved_or_published": sum(n for a, n in loads.values()),
                    "daily_capacity": len(loads) * self.daily_limit, "daily_limit": self.daily_limit, "platforms": platforms,
                    "paired_daily_capacity": min(p["daily_capacity"] for p in platforms.values()),
                    "reels": c.execute("SELECT count(*) FROM assets").fetchone()[0],
                    "statuses": counts, "paused": self.setting(c, "paused", False),
                    "schema_version": SCHEMA_VERSION, "snapshot_generation": self.setting(c, 'snapshot_generation', 0),
                    "snapshot_fresh": self._snapshot_fresh(c)}

    def queue(self, limit=20):
        with self.connect() as c:
            return [dict(r) for r in c.execute("""SELECT j.id,j.status,j.platform,j.slot_date,a.name AS username,j.last_error
                FROM jobs j LEFT JOIN accounts a ON a.id=j.account_id
                WHERE j.status NOT IN ('published','cancelled','duplicate') ORDER BY j.created_at LIMIT ?""", (limit,))]

    def cancel(self, job_id):
        with self.tx() as c:
            j = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not j or j["status"] not in PREPARE or j["leased"]:
                return False
            if j['schedule_id'] or json.loads(j['payload']).get('submission'):
                return False
            c.execute("UPDATE jobs SET status='cancelled',counts_quota=0,updated_at=? WHERE id=?", (iso(), job_id))
            c.execute("UPDATE batch_progress SET done=0 WHERE batch_id=?", (j["batch_id"],))
            self.event(c, "cancelled", job_id)
            return True

    def retry(self, job_id):
        with self.tx() as c:
            j = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not j or j["status"] != "failed" or j["schedule_id"]:
                return False
            p = json.loads(j["payload"])
            if p.get("submission"):
                return False
            asset = c.execute('SELECT * FROM assets WHERE id=?', (j['asset_id'],)).fetchone()
            shared = json.loads(asset['payload'])
            if asset['leased_job_id'] or shared.get('failure_kind') == 'source':
                return False
            stage = asset['status'] if asset['status'] in PREPARE else shared.get('resume_status', p.get('resume_status', 'queued'))
            if stage not in PREPARE:
                return False
            if asset['status'] == 'failed':
                shared.update(failure_attempts=0, failure_reason=None, failure_kind=None)
                c.execute('UPDATE assets SET status=?,payload=?,updated_at=? WHERE id=?',
                          (stage, json.dumps(shared, ensure_ascii=False), iso(), asset['id']))
            p.update(resume_status=stage, failure_kind=None, failure_reason=None)
            c.execute("""UPDATE jobs SET status=?,attempts=0,next_attempt=?,updated_at=?,last_error=NULL,
                payload=?,counts_quota=0,account_id=NULL,slot_date=NULL,planned_at=NULL WHERE id=?""",
                      (stage, iso(), iso(), json.dumps(p, ensure_ascii=False), job_id))
            self.allocate(c, job_id, now().astimezone(ZONE).date())
            c.execute("UPDATE batch_progress SET done=0 WHERE batch_id=?", (j["batch_id"],))
            self.event(c, "manual_retry", job_id)
            return True

    def notify(self, key, chat_id, text, options=None):
        if chat_id == 0:
            return
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO outbox(dedupe_key,chat_id,text,created_at,options) VALUES(?,?,?,?,?)",
                      (key, chat_id, text, iso(), json.dumps(options or {})))

    def notifications(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT 10")]

    def mark_notified(self, item_id, delivered=True):
        with self.tx() as c:
            c.execute("UPDATE outbox SET sent=? WHERE id=?", (1 if delivered else -1, item_id))

    def account_username(self, account_id):
        with self.connect() as c:
            row = c.execute("SELECT CASE WHEN platform='tiktok' THEN username ELSE coalesce(nullif(name,''),username) END FROM accounts WHERE id=?", (account_id,)).fetchone()
            return row[0] if row else ""

    def publications_to_report(self, limit=20):
        with self.connect() as c:
            return [self.decode(r) for r in c.execute("""SELECT j.*,
                CASE WHEN a.platform='tiktok' THEN a.username ELSE coalesce(nullif(a.name,''),a.username) END AS username FROM jobs j
                JOIN accounts a ON a.id=j.account_id JOIN batches b ON b.id=j.batch_id
                WHERE j.status='published' AND j.schedule_id IS NOT NULL AND b.chat_id!=0
                AND coalesce(json_extract(j.payload,'$.share_next_poll'),'')<=?
                AND NOT EXISTS(SELECT 1 FROM outbox WHERE dedupe_key='published:'||j.id)
                ORDER BY j.updated_at LIMIT ?""", (iso(), limit))]

    def notify_publication(self, job_id):
        with self.tx() as c:
            row = c.execute("""SELECT j.*,coalesce(r.report_chat_id,CASE WHEN b.chat_id>0 THEN b.chat_id ELSE 0 END) AS chat_id
                FROM jobs j JOIN batches b ON b.id=j.batch_id LEFT JOIN batch_routes r ON r.batch_id=b.id
                WHERE j.id=? AND j.status='published' AND b.chat_id!=0""", (job_id,)).fetchone()
            if not row or row['chat_id'] == 0:
                return False
            payload = json.loads(row["payload"])
            if not payload.get("post_url"):
                return False
            from .telegram_views import publication
            text, options = publication(self.decode(row))
            inserted = c.execute("INSERT OR IGNORE INTO outbox(dedupe_key,chat_id,text,created_at,options) VALUES(?,?,?,?,?)",
                                 ("published:" + job_id, row["chat_id"], text, iso(), json.dumps(options))).rowcount
            if inserted:
                self.event(c, "publication_link_queued", job_id, post_url=payload["post_url"])
            return bool(inserted)

    def batch_reports(self):
        with self.connect() as c:
            batches = c.execute("""SELECT b.* FROM batches b WHERE b.chat_id!=0 AND EXISTS(SELECT 1 FROM jobs WHERE batch_id=b.id)
                AND NOT EXISTS(SELECT 1 FROM jobs WHERE batch_id=b.id AND status NOT IN ('published','failed','cancelled','duplicate'))
                AND NOT EXISTS(SELECT 1 FROM batch_progress WHERE batch_id=b.id)
                AND NOT EXISTS(SELECT 1 FROM outbox WHERE dedupe_key='batch-done:'||b.id)""").fetchall()
            for original in batches:
                b = dict(original)
                route = c.execute('SELECT report_chat_id FROM batch_routes WHERE batch_id=?', (b['id'],)).fetchone()
                b['chat_id'] = route[0] if route else max(0, b['chat_id'])
                if b['chat_id'] <= 0:
                    continue
                totals = dict(c.execute("SELECT status,count(*) FROM jobs WHERE batch_id=? GROUP BY status", (b["id"],)).fetchall())
                from .telegram_views import LABELS
                text = f"Batch {b['id'][:8]} selesai. " + ", ".join(f"{LABELS.get(k, k)}: {v}" for k, v in totals.items())
                c.execute("INSERT OR IGNORE INTO outbox(dedupe_key,chat_id,text,created_at) VALUES(?,?,?,?)",
                          ("batch-done:" + b["id"], b["chat_id"], text, iso()))

    def track_active_batches(self):
        with self.tx() as c:
            c.execute("""INSERT OR IGNORE INTO batch_progress(batch_id)
                SELECT DISTINCT j.batch_id FROM jobs j JOIN batches b ON b.id=j.batch_id
                WHERE b.chat_id!=0 AND j.status NOT IN ('published','failed','cancelled','duplicate')""")

    def has_progress(self, batch_id):
        with self.connect() as c:
            return c.execute("SELECT 1 FROM batch_progress WHERE batch_id=?", (batch_id,)).fetchone() is not None

    def progress_due(self):
        with self.connect() as c:
            return [r[0] for r in c.execute("SELECT batch_id FROM batch_progress WHERE done=0 AND next_poll<=? ORDER BY next_poll LIMIT 20", (iso(),))]

    def progress_snapshot(self, batch_id):
        with self.tx() as c:
            row = c.execute("""SELECT b.*,p.message_id AS progress_message_id,p.signature,p.page,p.receipt
                FROM batches b JOIN batch_progress p ON p.batch_id=b.id WHERE b.id=?""", (batch_id,)).fetchone()
            if not row:
                return None
            batch = dict(row)
            route = c.execute('SELECT report_chat_id FROM batch_routes WHERE batch_id=?', (batch_id,)).fetchone()
            batch['report_chat_id'] = route[0] if route else max(0, batch['chat_id'])
            batch["receipt"] = json.loads(batch["receipt"])
            jobs = [self.decode(r) for r in c.execute("""SELECT j.*,a.name AS username FROM jobs j LEFT JOIN accounts a ON a.id=j.account_id
                WHERE batch_id=? ORDER BY j.created_at,j.rowid""", (batch_id,))]
            return batch, jobs

    def progress_saved(self, batch_id, message_id, signature, done, observed, page=None):
        with self.tx() as c:
            # A stage may finish while Telegram is responding. Do not close a stale snapshot.
            changed = c.execute("SELECT 1 FROM jobs WHERE batch_id=? AND updated_at>? LIMIT 1", (batch_id, observed)).fetchone()
            current = c.execute('SELECT page FROM batch_progress WHERE batch_id=?',(batch_id,)).fetchone()
            page_changed = page is not None and current and current['page'] != page
            c.execute("UPDATE batch_progress SET message_id=?,signature=?,done=?,next_poll=? WHERE batch_id=?",
                      (message_id, signature, int(done and not changed and not page_changed),
                       iso() if page_changed else iso(now() + timedelta(seconds=8)), batch_id))

    def progress_defer(self, batch_id, seconds=30, disabled=False):
        with self.tx() as c:
            c.execute("UPDATE batch_progress SET next_poll=?,done=? WHERE batch_id=?",
                      (iso(now() + timedelta(seconds=seconds)), int(disabled), batch_id))

    def progress_page(self, batch_id, page):
        with self.tx() as c:
            c.execute("UPDATE batch_progress SET page=?,done=0,next_poll=? WHERE batch_id=?", (page, iso(), batch_id))

    def batch_chat(self, job):
        with self.connect() as c:
            row = c.execute("""SELECT coalesce(r.report_chat_id,CASE WHEN b.chat_id>0 THEN b.chat_id ELSE 0 END)
                FROM batches b LEFT JOIN batch_routes r ON r.batch_id=b.id WHERE b.id=?""", (job["batch_id"],)).fetchone()
            return row[0] if row else 0

    def register_private_chat(self, user_id, chat_id):
        if type(user_id) is not int or user_id <= 0 or user_id != chat_id:
            raise ValueError('Identitas chat pribadi tidak sesuai pengirim.')
        with self.tx() as c:
            c.execute("""INSERT INTO private_chats VALUES(?,?,?) ON CONFLICT(user_id)
                DO UPDATE SET chat_id=excluded.chat_id,seen_at=excluded.seen_at""", (user_id, chat_id, iso()))
            c.execute("""UPDATE batch_progress SET done=0,next_poll='' WHERE batch_id IN
                (SELECT batch_id FROM batch_routes WHERE report_chat_id=?)
                AND (message_id IS NULL OR EXISTS(SELECT 1 FROM jobs WHERE batch_id=batch_progress.batch_id
                    AND status NOT IN ('published','failed','cancelled','duplicate')))""", (chat_id,))
            c.execute("""UPDATE outbox SET sent=0 WHERE chat_id=? AND sent=-1 AND id IN
                (SELECT json_extract(detail,'$.outbox_id') FROM events WHERE kind='telegram_notification_unreachable'
                 AND json_extract(detail,'$.recoverable')=1)""", (chat_id,))

    def batch_slots(self, batch_id):
        with self.connect() as c:
            return dict(c.execute("SELECT coalesce(slot_date,'belum dialokasikan'),count(*) FROM jobs WHERE batch_id=? GROUP BY slot_date", (batch_id,)).fetchall())

    def issue_pair_code(self):
        import hashlib
        code = secrets.token_hex(6)
        with self.tx() as c:
            self.put_setting(c, "pair_hash", hashlib.sha256(code.encode()).hexdigest())
            self.put_setting(c, "pair_expires", iso(now() + timedelta(minutes=30)))
            self.event(c, "pair_code_issued")
        return code

    def pair(self, code, user_id, chat_id):
        import hashlib
        with self.tx() as c:
            expected = self.setting(c, "pair_hash", "")
            expires = self.setting(c, "pair_expires", "")
            if not expected or expires < iso() or not secrets.compare_digest(expected, hashlib.sha256(code.encode()).hexdigest()):
                return False
            users = self.setting(c, "allowed_users", [])
            if user_id not in users:
                users.append(user_id)
            self.put_setting(c, "allowed_users", users)
            self.put_setting(c, "pair_hash", "")
            self.event(c, "telegram_paired", user_id=user_id, chat_id=chat_id)
            return True


class AssetStore:
    """Give downloader/Drive durable media checkpoints shared by both destinations."""

    def __init__(self, store):
        self.store = store

    def job(self, asset_id):
        return self.store.asset(asset_id)

    def checkpoint(self, asset_id, status=None, payload=None):
        return self.store.asset_checkpoint(asset_id, status, payload)

    def record(self, kind, job_id=None, **detail):
        return self.store.record(kind, asset_id=job_id, **detail)

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        return self.store.set(key, value)
