"""Read one existing media manifest, verify Drive and generate a fixture persona; never publish."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sqlite3
import sys
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fahra_automation.analysis import Analyzer
from fahra_automation.config import Config
from fahra_automation.db import Store
from fahra_automation.drive import Drive
from fahra_automation.personas import PersonaService
from fahra_automation.util import iso, now


async def preview(production_root, output):
    if output.exists() or not output.resolve().is_relative_to(ROOT / 'runtime'):
        raise ValueError('Gunakan folder baru di runtime worktree.')
    production = Config.load(production_root)
    with sqlite3.connect(production.database_path.as_uri() + '?mode=ro', uri=True) as source:
        row = source.execute("""SELECT id,payload FROM assets WHERE status='drive_ready'
            AND json_type(payload,'$.drive_file_id')='text'
            AND json_type(payload,'$.caption_review.observations')='text'
            ORDER BY created_at DESC LIMIT 1""").fetchone()
    if row is None:
        raise ValueError('Manifest media dengan bukti caption belum tersedia.')
    source_id, payload = row[0], json.loads(row[1])
    payload = {k: v for k, v in payload.items() if k not in
               {'submission', 'delivery_metadata', 'delivery_candidate'}}
    output.mkdir(parents=True)
    data = copy.deepcopy(Config.load(ROOT).data)
    data['workflow'].update(mode='shadow', publish_enabled=False, runtime_dir=str(output))
    data['ai'].update(production.section('ai'))
    for key in ('credentials_file', 'api_key_file'):
        if key in data['ai']:
            data['ai'][key] = str(production.path(data['ai'][key]))
    data['drive'].update(production.section('drive'))
    data['drive']['sync_dir'] = str(production.path(data['drive']['sync_dir']))
    config = Config(ROOT, data)
    store = Store(output / 'fixture.sqlite3')
    accounts = [{'id': 'preview-' + p, 'type': p, 'username': 'preview_' + p,
                 'name': 'Preview', 'isConnected': True, 'publishEligible': True}
                for p in ('tiktok', 'facebook', 'youtube')]
    day = now().date()
    store.apply_snapshot(accounts, [], (day - timedelta(days=1)).isoformat(),
                         (day + timedelta(days=2)).isoformat())
    store.enqueue(0, 0, 0, [('https://www.instagram.com/reel/previewlatency/', 'previewlatency')])
    job = store.job(store.queue()[0]['id'])
    store.asset_checkpoint(job['asset_id'], 'drive_ready', payload=payload)
    job = store.job(job['id'])
    analyzer = Analyzer(config, None, store)
    persona = PersonaService(config, store, analyzer)
    started = time.monotonic()
    first = await persona.prepare_delivery(job)
    generation_seconds = time.monotonic() - started
    store.begin_sync()
    assert store.job(job['id'])['payload']['delivery_candidate'] == first
    started = time.monotonic()
    with patch.object(analyzer, 'delivery_metadata', side_effect=AssertionError('AI diulang')):
        second = await persona.prepare_delivery(store.job(job['id']))
    assert first == second
    reuse_seconds = time.monotonic() - started
    drive = Drive(config, pool_connections=True)
    try:
        started = time.monotonic()
        results = await asyncio.gather(*(drive.verify_delivery(job) for _ in range(3)))
        drive_seconds = time.monotonic() - started
    finally:
        await drive.close()
    assert all(r['sha256'] == payload['render_sha256'] for r in results)
    with store.connect() as connection:
        assert connection.execute('SELECT count(*) FROM jobs WHERE schedule_id IS NOT NULL').fetchone()[0] == 0
    report = {'result': 'PASS', 'at': iso(), 'source_asset_id': source_id,
              'production_database_access': 'read-only selected manifest; no DB copy',
              'publication_requests': 0, 'reply_requests': 0, 'production_mutations': 0,
              'real_ai_generations': 1, 'candidate_survived_snapshot_invalidation': True,
              'retry_ai_generations': 0, 'generation_seconds': round(generation_seconds, 3),
              'reuse_seconds': round(reuse_seconds, 3), 'metadata': first,
              'drive_full_verifications': 3, 'drive_concurrency_limit': 2,
              'drive_seconds': round(drive_seconds, 3), 'drive_results': results}
    target = output / 'report.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    target.chmod(0o600)
    return {'result': 'PASS', 'report': str(target), 'publication_requests': 0,
            'generation_seconds': report['generation_seconds'], 'reuse_seconds': report['reuse_seconds'],
            'drive_seconds': report['drive_seconds'], 'drive_full_verifications': 3}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--production-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(preview(args.production_root.resolve(), args.output.resolve())), indent=2))


if __name__ == '__main__':
    main()
