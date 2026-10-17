import json
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import test_audit_operations as fixtures
from fahra_automation.metrics import ContentMetrics
from fahra_automation.util import iso, now


class MetricsTest(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.Operations.setUp
    snapshot = fixtures.Operations.snapshot

    async def test_disabled_has_no_reads_then_samples_once_preserving_missing_values(self):
        self.snapshot([{**self.account, 'publishEligible':True}])
        self.store.enqueue(1,1,1,[('https://www.instagram.com/reel/metric/', 'metric')])
        job = next(j for j in self.store.queue() if j['platform'] == 'tiktok')
        with patch('fahra_automation.db.iso', return_value=iso(now()-timedelta(hours=24,minutes=5))):
            self.store.checkpoint(job['id'], 'published', payload={'post_id':'1234567890123456'})
        api = type('API', (), {'request': AsyncMock(return_value={'like':0, 'views':None, 'share':True})})()
        worker = ContentMetrics(self.config,self.store,api)
        self.assertEqual(await worker.once(),0)
        api.request.assert_not_awaited()
        self.config.data['metrics']['enabled'] = True
        self.assertEqual(await worker.once(),1)
        self.assertEqual(await worker.once(),0)
        self.assertEqual(api.request.await_args.args[0], 'GET')
        self.assertEqual(api.request.await_args.kwargs['params'], {'accountId':'tt'})
        with self.store.connect() as c:
            payload = json.loads(c.execute('SELECT payload FROM content_metrics').fetchone()[0])
        self.assertEqual(payload['metrics']['like'],0)
        self.assertIsNone(payload['metrics']['views'])
        self.assertIsNone(payload['metrics']['share'])

    async def test_missed_window_never_backfills_a_current_value_as_old_performance(self):
        self.snapshot([{**self.account, 'publishEligible':True}])
        self.store.enqueue(1,1,1,[('https://www.instagram.com/reel/oldmetric/', 'oldmetric')])
        job = next(j for j in self.store.queue() if j['platform']=='tiktok')
        with patch('fahra_automation.db.iso', return_value=iso(now()-timedelta(hours=30))):
            self.store.checkpoint(job['id'],'published',payload={'post_id':'1234567890123456'})
        self.config.data['metrics']['enabled'] = True
        api = type('API', (), {'request':AsyncMock()})()
        await ContentMetrics(self.config,self.store,api).once()
        api.request.assert_not_awaited()
        with self.store.connect() as c:
            data = json.loads(c.execute('SELECT payload FROM content_metrics').fetchone()[0])
        self.assertEqual(data['status'],'missed_window')
