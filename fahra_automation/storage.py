"""Move the operational SQLite file while keeping one runtime and one worker lock."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from .config import ConfigurationError
from .util import iso, process_lock


def _paths(config):
    runtime = config.runtime.absolute()
    root = config.root.absolute()
    if runtime.is_relative_to(root):
        runtime = root.resolve() / runtime.relative_to(root)
    return runtime / 'database-location.json', runtime / 'database-migration.json'


def _no_symlink(path):
    path = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ConfigurationError('Lokasi database tidak boleh melalui symlink.')
    return path


def _json(path):
    try:
        if path.stat().st_size > 65536:
            raise ValueError('Metadata terlalu besar.')
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError('Metadata bukan object.')
        return value
    except (OSError, UnicodeError, ValueError):
        raise ConfigurationError('Metadata lokasi database tidak dapat diverifikasi.') from None


def database_path(config):
    pointer, marker = _paths(config)
    _no_symlink(pointer)
    _no_symlink(marker)
    if marker.exists():
        raise ConfigurationError('Migrasi database belum ditutup; periksa storage status sebelum menjalankan worker.')
    if not pointer.exists():
        default = _no_symlink(pointer.parent / 'workflow.sqlite3')
        if default.exists() and not default.is_file():
            raise ConfigurationError('Path database lama sudah dipindahkan; pointer lokasi diperlukan.')
        return default
    item = _json(pointer)
    if type(item.get('version')) is not int or item['version'] != 1 or item.get('root') != str(config.root.resolve()) or not isinstance(item.get('database'), str):
        raise ConfigurationError('Pointer database tidak cocok dengan proyek ini.')
    destination = Path(item['database'])
    if not destination.is_absolute():
        raise ConfigurationError('Pointer database harus menunjuk path absolut.')
    destination = _no_symlink(destination)
    if not destination.is_file():
        raise ConfigurationError('Database pada pointer tidak tersedia; jangan membuat database kosong pengganti.')
    try:
        with destination.open('rb') as stream:
            if stream.read(16) != b'SQLite format 3\x00':
                raise ConfigurationError('Database pada pointer kosong atau bukan SQLite.')
    except OSError:
        raise ConfigurationError('Database pada pointer tidak dapat dibaca.') from None
    return destination


def status(config):
    pointer, marker = _paths(config)
    result = {'pointer': str(pointer), 'migration_incomplete': marker.exists(), 'database': None, 'ready': False}
    if marker.exists():
        try:
            _no_symlink(marker)
            item = _json(marker)
            result['migration'] = {key: item.get(key) for key in ('id','phase','started_at','source','destination','archive')}
        except ConfigurationError as exc:
            result['error'] = str(exc)
        return result
    try:
        path = database_path(config)
        result.update(database=str(path), ready=path.is_file(), relocated=pointer.exists())
    except ConfigurationError as exc:
        result['error'] = str(exc)
    return result


def _sync_dir(path):
    if os.name == 'nt':
        return  # File fsync + atomic replace are available; POSIX directory descriptors are not.
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path, value, *, exclusive=False):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode() + b'\n'
    target = path if exclusive else path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if not exclusive:
        target.replace(path)
    _sync_dir(path.parent)


def _readonly(path):
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5, isolation_level=None)
    connection.execute('PRAGMA query_only=ON')
    return connection


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def fingerprint(connection):
    """Hash types and values, including blobs, without exposing row contents in evidence."""
    integrity = [row[0] for row in connection.execute('PRAGMA integrity_check')]
    if integrity != ['ok'] or connection.execute('PRAGMA foreign_key_check').fetchone():
        raise ConfigurationError('Integritas atau foreign key database tidak lulus.')
    schema = [tuple(row) for row in connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
    schema_hash = hashlib.sha256(json.dumps(schema, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    tables = {}
    for name, in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        columns = [row[1] for row in connection.execute('PRAGMA table_info(' + _quote(name) + ')')]
        order = ','.join(_quote(column) for column in columns)
        digest, count = hashlib.sha256(), 0
        for row in connection.execute('SELECT * FROM ' + _quote(name) + ' ORDER BY ' + order):
            values = []
            for value in row:
                if isinstance(value, bytes):
                    values.append(['blob', base64.b64encode(value).decode()])
                elif isinstance(value, float):
                    values.append(['float', value.hex()])
                else:
                    values.append([type(value).__name__, value])
            digest.update(json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
            count += 1
        tables[name] = {'rows': count, 'sha256': digest.hexdigest()}
    return {'schema_sha256': schema_hash, 'user_version': connection.execute('PRAGMA user_version').fetchone()[0],
            'application_id': connection.execute('PRAGMA application_id').fetchone()[0], 'tables': tables}


def _quiescent(connection, config):
    # In WAL mode an already-open connection can keep writing the old WAL after rename.
    # DELETE mode gives us SQLite's SQLITE_READONLY_DBMOVED guard for stale writers.
    if connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
        raise ConfigurationError('Migrasi lokasi memerlukan journal_mode=DELETE; WAL tidak boleh dipindah dengan jalur ini.')
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'settings','jobs','submission_attempts'} <= tables:
        raise ConfigurationError('Database bukan workflow Fahra yang telah diinisialisasi.')
    settings = {key: json.loads(value) for key, value in connection.execute(
        "SELECT key,value FROM settings WHERE key IN ('paused','reply_enabled','account_report_enabled')")}
    report_default = config.data.get('account_report', {}).get('enabled', False)
    if settings.get('paused') is not True or settings.get('reply_enabled', False) is not False or settings.get('account_report_enabled', report_default) is not False:
        raise ConfigurationError('Pause proses, reply, dan laporan sebelum migrasi database.')
    checks = [('jobs', "leased<>0 OR status='submitting'"), ('assets', 'leased_job_id IS NOT NULL'),
              ('submission_attempts', "status='submitting'"),
              ('comment_events', "leased<>0 OR status IN ('processing','sending')"),
              ('reply_attempts', "status='sending'"), ('account_report_parts', "state='sending'"),
              ('webhook_inbox', "status='processing'")]
    for table, where in checks:
        if table in tables and connection.execute('SELECT 1 FROM ' + _quote(table) + ' WHERE ' + where + ' LIMIT 1').fetchone():
            raise ConfigurationError('Masih ada lease atau pengiriman aktif; migrasi ditahan.')


def migrate(config, destination):
    """Offline move. A failed/incomplete move remains blocked for explicit inspection/restore."""
    config.require_production_checkout()
    source = database_path(config)
    destination = Path(destination).expanduser()
    if not destination.is_absolute() or destination.suffix.lower() not in {'.sqlite3','.db'}:
        raise ConfigurationError('Tujuan migrasi harus file .sqlite3 atau .db baru dengan path absolut.')
    destination = _no_symlink(destination)
    if destination.exists() or destination == source or not source.is_file():
        raise ConfigurationError('Sumber harus tersedia dan tujuan migrasi harus belum ada.')
    pointer, marker = _paths(config)
    with process_lock(config.runtime / 'worker.lock'):
        # Recheck after acquiring the same lock used by the worker; another move may have finished.
        if database_path(config) != source or destination.exists():
            raise ConfigurationError('Lokasi database berubah; periksa ulang sebelum migrasi.')
        with closing(_readonly(source)) as check:
            _quiescent(check, config)
        identity = uuid.uuid4().hex
        archive = source.with_name(source.name + '.before-move-' + identity)
        item = {'version': 1, 'root': str(config.root.resolve()), 'id': identity, 'phase': 'copying',
                'started_at': iso(), 'source': str(source), 'destination': str(destination), 'archive': str(archive)}
        _atomic_json(marker, item, exclusive=True)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _no_symlink(destination)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        # Hold the SQLite write reservation through rename and pointer commit. An already-open
        # writer then encounters SQLite's moved-file protection instead of writing the archive.
        with closing(sqlite3.connect(source, timeout=5, isolation_level=None)) as barrier:
            barrier.execute('BEGIN IMMEDIATE')
            try:
                with closing(_readonly(source)) as read, closing(sqlite3.connect(destination)) as copied:
                    _quiescent(read, config)
                    before = fingerprint(read)
                    read.backup(copied)
                    copied.commit()
                    after = fingerprint(copied)
                    if before != after:
                        raise ConfigurationError('Isi seluruh tabel sumber dan tujuan tidak cocok.')
                with destination.open('rb') as stream:
                    os.fsync(stream.fileno())
                _sync_dir(destination.parent)
                item.update(phase='verified', fingerprint=after)
                _atomic_json(marker, item)
                source.rename(archive)
                _sync_dir(source.parent)
                # Old versions must fail opening this path, not create an empty DB.
                source.mkdir(mode=0o700)
                _atomic_json(source / 'MOVED.json', {'migration_id': identity, 'pointer': str(pointer)}, exclusive=True)
                _sync_dir(source.parent)
                item['phase'] = 'archived'
                _atomic_json(marker, item)
                pointer_value = {'version': 1, 'root': str(config.root.resolve()), 'database': str(destination),
                                 'migration_id': identity, 'moved_at': iso(), 'source_archive': str(archive)}
                _atomic_json(pointer, pointer_value)
                item['phase'] = 'committed'
                _atomic_json(marker, item)
                evidence = config.runtime / ('database-migration-' + identity + '.json')
                _atomic_json(evidence, item, exclusive=True)
                marker.unlink()
                _sync_dir(marker.parent)
            finally:
                barrier.rollback()
    return {'migrated': True, 'database': str(destination), 'archive': str(archive),
            'evidence': str(evidence), 'fingerprint': after, 'controls_preserved': True,
            'rollback': 'Use a fresh consistent backup/restore; never repoint to an old database after new writes.'}
