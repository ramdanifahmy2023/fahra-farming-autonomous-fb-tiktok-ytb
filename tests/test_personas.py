import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fahra_automation.analysis import Analyzer
from fahra_automation.db import Store
from fahra_automation.personas import DEFAULT_PROFILE, PersonaRegistry, PersonaService


class PersonaTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'workflow.sqlite3')
        self.config = SimpleNamespace(runtime=self.root)
        self.ai = SimpleNamespace(delivery_metadata=AsyncMock(return_value={
            'title': 'Kucing mengintip', 'caption': 'Matanya langsung melotot.',
            'description': 'Matanya langsung melotot.\n\n#kucing', 'hashtags': ['#kucing'], 'tags': []}))
        self.job = {'id': 'job1', 'platform': 'tiktok', 'account_id': 'account-a', 'status': 'rendered',
            'payload': {'generated': {'hook': 'Matanya langsung melotot'},
                        'caption_review': {'observations': 'Kucing melihat tangan.', 'confidence': .9, 'tone': 'ringan'}}}

    def test_versions_are_immutable_and_survive_restart(self):
        first = PersonaRegistry(self.store).bind('account-a', 'tiktok')
        revised = {**DEFAULT_PROFILE, 'instructions': 'Bahasa Indonesia formal, tanpa emoji.'}
        second = PersonaRegistry(self.store).bind('account-a', 'tiktok', revised)
        self.assertEqual((first['persona_version'], second['persona_version']), (1, 2))
        self.assertEqual(PersonaRegistry(self.store).bind('account-a', 'tiktok'), second)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM persona_versions').fetchone()[0], 2)
        self.assertEqual(first['profile'], DEFAULT_PROFILE)

    async def test_delivery_snapshot_stays_stable_on_profile_change_and_restart(self):
        service = PersonaService(self.config, self.store, self.ai)
        first = await service.prepare_delivery(self.job)
        self.job['payload']['delivery_metadata'] = first
        service.registry.bind('account-a', 'tiktok', {**DEFAULT_PROFILE, 'instructions': 'Gaya formal.'})
        again = await PersonaService(self.config, self.store, self.ai).prepare_delivery(self.job)
        self.assertEqual(again, first)
        self.ai.delivery_metadata.assert_awaited_once()

    async def test_account_reassignment_regenerates_metadata_but_preserves_shared_hook(self):
        service = PersonaService(self.config, self.store, self.ai)
        first = await service.prepare_delivery(self.job)
        hook = copy.deepcopy(self.job['payload']['generated'])
        self.job['payload']['delivery_metadata'] = first
        self.job['account_id'] = 'account-b'
        second = await service.prepare_delivery(self.job)
        self.assertEqual(second['account_id'], 'account-b')
        self.assertNotEqual(first['persona_id'], second['persona_id'])
        self.assertEqual(hook, self.job['payload']['generated'])
        self.assertEqual(self.ai.delivery_metadata.await_count, 2)

    async def test_intent_blocks_rebinding_or_generating_missing_metadata(self):
        self.job['payload']['submission'] = {'accountId': 'account-a'}
        with self.assertRaisesRegex(ValueError, 'ber-intent'):
            await PersonaService(self.config, self.store, self.ai).prepare_delivery(self.job)
        self.ai.delivery_metadata.assert_not_awaited()

    def test_youtube_metadata_requires_title_and_separate_search_tags(self):
        job = {**self.job, 'platform': 'youtube'}
        good = {'title': 'Kucing mengintip', 'caption': 'Matanya langsung melotot.',
                'hashtags': ['#kucing'], 'tags': ['tingkah kucing']}
        result = Analyzer.validate_delivery_metadata(good, job)
        self.assertEqual(result['tags'], ['tingkah kucing'])
        self.assertIn('#kucing', result['description'])
        for bad in ({**good, 'title': ''}, {**good, 'title': 'x' * 101},
                    {**good, 'tags': ['#kucing']}, {**good, 'tags': ['x' * 501]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Analyzer.validate_delivery_metadata(bad, job)

    def test_platform_tags_and_serious_tone_constraints(self):
        good = {'title': '', 'caption': 'Matanya langsung melotot.', 'hashtags': ['#kucing'], 'tags': []}
        self.assertEqual(Analyzer.validate_delivery_metadata(good, self.job)['tags'], [])
        with self.assertRaises(ValueError):
            Analyzer.validate_delivery_metadata({**good, 'tags': ['kucing']}, self.job)
        self.job['payload']['caption_review']['tone'] = 'serius'
        with self.assertRaises(ValueError):
            Analyzer.validate_delivery_metadata({**good, 'caption': 'Matanya langsung melotot 😭'}, self.job)
