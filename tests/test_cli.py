import unittest
from unittest.mock import Mock, patch

from fahra_automation.cli import run


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_worker_cannot_reconcile_before_lock(self):
        config, store = Mock(), Mock()
        config.runtime = Mock()
        config.runtime.__truediv__ = Mock(return_value='worker.lock')
        with patch('fahra_automation.cli.process_lock',side_effect=RuntimeError('already running')), \
             patch('fahra_automation.cli.AttemptJournal') as journal, \
             patch('fahra_automation.cli.Repliz') as api:
            with self.assertRaises(RuntimeError):
                await run(config,store)
        journal.assert_not_called()
        api.assert_not_called()
        store.recover.assert_not_called()
        store.checkpoint.assert_not_called()
