"""A checked restore bundle never grants permission to publish."""
import json
import os
import shutil
import sqlite3
import uuid
import hashlib
from contextlib import closing
from pathlib import Path

from .db import Store
from .journal import AttemptJournal
from .util import digest_file, iso


def read_states(folder):
    states = {}
    for path in sorted(folder.rglob('*.json')):
        parts = path.relative_to(folder).parts
        if len(parts) not in {2,3} or len(parts)==3 and parts[0]!='account-reports':
            raise ValueError('Namespace jurnal backup tidak dikenal.')
        item = json.loads(path.read_text())
        if path.parent.name != hashlib.sha256(item['action_id'].encode()).hexdigest():
            raise ValueError('Identitas folder jurnal berubah.')
        key = ('report:' if len(parts)==3 else '') + item['action_id']
        states[key] = item
    return states


def legacy_reply_gaps(connection, states):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'reply_attempts' not in tables:
        return []
    gaps = []
    for identity, account, comment, origin, job in connection.execute("""SELECT e.id,e.account_id,e.platform_id,
        json_extract(j.payload,'$.legacy_origin'),j.id FROM reply_attempts a
        JOIN comment_events e ON e.id=a.event_id LEFT JOIN jobs j ON j.id=e.job_id
        WHERE a.status IN ('sending','send_unknown','sent_unverified')"""):
        if 'reply:' + account + ':' + comment in states:
            continue
        mapped = 'legacy_mapping' in tables and connection.execute(
            "SELECT 1 FROM legacy_mapping WHERE origin=? AND kind='job' AND new_id=?", (origin,job)).fetchone()
        if not mapped:
            raise ValueError('Jurnal intent balasan baru tidak lengkap.')
        gaps.append(identity)
    return sorted(gaps)


def create(config, store, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = destination / ('bundle-' + uuid.uuid4().hex)
    folder.mkdir(mode=0o700)
    journal = AttemptJournal(config)
    with journal.barrier(exclusive=True), store.tx():
        with store.connect() as source, closing(sqlite3.connect(folder / 'workflow.sqlite3')) as target:
            source.backup(target)
        frozen = tuple(sorted(journal.root.rglob('*.json')))
        cut_at = iso()
    # Journal records are immutable; copy only the filenames frozen at the database boundary.
    for path in frozen:
        if path.is_symlink() or any((journal.root / p).is_symlink() for p in path.relative_to(journal.root).parents):
            raise ValueError('Jurnal symlink tidak boleh dicadangkan.')
        target = folder / 'attempts' / path.relative_to(journal.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    ownership = config.runtime / 'ownership.json'
    if ownership.is_file() and not ownership.is_symlink():
        shutil.copyfile(ownership, folder / 'ownership.evidence.json')
    safe = {section: {key: value for key, value in config.section(section).items()
                      if key in allowed} for section, allowed in {
        'workflow': {'project','timezone','daily_limit','window_start','window_end','schedule_lead_seconds'},
        'video': {'crf','hook_seconds','min_duration_seconds','max_duration_seconds'},
        'reply': {'daily_limit','max_age_hours'}}.items()}
    safe['workflow'].update(mode='shadow', publish_enabled=False)
    (folder / 'config.safe.json').write_text(json.dumps(safe, indent=2))
    files = {str(p.relative_to(folder)): {'size': p.stat().st_size, 'sha256': digest_file(p)}
             for p in folder.rglob('*') if p.is_file()}
    states = read_states(folder / 'attempts')
    with closing(sqlite3.connect(folder / 'workflow.sqlite3')) as connection:
        gaps = legacy_reply_gaps(connection, states)
    manifest = {'version': 1, 'at': cut_at, 'files': files, 'restore_mode': 'shadow',
                'journal_states': len(states), 'legacy_reply_gaps':gaps}
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    for p in folder.rglob('*'):
        if p.is_file():
            p.chmod(0o600)
            with p.open('rb') as stream:
                os.fsync(stream.fileno())
    verify(folder)
    return folder


def verify(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest.get('version') != 1 or manifest.get('restore_mode') != 'shadow':
        raise ValueError('Manifest backup tidak dikenal.')
    files = manifest.get('files', {})
    if not {'workflow.sqlite3','config.safe.json'} <= set(files):
        raise ValueError('Backup tidak lengkap.')
    actual = {str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file() and p.name != 'manifest.json'}
    if actual != set(files):
        raise ValueError('Daftar file backup berubah.')
    for relative, expected in files.items():
        path = folder / relative
        if (Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink()
                or not path.resolve().is_relative_to(folder) or not path.is_file()
                or path.stat().st_size != expected['size'] or digest_file(path) != expected['sha256']):
            raise ValueError('Checksum/lokasi backup tidak cocok: ' + relative)
    with closing(sqlite3.connect((folder / 'workflow.sqlite3').as_uri() + '?mode=ro', uri=True)) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database backup rusak.')
        jobs = c.execute("""SELECT id FROM jobs WHERE status IN ('submitting','submit_unknown')
            OR (schedule_id IS NULL AND json_extract(payload,'$.submission') IS NOT NULL)""").fetchall()
        states = read_states(folder / 'attempts')
        if len(states) != manifest['journal_states'] or any(j[0] not in states for j in jobs):
            raise ValueError('Jurnal intent kiriman tidak lengkap.')
        if legacy_reply_gaps(c, states) != manifest.get('legacy_reply_gaps', []):
            raise ValueError('Daftar kekurangan bukti reply legacy berubah.')
    return manifest


def restore(folder, destination):
    verify(folder)
    folder, destination = Path(folder), Path(destination)
    if destination.exists():
        raise ValueError('Restore harus menuju folder baru yang belum ada.')
    destination.mkdir(parents=True, mode=0o700)
    runtime = destination / 'runtime'
    runtime.mkdir()
    shutil.copyfile(folder / 'workflow.sqlite3', runtime / 'workflow.sqlite3')
    if (folder / 'attempts').exists():
        shutil.copytree(folder / 'attempts', runtime / 'attempts')
    shutil.copyfile(folder / 'config.safe.json', destination / 'config.safe.json')
    store = Store(runtime / 'workflow.sqlite3')
    store.set('paused', True)
    store.set('reply_enabled', False)
    store.set('account_report_enabled', False)
    store.set('sync_in_progress', True)
    store.set('restore_requires_reconciliation', True)
    store.record('restore_shadow', backup_at=json.loads((folder / 'manifest.json').read_text())['at'])
    (destination / 'RESTORE.txt').write_text('SHADOW. Ownership sengaja tidak diaktifkan. Lengkapi konfigurasi, '
        'cocokkan semua jurnal dan provider, lalu lakukan proses ownership resmi sebelum hak kirim diberikan.\n')
    return destination
