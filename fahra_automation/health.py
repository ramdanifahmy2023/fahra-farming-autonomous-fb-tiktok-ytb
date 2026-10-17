"""Local operational evidence; reading a report never refreshes a heartbeat."""
import json
import shutil
from datetime import datetime

from .util import iso, now, safe_error


def record(store, component, state, *, reason='', duration_ms=None, details=None):
    if state not in {'ok', 'waiting', 'error'}:
        raise ValueError('Status health tidak valid.')
    reason = safe_error(reason)[:800] if reason else ''
    with store.tx() as c:
        old = c.execute('SELECT * FROM health WHERE component=?', (component,)).fetchone()
        if (old and state in {'ok','waiting'} and old['state'] == state and old['reason'] == reason
                and (now() - datetime.fromisoformat(old['checked_at'])).total_seconds() < 30):
            return
        failures = (old['failures'] if old else 0) + 1 if state == 'error' else 0
        success = iso() if state == 'ok' else old['last_success'] if old else None
        c.execute('INSERT OR REPLACE INTO health VALUES(?,?,?,?,?,?,?,?)',
                  (component, iso(), success, state, failures, reason, duration_ms,
                   json.dumps(details or {}, ensure_ascii=False)))
        if failures == 3:
            store.event(c, 'health_alert', component=component, reason=reason)
            c.execute("""INSERT OR IGNORE INTO outbox(dedupe_key,chat_id,text,created_at,options)
                VALUES(?,265732518,?,?,'{}')""",
                ('health:' + component + ':' + (success or 'initial'),
                 f'Bot perlu diperiksa: {component}. Tiga pemeriksaan gagal. {reason}', iso()))


def report(store):
    with store.connect() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        loops = [dict(r) for r in c.execute('SELECT * FROM health ORDER BY component')] if 'health' in tables else []
        for row in loops:
            row['age_seconds'] = round((now() - datetime.fromisoformat(row['checked_at'])).total_seconds())
            row['stale'] = row['age_seconds'] > (7200 if row['component'] in {'backup', 'cleanup'} else 900)
            row['details'] = json.loads(row['details'])
        expected = {'media_worker','publication_monitor','account_sync','telegram_poll','telegram_outbox',
                    'reply_poll','reply_worker','reply_profiles','backup','cleanup'}
        loops.extend({'component': name, 'state': 'unknown', 'stale': True,
                      'reason': 'Belum ada heartbeat dari versi ini.'}
                     for name in sorted(expected - {row['component'] for row in loops}))
        groups = [dict(r) for r in c.execute("""SELECT
            CASE WHEN json_extract(a.payload,'$.legacy_origin') IS NULL THEN 'baru' ELSE 'legacy' END AS origin,
            j.status,count(*) AS deliveries,count(DISTINCT j.asset_id) AS sources,min(j.created_at) AS oldest,
            min(j.next_attempt) AS next_check FROM jobs j JOIN assets a ON a.id=j.asset_id
            GROUP BY origin,j.status""")]
        holds = [dict(r) for r in c.execute("""SELECT status,last_error,count(*) AS deliveries FROM jobs
            WHERE last_error IS NOT NULL GROUP BY status,last_error ORDER BY deliveries DESC LIMIT 20""")]
        replies = [dict(r) for r in c.execute("""SELECT e.status,a.platform,e.reason,count(*) AS comments,
            min(e.created_at) AS oldest FROM comment_events e LEFT JOIN accounts a ON a.id=e.account_id
            GROUP BY e.status,a.platform,e.reason ORDER BY comments DESC LIMIT 30""")] if 'comment_events' in tables else []
        capabilities = [dict(r) for r in c.execute('SELECT account_id,operation,state,evidence,expires_at,reason FROM account_capabilities')] if 'account_capabilities' in tables else []
        usage = [dict(r) for r in c.execute("""SELECT substr(at,1,10) AS utc_day,operation,model,count(*) AS calls,
            sum(total_tokens) AS reported_tokens,sum(total_tokens IS NULL) AS missing_usage
            FROM ai_usage GROUP BY utc_day,operation,model""")] if 'ai_usage' in tables else []
        timings = [dict(r) for r in c.execute('SELECT *,total_ms/samples AS mean_ms FROM timings')] if 'timings' in tables else []
    return {'at': iso(), 'snapshot_fresh': store.snapshot_fresh(), 'loops': loops,
            'work': groups, 'holds': holds, 'replies': replies, 'account_capabilities': capabilities,
            'ai_usage': usage, 'timings': timings, 'disk_free_bytes': shutil.disk_usage(store.path.parent).free,
            'next_action': 'Periksa loop stale/error dan alasan hold. Reconcile hasil ambigu sebelum retry.'}
