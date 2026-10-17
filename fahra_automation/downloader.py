from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .attribution import from_metadata, require_attribution
from .config import ConfigurationError
from .util import iso, normalize_url, exception_leaves, safe_error
from .journal import AttemptJournal


class DownloadUncertain(RuntimeError):
    """A start may have reached MCP; a proven job mapping is required before retry."""


class DownloadFailed(ValueError):
    """A recorded MCP job has ended without a usable video."""


def download_failure(message):
    detail = str(message).lower()
    if 'no video' in detail or 'only photos' in detail:
        reason = 'Sumber tidak menghasilkan video. Kirim link posting yang berisi video.'
    elif 'certain audiences' in detail or 'not available to everyone' in detail:
        reason = 'Akses sumber dibatasi. Gunakan video yang dapat diakses downloader.'
    elif 'empty media response' in detail:
        reason = 'Instagram mengembalikan data media kosong. Sumber perlu diperiksa sebelum diulang.'
    else:
        reason = 'Download selesai dengan kegagalan. Periksa sumber sebelum mencoba ulang.'
    return DownloadFailed(reason + ' Detail: ' + safe_error(message))


OWNER_METADATA_SCRIPT = r'''
import contextlib, fcntl, io, json, sys

class QuietLogger:
    def debug(self, *args): pass
    def warning(self, *args): pass
    def error(self, *args): pass

def selected(item):
    if not isinstance(item, dict):
        raise ValueError('Invalid Instagram metadata')
    result = {key:item[key] for key in ('id','display_id','extractor_key','webpage_url','original_url','channel') if key in item}
    for field in ('owner','user'):
        if field in item:
            value = item[field]
            result[field] = {'username':value.get('username')} if isinstance(value,dict) else value
    for field in ('coauthor_producers','collaborators'):
        if field in item:
            result[field] = [{'username':value.get('username')} if isinstance(value,dict) else value for value in item[field]]
    if item.get('entries') is not None:
        result['entries'] = [selected(entry) for entry in item['entries']]
    return result

try:
    request = json.load(sys.stdin)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        with open('/tmp/instagram-mcp.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            import bot, yt_dlp
            options = bot.ytdlp_options(skip_download=True, extract_flat=False, playlistend=50,
                getcomments=False, cachedir=False, cookiefile=None, cookiesfrombrowser=None,
                writeinfojson=False, quiet=True, no_warnings=True, logger=QuietLogger())
            with yt_dlp.YoutubeDL(options) as ydl:
                metadata = selected(ydl.extract_info(request['url'], download=False))
    response = {'ok':True, 'metadata':metadata}
except Exception as exc:
    response = {'ok':False, 'error_type':type(exc).__name__}
print(json.dumps(response, ensure_ascii=False))
'''


class Downloader:
    def __init__(self, config):
        self.config = config
        self.journal = AttemptJournal(config)

    @asynccontextmanager
    async def session(self):
        cfg = self.config.section("downloader")
        env = {**os.environ, "INSTAGRAM_MCP_DOCKER": str(self.config.root / "scripts/docker-for-mcp.py"),
               "PW_DOCKER": cfg["docker"], "PW_VIDEO_MAX_HEIGHT": str(cfg["max_height"]),
               "INSTAGRAM_MCP_CONTAINER": cfg["container"]}
        params = StdioServerParameters(command="/bin/sh", args=[str(self.config.path(cfg["project_dir"]) / "start-mcp.sh")], env=env)
        try:
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    yield client
        except BaseExceptionGroup as exc:
            leaves = exception_leaves(exc)
            if any(isinstance(e, asyncio.CancelledError) for e in leaves):
                raise asyncio.CancelledError() from None
            if any(isinstance(e, DownloadUncertain) for e in leaves):
                raise DownloadUncertain(safe_error(exc)) from None
            if all(isinstance(e, ConfigurationError) for e in leaves):
                raise ConfigurationError(safe_error(exc)) from None
            if len(leaves) == 1:
                raise leaves[0] from None
            raise DownloadUncertain('Beberapa gangguan downloader perlu dicocokkan: ' + safe_error(exc)) from None

    @staticmethod
    async def call(client, name, arguments):
        result = await client.call_tool(name, arguments)
        if result.isError:
            raise RuntimeError("MCP downloader gagal: " + str(result.content[0].text))
        data = result.structuredContent or json.loads(result.content[0].text)
        return data

    async def health(self):
        async with self.session() as client:
            return await self.call(client, "instagram_worker_status", {})

    async def inspect_source(self, url):
        canonical_url, _ = normalize_url(url)
        cfg = self.config.section('downloader')
        try:
            process = await asyncio.create_subprocess_exec(
                cfg['docker'], 'exec', '-i', '-w', '/app', cfg['container'],
                'timeout', '--signal=TERM', '--kill-after=5s', '60s', 'python', '-c', OWNER_METADATA_SCRIPT,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError:
            raise ConfigurationError('Worker metadata Instagram belum dapat dijalankan.') from None
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(json.dumps({'url': canonical_url}).encode()), timeout=75)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ConfigurationError('Pembacaan pemilik Instagram timeout; kiriman ditahan tanpa menebak sumber.') from None
        if process.returncode or len(stdout) > 512_000:
            raise ConfigurationError('Metadata pemilik Instagram belum tersedia dari worker downloader.')
        try:
            response = json.loads(stdout)
        except (ValueError, UnicodeDecodeError):
            raise ConfigurationError('Respons metadata pemilik Instagram tidak valid.') from None
        if not isinstance(response, dict) or response.get('ok') is not True:
            raise ConfigurationError('Pembacaan pemilik Instagram belum berhasil; kiriman ditahan.')
        return from_metadata(canonical_url, response.get('metadata'))

    def source_path(self, value, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}', job_id):
            raise ValueError('Identitas job MCP tidak valid.')
        path = Path(value)
        root = self.config.path(self.config.section('downloader')['project_dir']) / 'downloads/mcp' / job_id / 'media'
        if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('Path media MCP harus absolut dan tanpa symlink/traversal.')
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve(strict=True)):
            raise ValueError('File MCP berada di luar direktori media job yang bersangkutan.')
        if not resolved.is_file() or resolved.stat().st_size <= 0:
            raise ValueError('File MCP hilang atau kosong.')
        return resolved

    async def download(self, job, store):
        async with self.session() as client:
            job_id = job["payload"].get("mcp_job_id")
            asset_id = job.get('asset_id') or job['id']
            action_id = 'download:' + asset_id
            record = self.journal.state(action_id)
            if not job_id and record and record.get('schedule_id'):
                job_id = record['schedule_id']
                store.checkpoint(job['id'], payload={'mcp_job_id': job_id})
            if not job_id:
                if job['payload'].get('mcp_start_intent') or record and record.get('state') in {'intended', 'unknown', 'accepted'}:
                    raise DownloadUncertain('Start MCP belum memiliki ID pasti; cocokkan manifest sebelum mencoba lagi.')
                self.journal.begin(action_id, {'url': job['url'], 'asset_id': asset_id})
                store.checkpoint(job['id'], payload={'mcp_start_intent': {'at': iso(), 'url': job['url']}})
                try:
                    started = await self.call(client, "start_instagram_download", {"url": job["url"], "max_items": 1})
                    job_id = started["job_id"]
                    if not isinstance(job_id, str) or not job_id:
                        raise ValueError('MCP tidak mengembalikan ID job.')
                except Exception as exc:
                    self.journal.mark(action_id, 'unknown')
                    raise DownloadUncertain('Hasil start MCP belum pasti; tidak mengunduh ulang otomatis.') from exc
                self.journal.mark(action_id, 'accepted', schedule_id=job_id)
                store.checkpoint(job["id"], payload={"mcp_job_id": job_id})
            while True:
                state = await self.call(client, "instagram_download_status", {"job_id": job_id, "wait_seconds": 20})
                if state["status"] == "failed":
                    raise download_failure(state.get("error", "Download gagal"))
                if state["status"] in {"completed", "partial"}:
                    if state['status'] == 'partial':
                        raise DownloadUncertain('Manifest MCP partial ditahan; belum sah untuk publikasi/cleanup.')
                    if len(state.get("files", [])) != 1:
                        raise ValueError("Satu link harus menghasilkan satu video.")
                    try:
                        matching_source = normalize_url(state.get('url', '')) == normalize_url(job['url'])
                    except ValueError:
                        matching_source = False
                    if state.get('job_id') != job_id or not matching_source:
                        raise ConfigurationError('Manifest MCP tidak cocok dengan Reel sumber; pengiriman ditahan.')
                    path = self.source_path(state['files'][0]['path'], job_id)
                    if job['payload'].get('source_attribution'):
                        require_attribution(job)
                        attribution = {key:job['payload'][key] for key in ('source_username', 'source_attribution')}
                    else:
                        attribution = await self.inspect_source(job['url'])
                    return {"source_path": str(path), "source_caption": state.get("caption") or "",
                            'mcp_status': 'completed', 'mcp_job_id': job_id, **attribution}
