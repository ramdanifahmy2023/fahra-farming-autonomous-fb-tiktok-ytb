import json
import tempfile
import unittest
from pathlib import Path

from fahra_automation.db import Store


class DeliveryCopyHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'workflow.sqlite3'
        self.store = Store(self.path)
        self.store.apply_snapshot([
            {'id': 'account-' + name, 'username': 'fixture_' + name, 'name': name,
             'type': 'tiktok', 'isConnected': True, 'publishEligible': True}
            for name in ('a', 'b')], [], '2026-10-20', '2026-10-21')
        self.store.enqueue(1, 2, 3, [(f'https://instagram.com/reel/copy{i}/', f'copy{i}')
                                    for i in range(12)], can_allocate=False)
        with self.store.connect() as c:
            self.jobs = [dict(r) for r in c.execute('SELECT id,asset_id FROM jobs ORDER BY rowid')]

    def prepare(self, job, caption, account='account-a', metadata_account=None):
        payload = {'delivery_metadata': {'caption': caption,
                   'account_id': metadata_account if metadata_account is not None else account}}
        with self.store.tx() as c:
            c.execute('UPDATE jobs SET account_id=?,payload=? WHERE id=?',
                      (account, json.dumps(payload), job['id']))

    def test_history_excludes_current_asset_other_accounts_and_stale_bindings(self):
        current, sibling, previous, other, stale = (self.jobs[i] for i in (0, 1, 3, 6, 9))
        self.prepare(current, 'Caption saat ini.')
        self.prepare(sibling, 'Caption video yang sama.')
        self.prepare(previous, 'Caption akun sebelumnya.')
        self.prepare(other, 'Gaya akun lain.', account='account-b')
        self.prepare(stale, 'Metadata sebelum pindah akun.', metadata_account='account-b')
        self.assertEqual(self.store.recent_delivery_captions('account-a', current['id']),
                         [{'caption': 'Caption akun sebelumnya.'}])
        self.assertEqual(self.store.recent_delivery_captions('account-a', 'unknown'), [])
        self.assertEqual(self.store.recent_delivery_captions(None, current['id']), [])

    def test_history_is_bounded_read_only_and_survives_reopen(self):
        current = self.jobs[0]
        for index, job in enumerate(self.jobs[3::3]):
            self.prepare(job, f'Caption {index}.')
        store = Store(self.path, readonly=True)
        before = self.path.read_bytes()
        result = store.recent_delivery_captions('account-a', current['id'], limit=100)
        self.assertEqual(len(result), 8)
        self.assertEqual(result[0], {'caption': 'Caption 10.'})
        self.assertTrue(all(set(row) == {'caption'} for row in result))
        self.assertEqual(store.recent_delivery_captions('account-a', current['id'], limit=0), [])
        self.assertEqual(self.path.read_bytes(), before)
