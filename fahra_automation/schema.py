"""Versioned additions to the operational database; legacy imports remain separate."""

VERSION = 3


def upgrade(connection, version):
    if version == VERSION:
        return
    if version not in {0, 1, 2}:
        raise ValueError('Versi database belum didukung.')
    if version < 2:
        _upgrade_v2(connection, version)
    connection.execute('BEGIN IMMEDIATE')
    try:
        connection.execute('CREATE INDEX IF NOT EXISTS external_account_day ON external_schedules(account_id,slot_date)')
        connection.execute('CREATE INDEX IF NOT EXISTS events_job_kind_at ON events(job_id,kind,at)')
        connection.execute('CREATE INDEX IF NOT EXISTS jobs_asset_state ON jobs(asset_id,status)')
        connection.execute('CREATE INDEX IF NOT EXISTS jobs_account_schedule ON jobs(account_id,schedule_id)')
        connection.execute("""CREATE INDEX IF NOT EXISTS jobs_published_account_post
            ON jobs(account_id,json_extract(payload,'$.post_id')) WHERE status='published'""")
        connection.execute('''CREATE TABLE IF NOT EXISTS job_waits(
            job_id TEXT PRIMARY KEY REFERENCES jobs(id), dependency TEXT NOT NULL,
            reason TEXT NOT NULL, created_at TEXT NOT NULL)''')
        connection.execute('CREATE INDEX IF NOT EXISTS job_waits_dependency ON job_waits(dependency)')
        connection.execute('''CREATE TABLE IF NOT EXISTS provider_limits(
            schedule_id TEXT NOT NULL,error_hash TEXT NOT NULL,account_id TEXT NOT NULL,
            category TEXT NOT NULL,first_seen TEXT NOT NULL,blocked_until TEXT NOT NULL,
            PRIMARY KEY(schedule_id,error_hash))''')
        connection.execute("""INSERT OR IGNORE INTO schema_migrations
            VALUES(3,strftime('%Y-%m-%dT%H:%M:%fZ','now'))""")
        connection.execute('PRAGMA user_version=3')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _upgrade_v2(connection, version):
    if version not in {0, 1}:
        raise ValueError('Versi database belum didukung.')
    connection.execute('BEGIN IMMEDIATE')
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS account_capabilities(
            account_id TEXT NOT NULL REFERENCES accounts(id), operation TEXT NOT NULL,
            state TEXT NOT NULL, evidence TEXT NOT NULL, identity TEXT NOT NULL,
            observed_at TEXT NOT NULL, expires_at TEXT NOT NULL, reason TEXT NOT NULL,
            PRIMARY KEY(account_id,operation))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS batch_routes(
            batch_id TEXT PRIMARY KEY REFERENCES batches(id), report_chat_id INTEGER NOT NULL)""")
        connection.execute("""INSERT OR IGNORE INTO batch_routes SELECT id,
            CASE WHEN chat_id>0 THEN chat_id WHEN chat_id<0 AND user_id>0 THEN user_id ELSE 0 END FROM batches""")
        connection.execute("""CREATE TABLE IF NOT EXISTS private_chats(
            user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, seen_at TEXT NOT NULL)""")
        connection.execute("""UPDATE batch_progress SET message_id=NULL,signature='',page=0,done=0,next_poll=''
            WHERE batch_id IN (SELECT id FROM batches WHERE chat_id<0)""")
        connection.execute("UPDATE outbox SET sent=-2 WHERE chat_id<0 AND sent=0")
        connection.execute("""CREATE TABLE IF NOT EXISTS sender_fairness(
            user_id INTEGER PRIMARY KEY, last_claimed TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS timings(
            name TEXT PRIMARY KEY, samples INTEGER NOT NULL, total_ms REAL NOT NULL,
            max_ms REAL NOT NULL, last_ms REAL NOT NULL, updated_at TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS health(
            component TEXT PRIMARY KEY, checked_at TEXT NOT NULL, last_success TEXT,
            state TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
            duration_ms REAL, details TEXT NOT NULL DEFAULT '{}')""")
        connection.execute("""CREATE TABLE IF NOT EXISTS ai_usage(
            id INTEGER PRIMARY KEY, at TEXT NOT NULL, operation TEXT NOT NULL, subject_id TEXT,
            model TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
            total_tokens INTEGER, duration_ms REAL NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS content_metrics(
            job_id TEXT NOT NULL REFERENCES jobs(id), age_hours INTEGER NOT NULL,
            observed_at TEXT NOT NULL, actual_age_hours REAL NOT NULL,
            account_id TEXT NOT NULL, post_id TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(job_id,age_hours))""")
        connection.execute("""INSERT OR IGNORE INTO schema_migrations
            VALUES(2,strftime('%Y-%m-%dT%H:%M:%fZ','now'))""")
        connection.execute('PRAGMA user_version=2')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
