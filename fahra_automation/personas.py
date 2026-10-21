from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext

from .config import EXCLUDED_ACCOUNT_ID
from .util import iso

LEGACY_DEFAULT_PROFILE = {
    'name': 'Indonesia santai', 'language': 'id',
    'instructions': 'Bahasa Indonesia santai/lucu Gen Z sesuai konteks, singkatan wajar, '
                    '0–2 emoji bila cocok. Konten serius tetap tenang tanpa lelucon. '
                    'Jangan mengarang pengalaman pribadi, fakta, identitas, atau janji.',
}
DEFAULT_PROFILE = {
    'name': 'Indonesia natural', 'language': 'id',
    'instructions': 'Tulis seperti mengirim video ke teman: pilih satu detail konkret yang didukung '
                    'bukti, lalu beri pengamatan atau reaksi singkat. Pertahankan sudut hook dan '
                    'caption bersama; jangan kembali menjadi narator urutan adegan. Bahasa Indonesia '
                    'sehari-hari, singkatan dan humor hanya bila pas, tanpa memaksa slang Gen Z, '
                    'punchline, sapaan, atau pertanyaan. Nol sampai dua emoji untuk konteks ringan '
                    'bila membantu; konten serius tenang tanpa humor atau emoji. Jangan mengarang '
                    'pengalaman pribadi, fakta, identitas, penyebab, atau janji.',
}
PLATFORMS = {'tiktok', 'facebook', 'youtube'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


LEGACY_DEFAULT_HASH = digest(LEGACY_DEFAULT_PROFILE)


def is_default_profile(profile):
    return isinstance(profile, dict) and profile in (LEGACY_DEFAULT_PROFILE, DEFAULT_PROFILE)


def validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) != {'name', 'language', 'instructions'}:
        raise ValueError('Persona memerlukan name, language, instructions saja.')
    if profile['language'] != 'id' or any(not isinstance(profile[k], str) or not profile[k].strip()
                                         for k in ('name', 'instructions')):
        raise ValueError('Persona harus berbahasa Indonesia dan memiliki instruksi gaya.')
    if len(profile['name']) > 100 or len(profile['instructions']) > 3000:
        raise ValueError('Profil persona terlalu panjang.')
    return {k: v.strip() for k, v in profile.items()}


class PersonaRegistry:
    """Immutable profile versions; only an account's current binding may change."""

    def __init__(self, store):
        self.store = store
        with store.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS persona_versions(
                    persona_id TEXT NOT NULL, version INTEGER NOT NULL, profile TEXT NOT NULL,
                    hash TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(persona_id, version));
                CREATE TABLE IF NOT EXISTS persona_accounts(
                    account_id TEXT PRIMARY KEY, platform TEXT NOT NULL, persona_id TEXT NOT NULL,
                    version INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(persona_id,version) REFERENCES persona_versions(persona_id,version));
            ''')

    def bind(self, account_id, platform, profile=None, *, connection=None):
        if not account_id or account_id == EXCLUDED_ACCOUNT_ID or platform not in PLATFORMS:
            raise ValueError('Identitas/platform persona tidak diizinkan.')
        with nullcontext(connection) if connection is not None else self.store.tx() as c:
            current = c.execute('''SELECT a.*,v.profile,v.hash FROM persona_accounts a
                JOIN persona_versions v USING(persona_id,version) WHERE account_id=?''', (account_id,)).fetchone()
            if current and current['platform'] != platform:
                raise ValueError('Platform identitas akun berubah.')
            if profile is None and current:
                if current['hash'] != LEGACY_DEFAULT_HASH or json.loads(current['profile']) != LEGACY_DEFAULT_PROFILE:
                    return self.decode(current)
            clean = validate_profile(profile if profile is not None else DEFAULT_PROFILE)
            profile_hash = digest(clean)
            if current and current['hash'] == profile_hash:
                return self.decode(current)
            persona_id = 'account:' + account_id
            version = c.execute('SELECT coalesce(max(version),0)+1 FROM persona_versions WHERE persona_id=?',
                                (persona_id,)).fetchone()[0]
            c.execute('INSERT INTO persona_versions VALUES(?,?,?,?,?)',
                      (persona_id, version, json.dumps(clean, ensure_ascii=False), profile_hash, iso()))
            c.execute('''INSERT INTO persona_accounts VALUES(?,?,?,?,?) ON CONFLICT(account_id)
                DO UPDATE SET persona_id=excluded.persona_id,version=excluded.version,updated_at=excluded.updated_at''',
                      (account_id, platform, persona_id, version, iso()))
            return {'account_id': account_id, 'platform': platform, 'persona_id': persona_id,
                    'persona_version': version, 'persona_hash': profile_hash, 'profile': clean}

    @staticmethod
    def decode(row):
        return {'account_id': row['account_id'], 'platform': row['platform'], 'persona_id': row['persona_id'],
                'persona_version': row['version'], 'persona_hash': row['hash'], 'profile': json.loads(row['profile'])}


class PersonaService:
    def __init__(self, config, store, analyzer=None):
        from .analysis import Analyzer
        self.config, self.store = config, store
        self.registry = PersonaRegistry(store)
        self.analyzer = analyzer or Analyzer(config, None)

    @staticmethod
    def evidence_hash(payload):
        return digest({k: payload.get(k) for k in
                       ('source_sha256', 'render_sha256', 'generated', 'caption_review', 'transcript')})

    def save_candidate(self, job, metadata):
        with self.store.tx() as connection:
            row = connection.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if row is None:
                return  # Preview callers may supply an unstored fixture delivery.
            current = self.store.decode(row)
            payload = current['payload']
            if (current['status'] != 'drive_ready' or current.get('schedule_id')
                    or payload.get('submission') or payload.get('delivery_metadata')
                    or current['account_id'] != metadata['account_id']
                    or current['platform'] != metadata['platform']
                    or self.evidence_hash(payload) != metadata['evidence_hash']):
                return
            payload['delivery_candidate'] = metadata
            connection.execute('UPDATE jobs SET payload=? WHERE id=?',
                               (json.dumps(payload, ensure_ascii=False), job['id']))

    async def prepare_delivery(self, job):
        payload = job['payload']
        account_id, platform = job.get('account_id'), job.get('platform')
        binding = self.registry.bind(account_id, platform)
        evidence_hash = self.evidence_hash(payload)
        old = payload.get('delivery_metadata')
        sent = bool(job.get('schedule_id') or payload.get('submission') or job.get('status') in
                    {'submitting', 'submit_unknown', 'scheduled', 'published'})
        if old and old.get('account_id') == account_id and old.get('platform') == platform:
            if sent or old.get('evidence_hash') == evidence_hash:
                return old
        if sent:
            raise ValueError('Metadata delivery ber-intent tidak boleh diganti atau dipindahkan akun.')
        candidate = payload.get('delivery_candidate')
        if (isinstance(candidate, dict) and candidate.get('evidence_hash') == evidence_hash
                and all(candidate.get(key) == binding[key] for key in
                        ('account_id', 'platform', 'persona_id', 'persona_version', 'persona_hash'))):
            return candidate
        folder = self.config.runtime / 'personas/deliveries' / job['id'] / evidence_hash[:16]
        generated = await self.analyzer.delivery_metadata(job, binding, folder)
        metadata = {**generated, **{k: v for k, v in binding.items() if k != 'profile'},
                    'persona_profile': binding['profile'], 'evidence_hash': evidence_hash, 'prepared_at': iso()}
        self.save_candidate(job, metadata)
        return metadata
