import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fahra_automation.analysis import Analyzer
from fahra_automation.db import Store
from fahra_automation.personas import (DEFAULT_PROFILE, LEGACY_DEFAULT_HASH, LEGACY_DEFAULT_PROFILE,
                                      PersonaRegistry, PersonaService, digest, is_default_profile)
from fahra_automation.reply_ai import ReplyAI, STYLE_VERSION


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

    def test_only_exact_known_profiles_are_defaults(self):
        self.assertTrue(is_default_profile(copy.deepcopy(LEGACY_DEFAULT_PROFILE)))
        self.assertTrue(is_default_profile(copy.deepcopy(DEFAULT_PROFILE)))
        self.assertFalse(is_default_profile({**LEGACY_DEFAULT_PROFILE, 'instructions': 'Gaya khusus akun.'}))
        self.assertFalse(is_default_profile({**DEFAULT_PROFILE, 'name': 'Nama khusus'}))
        self.assertFalse(is_default_profile(None))
        self.assertEqual(LEGACY_DEFAULT_HASH, digest(LEGACY_DEFAULT_PROFILE))

    def test_legacy_default_upgrade_is_immutable_idempotent_and_survives_restart(self):
        registry = PersonaRegistry(self.store)
        old = registry.bind('account-a', 'tiktok', LEGACY_DEFAULT_PROFILE)
        with self.store.connect() as c:
            before = tuple(c.execute('SELECT * FROM persona_versions WHERE version=1').fetchone())
        upgraded = registry.bind('account-a', 'tiktok')
        self.assertEqual(upgraded['persona_version'], 2)
        self.assertEqual(upgraded['profile'], DEFAULT_PROFILE)
        self.assertNotEqual(upgraded['persona_hash'], old['persona_hash'])
        self.assertEqual(PersonaRegistry(self.store).bind('account-a', 'tiktok'), upgraded)
        with self.store.connect() as c:
            self.assertEqual(tuple(c.execute('SELECT * FROM persona_versions WHERE version=1').fetchone()), before)
            self.assertEqual(c.execute('SELECT count(*) FROM persona_versions').fetchone()[0], 2)

    def test_custom_or_mismatched_legacy_binding_is_not_upgraded(self):
        registry = PersonaRegistry(self.store)
        custom = registry.bind('account-a', 'tiktok', {**LEGACY_DEFAULT_PROFILE, 'instructions': 'Bahasa formal khusus akun.'})
        self.assertEqual(registry.bind('account-a', 'tiktok'), custom)
        legacy = registry.bind('account-b', 'facebook', LEGACY_DEFAULT_PROFILE)
        with self.store.connect() as c:
            c.execute("UPDATE persona_versions SET hash='unrecognized' WHERE persona_id=?", (legacy['persona_id'],))
        preserved = registry.bind('account-b', 'facebook')
        self.assertEqual(preserved['persona_version'], 1)
        self.assertEqual(preserved['profile'], LEGACY_DEFAULT_PROFILE)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM persona_versions').fetchone()[0], 2)

    def test_concurrent_legacy_upgrade_creates_one_new_version(self):
        registry = PersonaRegistry(self.store)
        registry.bind('account-a', 'tiktok', LEGACY_DEFAULT_PROFILE)
        barrier = Barrier(4)
        def bind():
            barrier.wait(timeout=5)
            return registry.bind('account-a', 'tiktok')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: bind(), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(results[0]['persona_version'], 2)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM persona_versions').fetchone()[0], 2)

    async def test_default_upgrade_preserves_cached_and_submitted_delivery_snapshots(self):
        service = PersonaService(self.config, self.store, self.ai)
        old = service.registry.bind('account-a', 'tiktok', LEGACY_DEFAULT_PROFILE)
        evidence_hash = digest({k: self.job['payload'].get(k) for k in
                               ('source_sha256', 'render_sha256', 'generated', 'caption_review', 'transcript')})
        snapshot = {**{k: v for k, v in old.items() if k != 'profile'}, 'persona_profile': old['profile'],
                    'caption': 'Caption lama tetap.', 'evidence_hash': evidence_hash}
        self.job['payload']['delivery_metadata'] = copy.deepcopy(snapshot)
        before = copy.deepcopy(self.job)
        self.assertEqual(await service.prepare_delivery(self.job), snapshot)
        self.assertEqual(self.job, before)
        self.assertEqual(service.registry.bind('account-a', 'tiktok')['persona_version'], 2)
        self.job.update(status='scheduled', schedule_id='schedule-fixture')
        self.job['payload']['submission'] = {'accountId': 'account-a', 'description': 'Caption lama tetap.'}
        self.assertEqual(await service.prepare_delivery(self.job), snapshot)
        self.ai.delivery_metadata.assert_not_awaited()

    async def test_legacy_and_current_default_reply_keep_markdown_style_and_persona_snapshot(self):
        ai = ReplyAI(SimpleNamespace(root=Path(__file__).resolve().parents[1]))
        ai.generate = AsyncMock(return_value={'action':'reply', 'text':'Kucingnya ikut duduk.'})
        context = {'quality':'usable', 'tone':'ringan', 'topic':'kucing', 'hash':'fixture-context'}
        for index, profile in enumerate((LEGACY_DEFAULT_PROFILE, DEFAULT_PROFILE), start=1):
            with self.subTest(version=index):
                persona = {'profile':copy.deepcopy(profile), 'persona_version':index}
                event = {'id':'default-fixture', 'payload':{'text':'Ikut duduk dia'}, 'persona':persona}
                before = copy.deepcopy(event)
                result = await ai.draft(event, context, [], self.root / 'draft')
                prompt, content = ai.generate.call_args.args[:2]
                self.assertIn('Tulis seperti teman membalas chat.', prompt)
                self.assertEqual(result['style_version'], STYLE_VERSION)
                self.assertEqual(result['persona'], persona)
                self.assertEqual(json.loads(content)['persona'], profile)
                self.assertEqual(event, before)

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
