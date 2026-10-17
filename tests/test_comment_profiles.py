import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx

from fahra_automation.comment_profiles import CommentProfiles, defaults, merge_config, patch_config, verify_config, platform_patch, custom_reply_config
from fahra_automation.config import Config, EXCLUDED_ACCOUNT_ID
from fahra_automation.repliz import Repliz, ReplizError


class CommentProfileTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = json.loads((Config.load().root / "profiles/tiktok-comments-id.json").read_text())
        self.api = Mock()
        self.config = Config.load()
        self.config.data = copy.deepcopy(self.config.data)
        self.config.data['workflow']['runtime_dir'] = self.temp.name
        self.config.require_live_action = Mock()
        self.manager = CommentProfiles(self.api, self.profile, Path(self.temp.name), config=self.config)
        self.account = {"id": "account1", "username": "akun", "type": "tiktok", "config": defaults()}

    def test_preserves_existing_reply_and_dm_and_content_schema(self):
        current = defaults()
        current["reply"].update(isActive=True, text="Existing reply")
        current["chat"].update(isActive=True, type="ai", prompt="Existing instructions")
        wanted = merge_config(current, patch_config(self.profile))
        self.assertEqual(wanted["reply"], current["reply"])
        self.assertEqual(wanted["chat"], current["chat"])
        self.assertTrue(wanted["like"]["isActive"])
        self.assertTrue(wanted["delete"]["isActive"])
        self.assertNotIn("chat", merge_config(current, patch_config(self.profile), content=True))
        self.assertFalse(defaults()["like"]["isActive"])

    def test_keywords_cover_examples_and_leave_common_content_words(self):
        keywords = patch_config(self.profile)["delete"]["keywords"]
        for comment in ("ayo daftar SLOT GACOR", "kirim kode otp ke saya", "kirim foto telanjang", "akan kubunuh kamu"):
            self.assertTrue(any(k in comment.lower() for k in keywords))
        for comment in ("videonya jelek", "mahal banget", "kucing dan anjing lucu", "babi panggang", "jangan promosi judi di sini", "ini penipuan, hati-hati"):
            self.assertFalse(any(k in comment.lower() for k in keywords))

    def test_legacy_config_adds_inactive_required_fields_without_changing_rules(self):
        old = {"reply": {"isActive": True, "type": "ai", "prompt": "Existing policy", "text": "",
                         "keyword": {"isExactMatch": True, "values": [{"keyword": "harga", "text": "cek toko"}]}},
               "chat": {"isActive": True, "type": "ai", "prompt": "Existing DM policy"}}
        merged = merge_config(old, patch_config(self.profile))
        self.assertEqual(merged["reply"]["prompt"], "Existing policy")
        self.assertEqual(merged["reply"]["keyword"], old["reply"]["keyword"])
        self.assertEqual(merged["reply"]["exception"], {"isActive": False, "isExactMatch": False, "keywords": []})
        self.assertTrue(merged["chat"]["isActive"])
        self.assertFalse(merged["message"]["isActive"])

    async def test_excluded_identity_or_username_never_written(self):
        self.api.request = AsyncMock()
        for changes in ({"id": EXCLUDED_ACCOUNT_ID}, {"username": "@IQ7FANSPAGE"}):
            a = {**self.account, **changes}
            with self.assertRaises(ValueError):
                await self.manager.update_one("account", a, a, a)
        self.api.request.assert_not_awaited()

    async def test_put_timeout_reads_back_and_does_not_repeat(self):
        wanted = merge_config(self.account["config"], self.manager.patch)
        self.api.request = AsyncMock(side_effect=[self.account, httpx.ReadTimeout("timeout"), {**self.account, "config": wanted}])
        await self.manager.update_one("account", self.account, self.account, self.account)
        methods = [c.args[0] for c in self.api.request.call_args_list]
        self.assertEqual(methods, ["GET", "PUT", "GET"])

    async def test_concurrent_config_change_aborts_before_put(self):
        changed = copy.deepcopy(self.account)
        changed["config"]["chat"]["text"] = "Changed by operator"
        self.api.request = AsyncMock(return_value=changed)
        with self.assertRaisesRegex(RuntimeError, "berubah setelah audit"):
            await self.manager.update_one("account", self.account, self.account, self.account)
        self.assertEqual(self.api.request.await_count, 1)

    async def test_foreign_content_owner_is_rejected(self):
        content = {"id": "content1", "accountId": "different", "config": defaults()}
        self.api.request = AsyncMock(return_value=content)
        with self.assertRaisesRegex(RuntimeError, "Pemilik"):
            await self.manager.update_one("content", content, self.account, content)
        self.assertEqual(self.api.request.await_count, 1)

    async def test_template_create_timeout_reconciles_name(self):
        expected = merge_config({}, self.manager.patch)
        self.api.pages = AsyncMock(side_effect=[[], [{"id": "template1", "name": self.profile["name"], "config": expected}]])
        self.api.request = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        self.assertEqual(await self.manager.ensure_template(), "template1")
        self.assertEqual(self.api.request.await_count, 1)

    async def test_template_existing_with_different_rules_is_not_overwritten(self):
        self.api.pages = AsyncMock(return_value=[{"id": "template1", "name": self.profile["name"], "config": defaults()}])
        self.api.request = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "konfigurasi berbeda"):
            await self.manager.ensure_template()
        self.api.request.assert_not_awaited()

    async def test_template_unknown_across_retries_never_reposts(self):
        self.api.pages = AsyncMock(return_value=[])
        self.api.request = AsyncMock(side_effect=httpx.ReadTimeout('timeout'))
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await self.manager.ensure_template()
        self.assertEqual(self.api.request.await_count, 1)

    async def test_template_post_success_then_get_404_still_blocks_recreate(self):
        self.api.pages = AsyncMock(return_value=[])
        self.api.request = AsyncMock(side_effect=[{'id': 'created-id'}, ReplizError(404, 'not visible yet')])
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await self.manager.ensure_template()
        self.assertEqual([x.args[0] for x in self.api.request.call_args_list], ['POST', 'GET'])

    def test_platform_capabilities_disable_youtube_like_and_preserve_other_modules(self):
        current = defaults()
        current['reply'].update(isActive=True, prompt='Legacy policy', type='ai')
        current['message']['text'] = 'DM untouched'
        for platform in ('tiktok', 'facebook', 'youtube'):
            wanted = custom_reply_config(current, self.profile, platform)
            self.assertEqual(wanted['like']['isActive'], platform != 'youtube')
            self.assertFalse(wanted['reply']['isActive'])
            self.assertEqual(wanted['reply']['prompt'], 'Legacy policy')
            self.assertEqual(wanted['message'], current['message'])

    async def test_youtube_account_automation_is_never_written(self):
        self.api.request = AsyncMock()
        account = {**self.account, 'type': 'youtube'}
        await self.manager.update_one('account', account, account, account)
        self.api.request.assert_not_awaited()

    async def test_live_guard_prevents_put(self):
        self.api.request = AsyncMock(return_value=self.account)
        self.config.require_live_action.side_effect = RuntimeError('shadow')
        with self.assertRaisesRegex(RuntimeError, 'shadow'):
            await self.manager.update_one('account', self.account, self.account, self.account)
        self.assertEqual([x.args[0] for x in self.api.request.call_args_list], ['GET'])

    async def test_post_create_timeout_is_reconciled_then_never_recreated(self):
        self.config.data = copy.deepcopy(self.config.data)
        self.config.data['workflow']['runtime_dir'] = self.temp.name
        account = {**self.account, 'isConnected': True, 'publishEligible': True}
        account['config']['reply']['isActive'] = True
        overrides = []
        async def request(method, path, **kwargs):
            if method == 'GET':
                return copy.deepcopy(account)
            self.assertEqual(method, 'POST')
            overrides.append({'id': 'override1', 'accountId': account['id'],
                              'contentId': 'post1', 'config': kwargs['json']['config']})
            raise httpx.ReadTimeout('response lost')
        self.api.request = AsyncMock(side_effect=request)
        self.api.pages = AsyncMock(side_effect=lambda *args: copy.deepcopy(overrides))
        job = {'id': 'job1', 'status': 'published', 'account_id': account['id'], 'payload': {'post_id': 'post1'}}
        result = await self.manager.ensure_post(job)
        self.assertTrue(result['verified'])
        self.assertFalse(overrides[0]['config']['reply']['isActive'])
        await self.manager.ensure_post(job)
        self.assertEqual(sum(x.args[0] == 'POST' for x in self.api.request.call_args_list), 1)

    async def test_post_429_definite_rejection_allows_later_retry(self):
        account = {**self.account, 'isConnected': True, 'publishEligible': True}
        overrides = []
        error = ReplizError(429, 'limited', retry_after=60)
        async def request(method, path, **kwargs):
            if method == 'GET':
                return copy.deepcopy(account)
            if error:
                raise error
            overrides.append({'id': 'override1', 'accountId': account['id'],
                              'contentId': 'post1', 'config': kwargs['json']['config']})
            return overrides[0]
        self.api.request = AsyncMock(side_effect=request)
        self.api.pages = AsyncMock(side_effect=lambda *args: copy.deepcopy(overrides))
        job = {'id': 'job1', 'status': 'published', 'account_id': account['id'], 'payload': {'post_id': 'post1'}}
        with self.assertRaises(ReplizError):
            await self.manager.ensure_post(job)
        error = None
        self.assertTrue((await self.manager.ensure_post(job))['verified'])
        self.assertEqual(sum(x.args[0] == 'POST' for x in self.api.request.call_args_list), 2)

    def test_readback_ignores_only_generated_payloads(self):
        expected = defaults()
        actual = copy.deepcopy(expected)
        actual["message"]["opening"]["postback"]["button"]["payload"] = "server_generated"
        self.assertTrue(verify_config(actual, expected))
        actual["reply"]["text"] = "unwanted change"
        self.assertFalse(verify_config(actual, expected))

    def test_content_readback_ignores_delete_reply_runtime_status_without_mutation(self):
        expected = merge_config({}, self.manager.patch, content=True)
        for previous in (None, 'pending', 'success', 'error'):
            for current in (None, 'pending', 'success', 'error'):
                with self.subTest(previous=previous, current=current):
                    before = copy.deepcopy(expected)
                    before['delete']['status'] = previous
                    before['reply']['status'] = current
                    actual = copy.deepcopy(expected)
                    actual['delete']['status'] = current
                    actual['reply']['status'] = previous
                    untouched = copy.deepcopy((actual, before))
                    self.assertTrue(verify_config(actual, expected))
                    self.assertTrue(verify_config(actual, before))
                    self.assertEqual((actual, before), untouched)

    def test_runtime_status_does_not_hide_policy_schema_or_nested_changes(self):
        expected = merge_config({}, self.manager.patch, content=True)
        changes = [
            ('delete', 'isActive', False), ('delete', 'keywords', ['different keyword']),
            ('delete', 'type', 'ai'), ('reply', 'isActive', True),
            ('reply', 'delay', {'isActive': False, 'value': 11, 'type': 'second'}),
            ('reply', 'condition', {'isActive': False, 'isExactMatch': False, 'keywords': [], 'status': 'pending'}),
            ('reply', 'keyword', []), ('reply', 'unexpected', 'value'),
            ('like', 'isActive', False), ('like', 'status', 'pending'),
            ('message', 'status', 'success'), ('story', 'status', 'error'),
        ]
        for module, key, value in changes:
            with self.subTest(module=module, field=key):
                actual = copy.deepcopy(expected)
                actual['delete']['status'] = 'pending'
                actual['reply']['status'] = 'error'
                actual[module][key] = value
                self.assertFalse(verify_config(actual, expected))
        actual = copy.deepcopy(expected)
        del actual['reply']['delay']['value']
        self.assertFalse(verify_config(actual, expected))
        actual = copy.deepcopy(expected)
        actual['delete'] = None
        self.assertFalse(verify_config(actual, expected))

    async def test_existing_youtube_profile_runtime_changes_never_write(self):
        account = {**self.account, 'type': 'youtube', 'isConnected': True, 'publishEligible': True}
        config = custom_reply_config(account['config'], self.profile, 'youtube')
        row = {'id': 'override1', 'accountId': account['id'], 'detail': {'id': 'post1'}, 'config': config}
        self.api.pages = AsyncMock(side_effect=lambda *args: [copy.deepcopy(row)])
        async def read_account(method, path, **kwargs):
            self.assertEqual((method, path), ('GET', '/public/account/' + account['id']))
            return copy.deepcopy(account)
        self.api.request = AsyncMock(side_effect=read_account)
        job = {'id': 'job1', 'status': 'published', 'account_id': account['id'], 'payload': {'post_id': 'post1'}}
        for delete_status, reply_status in [('pending', 'pending'), ('success', 'error'), ('error', 'success')]:
            config['delete']['status'] = delete_status
            config['reply']['status'] = reply_status
            self.assertEqual(await self.manager.ensure_post(job),
                             {'verified': True, 'automation_id': 'override1', 'owner': 'custom'})
        self.config.require_live_action.assert_not_called()
        self.assertEqual([c.args[0] for c in self.api.request.call_args_list], ['GET'] * 3)
        self.assertEqual(self.api.pages.await_count, 3)
        self.assertFalse(config['like']['isActive'])
        self.assertFalse(config['reply']['isActive'])

    async def test_youtube_create_readback_pending_reconciles_without_second_write(self):
        account = {**self.account, 'type': 'youtube', 'isConnected': True, 'publishEligible': True}
        rows = []
        async def request(method, path, **kwargs):
            if method == 'GET':
                self.assertEqual(path, '/public/account/' + account['id'])
                return copy.deepcopy(account)
            self.assertEqual((method, path), ('POST', '/public/automation'))
            saved = copy.deepcopy(kwargs['json']['config'])
            saved['delete']['status'] = saved['reply']['status'] = 'pending'
            rows.append({'id': 'override1', 'accountId': account['id'], 'contentId': None,
                         'detail': {'id': 'post1'}, 'config': saved})
            return {'id': 'override1'}
        self.api.request = AsyncMock(side_effect=request)
        self.api.pages = AsyncMock(side_effect=lambda *args: copy.deepcopy(rows))
        job = {'id': 'job1', 'status': 'published', 'account_id': account['id'], 'payload': {'post_id': 'post1'}}
        self.assertTrue((await self.manager.ensure_post(job))['verified'])
        rows[0]['config']['delete']['status'] = 'error'
        rows[0]['config']['reply']['status'] = 'success'
        self.assertTrue((await self.manager.ensure_post(job))['verified'])
        self.assertEqual([c.args[0] for c in self.api.request.call_args_list], ['GET', 'POST', 'GET'])
        self.assertEqual(len(rows), 1)

    async def test_repliz_204_does_not_raise_json_error(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(204))
        async with httpx.AsyncClient(base_url="https://example.test", transport=transport) as client:
            api = Repliz(self.config, client=client)
            self.assertEqual(await api.request("PUT", "/public/account/id/automation", json={}), {})
