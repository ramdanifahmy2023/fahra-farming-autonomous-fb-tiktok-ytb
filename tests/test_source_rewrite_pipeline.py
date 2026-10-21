import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fahra_automation.attribution import caption_from_metadata, from_metadata
from fahra_automation.captioning import REWRITE_POLICY_VERSION, deterministic_source_copy
from fahra_automation.config import Config, ConfigurationError
from fahra_automation.db import Store
from fahra_automation.personas import PersonaService
from fahra_automation.pipeline import Pipeline
from fahra_automation.repliz import Repliz
from fahra_automation.util import now


URL = 'https://www.instagram.com/reel/REWRITE/'


def source(caption):
    return caption_from_metadata(URL, {'id': 'REWRITE', 'extractor_key': 'Instagram',
                                      'channel': 'fixture_creator', 'description': caption})


class RewritePipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        data = copy.deepcopy(Config.load().data)
        data['workflow'].update(runtime_dir='runtime', journal_dir='runtime/attempts', min_free_disk_gb=0)
        self.config = Config(self.root, data)
        self.store = Store(self.root / 'runtime/workflow.sqlite3')
        self.store.enqueue(0, 0, 1, [(URL, 'REWRITE')], can_allocate=False)
        self.delivery = self.store.queue()[0]
        self.asset_id = self.delivery['asset_id']
        self.analyzer = SimpleNamespace(ready=Mock(), analyze=AsyncMock(return_value={
            'generated': {'hook': '', 'caption': 'Kucing tidur.', 'hashtags': [], 'title': 'Kucing tidur'},
            'caption_policy_version': REWRITE_POLICY_VERSION}))
        self.media = SimpleNamespace(validate_shared=Mock(), probe=Mock(), render=Mock())
        self.downloader = SimpleNamespace(inspect_source=AsyncMock(return_value=source('Kucingnya tidur.')))
        self.drive = SimpleNamespace(ready=Mock(), upload=Mock(return_value={'drive_file_id': 'file'}))
        self.pipeline = Pipeline(self.config, self.store, None, self.downloader, self.media,
                                 self.analyzer, self.drive, personas=SimpleNamespace())
        self.store.asset_checkpoint(self.asset_id, 'downloaded', payload={
            'source_path': '/fixture/source.mp4', 'source_probe': {'duration': 10},
            **from_metadata(URL, {'id': 'REWRITE', 'extractor_key': 'Instagram', 'channel': 'fixture_creator'})})

    async def test_old_download_is_enriched_before_rewrite(self):
        await self.pipeline.prepare(self.delivery)
        analyzed = self.analyzer.analyze.call_args.args[0]
        self.assertEqual(analyzed['payload']['source_caption'], 'Kucingnya tidur.')
        self.assertEqual(self.store.asset(self.asset_id)['status'], 'analyzed')
        self.downloader.inspect_source.assert_awaited_once_with(URL)
        self.media.render.assert_not_called()

    async def test_analyzed_legacy_asset_is_rewritten_before_first_render(self):
        self.store.asset_checkpoint(self.asset_id, 'analyzed', payload={
            'caption_policy_version': 'id-natural-v4', 'generated': {'hook': 'Hook lama', 'caption': 'Cerita lama'}})
        await self.pipeline.prepare(self.delivery)
        self.analyzer.analyze.assert_awaited_once()
        self.media.render.assert_not_called()
        self.assertEqual(self.store.asset(self.asset_id)['payload']['generated']['hook'], '')

    async def test_invalid_existing_caption_proof_is_not_silently_replaced(self):
        payload = source('Kucingnya tidur.')
        payload['source_caption'] = 'Isi yang sudah diganti.'
        self.store.asset_checkpoint(self.asset_id, payload=payload)
        with self.assertRaises(ConfigurationError):
            await self.pipeline.prepare(self.delivery)
        self.analyzer.analyze.assert_not_awaited()
        self.downloader.inspect_source.assert_not_awaited()

    async def test_changed_owner_is_held_before_rewrite(self):
        self.downloader.inspect_source.return_value = caption_from_metadata(URL, {
            'id': 'REWRITE', 'extractor_key': 'Instagram', 'channel': 'another_owner', 'description': 'Caption.'})
        with self.assertRaises(ConfigurationError):
            await self.pipeline.prepare(self.delivery)
        self.analyzer.analyze.assert_not_awaited()
        self.assertEqual(self.store.asset(self.asset_id)['payload']['source_username'], 'fixture_creator')

    async def test_rendered_artifact_keeps_its_existing_copy_and_media(self):
        original = {'caption_policy_version': 'id-natural-v4', 'generated': {'hook': 'Hook lama', 'caption': 'Arsip'},
                    'render_path': '/fixture/final.mp4', 'render_sha256': 'original-hash'}
        self.store.asset_checkpoint(self.asset_id, 'rendered', payload=original)
        await self.pipeline.prepare(self.delivery)
        self.media.render.assert_not_called()
        self.analyzer.analyze.assert_not_awaited()
        self.downloader.inspect_source.assert_not_awaited()
        self.assertEqual(self.store.asset(self.asset_id)['payload']['generated'], original['generated'])


class RewritePublicationTest(unittest.TestCase):
    def job(self, caption, platform='tiktok'):
        return {'id': 'delivery', 'platform': platform, 'account_id': 'fixture-account', 'url': URL,
                'source_id': 'REWRITE', 'payload': {**source(caption), 'caption_policy_version': REWRITE_POLICY_VERSION,
                    'drive_url': 'https://example.invalid/video.mp4',
                    'delivery_metadata': {'account_id': 'fixture-account', 'platform': platform,
                        'caption': caption, 'description': caption, 'title': 'Video dari Instagram',
                        'hashtags': [], 'tags': []}}}

    def test_verified_empty_caption_yields_only_credit_on_all_platforms(self):
        for platform in ('tiktok', 'facebook', 'youtube'):
            with self.subTest(platform=platform):
                body = Repliz.payload(self.job('', platform), now())
                self.assertEqual(body['description'], 'source: @fixture_creator')
                self.assertEqual(body['title'], 'Video dari Instagram')

    def test_empty_or_missing_source_does_not_allow_invented_description(self):
        job = self.job('')
        job['payload']['delivery_metadata']['caption'] = 'Cerita buatan.'
        with self.assertRaises(ValueError):
            Repliz.payload(job, now())
        job = self.job('')
        job['payload'].pop('source_caption_provenance')
        with self.assertRaises(ConfigurationError):
            Repliz.payload(job, now())

    def test_nonprose_source_is_preserved_without_invented_story(self):
        for caption in ('😭', '#kucing #tidur', 'https://example.invalid/video', '@someone', '123'):
            generated = deterministic_source_copy(caption)
            expected = '\n\n'.join(part for part in
                (generated['caption'], 'source: @fixture_creator', ' '.join(generated['hashtags'])) if part)
            for platform in ('tiktok', 'facebook', 'youtube'):
                with self.subTest(caption=caption, platform=platform):
                    job = self.job(caption, platform)
                    metadata = job['payload']['delivery_metadata']
                    metadata.update(generated)
                    metadata['description'] = '\n\n'.join(part for part in
                        (generated['caption'], ' '.join(generated['hashtags'])) if part)
                    self.assertEqual(Repliz.payload(job, now())['description'], expected)
                    metadata['caption'] = 'Cerita sedih.'
                    with self.assertRaises(ValueError):
                        Repliz.payload(job, now())

    def test_source_changes_invalidate_new_candidates_without_changing_legacy_hash(self):
        old = {'source_sha256': 'source', 'generated': {'caption': 'Arsip'}}
        unchanged = PersonaService.evidence_hash(old)
        self.assertEqual(unchanged, PersonaService.evidence_hash({**old, 'source_caption': 'Extra legacy field'}))
        new = {**old, **self.job('Caption asli')['payload']}
        self.assertNotEqual(PersonaService.evidence_hash(new),
                            PersonaService.evidence_hash({**new, 'source_caption': 'Caption berbeda'}))
