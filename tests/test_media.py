import copy
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fahra_automation.config import Config
from fahra_automation.downloader import Downloader, DownloadUncertain
from fahra_automation.media import Media


GOOD = {'duration': 30, 'width': 1080, 'height': 1920, 'codec': 'h264', 'audio': True,
        'audio_codec': 'aac', 'pixel_format': 'yuv420p', 'fps': 30, 'size': 1024}


class MediaTest(unittest.TestCase):
    def test_shared_profile_accepts_boundaries_and_no_audio(self):
        for duration in (3, 90):
            for platform in ('tiktok', 'facebook', 'youtube'):
                Media.validate_delivery({**GOOD, 'duration': duration}, platform)
        Media.validate_delivery({**GOOD, 'audio': False, 'audio_codec': None}, 'youtube')

    def test_shared_profile_rejects_unapproved_adaptation_and_invalid_output(self):
        for patch in ({'duration': 2.9}, {'duration': 90.1}, {'duration': float('nan')},
                      {'width': 1920, 'height': 1080}, {'width': 360, 'height': 640},
                      {'codec': 'hevc'}, {'audio_codec': 'mp3'}, {'pixel_format': 'yuv444p'},
                      {'fps': 23}, {'size': 1_000_000_001}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                Media.validate_delivery({**GOOD, **patch}, 'youtube')


class DownloaderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = Config.load()
        self.config.data = copy.deepcopy(self.config.data)
        self.config.data['workflow']['runtime_dir'] = str(self.root)
        self.config.data['workflow']['journal_dir'] = str(self.root / 'journal')
        self.config.data['downloader']['project_dir'] = str(self.root / 'mcp')
        self.downloader = Downloader(self.config)
        @asynccontextmanager
        async def session():
            yield object()
        self.downloader.session = session
        self.job = {'id': 'asset1', 'url': 'https://instagram.com/reel/a/', 'payload': {}}
        self.store = Mock()

    async def test_ambiguous_start_holds_after_db_restore_without_second_start(self):
        self.downloader.call = AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(DownloadUncertain):
            await self.downloader.download(self.job, self.store)
        with self.assertRaises(DownloadUncertain):
            await self.downloader.download(self.job, self.store)
        self.assertEqual(self.downloader.call.await_count, 1)

    async def test_durable_job_id_resumes_after_db_checkpoint_crash(self):
        source = self.root / 'mcp/downloads/mcp/mcp-a/media/source.mp4'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'fixture')
        self.downloader.journal.begin('download:asset1', {'url': self.job['url']})
        self.downloader.journal.mark('download:asset1', 'accepted', schedule_id='mcp-a')
        self.downloader.call = AsyncMock(return_value={'status': 'completed', 'files': [{'path': str(source)}]})
        result = await self.downloader.download(self.job, self.store)
        self.assertEqual(result['mcp_job_id'], 'mcp-a')
        self.assertEqual(self.downloader.call.call_args.args[1], 'instagram_download_status')

    def test_source_rejects_foreign_job_arbitrary_file_and_symlink_parent(self):
        source = self.root / 'mcp/downloads/mcp/mcp-a/media/source.mp4'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'fixture')
        other = self.root / 'mcp/downloads/mcp/mcp-b/media'
        other.mkdir(parents=True)
        arbitrary = self.root / 'private.txt'
        arbitrary.write_text('fixture')
        alias = self.root / 'alias'
        alias.symlink_to(source.parent, target_is_directory=True)
        for path, identity in [(source, 'mcp-b'), (arbitrary, 'mcp-a'), (alias / source.name, 'mcp-a'),
                               (source, '../mcp-a')]:
            with self.subTest(path=path, identity=identity), self.assertRaises(ValueError):
                self.downloader.source_path(str(path), identity)

    async def test_partial_or_failed_preserves_mapping_without_restart(self):
        self.job['payload']['mcp_job_id'] = 'known'
        self.downloader.call = AsyncMock(return_value={'status': 'partial', 'files': [{'path': 'x'}]})
        with self.assertRaises(DownloadUncertain):
            await self.downloader.download(self.job, self.store)
        self.downloader.call.return_value = {'status': 'failed', 'error': 'network'}
        with self.assertRaises(RuntimeError):
            await self.downloader.download(self.job, self.store)
        self.store.checkpoint.assert_not_called()
