from __future__ import annotations

import hashlib
import json
import re

from .config import ConfigurationError
from .util import iso, normalize_url


def username(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?', value)
            or '..' in value):
        raise ConfigurationError('Username sumber Instagram belum valid; atribusi perlu diperiksa.')
    return value.lower()


def from_metadata(url, metadata):
    try:
        canonical_url, shortcode = normalize_url(url)
    except ValueError:
        raise ConfigurationError('URL sumber Instagram belum valid untuk atribusi.') from None
    if (not isinstance(metadata, dict) or metadata.get('extractor_key') != 'Instagram'
            or metadata.get('id') != shortcode):
        raise ConfigurationError('Metadata sumber bukan pemilik Reel Instagram yang diminta.')
    for key in ('display_id',):
        if metadata.get(key) is not None and metadata[key] != shortcode:
            raise ConfigurationError('Identitas Reel pada metadata bertentangan.')
    for key in ('webpage_url', 'original_url'):
        if metadata.get(key):
            try:
                actual = normalize_url(metadata[key])[1]
            except ValueError:
                raise ConfigurationError('URL metadata Instagram tidak valid.') from None
            if actual != shortcode:
                raise ConfigurationError('URL metadata menunjuk Reel lain.')
    evidence = []

    def collect(item, prefix):
        if not isinstance(item, dict):
            raise ConfigurationError('Metadata pemilik Instagram tidak lengkap.')
        if item.get('extractor_key') not in (None, 'Instagram'):
            raise ConfigurationError('Metadata anak berasal dari extractor lain.')
        if any(item.get(field) is not None and not isinstance(item[field], dict) for field in ('owner', 'user')):
            raise ConfigurationError('Struktur pemilik Instagram tidak valid.')
        for field, value in (('channel', item.get('channel')),
                             ('owner.username', (item.get('owner') or {}).get('username') if isinstance(item.get('owner'), dict) else None),
                             ('user.username', (item.get('user') or {}).get('username') if isinstance(item.get('user'), dict) else None)):
            if value is not None:
                evidence.append({'field': prefix + field, 'username': username(value)})
        for field in ('coauthor_producers', 'collaborators'):
            if item.get(field):
                if not isinstance(item[field], list):
                    raise ConfigurationError('Daftar kolaborator Instagram tidak valid.')
                for index, collaborator in enumerate(item[field]):
                    if not isinstance(collaborator, dict) or not collaborator.get('username'):
                        raise ConfigurationError('Pemilik/kolaborator Instagram belum dapat dibedakan.')
                    evidence.append({'field': f'{prefix}{field}[{index}].username', 'username': username(collaborator['username'])})

    collect(metadata, '')
    primary = [entry for entry in evidence if entry['field'] in {'channel', 'owner.username', 'user.username'}]
    if not primary:
        raise ConfigurationError('Username pemilik Reel tidak tersedia; nama tampilan/caption tidak digunakan.')
    entries = metadata.get('entries')
    if entries is not None:
        if not isinstance(entries, list) or not entries:
            raise ConfigurationError('Metadata koleksi Instagram tidak lengkap.')
        for index, entry in enumerate(entries):
            collect(entry, f'entries[{index}].')
    identities = {entry['username'] for entry in evidence}
    if len(identities) != 1:
        raise ConfigurationError('Metadata memuat pemilik/kolaborator berbeda; atribusi perlu diperiksa.')
    identity = primary[0]['username']
    proof = {'version': 1, 'username': identity, 'source_id': shortcode, 'source_url': canonical_url,
             'provider': 'yt-dlp:Instagram', 'evidence': evidence, 'verified_at': iso()}
    proof['metadata_sha256'] = hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'source_username': identity, 'source_attribution': proof}


def require_attribution(job):
    proof = job.get('payload', {}).get('source_attribution')
    if not isinstance(proof, dict) or proof.get('version') != 1 or proof.get('provider') != 'yt-dlp:Instagram':
        raise ConfigurationError('Bukti username sumber Instagram belum tersedia; pengiriman ditahan.')
    identity = username(job['payload'].get('source_username'))
    try:
        url, shortcode = normalize_url(job.get('url', ''))
    except ValueError:
        raise ConfigurationError('URL sumber kiriman belum valid.') from None
    evidence = proof.get('evidence')
    if (proof.get('username') != identity or proof.get('source_id') != shortcode or proof.get('source_url') != url
            or job.get('source_id', shortcode) != shortcode
            or not re.fullmatch(r'[0-9a-f]{64}', str(proof.get('metadata_sha256', '')))
            or not isinstance(evidence, list) or not evidence
            or not any(isinstance(row, dict) and row.get('field') in {'channel', 'owner.username', 'user.username'} for row in evidence)
            or any(not isinstance(row, dict) or row.get('username') != identity for row in evidence)):
        raise ConfigurationError('Bukti atribusi tidak cocok dengan sumber kiriman.')
    return identity


def credit_description(description, identity, limit):
    identity = username(identity)
    if not isinstance(description, str) or not description.strip():
        raise ValueError('Caption/deskripsi publikasi kosong.')
    credit = 'source: @' + identity
    lines = description.strip().splitlines()
    kept = []
    for line in lines:
        if re.match(r'\s*source\s*:', line, re.IGNORECASE):
            if line.strip().lower() != credit:
                raise ConfigurationError('Caption memuat atribusi sumber yang bertentangan dengan metadata.')
            continue
        kept.append(line)
    text = '\n'.join(kept).strip()
    if re.search(r'(?i)(?:^|\s)source\s*:', text):
        raise ConfigurationError('Atribusi tertanam dalam caption perlu diperiksa.')
    match = re.search(r'(?:^|\s)(#[\w]+(?:\s+#[\w]+)*)\s*$', text)
    if match:
        body, tags = text[:match.start()].rstrip(), match.group(1)
        result = '\n\n'.join(part for part in (body, credit, tags) if part)
    else:
        result = text + '\n\n' + credit if text else credit
    if len(result) > limit:
        raise ConfigurationError('Caption beserta kredit sumber melebihi batas platform; teks perlu diperiksa.')
    return result
