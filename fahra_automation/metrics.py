"""Optional content samples, measured since verified publication."""
import asyncio
import json
import re
import math
from datetime import timedelta

from .config import EXCLUDED_ACCOUNT_ID
from .repliz import timestamp
from .util import iso, now, safe_error

FIELDS = ('views','like','dislike','comment','share','reach','watched','favourite','newFollower')


class ContentMetrics:
    def __init__(self, config, store, api):
        self.config, self.store, self.api = config, store, api

    async def once(self):
        if not self.config.data.get('metrics', {}).get('enabled', False):
            self.store.health('content_metrics', 'waiting', reason='Sampling belum diaktifkan.')
            return 0
        if not self.store.snapshot_fresh():
            self.store.health('content_metrics', 'waiting', reason='Menunggu snapshot akun lengkap.')
            return 0
        with self.store.connect() as c:
            rows = [dict(r) for r in c.execute("""SELECT j.id,min(e.at) AS published_at FROM jobs j
                JOIN assets asset ON asset.id=j.asset_id JOIN accounts a ON a.id=j.account_id
                JOIN events e ON e.job_id=j.id AND e.kind='status_changed'
                    AND json_extract(e.detail,'$.status')='published'
                WHERE j.status='published' AND a.connected=1 AND a.id!=?
                    AND json_extract(asset.payload,'$.legacy_origin') IS NULL
                GROUP BY j.id ORDER BY published_at""", (EXCLUDED_ACCOUNT_ID,))]
        attempts, failures = 0, []
        for row in rows:
            job = self.store.job(row['id'])
            payload = job['payload']
            if payload.get('metric_retry_at', '') > iso():
                continue
            age = (now()-timestamp(row['published_at'])).total_seconds()/3600
            with self.store.connect() as c:
                sampled = {r[0] for r in c.execute('SELECT age_hours FROM content_metrics WHERE job_id=?', (job['id'],))}
            due = [hour for hour in (24,72,168) if hour <= age and hour not in sampled]
            if not due:
                continue
            post = payload.get('post_id')
            if not isinstance(post, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', post):
                continue
            hour = due[0]
            sample = {'metrics':{field:None for field in FIELDS}, 'age_basis':'verified_publication',
                      'source_id':job['source_id'],'platform':job['platform'],
                      'persona':payload.get('delivery_metadata',{}).get('persona_version'),
                      'hook':payload.get('generated',{}).get('hook'),
                      'hook_style':payload.get('hook_style'), 'status':'missed_window'}
            attempts += 1
            try:
                if age <= hour + 1:
                    body = await self.api.request('GET', '/public/content/' + post + '/statistic',
                                                  params={'accountId':job['account_id']})
                    if not isinstance(body, dict):
                        raise ValueError('Statistik konten bukan object.')
                    body = body.get('data', body)
                    if not isinstance(body, dict):
                        raise ValueError('Statistik konten tidak valid.')
                    sample['metrics'] = {key:value if type(value) in {int,float} and math.isfinite(value) and value >= 0 else None
                                         for key in FIELDS for value in [body.get(key)]}
                    sample['status'] = 'sampled'
                with self.store.tx() as c:
                    c.execute('INSERT OR IGNORE INTO content_metrics VALUES(?,?,?,?,?,?,?)',
                              (job['id'],hour,iso(),age,job['account_id'],post,json.dumps(sample,ensure_ascii=False)))
            except Exception as exc:
                failures.append(safe_error(exc))
                self.store.checkpoint(job['id'], payload={'metric_retry_at':iso(now()+timedelta(minutes=15))})
            if attempts >= 5:
                break
        self.store.health('content_metrics', 'error' if failures else 'ok', reason='; '.join(failures),
                          details={'attempted':attempts, 'failed':len(failures)})
        return attempts

    async def loop(self):
        while True:
            try:
                await self.once()
            except Exception as exc:
                self.store.health('content_metrics', 'error', reason=safe_error(exc))
            await asyncio.sleep(300)
