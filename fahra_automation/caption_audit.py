"""Preview source-caption rewrites without writing to the production queue."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx

from .analysis import Analyzer
from .attribution import require_attribution, require_source_caption
from .captioning import POLICY_VERSION
from .config import Config, ConfigurationError
from .downloader import Downloader
from .util import iso, safe_error


async def audit(config, job_ids, folder):
    if folder.exists():
        raise ValueError("Gunakan folder audit baru agar bukti sebelumnya tidak tertimpa.")
    database = config.database_path
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        selected = []
        for identity in dict.fromkeys(job_ids):
            row = connection.execute("SELECT id,url,source_id,payload,asset_id FROM jobs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("ID pekerjaan tidak ditemukan: " + identity)
            selected.append({**dict(row), "payload": json.loads(row["payload"])})
    folder.mkdir(parents=True)
    cfg = config.section("ai")
    analyzer = Analyzer(config, None)
    preview_config = Config(config.root, copy.deepcopy(config.data))
    preview_config.data['workflow']['runtime_dir'] = str(folder.resolve() / 'preview-runtime')
    downloader = Downloader(preview_config)
    report = {"at": iso(), "policy_version": POLICY_VERSION, "published": False, "results": []}
    async with httpx.AsyncClient(base_url=cfg["base_url"].rstrip("/") + "/",
                                headers={"Authorization": "Bearer " + config.ai_key()}, timeout=180) as client:
        for job in selected:
            item = {"job_id": job["id"], "url": job["url"], "before": job["payload"].get("generated")}
            try:
                if 'source_caption_provenance' not in job['payload']:
                    proof = await downloader.inspect_source(job['url'])
                    enriched = {**job, 'payload': {**job['payload'], **proof}}
                    require_source_caption(enriched)
                    if (job['payload'].get('source_attribution')
                            and require_attribution(job) != require_attribution(enriched)):
                        raise ConfigurationError('Pemilik sumber berubah saat memeriksa caption Instagram.')
                    job = enriched
                require_source_caption(job)
                item.update(source_caption=job['payload']['source_caption'],
                            source_caption_provenance=job['payload']['source_caption_provenance'])
                result = await analyzer.generate(client, job, [], '', folder / job["id"])
                item.update(status="generated", after=result["generated"], review=result["caption_review"])
            except Exception as exc:
                item.update(status="needs_review", error=safe_error(exc))
            report["results"].append(item)
            target = folder / "report.json"
            partial = target.with_suffix(".partial")
            partial.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            partial.chmod(0o600)
            partial.replace(target)
            print(json.dumps(item, ensure_ascii=False), flush=True)
    return report


async def main():
    parser = argparse.ArgumentParser(description="Uji rewrite caption Instagram terverifikasi; antrean dan posting tidak diubah.")
    parser.add_argument("--job", required=True, nargs="+", help="ID pekerjaan lengkap")
    parser.add_argument("--folder", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = await audit(Config.load(), args.job, args.folder)
    except Exception as exc:
        print(safe_error(exc))
        raise SystemExit(1) from None
    print(json.dumps({"report": str(args.folder / "report.json"), "generated": sum(r["status"] == "generated" for r in report["results"]),
                      "needs_review": sum(r["status"] != "generated" for r in report["results"]), "published": False}))
    if any(r["status"] != "generated" for r in report["results"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
