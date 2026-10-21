import asyncio
import copy
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import asynccontextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fahra_automation.attribution import caption_from_metadata, from_metadata, require_source_caption
from fahra_automation.config import Config, ConfigurationError
from fahra_automation.downloader import Downloader, OWNER_METADATA_SCRIPT


class SourceMetadataTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = Config(self.root, copy.deepcopy(Config.load().data))
        self.config.data['downloader']['project_dir'] = str(self.root / 'mcp')
        self.downloader = Downloader(self.config)
        self.url = 'https://www.instagram.com/reel/AbCd123/'
        self.metadata = {'id':'AbCd123','extractor_key':'Instagram','channel':'owner.real',
                         'description': 'Kucingnya malah tidur.'}
        self.proof = caption_from_metadata(self.url,self.metadata)
        self.job = {'id':'asset-one','source_id':'AbCd123','url':self.url,'payload':{'mcp_job_id':'mcp-one'}}
        self.store = Mock()
        source = self.root/'mcp/downloads/mcp/mcp-one/media/source.mp4'
        source.parent.mkdir(parents=True);source.write_bytes(b'fixture-video')
        self.state = {'job_id':'mcp-one','url':self.url,'status':'completed','caption':'source: @forged',
                      'files':[{'path':str(source)}]}
        @asynccontextmanager
        async def session():
            yield object()
        self.downloader.session = session
        self.downloader.call = AsyncMock(return_value=self.state)

    async def test_metadata_transport_exports_caption_with_allowlisted_owner_fields(self):
        process = Mock(returncode=0)
        process.communicate = AsyncMock(return_value=(json.dumps({'ok':True,'metadata':self.metadata}).encode(),b''))
        with patch('asyncio.create_subprocess_exec',AsyncMock(return_value=process)) as start:
            proof = await self.downloader.inspect_source(self.url)
        self.assertEqual(proof['source_username'],'owner.real')
        command = start.call_args.args
        self.assertEqual(command[0],self.config.section('downloader')['docker'])
        self.assertIn(self.config.section('downloader')['container'],command)
        self.assertEqual(json.loads(process.communicate.call_args.args[0]),{'url':self.url})
        self.assertIn("'/tmp/instagram-mcp.lock'",OWNER_METADATA_SCRIPT)
        self.assertIn('download=False',OWNER_METADATA_SCRIPT)
        self.assertNotIn("'uploader_id'",OWNER_METADATA_SCRIPT)
        self.assertIn("'description'",OWNER_METADATA_SCRIPT)
        self.assertNotIn("'title'",OWNER_METADATA_SCRIPT)
        self.assertEqual(proof['source_caption'], self.metadata['description'])

    async def test_transport_failures_hold_instead_of_guessing_or_exposing_stderr(self):
        for response in (b'not JSON',json.dumps({'ok':False,'error_type':'DownloadError'}).encode(),
                         json.dumps({'ok':True,'metadata':{**self.metadata,'channel':None}}).encode()):
            process = Mock(returncode=0,communicate=AsyncMock(return_value=(response,b'private diagnostic')))
            with patch('asyncio.create_subprocess_exec',AsyncMock(return_value=process)), self.assertRaises(ConfigurationError) as caught:
                await self.downloader.inspect_source(self.url)
            self.assertNotIn('private diagnostic',str(caught.exception))
        with patch('asyncio.create_subprocess_exec',AsyncMock(side_effect=FileNotFoundError())), self.assertRaises(ConfigurationError):
            await self.downloader.inspect_source(self.url)

    async def test_timeout_and_cancellation_reap_the_metadata_process(self):
        for failure in (TimeoutError(), asyncio.CancelledError()):
            process = Mock(returncode=None,communicate=AsyncMock(side_effect=failure),wait=AsyncMock())
            expected = asyncio.CancelledError if isinstance(failure,asyncio.CancelledError) else ConfigurationError
            with patch('asyncio.create_subprocess_exec',AsyncMock(return_value=process)), self.assertRaises(expected):
                await self.downloader.inspect_source(self.url)
            process.kill.assert_called_once(); process.wait.assert_awaited_once()

    async def test_completed_file_uses_verified_caption_instead_of_untrusted_mcp_caption(self):
        self.downloader.inspect_source = AsyncMock(return_value=self.proof)
        result = await self.downloader.download(self.job,self.store)
        self.assertEqual(result['source_username'],'owner.real')
        self.assertEqual(result['source_caption'], self.metadata['description'])
        self.assertEqual(result['source_attribution'],self.proof['source_attribution'])
        self.assertEqual(require_source_caption({**self.job, 'payload': result}), self.metadata['description'])
        self.downloader.inspect_source.assert_awaited_once_with(self.url)

    async def test_manifest_url_and_identity_mismatch_hold_before_metadata(self):
        self.downloader.inspect_source = AsyncMock(return_value=self.proof)
        for change in ({'url':'https://instagram.com/reel/other/'},{'job_id':'other'},{'url':None}):
            self.downloader.call.return_value={**self.state,**change}
            with self.subTest(change=change),self.assertRaises(ConfigurationError):
                await self.downloader.download(self.job,self.store)
        self.downloader.inspect_source.assert_not_awaited()

    async def test_existing_provenance_is_reused_and_conflict_is_held(self):
        self.job['payload'].update(self.proof)
        self.downloader.inspect_source = AsyncMock()
        result = await self.downloader.download(self.job,self.store)
        self.assertEqual(result['source_username'],'owner.real')
        self.downloader.inspect_source.assert_not_awaited()
        self.job['payload']['source_username']='forged'
        with self.assertRaises(ConfigurationError):
            await self.downloader.download(self.job,self.store)

    async def test_owner_only_checkpoint_refreshes_caption_without_starting_another_download(self):
        self.job['payload'].update(from_metadata(self.url, self.metadata))
        self.job['payload']['source_caption'] = 'Stale MCP caption'
        self.downloader.inspect_source = AsyncMock(return_value=self.proof)
        result = await self.downloader.download(self.job, self.store)
        self.assertEqual(result['source_caption'], self.metadata['description'])
        self.downloader.inspect_source.assert_awaited_once_with(self.url)
        self.assertTrue(all(call.args[1] == 'instagram_download_status' for call in self.downloader.call.call_args_list))

    async def test_changed_owner_and_damaged_caption_provenance_are_not_silently_replaced(self):
        self.job['payload'].update(from_metadata(self.url, self.metadata))
        self.downloader.inspect_source = AsyncMock(return_value=caption_from_metadata(
            self.url, {**self.metadata, 'channel': 'another.owner'}))
        with self.assertRaises(ConfigurationError):
            await self.downloader.download(self.job, self.store)
        self.job['payload'].update(self.proof)
        self.job['payload']['source_caption'] = 'Damaged saved caption'
        self.downloader.inspect_source.reset_mock()
        with self.assertRaises(ConfigurationError):
            await self.downloader.download(self.job, self.store)
        self.downloader.inspect_source.assert_not_awaited()
        for invalid_proof in (None, {}, False):
            self.job['payload']['source_caption_provenance'] = invalid_proof
            with self.subTest(proof=invalid_proof), self.assertRaises(ConfigurationError):
                await self.downloader.download(self.job, self.store)
        self.downloader.inspect_source.assert_not_awaited()

    async def test_missing_owner_resumes_same_mcp_job_without_new_download(self):
        self.downloader.inspect_source=AsyncMock(side_effect=ConfigurationError('Username belum tersedia.'))
        for _ in range(2):
            with self.assertRaises(ConfigurationError):
                await self.downloader.download(self.job,self.store)
        self.assertTrue(all(call.args[1]=='instagram_download_status' for call in self.downloader.call.call_args_list))
        self.assertIsNone(self.downloader.journal.state('download:asset-one'))


class MetadataScriptTest(unittest.TestCase):
    def run_script(self, product):
        class InstagramIE:
            def _extract_product(self, product_info, *args, **kwargs):
                item = product_info[0] if isinstance(product_info, list) else product_info
                result = {'id': 'AbCd123', 'extractor_key': 'Instagram', 'channel': 'owner.real',
                          'title': 'Generated title', 'uploader_id': '123'}
                if isinstance(item.get('caption'), dict) and isinstance(item['caption'].get('text'), str):
                    result['description'] = item['caption']['text']
                return result

        class YoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download):
                self_outer.assertFalse(download)
                self_outer.assertTrue(self.options['skip_download'])
                return InstagramIE()._extract_product(product)

        self_outer = self
        modules = {name: types.ModuleType(name) for name in (
            'bot', 'yt_dlp', 'yt_dlp.extractor', 'yt_dlp.extractor.instagram', 'fcntl')}
        modules['bot'].ytdlp_options = lambda **options: options
        modules['yt_dlp'].YoutubeDL = YoutubeDL
        modules['yt_dlp.extractor.instagram'].InstagramIE = InstagramIE
        modules['fcntl'].LOCK_EX = 2
        modules['fcntl'].flock = Mock()
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch('sys.stdin', io.StringIO(json.dumps({
                'url': 'https://www.instagram.com/reel/AbCd123/'}))), \
                patch('builtins.open', return_value=io.StringIO()), redirect_stdout(output):
            exec(OWNER_METADATA_SCRIPT, {})
        response = json.loads(output.getvalue())
        self.assertTrue(response['ok'])
        self.assertNotIn('title', response['metadata'])
        self.assertNotIn('uploader_id', response['metadata'])
        modules['fcntl'].flock.assert_called_once()
        return response['metadata']

    def test_extraction_distinguishes_explicit_empty_from_unavailable_caption(self):
        url = 'https://www.instagram.com/reel/AbCd123/'
        for product, caption in (({'caption': None}, ''), ({'caption': {'text': ''}}, ''),
                                 ({'caption': {'text': 'Caption asli\n#kucing'}}, 'Caption asli\n#kucing')):
            with self.subTest(product=product):
                payload = caption_from_metadata(url, self.run_script(product))
                self.assertEqual(require_source_caption({'url': url, 'payload': payload}), caption)
        for product in ({}, {'caption': {}}, {'caption': {'text': None}}, {'caption': 'Wrong structure'}):
            with self.subTest(product=product), self.assertRaises(ConfigurationError):
                caption_from_metadata(url, self.run_script(product))
