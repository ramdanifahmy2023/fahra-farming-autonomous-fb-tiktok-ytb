import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from fahra_automation.cleanup import Cleaner, CleanupBlocked
from fahra_automation.config import Config
from fahra_automation.db import Store
from fahra_automation.storage import migrate
from fahra_automation.util import iso, now, process_lock


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root, self.peer = self.base / 'project', self.base / 'peer'
        code_root = patch('fahra_automation.cleanup.CODE_ROOT', self.root)
        code_root.start()
        self.addCleanup(code_root.stop)
        for root in (self.root, self.peer):
            root.mkdir()
            (root / '.git').mkdir()
            (root / 'bank').mkdir()
            (root / 'config.example.toml').write_text('[workflow]\nruntime_dir="runtime"\n[drive]\nsync_dir="bank"\n')
        self.mcp = self.base / 'downloader/downloads/mcp'
        self.mcp.mkdir(parents=True)
        self.store = Store(self.root / 'runtime/workflow.sqlite3')
        self.peer_store = Store(self.peer / 'runtime/workflow.sqlite3')
        self.config = Config(self.root, {'workflow': {'runtime_dir': 'runtime'},
            'cleanup': {'enabled': True, 'retention_hours': 24, 'interval_seconds': 3600, 'peer_roots': [str(self.peer)]},
            'drive': {'sync_dir': 'bank'}, 'downloader': {'project_dir': str(self.base / 'downloader')}})
        guard = patch.object(Config, 'require_live_action')
        guard.start()
        self.addCleanup(guard.stop)
        self.cleaner = Cleaner(self.config, self.store)
        self.cleaner.verify_remote = AsyncMock()
        self.store.enqueue(1, 1, 1, [('https://www.instagram.com/reel/cleanup/', 'cleanup')], can_allocate=False)
        with self.store.connect() as c:
            self.jobs = [dict(r) for r in c.execute('SELECT * FROM jobs')]
        self.paired = 'asset_id' in self.jobs[0]
        self.owner = self.jobs[0].get('asset_id', self.jobs[0]['id'])
        accounts = [{'id': 'acct-' + j.get('platform', 'tiktok'), 'type': j.get('platform', 'tiktok'),
                     'username': 'someone', 'name': 'Somebody', 'isConnected': True, 'publishEligible': True} for j in self.jobs]
        self.store.sync_accounts(accounts)
        self.folder = self.root / 'runtime/media' / self.owner
        analysis = self.folder / 'analysis'
        analysis.mkdir(parents=True)
        self.frames = [analysis / f'frame-{i}.jpg' for i in range(6)]
        for frame in self.frames:
            frame.write_bytes(b'\xff\xd8' + b'frame' * 30 + b'\xff\xd9')
        self.audio = analysis / 'speech.wav'
        self.audio.write_bytes(b'RIFF' + b'audio' * 50)
        self.trace = analysis / 'caption-review.json'
        self.trace.write_text('{"transcript":"contoh","context":"tetap ada"}')
        self.render = self.folder / 'final.mp4'
        self.render.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'rendered' * 100)
        self.mcp_id = 'a' * 32
        self.source = self.mcp / self.mcp_id / 'media/Video source.mp4'
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'original' * 100)
        (self.source.parent.parent / 'status.json').write_text(json.dumps({'job_id': self.mcp_id, 'status': 'completed',
            'files': [{'path': str(self.source), 'size_bytes': self.source.stat().st_size}]}))
        self.bank = self.root / 'bank' / (self.owner + '.mp4')
        self.bank.write_bytes(self.render.read_bytes())
        self.payload = {'source_path': str(self.source), 'source_sha256': self.sha(self.source),
            'mcp_job_id': self.mcp_id, 'render_path': str(self.render), 'render_sha256': self.sha(self.render),
            'drive_verified_sha256': self.sha(self.render), 'drive_file_id': 'drive-file-12345',
            'bank_path': str(self.bank), 'drive_relative_path': self.bank.name, 'transcript': 'tetap disimpan'}
        if self.paired:
            self.store.asset_checkpoint(self.owner, 'drive_ready', payload=self.payload)
        for job in self.jobs:
            platform = job.get('platform', 'tiktok')
            with self.store.tx() as c:
                c.execute('UPDATE jobs SET account_id=? WHERE id=?', ('acct-' + platform, job['id']))
            link = {'tiktok': 'https://www.tiktok.com/@someone/video/123456',
                    'facebook': 'https://www.facebook.com/reel/123456',
                    'youtube': 'https://www.youtube.com/watch?v=abcdefghijk'}[platform]
            with patch('fahra_automation.db.iso', return_value=iso(now() - timedelta(hours=25))):
                self.store.checkpoint(job['id'], 'published', payload={**self.payload, 'post_id': '123456', 'post_url': link}, schedule_id='schedule-' + job['id'])

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def states(self):
        return [g['state'] for g in self.cleaner.plan()['groups']]

    def assert_kept(self):
        for file in (self.source, self.render, self.audio, self.bank, self.trace, *self.frames):
            self.assertTrue(file.exists(), str(file))

    def test_legacy_bank_is_mapped_but_old_workspace_references_still_block(self):
        moved = self.peer / 'runtime/media' / self.owner
        moved.parent.mkdir(parents=True)
        self.folder.rename(moved)
        bank = self.peer / 'bank' / self.bank.name
        self.bank.rename(bank)
        payload = {**self.payload, 'legacy_origin':'tiktok', 'bank_path':str(bank),
                   'render_path':str(moved/'final.mp4')}
        self.store.asset_checkpoint(self.owner, payload=payload)
        self.peer_store.enqueue(1,1,1,[('https://www.instagram.com/reel/legacy/', 'legacy')],can_allocate=False)
        peer_asset = self.peer_store.job(self.peer_store.queue()[0]['id'])['asset_id']
        self.peer_store.asset_checkpoint(peer_asset,payload=payload)
        with self.peer_store.tx() as c:
            c.execute('PRAGMA defer_foreign_keys=ON')
            c.execute('UPDATE assets SET id=? WHERE id=?',(self.owner,peer_asset))
            c.execute('UPDATE jobs SET asset_id=? WHERE asset_id=?',(self.owner,peer_asset))
        folder = self.config.runtime/'migrations/final-fixture'
        archived = self.peer_store.backup(folder)
        manifest = {'id':'migration-fixture','sources':[{'origin':'tiktok','root':str(self.peer),
                    'file':archived.name,'sha256':self.sha(archived)}]}
        (folder/'manifest.json').write_text(json.dumps(manifest))
        self.config.data['workflow']['legacy_roots'] = [str(self.peer)]
        self.store.set('migration_id','migration-fixture')
        self.store.set('migration_report',{'sources':[{'origin':'tiktok','snapshot':str(archived),'sha256':self.sha(archived)}]})
        with self.store.tx() as c:
            c.execute('CREATE TABLE legacy_mapping(origin TEXT,kind TEXT,legacy_id TEXT,new_id TEXT)')
            c.execute("INSERT INTO legacy_mapping VALUES('tiktok','asset',?,?)",(self.owner,self.owner))
        plan = self.cleaner.plan()
        item = next(g for g in plan['groups'] if g['id']==self.owner)
        self.assertEqual(item['state'],'blocked')
        self.assertIn('masih dirujuk',item['reason'])
        self.assertTrue(bank.exists())
        self.assertTrue((moved/'final.mp4').exists())

    def test_plan_is_read_only_and_lists_only_three_large_work_files(self):
        report = self.cleaner.plan()
        self.assertEqual(report['eligible_files'], 3)
        self.assertEqual({f['kind'] for f in report['groups'][0]['files']}, {'source', 'render', 'audio'})
        self.assert_kept()
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='cleanup_files'").fetchone()[0], 0)

    async def test_apply_keeps_bank_context_history_and_is_idempotent(self):
        before = {p: self.sha(p) for p in (self.bank, self.trace, *self.frames)}
        report = await self.cleaner.sweep()
        self.assertEqual(len(report['results']), 3)
        self.assertGreater(report['deleted_bytes'], 0)
        for p in (self.source, self.render, self.audio):
            self.assertFalse(p.exists())
        for p, digest in before.items():
            self.assertEqual(self.sha(p), digest)
        for j in self.jobs:
            job = self.store.job(j['id'])
            self.assertEqual(job['status'], 'published')
            self.assertEqual(job['payload']['transcript'], 'tetap disimpan')
        second = await self.cleaner.sweep()
        self.assertEqual(second['deleted_bytes'], 0)
        self.assertEqual(self.cleaner.verify_remote.await_count, 1)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM cleanup_files WHERE state='deleted'").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE kind='cleanup_file_intent'").fetchone()[0], 3)

    def test_retention_uses_published_event_not_updated_at(self):
        self.store.checkpoint(self.jobs[0]['id'], payload={'notification_updated': True})
        self.assertEqual(self.states(), ['eligible'])
        self.assertEqual(self.cleaner.plan(at=now() - timedelta(hours=2))['groups'][0]['state'], 'retention')

    async def test_already_absent_files_do_not_rehash_large_bank_archive(self):
        await self.cleaner.sweep()
        with patch('fahra_automation.cleanup.fingerprint', side_effect=AssertionError('No large files to hash again')):
            self.assertEqual(self.states(), ['clean'])

    async def test_reappearing_file_requires_full_validation_again(self):
        content = self.render.read_bytes()
        await self.cleaner.sweep()
        self.render.write_bytes(content)
        self.bank.write_bytes(b'changed archive')
        self.assertEqual(self.states(), ['blocked'])
        self.assertTrue(self.render.exists())

    def test_missing_publication_event_does_not_fall_back_to_creation_time(self):
        old = self.jobs[0]['id']
        replacement = ('c' if old[0] == 'b' else 'b') + old[1:]
        with self.store.tx() as c:
            c.execute('UPDATE jobs SET id=? WHERE id=?', (replacement, old))
        self.assertEqual(self.states(), ['blocked'])

    def test_missing_post_link_or_drive_checksum_blocks_cleanup(self):
        for key in ('post_url', 'drive_verified_sha256', 'drive_file_id'):
            with self.subTest(key=key):
                old = self.store.job(self.jobs[0]['id'])['payload'][key]
                self.store.checkpoint(self.jobs[0]['id'], payload={key: ''})
                self.assertEqual(self.states(), ['blocked'])
                self.store.checkpoint(self.jobs[0]['id'], payload={key: old})

    def test_every_incomplete_or_uncertain_target_keeps_shared_files(self):
        for state in ('queued', 'failed', 'cancelled', 'submitting', 'submit_unknown', 'scheduled'):
            with self.subTest(state=state):
                self.store.checkpoint(self.jobs[-1]['id'], state)
                self.assertEqual(self.states(), ['blocked'])
                self.assert_kept()

    def test_pair_waits_24_hours_after_last_publication(self):
        if not self.paired:
            self.skipTest('Only paired asset workflow')
        self.store.checkpoint(self.jobs[-1]['id'], 'scheduled')
        self.store.checkpoint(self.jobs[-1]['id'], 'published')
        self.assertEqual(self.states(), ['retention'])

    def test_missing_one_frame_or_changed_bank_keeps_files(self):
        data = self.frames[0].read_bytes()
        self.frames[0].unlink()
        self.assertEqual(self.states(), ['blocked'])
        self.frames[0].write_bytes(data)
        self.bank.write_bytes(b'changed')
        self.assertEqual(self.states(), ['blocked'])

    def add_reference(self, store, path):
        store.enqueue(2, 2, 2, [('https://www.instagram.com/reel/reference/', 'reference')], can_allocate=False)
        with store.connect() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM jobs WHERE source_id='reference'")]
        for identity in ids:
            store.checkpoint(identity, payload={'source_path': str(path)})

    def test_other_workspace_reference_keeps_all_files(self):
        self.add_reference(self.peer_store, self.source)
        self.assertEqual(self.states(), ['blocked'])
        self.assert_kept()

    def relocate(self, store, config, name):
        store.set('paused', True)
        store.set('reply_enabled', False)
        store.set('account_report_enabled', False)
        with patch.object(Config, 'require_production_checkout'):
            result = migrate(config, self.base / 'internal' / name / 'workflow.sqlite3')
        return Store(config.database_path), result

    def test_relocated_active_database_is_read_and_archive_still_protects_media(self):
        self.store, result = self.relocate(self.store, self.config, 'active')
        self.cleaner.store = self.store
        self.store.checkpoint(self.jobs[0]['id'], payload={'after_relocation': True})
        roots, _, _ = self.cleaner.scope()
        own, refs = self.cleaner.inventory(roots)
        current = next(j for j in own['jobs'] if j['id'] == self.jobs[0]['id'])
        self.assertTrue(current['payload']['after_relocation'])
        source_refs = refs[str(self.source)]
        self.assertTrue(any(db == str(self.store.path) for db, _, _ in source_refs))
        self.assertTrue(any(db == result['archive'] for db, _, _ in source_refs))
        group = self.cleaner.plan()['groups'][0]
        self.assertEqual(group['state'], 'blocked')
        self.assertIn('masih dirujuk', group['reason'])
        self.assert_kept()

    def test_relocated_peer_database_and_old_preview_still_protect_files(self):
        peer_config = Config(self.peer, {'workflow': {'runtime_dir': 'runtime'}})
        self.peer_store, _ = self.relocate(self.peer_store, peer_config, 'peer')
        self.add_reference(self.peer_store, self.source)
        preview = Store(self.peer / 'runtime/verification/preview.sqlite3')
        self.add_reference(preview, self.render)
        roots, _, _ = self.cleaner.scope()
        _, refs = self.cleaner.inventory(roots)
        self.assertTrue(any(db == str(self.peer_store.path) for db, _, _ in refs[str(self.source)]))
        self.assertTrue(any(db == str(preview.path) for db, _, _ in refs[str(self.render)]))
        self.assertEqual(self.states(), ['blocked'])
        self.assert_kept()

    def test_relocated_database_hardlink_or_missing_archive_fails_closed(self):
        self.store, result = self.relocate(self.store, self.config, 'active')
        self.cleaner.store = self.store
        alias = self.base / 'same-database.sqlite3'
        os.link(self.store.path, alias)
        with self.assertRaises(CleanupBlocked):
            self.cleaner.plan()
        alias.unlink()
        Path(result['archive']).unlink()
        with self.assertRaises(FileNotFoundError):
            self.cleaner.plan()
        self.assert_kept()

    def test_unproven_database_directory_cannot_be_skipped_as_tombstone(self):
        self.store, _ = self.relocate(self.store, self.config, 'active')
        self.cleaner.store = self.store
        marker = self.root / 'runtime/workflow.sqlite3/MOVED.json'
        marker.write_text(json.dumps({'migration_id': 'wrong', 'pointer': str(self.root / 'runtime/database-location.json')}))
        with self.assertRaises(CleanupBlocked):
            self.cleaner.plan()
        self.assert_kept()

    def test_other_job_and_archived_preview_reference_are_protected(self):
        preview = Store(self.peer / 'runtime/verification/archive/preview.sqlite3')
        self.add_reference(preview, self.render)
        self.assertEqual(self.states(), ['blocked'])

    def test_other_job_in_same_workspace_protects_source(self):
        self.add_reference(self.store, self.source)
        groups = {g['id']: g for g in self.cleaner.plan()['groups']}
        self.assertEqual(groups[self.owner]['state'], 'blocked')

    def test_bank_or_outside_path_cannot_replace_work_render(self):
        for path in (self.bank, self.base / 'outside.mp4'):
            with self.subTest(path=path):
                if self.paired:
                    self.store.asset_checkpoint(self.owner, payload={'render_path': str(path)})
                else:
                    self.store.checkpoint(self.jobs[0]['id'], payload={'render_path': str(path)})
                self.assertEqual(self.states(), ['blocked'])
        self.assert_kept()

    async def test_reply_context_still_builds_from_retained_frames_after_cleanup(self):
        from fahra_automation.reply_ai import ReplyAI
        await self.cleaner.sweep()
        ai = ReplyAI(self.config)
        ai.generate = AsyncMock(return_value={'facts': [{'id': 'f1', 'text': 'kucing', 'source': 'frames'}],
            'quality': 'usable', 'confidence': .9, 'summary': 'Kucing', 'topic': 'hewan', 'tone': 'ringan'})
        with patch('fahra_automation.reply_ai.Media.analysis_assets', side_effect=AssertionError('Source must not be reopened')):
            context = await ai.context(self.store.job(self.jobs[0]['id']), self.root / 'runtime/replies/context')
        self.assertEqual(len(context['frame_sha256']), 6)
        self.assertEqual(context['quality'], 'usable')

    async def test_reference_added_during_remote_verification_stops_deletion(self):
        async def remote(group):
            self.add_reference(self.peer_store, self.source)
        self.cleaner.verify_remote.side_effect = remote
        result = await self.cleaner.sweep()
        self.assertEqual(result['deleted_bytes'], 0)
        self.assert_kept()

    async def test_missing_or_corrupt_peer_database_fails_closed(self):
        self.peer_store.path.write_bytes(b'not sqlite')
        with self.assertRaises(sqlite3.DatabaseError):
            await self.cleaner.sweep()
        self.assert_kept()

    def test_symlink_and_hardlink_are_not_deleted(self):
        original = self.source.read_bytes()
        other = self.base / 'other.mp4'
        other.write_bytes(original)
        self.source.unlink()
        self.source.symlink_to(other)
        self.assertEqual(self.states(), ['blocked'])
        self.source.unlink()
        os.link(other, self.source)
        self.assertEqual(self.states(), ['blocked'])
        self.assertTrue(other.exists())

    def test_mcp_manifest_must_identify_own_completed_download(self):
        path = self.source.parent.parent / 'status.json'
        data = json.loads(path.read_text())
        data['job_id'] = 'b' * 32
        path.write_text(json.dumps(data))
        self.assertEqual(self.states(), ['blocked'])

    def test_changed_render_or_source_checksum_blocks_cleanup(self):
        original = self.render.read_bytes()
        self.render.write_bytes(original + b'changed')
        self.assertEqual(self.states(), ['blocked'])
        self.render.write_bytes(original)
        self.source.write_bytes(b'changed')
        self.assertEqual(self.states(), ['blocked'])

    async def test_remote_verification_failure_keeps_local_files(self):
        self.cleaner.verify_remote.side_effect = CleanupBlocked('Drive changed')
        result = await self.cleaner.sweep()
        self.assertEqual(result['deleted_bytes'], 0)
        self.assert_kept()

    async def test_worktree_disabled_and_short_retention_cannot_delete(self):
        self.config.data['cleanup']['enabled'] = False
        with self.assertRaises(CleanupBlocked):
            await self.cleaner.sweep()
        self.config.data['cleanup']['enabled'] = True
        (self.root / '.git').rmdir()
        (self.root / '.git').write_text('gitdir: elsewhere')
        with self.assertRaises(CleanupBlocked):
            await self.cleaner.sweep()
        self.config.data['cleanup']['retention_hours'] = 23
        with self.assertRaises(CleanupBlocked):
            self.cleaner.plan()
        self.assert_kept()

    async def test_global_lock_prevents_two_sweeps(self):
        with process_lock(self.mcp / '.peternakan-cleanup.lock'):
            with self.assertRaises(RuntimeError):
                await self.cleaner.sweep()
        self.assert_kept()

    async def test_production_root_argument_cannot_bypass_worktree_code_guard(self):
        code_root = self.base / 'worktree-code'
        code_root.mkdir()
        (code_root / '.git').write_text('gitdir: production/.git/worktrees/task')
        with patch('fahra_automation.cleanup.CODE_ROOT', code_root):
            with self.assertRaises(CleanupBlocked):
                await self.cleaner.sweep()
        self.assert_kept()

    async def test_cancellation_waits_for_unlink_before_releasing_shared_lock(self):
        started, release = threading.Event(), threading.Event()
        original = self.cleaner.remove_file
        def delayed(*args):
            started.set()
            if not release.wait(5):
                raise AssertionError('Test did not release file mutation')
            return original(*args)
        with patch.object(self.cleaner, 'remove_file', side_effect=delayed):
            task = asyncio.create_task(self.cleaner.sweep())
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(started.is_set())
                task.cancel()
                await asyncio.sleep(.01)
                with self.assertRaises(RuntimeError):
                    with process_lock(self.mcp / '.peternakan-cleanup.lock'):
                        pass
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.source.exists())
        self.assertTrue(self.render.exists())
        with process_lock(self.mcp / '.peternakan-cleanup.lock'):
            pass

    def test_file_changed_after_plan_is_not_removed(self):
        item = self.cleaner.plan()['groups'][0]['files'][0]
        Path(item['path']).write_bytes(b'replaced')
        with self.assertRaises(CleanupBlocked):
            self.cleaner.remove_file(item, self.owner, 'test')
        self.assertTrue(Path(item['path']).exists())

    async def test_crash_after_unlink_reconciles_intent_without_counting_bytes_twice(self):
        original = self.cleaner.ledger
        def crash(file, owner, state, run_id, error=None):
            if state == 'deleted':
                raise RuntimeError('simulated crash after unlink')
            return original(file, owner, state, run_id, error)
        with patch.object(self.cleaner, 'ledger', side_effect=crash):
            await self.cleaner.sweep()
        self.assertFalse(self.source.exists())
        result = await self.cleaner.sweep()
        with self.store.connect() as c:
            row = c.execute('SELECT state FROM cleanup_files WHERE path=?', (str(self.source),)).fetchone()
        self.assertEqual(row[0], 'missing_after_intent')
        self.assertEqual(result['deleted_bytes'], len(b'RIFF' + b'audio' * 50) + self.bank.stat().st_size)

    async def test_remote_verifier_checks_entire_file_not_only_mp4_header(self):
        group = self.cleaner.plan()['groups'][0]
        factory = httpx.AsyncClient
        for body, success in ((self.bank.read_bytes(), True), (self.bank.read_bytes()[:-1] + b'x', False),
                              (b'<html>denied</html>', False), (self.bank.read_bytes() + b'extra', False)):
            with self.subTest(success=success, size=len(body)):
                transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
                with patch('fahra_automation.cleanup.httpx.AsyncClient', side_effect=lambda **kw: factory(transport=transport, **kw)):
                    if success:
                        await Cleaner.verify_remote(self.cleaner, group)
                    else:
                        with self.assertRaises(CleanupBlocked):
                            await Cleaner.verify_remote(self.cleaner, group)

    async def test_live_ownership_required_before_any_delete(self):
        from fahra_automation.config import ConfigurationError
        with patch.object(Config,'require_live_action',side_effect=ConfigurationError('shadow')):
            with self.assertRaises(ConfigurationError):
                await self.cleaner.sweep()
        self.assert_kept()
