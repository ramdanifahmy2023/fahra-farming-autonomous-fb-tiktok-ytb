import copy
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fahra_automation.config import Config
from fahra_automation.attribution import from_metadata
from fahra_automation.downloader import Downloader, DownloadUncertain
from fahra_automation.media import Media


GOOD = {'duration': 30, 'width': 1080, 'height': 1920, 'codec': 'h264', 'audio': True,
        'audio_codec': 'aac', 'pixel_format': 'yuv420p', 'fps': 30, 'size': 1024}


class MediaTest(unittest.TestCase):
    def test_render_filter_keeps_original_orientation_zoom_and_hook_timing(self):
        media = Media(Config.load())
        vf = media.render_filter(720, 1280)
        self.assertTrue(vf.startswith('[0:v]scale=792:1408,crop=720:1280:(iw-ow)/2:(ih-oh)/2,setsar=1,fps=30[base];'))
        self.assertIn("enable='lt(t,3)'", vf)
        self.assertIn('[base][1:v]overlay=x=0:y=0:eof_action=repeat:', vf)
        self.assertLess(vf.index('fps=30'), vf.index('overlay='))
        self.assertTrue(vf.endswith('[video]'))
        for transformation in ('hflip', 'vflip', 'transpose', 'rotate'):
            self.assertNotIn(transformation, vf)

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

    def test_render_maps_overlay_and_optional_original_audio_without_changing_generated_text(self):
        generated = {'hook': 'Café: 100% seru!', 'caption': 'Kucing penasaran.'}
        original = copy.deepcopy(generated)
        for audio in (True, False):
            with self.subTest(audio=audio), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / 'final.mp4'
                media = Media(Config.load())
                info = {**GOOD, 'audio': audio, 'audio_codec': 'aac' if audio else None}
                media.probe = Mock(side_effect=[info, info])

                def run(command, **kwargs):
                    if command[1] == 'cp' and command[-2].endswith('/final.mp4'):
                        Path(command[-1]).write_bytes(b'new verified render')
                    return ''

                media.run = Mock(side_effect=run)
                self.assertEqual(media.render(Path(folder) / 'source.mp4', target, generated, 'asset-fixture'), info)
                commands = [call.args[0] for call in media.run.call_args_list]
                command = next(command for command in commands if 'ffmpeg' in command)
                maps = [command[index + 1] for index, value in enumerate(command) if value == '-map']
                self.assertEqual(maps, ['[video]', '0:a:0?'])
                self.assertIn('/tmp/fahra_automation-render-asset-fixture/hook.png', command)
                self.assertEqual(command[command.index('-c:v') + 1], 'libx264')
                self.assertEqual(command[command.index('-c:a') + 1], 'aac')
                self.assertEqual(command[command.index('-pix_fmt') + 1], 'yuv420p')
                self.assertNotIn('-shortest', command)
                self.assertIn('title=' + generated['hook'], command)
                self.assertIn('comment=' + generated['caption'], command)
                self.assertEqual(target.read_bytes(), b'new verified render')
                self.assertFalse(target.with_suffix('.partial.mp4').exists())
                layout = json.loads((target.parent / 'hook-layout.json').read_text())
                self.assertEqual((target.parent / 'hook.txt').read_text(), '\n'.join(layout['lines']))
                self.assertEqual(generated, original)

    def test_render_failure_or_invalid_result_never_replaces_existing_final(self):
        for failure in ('ffmpeg', 'probe'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / 'final.mp4'
                target.write_bytes(b'previous verified render')
                media = Media(Config.load())
                media.probe = Mock(side_effect=[GOOD, {**GOOD, 'duration': 1}])

                def run(command, **kwargs):
                    if 'ffmpeg' in command and failure == 'ffmpeg':
                        raise RuntimeError('fixture encode failed')
                    if command[1] == 'cp' and command[-2].endswith('/final.mp4'):
                        Path(command[-1]).write_bytes(b'invalid new render')
                    return ''

                media.run = Mock(side_effect=run)
                with self.assertRaises((RuntimeError, ValueError)):
                    media.render(Path(folder) / 'source.mp4', target,
                                 {'hook': 'Kucing penasaran', 'caption': 'Kucing melihat kardus.'}, 'asset-fixture')
                self.assertEqual(target.read_bytes(), b'previous verified render')
                cleanup = media.run.call_args.args[0]
                self.assertIn('python', cleanup)
                self.assertEqual(cleanup[-1], '/tmp/fahra_automation-render-asset-fixture')


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
        self.downloader.call = AsyncMock(return_value={'status': 'completed', 'job_id': 'mcp-a', 'url': self.job['url'], 'files': [{'path': str(source)}]})
        self.downloader.inspect_source = AsyncMock(return_value=from_metadata(
            self.job['url'], {'id': 'a', 'extractor_key': 'Instagram', 'channel': 'fixture_creator'}))
        result = await self.downloader.download(self.job, self.store)
        self.assertEqual(result['mcp_job_id'], 'mcp-a')
        self.assertEqual(self.downloader.call.call_args.args[1], 'instagram_download_status')
        self.downloader.inspect_source.assert_awaited_once_with(self.job['url'])
        self.assertEqual(result['source_username'], 'fixture_creator')

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
        with self.assertRaises(ValueError):
            await self.downloader.download(self.job, self.store)
        self.store.checkpoint.assert_not_called()
