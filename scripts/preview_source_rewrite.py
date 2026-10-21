"""Rewrite verified Instagram captions with real AI and validate fixture deliveries without publishing."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fahra_automation.analysis import Analyzer
from fahra_automation.attribution import require_source_caption
from fahra_automation.captioning import deterministic_source_copy
from fahra_automation.config import Config
from fahra_automation.db import Store
from fahra_automation.personas import PersonaService
from fahra_automation.repliz import Repliz
from fahra_automation.util import iso, now


async def preview(production_root, evidence_files, output):
    if output.exists() or not output.is_relative_to(ROOT / 'runtime'):
        raise ValueError('Gunakan folder baru di runtime worktree.')
    production = Config.load(production_root)
    data = copy.deepcopy(Config.load(ROOT).data)
    data['workflow'].update(mode='shadow', publish_enabled=False, runtime_dir=str(output),
                            journal_dir=str(output / 'attempts'))
    data['ai'].update(production.section('ai'))
    for key in ('credentials_file', 'api_key_file'):
        if key in data['ai']:
            data['ai'][key] = str(production.path(data['ai'][key]))
    config = Config(ROOT, data)
    output.mkdir(parents=True)
    store = Store(output / 'fixture.sqlite3')
    analyzer = Analyzer(config, None, store)
    personas = PersonaService(config, store, analyzer)
    cases = []
    async with httpx.AsyncClient(base_url=data['ai']['base_url'].rstrip('/') + '/',
            headers={'Authorization': 'Bearer ' + config.ai_key()}, timeout=180) as client:
        for evidence_file in evidence_files:
            evidence = json.loads(evidence_file.read_text())
            job = {'id': 'preview-' + evidence['source_id'], 'source_id': evidence['source_id'],
                   'url': evidence['source_url'], 'status': 'drive_ready', 'payload': evidence['payload']}
            source = require_source_caption(job)
            started = time.monotonic()
            result = await analyzer.generate(client, job, (), '', output / evidence['source_id'])
            duration = time.monotonic() - started
            job['payload'] = {**job['payload'], **result, 'analysis_model': data['ai']['model'],
                              'drive_url': 'https://example.invalid/preview.mp4'}
            with store.connect() as connection:
                calls_before = connection.execute('SELECT count(*) FROM ai_usage').fetchone()[0]
            deliveries = []
            for platform in ('tiktok', 'facebook', 'youtube'):
                delivery = {**job, 'id': job['id'] + '-' + platform, 'platform': platform,
                            'account_id': 'fixture-' + platform, 'payload': dict(job['payload'])}
                metadata = await personas.prepare_delivery(delivery)
                delivery['payload']['delivery_metadata'] = metadata
                body = Repliz.payload(delivery, now())
                assert metadata['caption'] == result['generated']['caption']
                assert metadata['persona_applied_to_caption'] is False
                assert body['description'].count('source: @' + job['payload']['source_username']) == 1
                deliveries.append({'platform': platform, 'metadata': metadata, 'payload': body})
            with store.connect() as connection:
                calls_after = connection.execute('SELECT count(*) FROM ai_usage').fetchone()[0]
            assert calls_after == calls_before
            assert len({d['payload']['description'] for d in deliveries}) == 1
            assert result['generated']['hook'] == ''
            source_only = deterministic_source_copy(source) is not None
            assert result['caption_review']['attempts'] in ((0,) if source_only else (1, 2))
            cases.append({'source_evidence': str(evidence_file), 'source_id': evidence['source_id'],
                          'source_caption': source, 'result': result, 'generation_seconds': round(duration, 3),
                          'delivery_ai_calls': calls_after - calls_before, 'deliveries': deliveries})
    with store.connect() as connection:
        usage = [dict(row) for row in connection.execute('SELECT * FROM ai_usage')]
    report = {'result': 'PASS', 'at': iso(), 'mode': 'real AI; verified source metadata; fixture delivery accounts',
              'production_database_access': 'none', 'publication_requests': 0, 'reply_requests': 0,
              'drive_requests': 0, 'production_mutations': 0, 'real_ai_calls': len(usage),
              'cases': cases, 'ai_usage': usage}
    target = output / 'report.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    target.chmod(0o600)
    return {'result': report['result'], 'report': str(target), 'real_ai_calls': len(usage),
            'cases': [{'source_id': c['source_id'], 'generated': c['result']['generated'],
                       'seconds': c['generation_seconds']} for c in cases], 'publication_requests': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--production-root', type=Path, required=True)
    parser.add_argument('--source-evidence', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(preview(args.production_root.resolve(),
        [path.resolve() for path in args.source_evidence], args.output.resolve())), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
