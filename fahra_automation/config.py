from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

EXCLUDED_ACCOUNT_ID = '6a9ec26462caae1e0415af16'
EXCLUDED_USERNAME = 'iq7fanspage'
PLATFORMS = ('tiktok', 'facebook', 'youtube')
FACEBOOK_PUBLISH_ACCOUNT_IDS = (
    '6a9d5ffa62caae1e0413ff1f', '6a9d5ff862caae1e0413ff19',
    '6a9d5ff662caae1e0413ff14', '6a9d5ff462caae1e0413ff10',
    '6a9d5ff262caae1e0413ff07', '6a9d5fef62caae1e0413ff00',
    '6a9d5fed62caae1e0413fef7', '6a9d5fea62caae1e0413fef1',
    '6a9d5fe762caae1e0413fee2', '6a9d5fe562caae1e0413fedb',
    '6a9d5fe362caae1e0413fed6', '6a9d5fbd62caae1e0413fe88',
    '6a8e856d62caae1e040677e7',
)
CODE_ROOT = Path(__file__).resolve().parents[1]


class ConfigurationError(RuntimeError):
    def __init__(self, message, *, dependency=None):
        super().__init__(message)
        self.dependency = dependency


def credential_pairs(path):
    if not path.exists():
        return {}
    pairs = {}
    for key, value in re.findall(r'^\s*([a-zA-Z][a-zA-Z0-9_ -]*)\s*[:=]\s*(.*?)\s*$', path.read_text().replace('\\_', '_'), re.M):
        key = re.sub(r'[- ]+', '_', key.lower().strip())
        key = {'model_combo_9router': 'model_combo'}.get(key, key)
        value = value.strip('`\"\'')
        if key in pairs and pairs[key] != value:
            raise ConfigurationError('Nilai kredensial bertentangan: ' + key)
        pairs[key] = value
    return pairs


@dataclass
class Config:
    root: Path
    data: dict

    @classmethod
    def load(cls, root=None):
        root = (root or CODE_ROOT).resolve()
        data = tomllib.loads((root / 'config.example.toml').read_text())
        local = root / 'config.local.toml'
        if local.exists():
            for section, values in tomllib.loads(local.read_text()).items():
                data.setdefault(section, {}).update(values)
        config = cls(root, data)
        ai = credential_pairs(config.path(data['ai']['credentials_file']))
        if not data['ai'].get('base_url'):
            data['ai']['base_url'] = ai.get('base_url', '')
        if not data['ai'].get('model'):
            data['ai']['model'] = ai.get('model_combo', '')
        drive_file = config.path(data['drive'].get('folder_link_file', 'Credential/google-drive-link.md'))
        if drive_file.exists() and not data['drive'].get('folder_id'):
            match = re.search(r'drive\.google\.com/drive/folders/([A-Za-z0-9_-]+)', drive_file.read_text())
            if match:
                data['drive']['folder_id'] = match[1]
        wf = data['workflow']
        if type(wf['daily_limit']) is not int or not 1 <= wf['daily_limit'] <= 20:
            raise ConfigurationError('Batas video harus 1–20 per akun per hari.')
        if wf['timezone'] != 'Asia/Jakarta' or wf.get('project') != 'fahra-automation':
            raise ConfigurationError('Identitas proyek/zona waktu tidak sesuai Fahra Automation.')
        if wf.get('mode') not in {'shadow', 'live'}:
            raise ConfigurationError('Mode workflow harus shadow atau live.')
        if not config.runtime.resolve().is_relative_to(root / 'runtime'):
            raise ConfigurationError('Runtime harus berada di folder runtime proyek ini.')
        if not 0 <= wf['window_start'] < wf['window_end'] <= 24:
            raise ConfigurationError('Jendela jadwal tidak valid.')
        for key, minimum, maximum in (
                ('schedule_lead_seconds', 30, 3600), ('max_attempts', 1, 10),
                ('sync_interval_seconds', 30, 600), ('max_batch_links', 1, 100),
                ('sync_timeout_seconds', 30, 600),
                ('max_pending_assets', 1, 10000), ('render_horizon_hours', 1, 168),
                ('backup_interval_seconds', 300, 86400), ('min_free_disk_gb', 1, 10000)):
            if type(wf.get(key)) is not int or not minimum <= wf[key] <= maximum:
                raise ConfigurationError(f'Konfigurasi workflow.{key} harus {minimum}–{maximum}.')
        for section, key in (('workflow','publish_enabled'), ('telegram','public_ingest'),
                             ('cleanup','enabled'), ('ai','transcription_enabled')):
            if type(data[section].get(key)) is not bool:
                raise ConfigurationError(f'Konfigurasi {section}.{key} harus boolean.')
        if type(data.get('metrics', {}).get('enabled', False)) is not bool:
            raise ConfigurationError('Konfigurasi metrics.enabled harus boolean.')
        for key in ('daily_limit', 'max_age_hours', 'poll_seconds'):
            if type(data['reply'].get(key)) is not int or data['reply'][key] <= 0:
                raise ConfigurationError(f'Konfigurasi reply.{key} harus bilangan positif.')
        for section, key, default, minimum, maximum in (
                ('workflow', 'prepare_workers', 2, 1, 2),
                ('repliz', 'sync_concurrency', 2, 1, 2),
                ('reply', 'poll_max_seconds', 300, 120, 3600),
                ('reply', 'inbox_group_size', 10, 1, 30),
                ('reply', 'inbox_max_pages', 20, 2, 100),
                ('drive', 'parallel_verifications', 2, 1, 2)):
            value = data[section].setdefault(key, default)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ConfigurationError(f'Konfigurasi {section}.{key} harus {minimum}–{maximum}.')
        if data['reply']['poll_max_seconds'] < data['reply']['poll_seconds']:
            raise ConfigurationError('Batas jeda komentar tidak boleh lebih kecil dari jeda awal.')
        facebook_ids = data['repliz'].get('facebook_publish_account_ids')
        if (not isinstance(facebook_ids, list) or not facebook_ids
                or any(not isinstance(account_id, str) or not account_id for account_id in facebook_ids)
                or len(facebook_ids) != len(set(facebook_ids))
                or not set(facebook_ids).issubset(FACEBOOK_PUBLISH_ACCOUNT_IDS)):
            raise ConfigurationError('Allowlist Facebook Page tidak valid.')
        hook = data.setdefault('webhook', {})
        if type(hook.get('enabled', False)) is not bool:
            raise ConfigurationError('Konfigurasi webhook.enabled harus boolean.')
        if hook.get('enabled'):
            if hook.get('host', '127.0.0.1') != '127.0.0.1':
                raise ConfigurationError('Webhook harus menerima koneksi melalui reverse proxy loopback.')
            if type(hook.get('port', 8769)) is not int or not 1024 <= hook.get('port', 8769) <= 65535:
                raise ConfigurationError('Port webhook tidak valid.')
        if data['cleanup']['retention_hours'] < 24 or data['cleanup']['interval_seconds'] < 300:
            raise ConfigurationError('Cleanup memerlukan retensi 24 jam dan interval 300 detik atau lebih.')
        return config

    def section(self, name):
        return self.data[name]

    @property
    def facebook_publish_account_ids(self):
        return frozenset(self.data['repliz']['facebook_publish_account_ids'])

    def path(self, value):
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.root / path

    @property
    def runtime(self):
        return self.path(self.data['workflow']['runtime_dir'])

    @property
    def database_path(self):
        from .storage import database_path
        return database_path(self)

    def telegram_token(self):
        path = self.path(self.data['telegram']['token_file'])
        if not path.exists():
            raise ConfigurationError('File token Telegram belum tersedia.')
        match = re.search(r'\b\d{6,15}:[A-Za-z0-9_-]{25,}\b', path.read_text())
        if not match:
            raise ConfigurationError('Format token Telegram tidak valid.')
        token = match.group()
        for forbidden in self.data['telegram'].get('forbidden_token_files', []):
            other_file = self.path(forbidden)
            if other_file.exists():
                other = re.search(r'\b\d{6,15}:[A-Za-z0-9_-]{25,}\b', other_file.read_text())
                if other and token.split(':')[0] == other.group().split(':')[0]:
                    raise ConfigurationError('Bot lama tidak boleh digunakan untuk paket baru.')
        return token

    def require_production_checkout(self):
        if self.root.resolve() != CODE_ROOT or not (CODE_ROOT / '.git').is_dir():
            raise ConfigurationError('Layanan/publikasi tidak boleh dijalankan dari worktree atau root kode berbeda.')

    def require_live_action(self):
        self.require_production_checkout()
        wf = self.section('workflow')
        if wf.get('mode') != 'live' or not wf.get('publish_enabled'):
            raise ConfigurationError('Mode shadow: tindakan publikasi/automation ditahan.')
        from .ownership import verify_ownership
        verify_ownership(self)

    def repliz_credentials(self):
        pairs = credential_pairs(self.path(self.data['repliz']['credentials_file']))
        if not all(pairs.get(k) for k in ('access_key', 'secret_key')):
            raise ConfigurationError('Kredensial Repliz tidak lengkap.')
        return pairs['access_key'], pairs['secret_key']

    def ai_key(self):
        pairs = credential_pairs(self.path(self.data['ai']['credentials_file']))
        if pairs.get('api_key'):
            return pairs['api_key']
        path = self.path(self.data['ai'].get('api_key_file', 'Credential/ai_key.txt'))
        if not path.exists() or not path.read_text().strip():
            raise ConfigurationError('File API key AI belum tersedia.')
        return path.read_text().strip()


def allowed_account(account):
    identity = account.get('id', account.get('_id'))
    return (account.get('type') in PLATFORMS and isinstance(identity, str) and bool(identity)
            and identity != EXCLUDED_ACCOUNT_ID
            and not (account.get('type') == 'tiktok'
                     and str(account.get('username', '')).lower().lstrip('@') == EXCLUDED_USERNAME))
