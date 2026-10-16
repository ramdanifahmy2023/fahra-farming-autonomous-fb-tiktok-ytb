"""Legacy snapshots use SQLite backup API; import never writes the source databases."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from .config import ConfigurationError, allowed_account
from .reply_store import ReplyStore
from .scheduler import offset_for
from .util import digest_file, iso


def connect_read(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def rows(connection, table):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [dict(r) for r in connection.execute('SELECT * FROM ' + table)] if table in tables else []


def insert(connection, table, value):
    columns = {r[1] for r in connection.execute('PRAGMA table_info(' + table + ')')}
    fields = [field for field in value if field in columns]
    connection.execute('INSERT INTO ' + table + '(' + ','.join(fields) + ') VALUES(' + ','.join('?' for _ in fields) + ')', [value[field] for field in fields])


def snapshot(config, final=False):
    config.require_production_checkout()
    from .ownership import legacy_locks, legacy_services
    if final and any(item['loaded'] for item in legacy_services(config)):
        raise ConfigurationError('Snapshot final memerlukan kedua layanan lama berhenti.')
    folder = config.runtime / 'migrations' / (('final-' if final else 'rehearsal-') + uuid.uuid4().hex)
    folder.mkdir(parents=True, mode=0o700)
    manifest = {'id': uuid.uuid4().hex, 'at': iso(), 'final': final, 'sources': []}
    from contextlib import nullcontext
    with (legacy_locks(config) if final else nullcontext()):
        for ordinal, root in enumerate(config.section('workflow')['legacy_roots']):
            origin = 'tiktok' if ordinal == 0 else 'fb-ytb'
            source_path = config.path(root) / 'runtime/workflow.sqlite3'
            target = folder / (origin + '.sqlite3')
            with closing(connect_read(source_path)) as source, closing(sqlite3.connect(target)) as destination:
                source.backup(destination)
                if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Snapshot legacy gagal quick_check.')
                count = destination.execute('SELECT count(*) FROM jobs').fetchone()[0]
            os.chmod(target, 0o600)
            manifest['sources'].append({'origin': origin, 'root': str(config.path(root)), 'file': target.name,
                                        'sha256': digest_file(target), 'jobs': count})
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return folder


def import_snapshots(store, folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / 'manifest.json').read_text())
    if store.get('migration_id'):
        if store.get('migration_id') == manifest['id']:
            return store.get('migration_report')
        raise ConfigurationError('Target sudah berisi migrasi lain; gunakan database staging kosong.')
    with store.connect() as connection:
        if connection.execute('SELECT EXISTS(SELECT 1 FROM jobs) OR EXISTS(SELECT 1 FROM assets)').fetchone()[0]:
            raise ConfigurationError('Importer memerlukan database baru tanpa pekerjaan atau aset.')
    ReplyStore(store)
    loaded = []
    for source in manifest['sources']:
        path = folder / source['file']
        if path.parent != folder or digest_file(path) != source['sha256']:
            raise ValueError('Checksum/lokasi snapshot legacy tidak cocok.')
        with closing(connect_read(path)) as connection:
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Database legacy rusak.')
            data = {name: rows(connection, name) for name in ('accounts','assets','jobs','batches','events','settings',
                    'video_contexts','comment_events','reply_attempts','outbox')}
        loaded.append((source, data))
    report = {'id': manifest['id'], 'at': iso(), 'final': manifest.get('final', False), 'sources': [], 'jobs': 0, 'assets': 0,
              'reply_events': 0, 'held_outbox': 0}
    with store.tx() as connection:
        if connection.execute('SELECT EXISTS(SELECT 1 FROM jobs) OR EXISTS(SELECT 1 FROM assets)').fetchone()[0]:
            raise ConfigurationError('Target berubah saat snapshot dibaca; pekerjaan atau aset sudah tersedia.')
        # Doctor may have populated this pool; only the complete legacy import may replace it.
        connection.execute('DELETE FROM external_schedules')
        connection.execute('DELETE FROM accounts')
        store.put_setting(connection,'repliz_synced_at',None)
        store.put_setting(connection,'sync_in_progress',True)
        store.put_setting(connection,'snapshot_generation',store.setting(connection,'snapshot_generation',0)+1)
        store.put_setting(connection,'snapshot_from_day','')
        store.put_setting(connection,'snapshot_to_day','')
        connection.execute('CREATE TABLE IF NOT EXISTS legacy_mapping(origin TEXT,kind TEXT,legacy_id TEXT,new_id TEXT,PRIMARY KEY(origin,kind,legacy_id))')
        account_offset, batch_number = 0, 0
        for source, data in loaded:
            origin = source['origin']
            defaults = ['tiktok'] if origin == 'tiktok' else ['facebook','youtube']
            accounts = sorted(data['accounts'], key=lambda row: row['ordinal'])
            for account in accounts:
                account['platform'] = account.get('platform', defaults[0])
                if not allowed_account({**account,'type': account['platform']}):
                    raise ValueError('Snapshot legacy berisi akun di luar cakupan.')
                account['ordinal'] += account_offset
                account['eligible'], account['slot_offset'] = 0, offset_for(account['id'], store.daily_limit, store.window_start, store.window_end)
                insert(connection,'accounts',account)
                connection.execute('INSERT INTO legacy_mapping VALUES(?,?,?,?)',(origin,'account',account['id'],account['id']))
            source_offset = account_offset
            account_offset = max([a['ordinal'] + 1 for a in accounts] + [account_offset])
            for batch in data['batches']:
                batch_number += 1
                original = batch['id']
                batch.update(chat_id=0,user_id=0,message_id=-batch_number)
                insert(connection,'batches',batch)
                connection.execute('INSERT INTO legacy_mapping VALUES(?,?,?,?)',(origin,'batch',original,batch['id']))
            assets = data['assets'] if data['assets'] else [{k:j[k] for k in ('id','source_id','url','status','payload','created_at','updated_at')} for j in data['jobs']]
            for asset in assets:
                payload = json.loads(asset['payload'])
                payload['legacy_origin'] = origin
                payload['legacy_source_id'] = asset['source_id']
                asset.update(source_id='legacy:' + origin + ':' + asset['source_id'], payload=json.dumps(payload,ensure_ascii=False),
                             required_destinations=json.dumps(defaults), leased_job_id=None)
                insert(connection,'assets',asset)
                connection.execute('INSERT INTO legacy_mapping VALUES(?,?,?,?)',(origin,'asset',asset['id'],asset['id']))
            for job in data['jobs']:
                payload = json.loads(job['payload'])
                payload['legacy_origin'] = origin
                if job['status'] == 'submitting':
                    job['status'] = 'submit_unknown'
                job.update(platform=job.get('platform',defaults[0]),asset_id=job.get('asset_id',job['id']),leased=0,
                           payload=json.dumps(payload,ensure_ascii=False), snapshot_generation=None)
                insert(connection,'jobs',job)
                connection.execute('INSERT INTO legacy_mapping VALUES(?,?,?,?)',(origin,'job',job['id'],job['id']))
            for event in data['events']:
                detail = json.loads(event['detail'])
                event.update(detail=json.dumps({**detail,'legacy_origin':origin,'legacy_seq':event['seq']},ensure_ascii=False))
                event.pop('seq')
                insert(connection,'events',event)
            for table in ('video_contexts','comment_events','reply_attempts'):
                for row in data[table]:
                    if table == 'reply_attempts':
                        row.pop('id')
                    elif table == 'comment_events':
                        row['leased'] = 0
                        if row['status'] == 'sending':
                            row['status'] = 'send_unknown'
                        elif row['status'] == 'processing':
                            row['status'] = 'queued'
                    if table == 'reply_attempts' and row['status'] == 'sending':
                        row['status'] = 'send_unknown'
                    insert(connection,table,row)
            settings = {row['key']:json.loads(row['value']) for row in data['settings']}
            for key,value in settings.items():
                if key == 'cursor':
                    store.put_setting(connection,'cursor:tiktok',value + source_offset)
                elif key.startswith('cursor:'):
                    store.put_setting(connection,key,value + source_offset)
                elif key in {'reply_since','reply_owner_aliases','reply_account_ids','reply_account_since'}:
                    store.put_setting(connection,key,value)
            held = sum(row.get('sent') == 0 for row in data['outbox'])
            report['sources'].append({'origin':origin,'jobs':len(data['jobs']),'assets':len(assets),'sha256':source['sha256'],
                                      'held_outbox':held,'snapshot':str(folder/source['file'])})
            report['jobs'] += len(data['jobs'])
            report['assets'] += len(assets)
            report['reply_events'] += len(data['comment_events'])
            report['held_outbox'] += held
        store.put_setting(connection,'migration_id',manifest['id'])
        store.put_setting(connection,'migration_final',manifest.get('final',False))
        store.put_setting(connection,'migration_reconciled',False)
        store.put_setting(connection,'migration_report',report)
        store.put_setting(connection,'reply_enabled',False)
        store.put_setting(connection,'paused',True)
        store.event(connection,'legacy_imported',report=report)
        if connection.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Relasi hasil migrasi tidak valid.')
    return report


async def reconcile(config, store, api):
    await api.sync(store)
    if not store.snapshot_fresh():
        raise ConfigurationError('Snapshot remote lengkap belum tersedia.')
    from .journal import AttemptJournal
    journal = AttemptJournal(config).reconcile_store(store)
    with store.connect() as connection:
        missing = [r[0] for r in connection.execute("""SELECT j.id FROM jobs j LEFT JOIN external_schedules s ON s.id=j.schedule_id
            WHERE j.status IN ('scheduled','submitting','submit_unknown') AND (j.schedule_id IS NULL OR s.id IS NULL)""")]
    report = {'at':iso(),'snapshot_generation':store.snapshot_generation(),'missing_active_schedules':missing,
              'journal':journal,'ready':not missing and not journal['unresolved'] and store.get('migration_final',False)}
    store.set('migration_reconciliation',report)
    store.set('migration_reconciled',report['ready'])
    store.record('migration_reconciled',**report)
    return report
