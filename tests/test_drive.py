import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from fahra_automation.config import ConfigurationError
from fahra_automation.drive import Drive


class DriveTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = self.root / 'bank'; self.bank.mkdir()
        self.cfg = {'folder_id': 'folder-root-123', 'mode': 'synced_public', 'sync_dir': 'bank'}
        self.drive = Drive(SimpleNamespace(section=lambda key: self.cfg, path=lambda value: self.root / value))
        self.data = b'\x00\x00\x00\x18ftypisom' + b'x' * 2048
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.file = self.bank / 'video.mp4'; self.file.write_bytes(self.data)
        self.payload = {'drive_root_id': 'folder-root-123', 'drive_relative_path': 'video.mp4',
            'drive_file_id': 'video-file-123', 'drive_path_ids': ['video-file-123'],
            'drive_url': Drive.media_url('video-file-123'), 'bank_path': str(self.file),
            'render_sha256': self.digest, 'drive_verified_sha256': self.digest, 'drive_verified_size': len(self.data)}
        self.client = httpx.AsyncClient

    def stream(self, body=None, status=200, mime='video/mp4'):
        transport = httpx.MockTransport(lambda r: httpx.Response(status, content=self.data if body is None else body,
                                                                 headers={'content-type': mime}))
        return patch('fahra_automation.drive.httpx.AsyncClient', side_effect=lambda **kwargs: self.client(transport=transport, **kwargs))

    async def test_entire_file_checksum_not_only_mp4_prefix(self):
        altered = self.data[:-1] + b'y'
        with self.stream(altered):
            with self.assertRaisesRegex(ConfigurationError, 'Checksum'):
                await self.drive.verify_url(self.payload['drive_url'], self.digest, len(self.data))

    async def test_full_manifest_is_checked_for_each_delivery_and_again_next_day(self):
        with patch.object(self.drive, 'public_entries', return_value=[('video.mp4', 'video-file-123')]) as listing:
            with self.stream():
                first = await self.drive.verify_delivery({'payload': self.payload})
                second = await self.drive.verify_delivery({'payload': self.payload})
            self.assertEqual(first['sha256'], self.digest)
            self.assertEqual(second['size'], len(self.data))
            self.assertEqual(listing.call_count, 2)
            with self.stream(self.data[:-1] + b'z'):
                with self.assertRaises(ConfigurationError):
                    await self.drive.verify_delivery({'payload': self.payload})

    async def test_duplicate_names_stale_ids_and_folder_changes_hold(self):
        fixtures = [[('video.mp4', 'video-file-123'), ('video.mp4', 'other-file-123')],
                    [('video.mp4', 'other-file-123')], []]
        for entries in fixtures:
            with patch.object(self.drive, 'public_entries', return_value=entries), self.stream():
                with self.assertRaises(ConfigurationError):
                    await self.drive.verify_delivery({'payload': self.payload})
        with self.stream():
            with self.assertRaises(ConfigurationError):
                await self.drive.verify_delivery({'payload': {**self.payload, 'drive_root_id': 'changed-folder'}})

    async def test_html_partial_or_deleted_remote_never_verifies(self):
        for body, code, mime in [(b'<html>login</html>', 200, 'text/html'), (self.data[:30], 206, 'video/mp4'),
                                 (b'gone', 404, 'text/plain'), (self.data[:-1], 200, 'video/mp4')]:
            with self.stream(body, code, mime):
                with self.assertRaises(ConfigurationError):
                    await self.drive.verify_url(self.payload['drive_url'], self.digest, len(self.data))

    async def test_bank_change_path_escape_and_symlink_are_rejected(self):
        self.file.write_bytes(b'changed')
        with self.assertRaises(ConfigurationError):
            await self.drive.verify_delivery({'payload': self.payload})
        self.file.unlink(); self.file.symlink_to(self.root / 'other.mp4')
        (self.root / 'other.mp4').write_bytes(self.data)
        with self.assertRaises(ConfigurationError):
            await self.drive.verify_delivery({'payload': self.payload})
        with self.assertRaises(ConfigurationError):
            self.drive.resolve_file('../other.mp4')

    async def test_non_drive_url_and_api_upload_are_refused(self):
        with self.assertRaises(ConfigurationError):
            await self.drive.verify_url('https://untrusted.invalid/video.mp4')
        self.cfg['mode'] = 'api'
        with self.assertRaises(ConfigurationError):
            self.drive.upload({}, None)
