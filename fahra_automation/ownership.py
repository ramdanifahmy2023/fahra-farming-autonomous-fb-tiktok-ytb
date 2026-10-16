from __future__ import annotations

import json
import os
import subprocess
from contextlib import ExitStack, contextmanager

from .config import ConfigurationError
from .util import iso, process_lock

LABEL = 'id.fahra.automation.social'


def legacy_services(config):
    result = []
    for label in config.section('workflow').get('legacy_labels', []):
        process = subprocess.run(['/bin/launchctl', 'print', f'gui/{os.getuid()}/{label}'], capture_output=True, text=True)
        result.append({'label': label, 'loaded': process.returncode == 0})
    return result


def verify_ownership(config):
    path = config.runtime / 'ownership.json'
    if not path.exists():
        raise ConfigurationError('Cutover belum selesai; kepemilikan publikasi belum diberikan.')
    value = json.loads(path.read_text())
    if value.get('root') != str(config.root.resolve()) or value.get('state') != 'active' or not value.get('migration_id'):
        raise ConfigurationError('Bukti kepemilikan workflow tidak valid.')
    if any(service['loaded'] for service in legacy_services(config)):
        raise ConfigurationError('Workflow lama masih terpasang aktif. Pengiriman baru ditahan.')
    from .db import Store
    store = Store(config.runtime / 'workflow.sqlite3')
    if store.get('migration_id') != value['migration_id'] or store.get('journal_restore_unresolved', []):
        raise ConfigurationError('Database/attempt journal belum cocok dengan bukti cutover.')


@contextmanager
def legacy_locks(config):
    with ExitStack() as stack:
        for value in config.section('workflow').get('legacy_roots', []):
            root = config.path(value)
            if not (root / 'runtime/workflow.sqlite3').exists():
                raise ConfigurationError('Database workflow lama tidak tersedia untuk pemeriksaan kepemilikan.')
            stack.enter_context(process_lock(root / 'runtime/worker.lock'))
        yield


def activate(config, store):
    config.require_production_checkout()
    if any(item['loaded'] for item in legacy_services(config)):
        raise ConfigurationError('Hentikan kedua LaunchAgent lama sebelum aktivasi.')
    if not store.get('migration_id') or not store.get('migration_reconciled'):
        raise ConfigurationError('Migrasi final dan rekonsiliasi belum selesai.')
    if store.get('journal_restore_unresolved', []):
        raise ConfigurationError('Ada pengiriman journal tanpa baris database.')
    with legacy_locks(config):
        payload = {'root': str(config.root.resolve()), 'state': 'active', 'at': iso(), 'migration_id': store.get('migration_id')}
        target = config.runtime / 'ownership.json'
        temporary = target.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump(payload, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        store.record('ownership_activated', migration_id=payload['migration_id'])
    return payload
