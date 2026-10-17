from __future__ import annotations

import copy
import json
import uuid
from datetime import timedelta

from .account_reports import AccountReports
from .config import Config
from .db import Store
from .repliz import Repliz
from .report_store import ReportStore
from .report_views import ZONE
from .util import now


async def preview(config, credentials_from=None):
    data = copy.deepcopy(config.data)
    folder = config.runtime / 'report-previews' / uuid.uuid4().hex
    folder.mkdir(parents=True)
    data['workflow']['runtime_dir'] = str(folder)
    if credentials_from:
        data['repliz']['credentials_file'] = str(credentials_from.resolve() / 'repliz.md')
    isolated = Config(config.root, data)
    store = Store(folder / 'preview.sqlite3')
    api = Repliz(isolated)
    try:
        worker = AccountReports(isolated, store, api)
        at = now()
        run = worker.db.create('preview', at.astimezone(ZONE).date().isoformat(), at, at + timedelta(minutes=10), at)
        await worker.collect(run)
        worker.freeze(worker.db.run(run['id']), now())
        with store.connect() as c:
            parts = [dict(r) for r in c.execute('SELECT part_no,text,content_hash FROM account_report_parts ORDER BY part_no')]
        evidence = {'run': worker.db.run(run['id']), 'snapshots': worker.db.rows(run['id']), 'parts': parts, 'sent': False}
        (folder / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        (folder / 'telegram.html').write_text('<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Preview laporan TikTok</title><style>body{font:16px/1.55 system-ui;max-width:430px;margin:24px auto;padding:0 16px;background:#f5f5f2;color:#222}article{white-space:pre-wrap;background:white;padding:20px;border:1px solid #ddd;margin-bottom:20px;border-radius:12px}a{color:#165b92}@media(prefers-color-scheme:dark){body{background:#17191c;color:#eee}article{background:#22262b;border-color:#444}a{color:#82c5fa}}</style>' + ''.join('<article>' + p['text'] + '</article>' for p in parts) + '</html>')
        rows = evidence['snapshots']
        return {'folder': str(folder), 'inventory_complete': bool(evidence['run']['inventory_complete']),
                'valid': sum(r['status'] == 'ok' for r in rows), 'total': len(rows), 'parts': len(parts), 'sent': False,
                'qualified': [{'username': r['username'], 'followers': r['data']['followers']} for r in rows if r['status'] == 'ok' and r['data']['followers'] > 600]}
    finally:
        await api.close()


def status(config, store):
    return {'enabled': store.get('account_report_enabled', config.data.get('account_report', {}).get('enabled', False)),
            'schedule': '08:00 Asia/Jakarta; collection 07:50; catch-up before 12:00',
            'destination': 'private admin 265732518 via @Fahra_autonomousbot',
            'health': store.get('account_report_health'), 'delivery_health': store.get('account_report_delivery_health'),
            'startup_health': store.get('startup_health'), **ReportStore(store).summary()}


async def operate(config, store, action):
    if action == 'status':
        return status(config, store)
    config.require_production_checkout()
    if action == 'disable':
        store.set('account_report_enabled', False)
    else:
        config.require_live_action()
        from .telegram import Telegram
        telegram = Telegram(config, store, None)
        try:
            worker = AccountReports(config, store, None, telegram)
            await worker.check_destination()
            worker.recover()
        finally:
            await telegram.client.aclose()
        if action == 'enable':
            if not store.get('account_report_activation_day'):
                store.set('account_report_activation_day', now().astimezone(ZONE).date().isoformat())
            store.set('account_report_enabled', True)
        elif action == 'pilot':
            if not store.get('account_report_enabled', False):
                raise ValueError('Aktifkan laporan sebelum meminta pilot.')
            store.set('account_report_pilot_day', now().astimezone(ZONE).date().isoformat())
    store.record('account_report_operator', action=action)
    return status(config, store)
