import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fahra_automation.db import Store
from fahra_automation.report_history import daily_series
from fahra_automation.report_store import ReportStore
from fahra_automation.report_views import ZONE


class ReportHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'history.sqlite3')
        self.reports = ReportStore(self.store)

    def sample(self, day, kind='daily', values=(599, 600), complete=True):
        at = datetime.fromisoformat(day).replace(hour=8, tzinfo=ZONE)
        run = self.reports.create(kind, day, at, at + timedelta(minutes=10), at)
        self.reports.inventory(run['id'], [{'id': str(i), 'username': f'user{i}', 'connected': True}
                                           for i in range(len(values))], 0)
        for i, value in enumerate(values):
            if value is not None:
                self.reports.metric(run['id'], str(i), data={'followers': value}, fetched=at)
        with self.store.tx() as connection:
            connection.execute("UPDATE account_report_runs SET state='ready',inventory_complete=? WHERE id=?",
                               (int(complete), run['id']))
        return self.reports.run(run['id'])

    def test_exact_days_only_daily_and_target_inclusive(self):
        self.sample('2026-10-13')
        self.sample('2026-10-15')
        self.sample('2026-10-18', 'pilot', (900, 900))
        self.sample('2026-10-20', values=(601, 602))
        points = daily_series(self.store, '2026-10-21')
        self.assertEqual([p['day'] for p in points], ['2026-10-15', '2026-10-20'])
        self.assertEqual([p['reached'] for p in points], [1, 2])
        self.assertTrue(all(p['complete'] for p in points))

    def test_unknown_and_incomplete_are_not_complete_trend_points(self):
        self.sample('2026-10-19', values=(600, None))
        self.sample('2026-10-20', complete=False)
        points = daily_series(self.store, '2026-10-21')
        self.assertEqual(points[0]['unknown'], 1)
        self.assertEqual(points[0]['below'], 0)
        self.assertFalse(any(p['complete'] for p in points))

    def test_current_daily_freeze_included_but_preview_never_added(self):
        run = self.sample('2026-10-21')
        with self.store.tx() as connection:
            connection.execute("UPDATE account_report_runs SET state='collecting' WHERE id=?", (run['id'],))
        rows = self.reports.rows(run['id'])
        self.assertEqual(daily_series(self.store, run['day']), [])
        self.assertEqual(daily_series(self.store, run['day'], current_run=run, current_rows=rows)[0]['reached'], 1)
        self.assertEqual(daily_series(self.store, run['day'], current_run={**run, 'kind': 'preview'}, current_rows=rows), [])
