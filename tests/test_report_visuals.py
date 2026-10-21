import unittest
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import Mock

from PIL import Image

from fahra_automation.report_chart import REACHED, _trend, render_chart
from fahra_automation.report_views import ZONE, account_groups, daily_series, render_report, target_percent
from fahra_automation.util import iso

AT = datetime(2026, 10, 21, 8, tzinfo=ZONE)


def run(kind='daily', complete=True):
    return {'id': 'fixture', 'day': '2026-10-21', 'kind': kind, 'inventory_complete': complete,
            'started_at': iso(AT), 'due_at': iso(AT)}


def row(identity, followers=None, status='ok', at=AT):
    return {'account_id': str(identity), 'username': f'fixture_{identity}', 'status': status,
            'fetched_at': iso(at) if status == 'ok' else None,
            'data': {'followers': followers, 'daily': [], 'warnings': []} if followers is not None else None}


def point(day, reached, *, complete=True):
    return {'day': day, 'reached': reached, 'below': 9 - reached, 'unknown': 0 if complete else 1,
            'total': 9 if complete else 10, 'complete': complete}


class ReportVisualTests(unittest.TestCase):
    def test_exact_target_and_unknown_cannot_be_classified_below(self):
        rows = [row('zero', 0), row('near', 599), row('exact', 600), row('above', 601),
                row('failed', status='error'), row('offline', status='disconnected')]
        reached, below, unknown = account_groups(rows)
        self.assertEqual({r['account_id'] for r in reached}, {'exact', 'above'})
        self.assertEqual({r['account_id'] for r in below}, {'zero', 'near'})
        self.assertEqual({r['account_id'] for r in unknown}, {'failed', 'offline'})
        report = '\n'.join(render_report(run(), rows, {}, {}, {}))
        self.assertIn('Sudah mencapai: <b>2 akun</b> · belum: <b>2 akun</b>', report)
        self.assertIn('Belum bisa dinilai: <b>2 akun</b>', report)
        self.assertIn('99,8% target · kurang 1', report)
        self.assertIn('0% target · kurang 600', report)
        self.assertEqual(report.count('100% target · tercapai'), 2)
        self.assertNotIn('belum tersedia follower', report)

    def test_progress_caps_at_target_without_hiding_followers(self):
        self.assertEqual(target_percent(9500), '100%')
        report = '\n'.join(render_report(run(), [row('high', 9500)], {}, {}, {}))
        self.assertIn('9.500 follower', report)
        self.assertIn('100% target · tercapai', report)

    def test_crossing_only_when_true_comparator_exists(self):
        rows = [row('up', 600), row('down', 599), row('still', 600)]
        yesterday = {r['account_id']: r for r in [row('up', 599, at=AT-timedelta(days=1)),
                                                row('down', 600, at=AT-timedelta(days=1)),
                                                row('still', 800, at=AT-timedelta(days=1))]}
        report = '\n'.join(render_report(run(), rows, yesterday, {}, yesterday))
        self.assertEqual(report.count('Baru mencapai 600 follower'), 1)
        self.assertEqual(report.count('Turun ke bawah 600 follower'), 1)
        self.assertIn('Harian: <b>−200</b>', report)
        self.assertIn('7 hari: baseline D−7 belum tersedia', report)
        self.assertNotIn('Baru mencapai', '\n'.join(render_report(run(), rows, {}, {}, {})))

    def test_every_current_account_once_in_readable_pages(self):
        rows = [row(i, i * 30) for i in range(60)]
        parts = render_report(run(), rows, {}, {}, {})
        report = '\n'.join(parts)
        for account in rows:
            self.assertEqual(report.count(f">@{account['username']}</a>"), 1)
        self.assertGreaterEqual(len(parts), 8)
        for part in parts:
            self.assertLessEqual(part.count('<a href='), 8)
            self.assertLessEqual(len(part.encode('utf-16-le')) // 2, 3500)
        self.assertEqual(report.count('Sudah mencapai:'), 1)

    def test_disconnected_keeps_last_dated_evidence(self):
        old = row('off', 900, at=AT-timedelta(days=1))
        report = '\n'.join(render_report(run(), [row('off', status='disconnected')], {}, {}, {'off': old}))
        self.assertIn('terputus dari Repliz', report)
        self.assertIn('Terakhir diketahui 900 follower · 20/10 08:00 WIB', report)
        self.assertIn('Sudah mencapai: <b>0 akun</b> · belum: <b>0 akun</b>', report)
        self.assertNotIn('<b>0 follower</b>', report)

    def test_failed_inventory_is_unknown_not_empty_or_zero(self):
        report = '\n'.join(render_report(run(complete=False), [], {}, {}, {}))
        self.assertIn('cakupan akun belum diketahui', report)
        self.assertIn('Status target belum dapat dihitung', report)
        self.assertNotIn('0 akun', report)
        self.assertNotIn('Tidak ada akun', report)

    def test_daily_series_uses_calendar_gaps_and_preserves_todays_daily_in_preview(self):
        series = [point('2026-10-19', 2), point('2026-10-21', 3)]
        calendar = daily_series(run(), series)
        self.assertEqual(len(calendar), 7)
        self.assertIsNone(dict(calendar)['2026-10-20'])
        self.assertEqual(dict(calendar)['2026-10-21']['reached'], 3)
        for kind in ('preview', 'pilot'):
            with self.subTest(kind=kind):
                self.assertEqual(daily_series(run(kind), series)[-1][0], '2026-10-21')
                report = '\n'.join(render_report(run(kind), [], {}, {}, {}, series=series))
                self.assertIn('21/10: 3', report)
                self.assertIn('Tren hanya memakai snapshot laporan harian', report)

    def test_chart_never_connects_across_missing_or_partial_days(self):
        draw = Mock()
        series = [point('2026-10-15', 1), point('2026-10-17', 2), point('2026-10-18', 3),
                  point('2026-10-19', 2, complete=False), point('2026-10-20', 4)]
        _trend(draw, run(), series, 0)
        segments = [call for call in draw.line.call_args_list if call.kwargs.get('fill') == REACHED]
        self.assertEqual(len(segments), 1, 'Only consecutive complete days 17 and 18 may connect.')
        labels = [call.args[1] for call in draw.text.call_args_list]
        self.assertEqual(labels.count('?'), 2)
        self.assertIn('2*', labels)
        report = '\n'.join(render_report(run(), [], {}, {}, {}, series=series))
        self.assertIn('19/10: 2 teramati (data parsial)', report)
        self.assertIn('16/10: belum tersedia', report)

    def test_chart_png_is_bounded_deterministic_and_all_states_render(self):
        for kind, rows, complete in (
                ('daily', [row(i, i * 20) for i in range(50)], True),
                ('preview', [row('long' * 30, 599)], True),
                ('pilot', [], False),
                ('daily', [row('off', status='disconnected')], True),
                ('daily', [row('exact', 600)], True),
                ('daily', [], True)):
            with self.subTest(kind=kind, rows=len(rows), complete=complete):
                current = run(kind, complete)
                data = render_chart(current, rows, [point('2026-10-19', 2)])
                self.assertEqual(data, render_chart(current, rows, [point('2026-10-19', 2)]))
                self.assertLess(len(data), 5_000_000)
                image = Image.open(BytesIO(data))
                self.assertEqual(image.format, 'PNG')
                self.assertEqual(image.width, 900)
                self.assertLessEqual(image.height, 2200)
                self.assertEqual(image.mode, 'RGB')
