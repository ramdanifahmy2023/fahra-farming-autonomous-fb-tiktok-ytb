from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import stat
import tomllib
import uuid
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import Config, EXCLUDED_ACCOUNT_ID
from .util import digest_file, iso, now, process_lock, safe_error

CODE_ROOT = Path(__file__).resolve().parents[1]


class CleanupBlocked(ValueError):
    pass


async def finish_mutation(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Keep the cross-process lock until an in-flight unlink and its journal finish.
        await task
        raise


def read_db(path, full=False):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)) as c:
        c.row_factory = sqlite3.Row
        c.execute('BEGIN')
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        data = {}
        for table in ('jobs', 'assets', 'events', 'cleanup_files'):
            query = 'SELECT * FROM ' + table
            if table == 'events':
                query += " WHERE kind='status_changed'"
            include = table in tables and (full or table in {'jobs', 'assets'})
            data[table] = [dict(r) for r in c.execute(query)] if include else []
        for table in ('jobs', 'assets'):
            for row in data[table]:
                row['payload'] = json.loads(row['payload'])
                if not isinstance(row['payload'], dict):
                    raise CleanupBlocked('Payload database tidak valid.')
        data['published_at'] = {}
        for event in data['events']:
            if json.loads(event['detail']).get('status') == 'published':
                data['published_at'][event['job_id']] = event['at']
        return data


def regular_file(path, boundary):
    path, boundary = Path(path), Path(boundary)
    if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(boundary):
        raise CleanupBlocked('Lokasi file di luar batas cleanup.')
    for current in (path, *path.parents):
        if current.is_symlink():
            raise CleanupBlocked('Symlink tidak boleh dipakai untuk cleanup.')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CleanupBlocked('Cleanup hanya menerima file biasa tanpa hardlink.')
    return info


def fingerprint(path, boundary):
    before = regular_file(path, boundary)
    digest = digest_file(path)
    after = regular_file(path, boundary)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(before, f) != getattr(after, f) for f in fields):
        raise CleanupBlocked('File berubah saat diperiksa.')
    return {'sha256': digest, 'bytes': after.st_size,
            'identity': [getattr(after, f) for f in fields]}


class Cleaner:
    def __init__(self, config, store):
        self.config, self.store = config, store

    def settings(self):
        cfg = self.config.data.get('cleanup', {})
        hours = cfg.get('retention_hours', 24)
        interval = cfg.get('interval_seconds', 3600)
        if type(hours) is not int or hours < 24 or type(interval) is not int or interval < 300:
            raise CleanupBlocked('Retensi minimal 24 jam dan interval minimal 300 detik.')
        return {**cfg, 'retention_hours': hours, 'interval_seconds': interval}

    def scope(self):
        cfg = self.settings()
        root = self.config.root.resolve()
        if self.config.runtime.resolve() != root / 'runtime' or self.store.path.absolute() != self.config.database_path:
            raise CleanupBlocked('Cleanup hanya untuk database operasional yang ditunjuk proyek.')
        peers = cfg.get('peer_roots', [])
        if not isinstance(peers, list) or not peers:
            raise CleanupBlocked('Daftar workspace lain wajib diatur untuk memeriksa pemakaian file bersama.')
        roots = [root]
        configurations = {root: self.config}
        banks = [self.config.path(self.config.section('drive')['sync_dir']).absolute()]
        for value in peers:
            peer = Path(value)
            if not peer.is_absolute() or peer.is_symlink() or not peer.is_dir():
                raise CleanupBlocked('Workspace lain tidak tersedia; cleanup ditahan.')
            peer = peer.resolve()
            if peer in roots:
                raise CleanupBlocked('Daftar workspace cleanup berulang.')
            settings = tomllib.loads((peer / 'config.example.toml').read_text())
            local = peer / 'config.local.toml'
            if local.exists():
                for key, value in tomllib.loads(local.read_text()).items():
                    settings.setdefault(key, {}).update(value)
            runtime = Path(settings['workflow']['runtime_dir'])
            if (peer / runtime).resolve() != peer / 'runtime':
                raise CleanupBlocked('Runtime workspace lain tidak berada pada lokasi yang didukung.')
            banks.append((peer / settings['drive']['sync_dir']).absolute())
            roots.append(peer)
            configurations[peer] = Config(peer, settings)
        self._database_scopes = {}
        self._relocation_markers = set()
        for path, configuration in configurations.items():
            active = configuration.database_path
            boundary = path if active.is_relative_to(path) else active.parent
            regular_file(active, boundary)
            self._database_scopes[active] = boundary
            pointer = path / 'runtime/database-location.json'
            if pointer.exists():
                regular_file(pointer, path)
                metadata = json.loads(pointer.read_text())
                archive = metadata.get('source_archive')
                if archive:
                    archive = Path(archive)
                    identity = metadata.get('migration_id', '')
                    if not re.fullmatch(r'[0-9a-f]{32}', identity) or not archive.name.endswith('.before-move-' + identity):
                        raise CleanupBlocked('Arsip relokasi database tidak cocok dengan pointer.')
                    archive_boundary = path if archive.is_relative_to(path) else archive.parent
                    regular_file(archive, archive_boundary)
                    self._database_scopes[archive] = archive_boundary
                previous = path / 'runtime/workflow.sqlite3'
                if previous.is_dir():
                    marker = previous / 'MOVED.json'
                    regular_file(marker, path)
                    moved = json.loads(marker.read_text())
                    if (not metadata.get('migration_id') or moved.get('migration_id') != metadata['migration_id']
                            or moved.get('pointer') != str(pointer)):
                        raise CleanupBlocked('Penanda database lama tidak cocok dengan relokasi.')
                    self._relocation_markers.add(previous)
        mcp = self.config.path(self.config.section('downloader')['project_dir']).resolve() / 'downloads/mcp'
        if not mcp.is_dir() or mcp.is_symlink():
            raise CleanupBlocked('Folder download MCP tidak tersedia.')
        return roots, banks, mcp

    def inventory(self, roots):
        references = defaultdict(set)
        own = None
        databases = dict(self._database_scopes)
        for root in roots:
            candidates = list((root / 'runtime').rglob('*.sqlite3'))
            candidates += list((root / 'runtime').rglob('*.sqlite3.before-move-*'))
            for db in candidates:
                if db.name.startswith('._'):
                    continue
                if db not in self._relocation_markers:
                    databases[db] = root
        for db, boundary in sorted(databases.items()):
            regular_file(db, boundary)
            snapshot = read_db(db, full=db == self.store.path.resolve())
            if db == self.store.path.resolve():
                own = snapshot
            for table in ('jobs', 'assets'):
                for row in snapshot[table]:
                    for key in ('source_path', 'render_path', 'bank_path'):
                        value = row['payload'].get(key)
                        if value:
                            references[str(Path(value).resolve())].add((str(db), table, row['id']))
        if own is None or not own['jobs'] and own['assets']:
            raise CleanupBlocked('Database operasional tidak lengkap.')
        return own, references

    @staticmethod
    def groups(snapshot):
        paired = bool(snapshot['assets']) or any('asset_id' in j for j in snapshot['jobs'])
        if paired:
            by_asset = defaultdict(list)
            for job in snapshot['jobs']:
                by_asset[job['asset_id']].append(job)
            return [(a['id'], a['payload'], by_asset[a['id']], json.loads(a['required_destinations']) if isinstance(a.get('required_destinations'), str) else a.get('required_destinations', [])) for a in snapshot['assets']]
        return [(j['id'], j['payload'], [j], False) for j in snapshot['jobs']]

    def media_scope(self, identity, payload, banks):
        origin = payload.get('legacy_origin')
        if not origin:
            return self.config.runtime, banks[0]
        with self.store.connect() as c:
            mapping = c.execute("""SELECT legacy_id FROM legacy_mapping
                WHERE origin=? AND kind='asset' AND new_id=?""", (origin, identity)).fetchone()
        if not mapping or mapping[0] != identity or origin not in {'tiktok','fb-ytb'}:
            raise CleanupBlocked('Pemetaan asset legacy belum terbukti.')
        if origin not in self._migration_scopes:
            report = self.store.get('migration_report', {})
            sources = [s for s in report.get('sources', []) if s.get('origin') == origin]
            if len(sources) != 1:
                raise CleanupBlocked('Manifest migrasi legacy tidak unik.')
            archived = Path(sources[0]['snapshot'])
            regular_file(archived, self.config.runtime / 'migrations')
            manifest_path = archived.parent / 'manifest.json'
            regular_file(manifest_path, self.config.runtime / 'migrations')
            manifest = json.loads(manifest_path.read_text())
            entries = [s for s in manifest.get('sources', []) if s.get('origin') == origin]
            if (manifest.get('id') != self.store.get('migration_id') or len(entries) != 1
                    or entries[0]['file'] != archived.name
                    or entries[0]['sha256'] != sources[0]['sha256']
                    or fingerprint(archived, self.config.runtime / 'migrations')['sha256'] != sources[0]['sha256']):
                raise CleanupBlocked('Bukti snapshot migrasi berubah.')
            root = Path(entries[0]['root']).resolve()
            expected = self.config.path(self.config.section('workflow')['legacy_roots'][0 if origin == 'tiktok' else 1]).resolve()
            peers = [Path(p).resolve() for p in self.settings()['peer_roots']]
            if root != expected or root not in peers:
                raise CleanupBlocked('Bank asal legacy di luar workspace yang diperiksa.')
            self._migration_scopes[origin] = (root / 'runtime', banks[peers.index(root) + 1], archived)
        media_runtime, bank, archived = self._migration_scopes[origin]
        with closing(sqlite3.connect(archived.as_uri() + '?mode=ro', uri=True)) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            table = 'assets' if 'assets' in tables else 'jobs'
            original = connection.execute('SELECT payload FROM ' + table + ' WHERE id=?', (mapping[0],)).fetchone()
        if not original or any(json.loads(original[0]).get(key) != payload.get(key)
                               for key in ('bank_path','render_path','source_path','source_sha256','render_sha256')):
            raise CleanupBlocked('File media tidak cocok dengan asset pada snapshot migrasi.')
        return media_runtime, bank

    def group_plan(self, identity, payload, jobs, paired, snapshot, references, banks, mcp, at):
        if not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise CleanupBlocked('ID media tidak valid.')
        if paired and (not isinstance(paired, list) or len(jobs) != len(paired) or {j['platform'] for j in jobs} != set(paired)):
            raise CleanupBlocked('Tujuan wajib asset belum lengkap sesuai manifest migrasi/ingest.')
        if not jobs or any(j['status'] != 'published' or j['leased'] for j in jobs):
            raise CleanupBlocked('Masih ada tujuan yang belum published atau masih diproses.')
        times = []
        for job in jobs:
            p = job['payload']
            if job.get('account_id') == EXCLUDED_ACCOUNT_ID or not job.get('account_id'):
                raise CleanupBlocked('Akun tujuan tidak boleh dibersihkan.')
            platform = job.get('platform', 'tiktok')
            hosts = {'tiktok': {'www.tiktok.com', 'tiktok.com'},
                     'facebook': {'www.facebook.com', 'facebook.com'},
                     'youtube': {'www.youtube.com', 'youtube.com', 'youtu.be'}}[platform]
            url = urlsplit(p.get('post_url', ''))
            if not job.get('schedule_id') or not p.get('post_id') or url.scheme != 'https' or url.hostname not in hosts or url.username:
                raise CleanupBlocked('ID jadwal dan tautan posting belum lengkap.')
            published_at = snapshot['published_at'].get(job['id'])
            if not published_at:
                raise CleanupBlocked('Waktu published tidak dapat dibuktikan dari event.')
            stamp = datetime.fromisoformat(published_at)
            if stamp.utcoffset() is None:
                raise CleanupBlocked('Waktu published tidak memiliki zona waktu.')
            times.append(stamp)
            for key in ('source_sha256', 'render_sha256', 'drive_verified_sha256', 'drive_file_id'):
                if not p.get(key) or p[key] != payload.get(key):
                    raise CleanupBlocked('Bukti media antar tujuan tidak cocok.')
        eligible_at = max(times) + timedelta(hours=self.settings()['retention_hours'])
        if at < eligible_at:
            return {'id': identity, 'state': 'retention', 'eligible_at': iso(eligible_at), 'files': []}
        media_runtime, bank_root = self.media_scope(identity, payload, banks)
        folder = media_runtime / 'media' / identity
        work_files = [Path(payload.get('source_path', '')), folder / 'final.mp4', folder / 'analysis/speech.wav']
        if all(not path.exists() and not path.is_symlink() for path in work_files):
            return {'id': identity, 'job_ids': sorted(j['id'] for j in jobs), 'state': 'clean',
                    'eligible_at': iso(eligible_at), 'files': [], 'absent': [str(p) for p in work_files]}
        if any(not re.fullmatch(r'[0-9a-f]{64}', payload.get(key, '')) for key in ('source_sha256', 'render_sha256')) or payload.get('drive_verified_sha256') != payload['render_sha256']:
            raise CleanupBlocked('Checksum Drive belum terverifikasi.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', payload.get('drive_file_id', '')):
            raise CleanupBlocked('ID file Drive tidak valid.')
        relative = Path(payload.get('drive_relative_path', ''))
        bank = Path(payload.get('bank_path', ''))
        if relative.is_absolute() or '..' in relative.parts or bank != bank_root / relative:
            raise CleanupBlocked('Lokasi bank konten tidak cocok dengan manifest.')
        backup = fingerprint(bank, bank_root)
        if backup['sha256'] != payload['render_sha256']:
            raise CleanupBlocked('Salinan bank konten berubah atau belum lengkap.')
        for i in range(6):
            frame = folder / 'analysis' / f'frame-{i}.jpg'
            regular_file(frame, media_runtime)
            data = frame.read_bytes()
            if len(data) < 100 or not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
                raise CleanupBlocked('Enam frame konteks komentar belum lengkap/valid.')
        job_ids = {j['id'] for j in jobs}
        allowed = {(str(self.store.path.resolve()), 'jobs', j) for j in job_ids}
        if paired:
            allowed.add((str(self.store.path.resolve()), 'assets', identity))
        source = Path(payload.get('source_path', ''))
        mcp_id = payload.get('mcp_job_id', '')
        if not re.fullmatch(r'[0-9a-f]{32}', mcp_id) or source.parent != mcp / mcp_id / 'media' or source.suffix.lower() != '.mp4':
            raise CleanupBlocked('Kepemilikan file MCP tidak dapat dibuktikan.')
        manifest = mcp / mcp_id / 'status.json'
        regular_file(manifest, mcp)
        status = json.loads(manifest.read_text())
        if status.get('job_id') != mcp_id or status.get('status') != 'completed' or not any(f.get('path') == str(source) for f in status.get('files', [])):
            raise CleanupBlocked('Manifest MCP tidak cocok atau download belum selesai.')
        render = Path(payload.get('render_path', ''))
        if render != folder / 'final.mp4':
            raise CleanupBlocked('Hasil render bukan file kerja milik media ini.')
        candidates = [(source, mcp, 'source', payload.get('source_sha256')),
                      (render, media_runtime, 'render', payload['render_sha256']),
                      (folder / 'analysis/speech.wav', media_runtime, 'audio', None)]
        files, absent = [], []
        for path, boundary, kind, expected in candidates:
            if any(path.is_relative_to(b) or path.resolve().is_relative_to(b.resolve()) for b in banks):
                raise CleanupBlocked('File bank konten tidak boleh dihapus.')
            if references.get(str(path.resolve()), set()) - allowed:
                raise CleanupBlocked('File masih dirujuk job, preview, atau workspace lain.')
            if not path.exists() and not path.is_symlink():
                absent.append(str(path))
                continue
            data = fingerprint(path, boundary)
            if kind == 'source' and not re.fullmatch(r'[0-9a-f]{64}', expected or ''):
                raise CleanupBlocked('Checksum sumber tidak tersedia.')
            if expected and data['sha256'] != expected:
                raise CleanupBlocked('Isi file lokal berubah dari hasil workflow.')
            files.append({'path': str(path), 'boundary': str(boundary), 'kind': kind, **data})
        return {'id': identity, 'job_ids': sorted(job_ids), 'state': 'eligible' if files else 'clean',
                'eligible_at': iso(eligible_at), 'files': files, 'absent': absent,
                'drive_file_id': payload['drive_file_id'], 'backup': backup, 'bank_path': str(bank)}

    def plan(self, at=None, only_id=None):
        self._migration_scopes = {}
        at = at or now()
        roots, banks, mcp = self.scope()
        snapshot, refs = self.inventory(roots)
        groups = []
        for identity, payload, jobs, paired in self.groups(snapshot):
            if only_id is not None and identity != only_id:
                continue
            try:
                group = self.group_plan(identity, payload, jobs, paired, snapshot, refs, banks, mcp, at)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                group = {'id': identity, 'state': 'blocked', 'reason': safe_error(exc), 'files': []}
            groups.append(group)
        files = [f for g in groups for f in g['files']]
        return {'at': iso(at), 'retention_hours': self.settings()['retention_hours'], 'groups': groups,
                'eligible_files': len(files), 'eligible_bytes': sum(f['bytes'] for f in files)}

    async def verify_remote(self, group):
        url = 'https://drive.usercontent.google.com/download'
        digest, size, prefix = hashlib.sha256(), 0, b''
        async with httpx.AsyncClient(follow_redirects=True, timeout=90) as client:
            async with client.stream('GET', url, params={'id': group['drive_file_id'], 'export': 'download', 'confirm': 't'}) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(1024 * 1024):
                    prefix = (prefix + chunk)[:64]
                    size += len(chunk)
                    if size > group['backup']['bytes']:
                        raise CleanupBlocked('Ukuran file Drive berubah.')
                    digest.update(chunk)
        if b'ftyp' not in prefix or size != group['backup']['bytes'] or digest.hexdigest() != group['backup']['sha256']:
            raise CleanupBlocked('Isi Drive tidak sama dengan salinan terverifikasi; file lokal dipertahankan.')

    def ledger(self, file, owner, state, run_id, error=None):
        with self.store.tx() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS cleanup_files(
                path TEXT PRIMARY KEY,owner TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,
                state TEXT NOT NULL,updated_at TEXT NOT NULL,run_id TEXT NOT NULL)''')
            c.execute('''INSERT INTO cleanup_files VALUES(?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                owner=excluded.owner,sha256=excluded.sha256,bytes=excluded.bytes,state=excluded.state,
                updated_at=excluded.updated_at,run_id=excluded.run_id''',
                (file['path'], owner, file['sha256'], file['bytes'], state, iso(), run_id))
            self.store.event(c, 'cleanup_file_' + state, owner, path=file['path'], bytes=file['bytes'],
                             sha256=file['sha256'], run_id=run_id, error=error)

    def remove_file(self, file, owner, run_id):
        path, boundary = Path(file['path']), Path(file['boundary'])
        if fingerprint(path, boundary) != {k: file[k] for k in ('sha256', 'bytes', 'identity')}:
            raise CleanupBlocked('File berubah setelah rencana dibuat.')
        self.ledger(file, owner, 'intent', run_id)
        # Persist intent before unlink. A crash after unlink is reconciled without claiming freed bytes twice.
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | os.O_NOFOLLOW
        fd = os.open(path.anchor, flags)
        try:
            for part in path.parent.parts[1:]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            current = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            identity = [getattr(current, f) for f in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')]
            if identity != file['identity'] or not stat.S_ISREG(current.st_mode):
                raise CleanupBlocked('File berubah sebelum penghapusan.')
            os.unlink(path.name, dir_fd=fd)
        finally:
            os.close(fd)
        self.ledger(file, owner, 'deleted', run_id)

    def reconcile_missing(self, plan, run_id):
        snapshot = read_db(self.store.path.resolve(), full=True)
        owners = {g['id']: set(g.get('absent', [])) for g in plan['groups'] if g['state'] in {'eligible', 'clean'}}
        for row in snapshot['cleanup_files']:
            if row['state'] == 'intent' and row['path'] in owners.get(row['owner'], set()):
                self.ledger(row, row['owner'], 'missing_after_intent', run_id)

    async def sweep(self):
        self.config.require_live_action()
        if not self.settings().get('enabled', False):
            raise CleanupBlocked('Cleanup belum diaktifkan di konfigurasi lokal.')
        if CODE_ROOT != self.config.root.resolve() or (CODE_ROOT / '.git').is_file() or not (self.config.root / '.git').is_dir():
            raise CleanupBlocked('Penghapusan hanya boleh dari checkout produksi, bukan linked worktree.')
        _, _, mcp = self.scope()
        with process_lock(mcp / '.peternakan-cleanup.lock'):
            run_id = uuid.uuid4().hex
            plan = await asyncio.to_thread(self.plan)
            await finish_mutation(self.reconcile_missing, plan, run_id)
            results = []
            for group in plan['groups']:
                if not group['files']:
                    continue
                try:
                    await self.verify_remote(group)
                    # Re-read both workspaces after network I/O; a new reference must keep the files.
                    fresh = await asyncio.to_thread(self.plan, only_id=group['id'])
                    current = next(g for g in fresh['groups'] if g['id'] == group['id'])
                    if current != group:
                        raise CleanupBlocked('Status, referensi, atau file berubah selama verifikasi Drive.')
                    for file in group['files']:
                        await finish_mutation(self.remove_file, file, group['id'], run_id)
                        results.append({'path': file['path'], 'state': 'deleted', 'bytes': file['bytes']})
                except Exception as exc:
                    results.append({'id': group['id'], 'state': 'blocked', 'error': safe_error(exc)})
                    self.store.record('cleanup_group_blocked', group['id'], error=safe_error(exc), run_id=run_id)
            report = {**plan, 'run_id': run_id, 'results': results,
                      'deleted_bytes': sum(r.get('bytes', 0) for r in results if r['state'] == 'deleted')}
            summary = {k: report[k] for k in ('at', 'run_id', 'eligible_files', 'eligible_bytes', 'deleted_bytes')}
            summary['blocked_groups'] = sum(g['state'] == 'blocked' for g in plan['groups']) + sum(r['state'] == 'blocked' for r in results)
            self.store.set('cleanup_last_run', summary)
            self.store.record('cleanup_completed', **summary)
            if results:
                folder = self.config.runtime / 'cleanup'
                folder.mkdir(parents=True, exist_ok=True)
                (folder / (run_id + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            return report

    async def loop(self):
        while True:
            try:
                if self.settings().get('enabled', False):
                    await self.sweep()
                    self.store.health('cleanup', 'ok')
                else:
                    self.store.health('cleanup', 'waiting')
            except Exception as exc:
                self.store.record('cleanup_failed', error=safe_error(exc))
                self.store.health('cleanup', 'error', reason=safe_error(exc))
            interval = self.config.data.get('cleanup', {}).get('interval_seconds', 3600)
            await asyncio.sleep(interval if type(interval) is int and interval >= 300 else 3600)
