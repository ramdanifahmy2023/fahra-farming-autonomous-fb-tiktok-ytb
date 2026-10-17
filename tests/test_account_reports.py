import asyncio
import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from fahra_automation.account_reports import AccountReports, ReportStopped, parse_statistics
from fahra_automation.config import Config, EXCLUDED_ACCOUNT_ID
from fahra_automation.db import Store
from fahra_automation.report_store import CHAT_ID, ReportStore, digest
from fahra_automation.report_views import ZONE, delta, render_report
from fahra_automation.telegram import Telegram, TelegramError
from fahra_automation.util import iso


AT = datetime(2026, 10, 18, 8, tzinfo=ZONE)


def metric(followers=601, **extra):
    return {'followersCount': followers, 'totalLikes': 1234, 'videosCount': 5, **extra}


def account(identity='a', name='valid', connected=True):
    return {'id': identity, 'username': name, 'type': 'tiktok', 'isConnected': connected}


class Bot:
    def __init__(self):
        self.sent = []
        self.error = None
        self.chat_type = 'private'

    async def call(self, method, **kwargs):
        return {'username': 'Fahra_autonomousbot'} if method == 'getMe' else {'id': CHAT_ID, 'type': self.chat_type}

    async def send_ui(self, method, chat_id, *, before_send=None, **kwargs):
        before_send()
        self.sent.append((chat_id, kwargs))
        if self.error:
            raise self.error
        return {'message_id': 100 + len(self.sent)}


class ReportCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root, copy.deepcopy(Config.load().data))
        self.config.require_live_action = Mock()
        self.store = Store(self.root / 'runtime/test.sqlite3')
        self.store.set('account_report_enabled', True)
        self.at = AT
        self.api = Mock()
        self.api.pages = AsyncMock(return_value=[account()])
        self.api.request = AsyncMock(return_value=metric())
        self.bot = Bot()
        self.worker = AccountReports(self.config, self.store, self.api, self.bot, clock=lambda: self.at)

    async def asyncTearDown(self):
        await self.worker.cancel_collection()
        self.temp.cleanup()

    def run_row(self, kind='daily', day=None, at=None):
        at = at or self.at
        return self.worker.db.create(kind, day or at.astimezone(ZONE).date().isoformat(), at, at + timedelta(minutes=10), at)

    async def ready(self, kind='daily'):
        run = self.run_row(kind)
        await self.worker.collect(run)
        self.worker.freeze(self.worker.db.run(run['id']), self.at)
        return run

    def part(self, run):
        with self.store.connect() as c:
            return dict(c.execute('SELECT * FROM account_report_parts WHERE run_id=? ORDER BY part_no', (run['id'],)).fetchone())

    async def test_threshold_all_accounts_are_stored(self):
        self.api.pages.return_value = [account(str(i), f'user{i}') for i in range(3)]
        self.api.request.side_effect = [metric(599), metric(600), metric(601)]
        run = await self.ready()
        text = self.part(run)['text']
        self.assertIn('@user2', text)
        self.assertNotIn('@user0', text)
        self.assertNotIn('@user1', text)
        self.assertEqual(len(self.worker.db.rows(run['id'])), 3)

    async def test_protected_identity_and_normalized_name_never_fetched(self):
        self.api.pages.return_value = [account(), account(EXCLUDED_ACCOUNT_ID), account('b', ' @IQ7FansPage ')]
        run = await self.ready()
        self.assertEqual(self.api.request.await_count, 1)
        self.assertEqual(self.worker.db.run(run['id'])['excluded'], 2)

    async def test_failed_inventory_never_claims_empty(self):
        self.api.pages.side_effect = ValueError('pagination incomplete')
        run = await self.ready()
        self.assertIn('cakupan akun belum diketahui', self.part(run)['text'])
        self.assertNotIn('Tidak ada akun', self.part(run)['text'])

    async def test_bad_connection_invalidates_inventory(self):
        self.api.pages.return_value = [account(connected=None)]
        run = await self.ready()
        self.assertFalse(self.worker.db.run(run['id'])['inventory_complete'])
        self.api.request.assert_not_awaited()

    async def test_total_failure_and_disconnected_distinct(self):
        self.api.pages.return_value = [account(), account('b', 'offline', False)]
        self.api.request.side_effect = TimeoutError()
        run = await self.ready()
        text = self.part(run)['text']
        self.assertIn('Statistik gagal untuk 1 akun', text)
        self.assertIn('terputus dari Repliz', text)
        self.assertEqual(self.api.request.await_count, 2)

    async def test_partial_error_not_zero(self):
        self.api.pages.return_value = [account(), account('b', 'failed')]
        self.api.request.side_effect = [metric(9500), metric(None), metric(None)]
        run = await self.ready()
        text = self.part(run)['text']
        self.assertIn('1/2 akun', text)
        self.assertIn('9.500 follower', text)
        self.assertIn('@failed', text)

    async def test_frozen_snapshot_immutable(self):
        run = await self.ready()
        before = self.part(run)
        self.worker.db.metric(run['id'], 'a', data=parse_statistics(metric(9999), self.at), fetched=self.at)
        self.worker.freeze(self.worker.db.run(run['id']), self.at)
        self.assertEqual(self.part(run), before)
        self.assertEqual(self.worker.db.rows(run['id'])[0]['data']['followers'], 601)

    async def test_stale_data_not_baseline(self):
        run = self.run_row()
        await self.worker.collect(run)
        self.worker.freeze(self.worker.db.run(run['id']), self.at + timedelta(minutes=21))
        self.assertEqual(self.worker.db.rows(run['id'])[0]['status'], 'stale')
        self.assertEqual(self.worker.db.baseline('2026-10-19', 1), {})

    async def test_pilot_not_daily_baseline(self):
        await self.ready('pilot')
        self.assertEqual(self.worker.db.baseline('2026-10-19', 1), {})

    async def test_rename_keeps_id_and_crossing(self):
        self.at = AT - timedelta(days=1)
        self.api.request.return_value = metric(599)
        await self.ready()
        self.at = AT
        self.api.pages.return_value = [account(name='renamed')]
        self.api.request.return_value = metric(700)
        run = await self.ready()
        self.assertIn('Username sebelumnya', self.part(run)['text'])
        self.assertIn('Harian: <b>+101</b>', self.part(run)['text'])
        self.assertNotIn('Baru dipantau', self.part(run)['text'])

    async def test_crossing_and_drop(self):
        self.at = AT - timedelta(days=1)
        self.api.pages.return_value = [account(), account('b', 'second')]
        self.api.request.side_effect = [metric(599), metric(800), metric(700), metric(600)]
        await self.ready()
        self.at = AT
        run = await self.ready()
        text = self.part(run)['text']
        self.assertIn('Baru melewati 600', text)
        self.assertIn('Turun ke ≤600', text)
        self.assertIn('−200', text)

    async def test_schedule_freezes_at_8_not_759(self):
        self.at = AT - timedelta(minutes=10)
        await self.worker.tick()
        await self.worker.collection
        self.at = AT - timedelta(minutes=1)
        await self.worker.tick()
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM account_report_parts').fetchone()[0], 0)
        self.at = AT
        await self.worker.tick()
        await self.worker.tick()
        self.assertEqual(len(self.worker.db.summary()['runs']), 1)
        self.assertEqual(self.worker.db.summary()['runs'][0]['state'], 'ready')

    async def test_deadline_cancels_slow_get_and_reports_failure(self):
        async def slow(*args):
            await asyncio.sleep(100)
        self.api.request.side_effect = slow
        self.at = AT - timedelta(minutes=10)
        await self.worker.tick()
        await asyncio.sleep(0)
        self.at = AT
        await self.worker.tick()
        run = self.worker.db.summary()['runs'][0]
        self.assertEqual(run['state'], 'ready')
        self.assertIn('belum selesai', self.part(run)['text'])

    async def test_catch_up_is_late_and_one_run(self):
        self.at = AT + timedelta(hours=2)
        await self.worker.tick()
        await self.worker.collection
        await self.worker.tick()
        run = self.worker.db.summary()['runs'][0]
        self.assertIn('terlambat', self.part(run)['text'])
        await self.worker.tick()
        self.assertEqual(len(self.worker.db.summary()['runs']), 1)

    async def test_sleep_after_collection_recollects_stale_snapshot(self):
        self.at = AT - timedelta(minutes=10)
        await self.worker.tick()
        await self.worker.collection
        self.at = AT + timedelta(hours=1)
        await self.worker.tick()
        await self.worker.collection
        await self.worker.tick()
        self.assertEqual(self.api.request.await_count, 2)
        self.assertIn('terlambat', self.part(self.worker.db.summary()['runs'][0])['text'])

    async def test_no_old_day_flood(self):
        self.store.set('account_report_activation_day', '2026-10-16')
        self.at = AT + timedelta(hours=4)
        await self.worker.tick()
        self.assertEqual([r['state'] for r in self.worker.db.summary()['runs']], ['missed'] * 3)
        self.api.request.assert_not_awaited()

    async def test_pause_publishing_does_not_pause_report(self):
        self.store.set('paused', True)
        run = await self.ready()
        await self.worker.flush_one()
        self.assertEqual(self.part(run)['state'], 'sent')

    async def test_toggle_and_guard_after_throttle(self):
        run = await self.ready()
        async def delay(method, chat, *, before_send, **kwargs):
            self.store.set('account_report_enabled', False)
            before_send()
        self.bot.send_ui = delay
        with self.assertRaises(ReportStopped):
            await self.worker.flush_one()
        self.assertIsNone(self.worker.journal.state('send:' + self.part(run)['id']))
        self.assertEqual(self.part(run)['state'], 'queued')

    async def test_group_destination_rejected(self):
        run = await self.ready()
        self.bot.chat_type = 'group'
        with self.assertRaises(ReportStopped):
            await self.worker.flush_one()
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.part(run)['state'], 'queued')

    async def test_send_once_restart_and_restore_old_database(self):
        run = await self.ready()
        await self.worker.flush_one()
        await self.worker.flush_one()
        restored = Store(self.root / 'runtime/restored.sqlite3')
        restored.set('account_report_enabled', True)
        worker = AccountReports(self.config, restored, self.api, self.bot, clock=lambda: self.at)
        worker.recover()
        await worker.flush_one()
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(worker.db.summary()['parts'][0]['state'], 'sent')
        self.assertEqual(worker.db.rows(run['id'])[0]['data']['followers'], 601)

    async def test_timeout_is_unknown_and_never_retried(self):
        run = await self.ready()
        self.bot.error = httpx.ReadTimeout('ambiguous')
        await self.worker.flush_one()
        self.worker.recovered = False
        await self.worker.flush_one()
        self.assertEqual(self.part(run)['state'], 'send_unknown')
        self.assertEqual(len(self.bot.sent), 1)

    async def test_crash_after_intent_blocks_resend(self):
        run = await self.ready()
        part = self.part(run)
        self.worker.journal.begin('send:' + part['id'], {'chat_id': CHAT_ID, 'text': part['text'], 'content_hash': part['content_hash']})
        await self.worker.flush_one()
        self.assertEqual(self.part(run)['state'], 'send_unknown')
        self.assertEqual(self.bot.sent, [])

    async def test_accepted_response_recovers_after_database_ack_failure(self):
        run = await self.ready()
        original = self.worker.db.part_state
        fail = True
        def flaky(*args, **kwargs):
            nonlocal fail
            if args[1] == 'sent' and fail:
                fail = False
                raise OSError('disk unavailable')
            return original(*args, **kwargs)
        with patch.object(self.worker.db, 'part_state', side_effect=flaky):
            await self.worker.flush_one()
        self.assertEqual(self.part(run)['message_id'], 101)
        self.assertEqual(self.part(run)['state'], 'sent')

    async def test_rate_limit_defers_then_retries(self):
        run = await self.ready()
        self.bot.error = TelegramError(429, 'rate limited', 60)
        await self.worker.flush_one()
        self.worker.recovered = False
        await self.worker.flush_one()
        self.assertEqual(len(self.bot.sent), 1)
        self.at += timedelta(seconds=61)
        self.bot.error = None
        await self.worker.flush_one()
        self.assertEqual(self.part(run)['state'], 'sent')
        self.assertEqual(len(self.bot.sent), 2)

    async def test_permanent_format_and_blocked_errors(self):
        for code in (400, 403, 404):
            self.at += timedelta(days=1)
            run = await self.ready()
            self.bot.error = TelegramError(code, 'rejected')
            await self.worker.flush_one()
            self.worker.recovered = False
            await self.worker.flush_one()
            self.assertEqual(self.part(run)['state'], 'failed')
        self.assertEqual(len(self.bot.sent), 3)

    async def test_corrupted_manifest_blocks_delivery(self):
        run = await self.ready()
        path = sorted(self.worker.journal.folder('manifest:' + run['id']).glob('*.json'))[-1]
        item = json.loads(path.read_text())
        item['body']['parts'][0]['text'] = 'changed'
        path.write_text(json.dumps(item))
        with self.assertRaises(ValueError):
            await self.worker.flush_one()
        self.assertEqual(self.bot.sent, [])

    async def test_cutoff_expires_queued_without_transport(self):
        run = await self.ready()
        self.at += timedelta(hours=4)
        await self.worker.flush_one()
        self.assertEqual(self.part(run)['state'], 'failed')
        self.assertEqual(self.bot.sent, [])

    async def test_stats_do_not_modify_publishing_snapshot(self):
        self.store.set('sentinel', 123)
        before = self.store.snapshot_fresh()
        await self.ready()
        self.assertEqual(self.store.snapshot_fresh(), before)
        self.assertEqual(self.store.accounts(), [])

    async def test_daily_schedule_takes_priority_over_manual_pilot(self):
        self.at = AT - timedelta(minutes=10)
        self.store.set('account_report_pilot_day', self.at.date().isoformat())
        await self.worker.tick()
        self.assertTrue(self.worker.collecting_id.startswith('daily:'))

    async def test_missed_delivery_reported_but_unknown_not_called_missed(self):
        self.at = AT - timedelta(days=1)
        run = await self.ready()
        self.at = AT
        next_run = self.run_row()
        self.assertEqual(self.worker.db.missed(next_run), 1)
        self.worker.db.part_state(self.part(run)['id'], 'send_unknown')
        self.assertEqual(self.worker.db.missed(next_run), 0)

    async def test_reconnect_and_missing_inventory_are_distinct(self):
        self.at = AT - timedelta(days=2)
        await self.ready()
        self.at = AT - timedelta(days=1)
        self.api.pages.return_value = [account(connected=False)]
        await self.ready()
        self.at = AT
        self.api.pages.return_value = [account()]
        run = await self.ready()
        self.assertIn('Terhubung kembali', self.part(run)['text'])
        self.assertIn('baseline D−1 belum tersedia', self.part(run)['text'])
        self.at += timedelta(days=1)
        self.api.pages.return_value = []
        run = await self.ready()
        self.assertIn('tidak ada pada inventaris Repliz', self.part(run)['text'])

    async def test_seven_day_exact_date_not_previous_available(self):
        self.at = AT - timedelta(days=7)
        self.api.request.return_value = metric(800)
        await self.ready()
        self.at = AT - timedelta(days=2)
        self.api.request.return_value = metric(650)
        await self.ready()
        self.at = AT
        self.api.request.return_value = metric(700)
        run = await self.ready()
        text = self.part(run)['text']
        self.assertIn('7 hari: <b>−100</b>', text)
        self.assertIn('baseline D−1 belum tersedia', text)

    async def test_second_part_timeout_preserves_first_message(self):
        self.api.pages.return_value = [account(str(i), 'user' + str(i)) for i in range(12)]
        run = await self.ready()
        await self.worker.flush_one()
        self.bot.error = TimeoutError()
        await self.worker.flush_one()
        self.worker.recovered = False
        self.worker.recover()
        with self.store.connect() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM account_report_parts WHERE run_id=? ORDER BY part_no', (run['id'],))]
        self.assertEqual(rows[0]['state'], 'sent')
        self.assertEqual(rows[0]['message_id'], 101)
        self.assertEqual(rows[1]['state'], 'send_unknown')
        self.assertEqual(rows[2]['state'], 'queued')

    async def test_manifest_before_database_commit_is_recoverable(self):
        run = self.run_row()
        await self.worker.collect(run)
        with patch.object(self.worker.db, 'restore', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                self.worker.freeze(self.worker.db.run(run['id']), self.at)
        self.worker.recover()
        self.assertEqual(self.worker.db.run(run['id'])['state'], 'ready')
        await self.worker.flush_one()
        self.assertEqual(self.part(run)['state'], 'sent')

    async def test_startup_retries_before_recovering_operational_workers(self):
        from fahra_automation.cli import wait_for_startup
        api, replies = Mock(), Mock()
        api.sync = AsyncMock(side_effect=[TimeoutError(), None])
        with patch('fahra_automation.cli.asyncio.sleep', new_callable=AsyncMock) as sleep, patch('fahra_automation.cli.AttemptJournal') as journal, patch.object(self.store, 'recover') as recover:
            await wait_for_startup(self.config, self.store, api, replies)
            self.assertEqual(api.sync.await_count, 2)
            sleep.assert_awaited_once_with(30)
            journal.return_value.reconcile_store.assert_called_once()
            recover.assert_called_once()
            replies.db.recover.assert_called_once()
        self.assertTrue(self.store.get('startup_health')['ready'])

    async def test_slow_startup_journal_does_not_block_event_loop(self):
        from fahra_automation.cli import wait_for_startup
        api, replies = Mock(), Mock()
        api.sync = AsyncMock()
        entered, release = threading.Event(), threading.Event()
        def reconcile(store):
            entered.set()
            if not release.wait(2):
                raise TimeoutError('event loop blocked')
        with patch('fahra_automation.cli.AttemptJournal') as journal:
            journal.return_value.reconcile_store.side_effect = reconcile
            task = asyncio.create_task(wait_for_startup(self.config, self.store, api, replies))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertFalse(task.done())
                self.assertEqual(self.store.get('startup_health')['phase'], 'reconciling_journal')
                release.set()
                await asyncio.wait_for(task, 2)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_startup_failure_does_not_enable_operational_workers(self):
        from fahra_automation.cli import wait_for_startup
        api, replies = Mock(), Mock()
        api.sync = AsyncMock(side_effect=TimeoutError())
        with patch('fahra_automation.cli.asyncio.sleep', side_effect=asyncio.CancelledError()), patch.object(self.store, 'recover') as recover:
            with self.assertRaises(asyncio.CancelledError):
                await wait_for_startup(self.config, self.store, api, replies)
            recover.assert_not_called()
            replies.db.recover.assert_not_called()
        self.assertFalse(self.store.get('startup_health')['ready'])

    async def test_native_transport_preserves_callback_and_html(self):
        calls = []
        async def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 77}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://telegram.invalid/')
        bot = Telegram(self.config, self.store, None, client=client)
        mark = Mock()
        try:
            await bot.send_ui('sendMessage', CHAT_ID, text='<b>601</b>', parse_mode='HTML', before_send=mark)
            mark.assert_called_once()
            self.assertNotIn('before_send', calls[0])
            self.assertTrue(calls[0]['link_preview_options']['is_disabled'])
        finally:
            await client.aclose()


class ParserAndViewTests(unittest.TestCase):
    def test_strict_counter(self):
        for bad in (None, True, '601', -1, 1.5, 10**16):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                parse_statistics(metric(bad), AT)
        self.assertEqual(parse_statistics(metric(0), AT)['followers'], 0)

    def test_unsorted_dates_conflict_future_and_zero_anomaly(self):
        fields = ('followersCount', 'dailyNewFollowers', 'dailyLostFollowers', 'dailyTotalFollowers', 'videoViews', 'profileViews', 'likes', 'comments', 'shares')
        value = metric(9471, metrics=[
            {'date': '2026-10-16', **dict.fromkeys(fields, 0)},
            {'date': '2026-10-15', 'followersCount': 9474},
            {'date': '2026-10-10', 'followersCount': 9480},
            {'date': '2026-10-12', 'followersCount': 9},
            {'date': '2026-10-12', 'followersCount': 10},
            {'date': '2026-10-20', 'followersCount': 9999},
            {'followersCount': 9}])
        result = parse_statistics(value, AT)
        self.assertEqual([d['day'] for d in result['daily']], ['2026-10-10', '2026-10-15'])
        self.assertTrue(any('serba nol' in w for w in result['warnings']))
        self.assertTrue(any('ganda' in w for w in result['warnings']))

    def test_zero_baseline_and_actual_interval(self):
        row = {'data': {'followers': 700}, 'fetched_at': iso(AT)}
        base = {'data': {'followers': 0}, 'fetched_at': iso(AT-timedelta(hours=28))}
        result = delta(row, base, 1)
        self.assertIn('baseline 0', result)
        self.assertNotIn('sekitar 24 jam', result)
        base['fetched_at'] = iso(AT-timedelta(hours=24))
        self.assertIn('sekitar 24 jam', delta(row, base, 1))

    def test_all_accounts_html_escape_and_utf16_budget(self):
        run = {'id':'fixture', 'day':'2026-10-18', 'kind':'daily', 'started_at':iso(AT), 'due_at':iso(AT), 'inventory_complete':1}
        rows = [{'account_id':str(i), 'username':f'user{i}' if i else '<script>😀&"', 'status':'ok', 'fetched_at':iso(AT), 'data':parse_statistics(metric(99999+i), AT)} for i in range(75)]
        parts = render_report(run, rows, {}, {}, {})
        combined = '\n'.join(parts)
        self.assertGreater(len(parts), 12)
        self.assertNotIn('<script>', combined)
        self.assertIn('&lt;script&gt;', combined)
        for i in range(1,75):
            self.assertIn(f'>@user{i}</a>', combined)
        class Parser(HTMLParser):
            def __init__(self):
                super().__init__(); self.stack=[]
            def handle_starttag(self, tag, attrs):
                self_outer.assertIn(tag, ('a','b')); self.stack.append(tag)
            def handle_endtag(self, tag):
                self_outer.assertEqual(self.stack.pop(), tag)
        self_outer = self
        for i, part in enumerate(parts, 1):
            self.assertLessEqual(len(part.encode('utf-16-le')) // 2, 3500)
            self.assertIn(f'Bagian {i}/{len(parts)}', part)
            parser = Parser(); parser.feed(part)
            self.assertEqual(parser.stack, [])
