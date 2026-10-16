from __future__ import annotations

import json
import os
import re
from pathlib import Path
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .util import iso
from .journal import AttemptJournal


class DownloadUncertain(RuntimeError):
    """A start may have reached MCP; a proven job mapping is required before retry."""


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
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                yield client

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
                    raise RuntimeError(state.get("error", "Download gagal"))
                if state["status"] in {"completed", "partial"}:
                    if state['status'] == 'partial':
                        raise DownloadUncertain('Manifest MCP partial ditahan; belum sah untuk publikasi/cleanup.')
                    if len(state.get("files", [])) != 1:
                        raise ValueError("Satu link harus menghasilkan satu video.")
                    path = self.source_path(state['files'][0]['path'], job_id)
                    return {"source_path": str(path), "source_caption": state.get("caption") or "",
                            'mcp_status': 'completed', 'mcp_job_id': job_id}
