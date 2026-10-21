from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import threading
from contextlib import AsyncExitStack
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from .config import ConfigurationError
from .db import ZONE
from .util import digest_file, iso, now


class FolderParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.entries, self.href, self.parts = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data):
        if self.href:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or not self.href:
            return
        url = urlsplit(self.href)
        if url.scheme == "https" and url.hostname == "drive.google.com":
            match = re.search(r"/(?:folders|file/d)/([A-Za-z0-9_-]+)", url.path)
            file_id = match[1] if match else parse_qs(url.query).get("id", [None])[0]
            if file_id:
                self.entries.append(("".join(self.parts).strip(), file_id))
        self.href = None


class Drive:
    def __init__(self, config, *, pool_connections=False):
        self.config = config
        self._sync_client = httpx.Client(follow_redirects=True, timeout=90) if pool_connections else None
        self._pool_connections = pool_connections
        self._async_client = None
        self._bank_lock = threading.RLock()
        self._verify_slots = asyncio.Semaphore(config.section('drive').get('parallel_verifications', 2))

    async def close(self):
        try:
            if self._async_client is not None:
                await self._async_client.aclose()
        finally:
            if self._sync_client is not None:
                await asyncio.to_thread(self._sync_client.close)

    def ready(self):
        cfg = self.config.section("drive")
        if cfg.get("mode") != "synced_public":
            raise ConfigurationError("Paket ini hanya memakai sinkronisasi Drive Desktop; upload API dinonaktifkan.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", cfg.get("folder_id", "")):
            raise ConfigurationError("Folder Google Drive belum valid.")
        root = self.config.path(cfg["sync_dir"])
        if not root.is_dir() or root.is_symlink():
            raise ConfigurationError("Bank konten belum tersedia atau volume tidak terpasang.")

    def login(self):
        raise ConfigurationError("Gunakan Drive Desktop dan folder pilihan; login API tidak dipakai.")

    def create_folder(self, name):
        raise ConfigurationError("Pembuatan folder melalui API dinonaktifkan.")

    @staticmethod
    def media_url(file_id):
        if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id):
            raise ConfigurationError("ID file Drive tidak valid.")
        return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"

    @staticmethod
    def public_entries(folder_id, *, client=None):
        request = client.get if client is not None else httpx.get
        response = request("https://drive.google.com/embeddedfolderview", params={"id": folder_id},
                             follow_redirects=True, timeout=45, headers={"Cache-Control": "no-cache"})
        response.raise_for_status()
        if response.url.host != "drive.google.com" or 'class="flip-entries"' not in response.text:
            raise ConfigurationError("Daftar folder Drive tidak dapat dibaca publik; periksa sharing.")
        parser = FolderParser()
        parser.feed(response.text)
        return parser.entries

    def resolve_file(self, relative):
        self.ready()
        path = Path(relative)
        if not relative or path.is_absolute() or '..' in path.parts or path.suffix.lower() != '.mp4':
            raise ConfigurationError("Lokasi relatif media Drive tidak valid.")
        folder_id = self.config.section('drive')['folder_id']
        chain = []
        for part in path.parts:
            kwargs = {'client': self._sync_client} if self._sync_client is not None else {}
            matches = [fid for name, fid in self.public_entries(folder_id, **kwargs) if name == part]
            if len(matches) > 1:
                raise ConfigurationError("Nama ganda di folder Drive; pencocokan dihentikan.")
            if not matches:
                raise ConfigurationError("Menunggu sinkronisasi Bank konten ke Google Drive.", dependency='drive')
            folder_id = matches[0]
            self.media_url(folder_id)
            chain.append(folder_id)
        return folder_id, chain

    @staticmethod
    def local_file(path, root, expected):
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ConfigurationError("Checksum hasil render belum tersedia.")
        path, root = Path(path).absolute(), Path(root).absolute()
        if '..' in path.parts or not path.is_relative_to(root) or not path.is_file():
            raise ConfigurationError("Lokasi media tidak cocok dengan bank konten.")
        current = path
        while current != root.parent:
            if current.is_symlink():
                raise ConfigurationError("Symlink media tidak boleh dipakai untuk publikasi.")
            current = current.parent
        before = path.stat()
        digest = digest_file(path)
        after = path.stat()
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, f) != getattr(after, f) for f in fields) or digest != expected:
            raise ConfigurationError("Isi media berubah atau checksum tidak cocok.")
        return after.st_size

    def upload(self, job, store):
        self.ready()
        return self.synced_file(job, store)

    def bank_file(self, job, store):
        payload = store.job(job['id'])['payload']
        source = Path(payload['render_path'])
        root = self.config.path(self.config.section('drive')['sync_dir']).absolute()
        expected = payload['render_sha256']
        self.local_file(source, source.parent, expected)
        relative = payload.get('drive_relative_path')
        if not relative:
            day = now().astimezone(ZONE)
            folder = root / day.strftime('%Y/%m/%d') / job['id'][0]
            depth = 1
            while folder.exists() and len([p for p in folder.glob('*.mp4') if not p.name.startswith('._')]) >= 8:
                if depth >= len(job['id']):
                    raise ConfigurationError("Folder bank penuh; lokasi media perlu diperiksa.")
                folder /= job['id'][depth]
                depth += 1
            relative = str((folder / (job['id'] + '.mp4')).relative_to(root))
            store.checkpoint(job['id'], payload={'drive_relative_path': relative})
        relative_path = Path(relative)
        if relative_path.is_absolute() or '..' in relative_path.parts or relative_path.suffix.lower() != '.mp4':
            raise ConfigurationError("Lokasi relatif bank konten tidak valid.")
        target = root / relative_path
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            raise ConfigurationError("Lokasi bank konten keluar dari folder pilihan.")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            partial = root.parent / ('.fa-' + job['id'] + '.partial')
            if partial.is_symlink():
                raise ConfigurationError("File sementara bank konten tidak valid.")
            shutil.copyfile(source, partial)
            with partial.open('rb') as stream:
                os.fsync(stream.fileno())
            self.local_file(partial, root.parent, expected)
            partial.replace(target)
            store.record('drive_sync_queued', job['id'], relative_path=relative)
        size = self.local_file(target, root, expected)
        return target, relative, expected, size

    def synced_file(self, job, store):
        with self._bank_lock:
            target, relative, expected, size = self.bank_file(job, store)
        file_id, chain = self.resolve_file(relative)
        url = self.media_url(file_id)
        digest, downloaded, prefix = hashlib.sha256(), 0, b''
        stream = self._sync_client.stream if self._sync_client is not None else httpx.stream
        with stream('GET', url, follow_redirects=True, timeout=90) as response:
            if response.status_code != 200 or 'text/html' in response.headers.get('content-type', ''):
                raise ConfigurationError("Drive belum memberikan seluruh file MP4.", dependency='drive')
            for chunk in response.iter_bytes(1024 * 1024):
                prefix = (prefix + chunk)[:64]
                downloaded += len(chunk)
                if downloaded > size:
                    raise ConfigurationError("Ukuran file Drive berubah.")
                digest.update(chunk)
        if b'ftyp' not in prefix or downloaded != size or digest.hexdigest() != expected:
            raise ConfigurationError("File Drive belum lengkap atau checksum tidak cocok.", dependency='drive')
        return {'drive_file_id': file_id, 'drive_url': url, 'bank_path': str(target),
                'drive_root_id': self.config.section('drive')['folder_id'], 'drive_path_ids': chain,
                'drive_relative_path': relative, 'drive_verified_sha256': expected,
                'drive_verified_size': size, 'drive_verified_at': iso()}

    @staticmethod
    async def local_stage(function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def verify_delivery(self, job):
        async with self._verify_slots:
            return await self._verify_delivery(job)

    async def _verify_delivery(self, job):
        self.ready()
        p = job['payload']
        if p.get('drive_root_id') != self.config.section('drive')['folder_id']:
            raise ConfigurationError("Folder Drive berubah; manifest perlu diverifikasi kembali.")
        relative = p.get('drive_relative_path', '')
        root = self.config.path(self.config.section('drive')['sync_dir']).absolute()
        if Path(p.get('bank_path', '')).absolute() != root / relative:
            raise ConfigurationError("Lokasi bank tidak sesuai manifest.")
        size = await self.local_stage(self.local_file, root / relative, root, p.get('render_sha256'))
        if p.get('drive_verified_sha256') != p.get('render_sha256') or p.get('drive_verified_size') != size:
            raise ConfigurationError("Manifest checksum/ukuran Drive tidak sesuai.")
        file_id, chain = await self.local_stage(self.resolve_file, relative)
        if file_id != p.get('drive_file_id') or chain != p.get('drive_path_ids'):
            raise ConfigurationError("ID atau lokasi file Drive berubah; pengiriman ditahan.")
        url = self.media_url(file_id)
        if p.get('drive_url') != url:
            raise ConfigurationError("Tautan media tidak cocok dengan ID Drive.")
        if self._pool_connections and self._async_client is None:
            self._async_client = httpx.AsyncClient(follow_redirects=True, timeout=90)
        kwargs = {'client': self._async_client} if self._async_client is not None else {}
        result = await self.verify_url(url, p['render_sha256'], size, **kwargs)
        return {**result, 'drive_file_id': file_id, 'drive_verified_at': iso()}

    @staticmethod
    async def verify_url(url, expected_sha256=None, expected_size=None, *, client=None):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'drive.usercontent.google.com' or parsed.username:
            raise ConfigurationError("Tautan media bukan URL Drive yang diizinkan.")
        digest, size, prefix = hashlib.sha256(), 0, b''
        async with AsyncExitStack() as stack:
            if client is None:
                client = await stack.enter_async_context(httpx.AsyncClient(follow_redirects=True, timeout=90))
            async with client.stream('GET', url, headers={'Cache-Control': 'no-cache'}) as response:
                if response.status_code != 200 or 'text/html' in response.headers.get('content-type', ''):
                    raise ConfigurationError("Drive belum memberikan seluruh file MP4.")
                async for chunk in response.aiter_bytes(1024 * 1024):
                    prefix = (prefix + chunk)[:64]
                    size += len(chunk)
                    if size > (expected_size if expected_size is not None else 1_000_000_000):
                        raise ConfigurationError("Ukuran file Drive melebihi manifest.")
                    digest.update(chunk)
        if b'ftyp' not in prefix or (expected_size is not None and size != expected_size):
            raise ConfigurationError("File Drive terpotong atau bukan MP4.")
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise ConfigurationError("Checksum Drive berubah; pengiriman ditahan.")
        return {'sha256': digest.hexdigest(), 'size': size}
