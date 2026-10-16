import copy
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from fahra_automation.config import ConfigurationError, EXCLUDED_ACCOUNT_ID
from fahra_automation.repliz import Repliz, SharedRateLimiter, MutationNotSent, ReplizError, publication_details
from fahra_automation.util import now, iso


class FixtureConfig:
    def __init__(self, live=True):
        self.live = live
    def section(self, key):
        return {'base_url': 'https://repliz.invalid', 'min_request_interval_seconds': 0}
    def require_live_action(self):
        if not self.live:
            raise ConfigurationError('Shadow mode.')


def page(rows, total=None, number=1, more=False):
    return {'docs': rows, 'totalDocs': len(rows) if total is None else total, 'page': number, 'hasNextPage': more}


def metadata_job(platform='youtube'):
    return {'id': 'a' * 32, 'platform': platform, 'account_id': platform + '-1', 'payload': {
        'drive_url': 'https://drive.usercontent.google.com/download?id=fixturefile',
        'delivery_metadata': {'account_id': platform + '-1', 'platform': platform,
            'title': 'Kucing penasaran', 'caption': 'Kucing memeriksa kardus.', 'description': 'Kucing memeriksa kardus. #kucing',
            'hashtags': ['#kucing'], 'tags': ['kucing']}}}


class ReplizContractTest(unittest.IsolatedAsyncioTestCase):
    def remote(self, handler=lambda r: httpx.Response(200, json={}), live=True):
        client = httpx.AsyncClient(base_url='https://repliz.invalid', transport=httpx.MockTransport(handler))
        remote = Repliz(FixtureConfig(live), client=client)
        self.addAsyncCleanup(remote.close)
        return remote

    async def test_pagination_requires_total_page_boolean_and_unique_ids(self):
        valid = page([{'id': 'one'}])
        fixtures = []
        for field in ('totalDocs', 'page', 'hasNextPage'):
            row = copy.deepcopy(valid); row.pop(field); fixtures.append(row)
        fixtures += [page([{'id': 'one'}, {'id': 'one'}]), page([None]), page([{'id': ''}]),
                     {**valid, 'totalDocs': True}, {**valid, 'page': 2}, {**valid, 'hasNextPage': 1}]
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                remote = self.remote()
                remote.request = AsyncMock(return_value=fixture)
                with self.assertRaises(ValueError):
                    await remote.pages('/public/account')

    async def test_complete_empty_and_multi_page_are_accepted(self):
        remote = self.remote()
        remote.request = AsyncMock(side_effect=[page([{'id': 'a'}], 2, 1, True), page([{'id': 'b'}], 2, 2)])
        self.assertEqual([r['id'] for r in await remote.pages('/public/account')], ['a', 'b'])
        remote.request = AsyncMock(return_value=page([]))
        self.assertEqual(await remote.pages('/public/account'), [])

    async def test_changing_total_and_false_last_page_are_rejected(self):
        remote = self.remote()
        for responses in ([page([{'id': 'a'}], 2, 1, True), page([{'id': 'b'}], 3, 2)],
                          [page([{'id': 'a'}], 2)],
                          [{**page([{'id': 'a'}]), 'totalPages': 2}]):
            remote.request = AsyncMock(side_effect=responses)
            with self.assertRaises(ValueError):
                await remote.pages('/public/account')

    async def test_sync_never_applies_accounts_when_schedule_is_incomplete(self):
        remote = self.remote()
        account = {'id': 'facebook-1', 'type': 'facebook', 'isConnected': True}
        store = SimpleNamespace(begin_sync=Mock(return_value=1), accounts=Mock(return_value=[]), apply_snapshot=Mock())
        remote.pages = AsyncMock(side_effect=[[account], [{'id': 's', 'accountId': 'facebook-1', 'type': 'reel', 'scheduleAt': iso()}]])
        with self.assertRaises(ValueError):
            await remote.sync(store)
        store.apply_snapshot.assert_not_called()
        store.begin_sync.assert_called_once()

    async def test_sync_filters_excluded_and_keeps_error_quota_records(self):
        remote = self.remote()
        accounts = [{'id': p + '-1', 'username': 'valid', 'type': p, 'isConnected': True}
                    for p in ('tiktok', 'facebook', 'youtube')]
        accounts += [{'id': EXCLUDED_ACCOUNT_ID, 'type': 'tiktok', 'isConnected': True},
                     {'id': 'blocked-name', 'type': 'tiktok', 'username': 'iq7fanspage', 'isConnected': True},
                     {'id': 'ig', 'type': 'instagram', 'isConnected': True}]
        schedule = {'id': 's', 'accountId': 'youtube-1', 'type': 'video', 'status': 'error', 'scheduleAt': iso()}
        store = SimpleNamespace(begin_sync=Mock(return_value=3), accounts=Mock(return_value=[]), apply_snapshot=Mock(return_value={'generation': 4}))
        remote.pages = AsyncMock(side_effect=[accounts, [schedule]])
        await remote.sync(store)
        chosen, schedules, *_ = store.apply_snapshot.call_args.args
        self.assertEqual(len(chosen), 3)
        self.assertEqual(schedules, [schedule])
        query = remote.pages.call_args_list[1].args[1]
        self.assertNotIn(EXCLUDED_ACCOUNT_ID, query['accountIds'])

    async def test_unknown_status_and_foreign_account_fail_closed(self):
        remote = self.remote()
        schedule = {'id': 's', 'accountId': 'a', 'type': 'video', 'status': 'pending', 'scheduleAt': iso()}
        for change in ({'status': 'new-provider-state'}, {'accountId': 'other'}, {'scheduleAt': '2026-10-16T10:00:00'}):
            with self.assertRaises(ValueError):
                remote.validate_schedules([{**schedule, **change}], {'a'})

    async def test_create_only_posts_without_second_preflight(self):
        calls = []
        def handler(request):
            calls.append(request.method)
            return httpx.Response(200, json={'scheduleId': 'schedule-one'})
        remote = self.remote(handler)
        body = remote.payload(metadata_job(), now() + timedelta(minutes=10))
        self.assertEqual(await remote.create(body, 'youtube'), 'schedule-one')
        self.assertEqual(calls, ['POST'])

    async def test_expired_slot_after_rate_limiter_causes_zero_post(self):
        calls = []
        remote = self.remote(lambda r: calls.append(r.method) or httpx.Response(200, json={'scheduleId': 's'}))
        body = remote.payload(metadata_job(), now() - timedelta(seconds=1))
        with self.assertRaises(MutationNotSent):
            await remote.create(body, 'youtube')
        self.assertEqual(calls, [])

    async def test_every_mutating_method_requires_live_guard(self):
        calls = []
        remote = self.remote(lambda r: calls.append(r.method) or httpx.Response(200, json={}), live=False)
        for method in ('POST', 'PATCH', 'PUT', 'DELETE'):
            with self.assertRaises(MutationNotSent):
                await remote.request(method, '/public/schedule')
        await remote.request('GET', '/public/account')
        self.assertEqual(calls, ['GET'])

    async def test_clients_share_limiter_and_429_blocks_next_request_budget(self):
        a = self.remote(lambda r: httpx.Response(429, headers={'Retry-After': '7'}, json={'message': 'slow'}))
        b = self.remote()
        with self.assertRaises(ReplizError) as caught:
            await a.request('GET', '/public/account')
        self.assertEqual(caught.exception.retry_after, 7)
        self.assertIs(a.limiter, SharedRateLimiter.shared('https://repliz.invalid', 0))
        b.limiter = SharedRateLimiter.shared('https://repliz.invalid', 0)
        self.assertIs(a.limiter, b.limiter)

    async def test_payload_types_limits_persona_and_reconciliation(self):
        for platform, kind in [('tiktok', 'video'), ('facebook', 'reel'), ('youtube', 'video')]:
            job = metadata_job(platform)
            body = Repliz.payload(job, now())
            self.assertEqual(body['type'], kind)
            self.assertEqual(body['medias'][0]['alt'], 'fa:' + job['id'])
            self.assertTrue(Repliz.matches_submission(body, body))
            altered = copy.deepcopy(body); altered['accountId'] = 'other'
            self.assertFalse(Repliz.matches_submission(altered, body))
            job['payload']['delivery_metadata']['account_id'] = 'other'
            with self.assertRaises(ConfigurationError): Repliz.payload(job, now())
        job = metadata_job('tiktok'); job['payload']['delivery_metadata']['caption'] = 'a' * 2201
        with self.assertRaises(ValueError): Repliz.payload(job, now())
        job = metadata_job(); job['payload']['delivery_metadata']['tags'] = ['a' * 501]
        with self.assertRaises(ValueError): Repliz.payload(job, now())

    async def test_ambiguous_matches_do_not_pick_a_schedule(self):
        remote = self.remote()
        job = metadata_job()
        body = remote.payload(job, now()); job['payload']['submission'] = body
        remote.pages = AsyncMock(return_value=[{**body, 'id': x, 'status': 'pending'} for x in ('s1', 's2')])
        with self.assertRaises(RuntimeError): await remote.reconcile(job)

    def test_tiktok_facebook_blank_title_uses_shared_hook(self):
        for platform in ('tiktok', 'facebook'):
            for title in ('', '  ', None):
                with self.subTest(platform=platform, title=title):
                    job = metadata_job(platform)
                    job['payload']['generated'] = {'hook': '  Kucing penasaran  '}
                    job['payload']['delivery_metadata']['title'] = title
                    self.assertEqual(Repliz.payload(job, now())['title'], 'Kucing penasaran')
            job['payload']['delivery_metadata'].pop('title')
            job['payload']['generated']['hook'] = 'a' * 101
            self.assertEqual(Repliz.payload(job, now())['title'], 'a' * 100)
            job['payload']['delivery_metadata']['title'] = 'Judul persona'
            self.assertEqual(Repliz.payload(job, now())['title'], 'Judul persona')

    def test_youtube_requires_explicit_title_despite_shared_hook(self):
        for title in ('', '  ', None):
            job = metadata_job('youtube')
            job['payload']['generated'] = {'hook': 'Kucing penasaran'}
            job['payload']['delivery_metadata']['title'] = title
            with self.subTest(title=title), self.assertRaises(ValueError):
                Repliz.payload(job, now())
        job['payload']['delivery_metadata'].pop('title')
        with self.assertRaises(ValueError):
            Repliz.payload(job, now())

    async def test_unique_marker_can_reconcile_remote_reschedule_and_legacy_title(self):
        body = Repliz.payload(metadata_job(), now())
        moved = {**copy.deepcopy(body), 'scheduleAt': iso(now() + timedelta(days=1))}
        self.assertTrue(Repliz.matches_submission(moved, body))
        moved['medias'][0]['alt'] = 'fa:' + 'b' * 32
        self.assertFalse(Repliz.matches_submission(moved, body))
        legacy = {**copy.deepcopy(body), 'title': 'pw:' + 'c' * 32}
        legacy['medias'][0].pop('alt')
        self.assertTrue(Repliz.matches_submission({**legacy, 'scheduleAt': iso(now() + timedelta(days=1))}, legacy))

    def test_platform_publication_links_require_post_evidence(self):
        self.assertIn('/@person/video/', publication_details({'status': 'success', 'postId': '123456789012345'}, 'person', 'tiktok')['post_url'])
        self.assertEqual(publication_details({'status': 'success', 'postUrl': 'https://www.youtube.com/watch?v=abcdefghijk'}, 'channel', 'youtube'), {})
