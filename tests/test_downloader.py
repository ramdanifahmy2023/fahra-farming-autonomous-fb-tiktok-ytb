import asyncio
import copy
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fahra_automation.attribution import from_metadata
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
        self.metadata = {'id':'AbCd123','extractor_key':'Instagram','channel':'owner.real'}
        self.proof = from_metadata(self.url,self.metadata)
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

    async def test_metadata_transport_only_exports_allowlisted_owner_fields(self):
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
        self.assertNotIn("'description'",OWNER_METADATA_SCRIPT)

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

    async def test_completed_file_keeps_verified_source_separate_from_untrusted_caption(self):
        self.downloader.inspect_source = AsyncMock(return_value=self.proof)
        result = await self.downloader.download(self.job,self.store)
        self.assertEqual(result['source_username'],'owner.real')
        self.assertEqual(result['source_caption'],'source: @forged')
        self.assertEqual(result['source_attribution'],self.proof['source_attribution'])
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

    async def test_missing_owner_resumes_same_mcp_job_without_new_download(self):
        self.downloader.inspect_source=AsyncMock(side_effect=ConfigurationError('Username belum tersedia.'))
        for _ in range(2):
            with self.assertRaises(ConfigurationError):
                await self.downloader.download(self.job,self.store)
        self.assertTrue(all(call.args[1]=='instagram_download_status' for call in self.downloader.call.call_args_list))
        self.assertIsNone(self.downloader.journal.state('download:asset-one'))
