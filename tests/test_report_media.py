import base64
import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import httpx
from PIL import Image

from fahra_automation import backup
from fahra_automation.account_reports import AccountReports
from fahra_automation.config import Config
from fahra_automation.db import Store
from fahra_automation.report_store import CHAT_ID, MAX_PHOTO_BYTES, decode_photo, digest, sending_body
from fahra_automation.report_views import ZONE
from fahra_automation.telegram import Telegram, TelegramError


def png(color='white', size=(80, 100)):
    output = BytesIO()
    Image.new('RGB', size, color).save(output, 'PNG')
    return base64.b64encode(output.getvalue()).decode('ascii')


class MediaBot:
    def __init__(self):
        self.sent = []
        self.error = None

    async def call(self, method, **kwargs):
        return {'username': 'Fahra_autonomousbot'} if method == 'getMe' else {'id': CHAT_ID, 'type': 'private'}

    async def send_ui(self, method, chat_id, *, before_send=None, **kwargs):
        before_send()
        self.sent.append((method, chat_id, kwargs))
        if self.error:
            raise self.error
        return {'message_id': 200 + len(self.sent)}


class ReportMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root, copy.deepcopy(Config.load().data))
        self.config.require_live_action = Mock()
        self.store = Store(self.root / 'runtime/test.sqlite3')
        self.store.set('account_report_enabled', True)
        self.at = datetime(2026, 10, 21, 8, tzinfo=ZONE)
        self.bot = MediaBot()
        self.worker = AccountReports(self.config, self.store, None, self.bot, clock=lambda: self.at)

    def tearDown(self):
        self.temp.cleanup()

    def freeze(self, *, photo=True):
        run = self.worker.db.create('daily', self.at.date().isoformat(), self.at,
                                    self.at + timedelta(minutes=10), self.at)
        parts = [{'text': '<b>Target 600</b>', 'photo': png()}, 'Daftar akun'] if photo else ['Laporan lama']
        manifest = self.worker.db.manifest(run, parts, self.at)
        self.worker.journal.begin('manifest:' + run['id'], manifest)
        self.worker.db.restore(manifest)
        return manifest

    def first(self, manifest):
        return self.worker.db.part(manifest['parts'][0]['id'])

    async def test_photo_and_roster_delivered_once_after_old_database_restore(self):
        manifest = self.freeze()
        photo = manifest['parts'][0]['photo']
        self.assertEqual(manifest['parts'][0]['content_hash'], digest({'text': '<b>Target 600</b>', 'photo': photo}))
        await self.worker.flush_one()
        self.assertEqual(self.bot.sent[0][0], 'sendPhoto')
        self.assertEqual(self.bot.sent[0][2]['photo'], decode_photo(photo))
        self.assertEqual(self.bot.sent[0][2]['caption'], '<b>Target 600</b>')
        self.assertEqual(self.first(manifest)['message_id'], 201)
        restored = Store(self.root / 'runtime/older.sqlite3')
        restored.set('account_report_enabled', True)
        worker = AccountReports(self.config, restored, None, self.bot, clock=lambda: self.at)
        worker.recover()
        self.assertEqual(worker.db.parts(manifest['run']['id']), manifest['parts'])
        await worker.flush_one()
        await worker.flush_one()
        self.assertEqual([call[0] for call in self.bot.sent], ['sendPhoto', 'sendMessage'])
        self.assertEqual(worker.db.part(manifest['parts'][0]['id'])['message_id'], 201)

    async def test_photo_rate_limit_retries_exact_bytes_after_restart(self):
        manifest = self.freeze()
        self.bot.error = TelegramError(429, 'rate limit', 60)
        await self.worker.flush_one()
        self.assertEqual(self.first(manifest)['state'], 'queued')
        self.worker.recovered = False
        self.worker.recover()
        self.at += timedelta(seconds=61)
        self.bot.error = None
        await self.worker.flush_one()
        self.assertEqual(self.first(manifest)['state'], 'sent')
        self.assertEqual(self.bot.sent[0], self.bot.sent[1])

    async def test_photo_timeout_never_repeats_and_text_can_continue(self):
        manifest = self.freeze()
        self.bot.error = httpx.ReadTimeout('ambiguous')
        await self.worker.flush_one()
        self.worker.recovered = False
        self.worker.recover()
        self.bot.error = None
        await self.worker.flush_one()
        self.assertEqual(self.first(manifest)['state'], 'send_unknown')
        self.assertEqual([call[0] for call in self.bot.sent], ['sendPhoto', 'sendMessage'])

    async def test_crash_after_photo_intent_blocks_photo_resend(self):
        manifest = self.freeze()
        part = self.first(manifest)
        self.worker.journal.begin('send:' + part['id'], sending_body(part))
        self.worker.recover()
        await self.worker.flush_one()
        self.assertEqual(self.first(manifest)['state'], 'send_unknown')
        self.assertEqual([call[0] for call in self.bot.sent], ['sendMessage'])

    async def test_legacy_text_manifest_and_intent_keep_original_hash(self):
        manifest = self.freeze(photo=False)
        part = self.first(manifest)
        self.assertNotIn('photo', manifest['parts'][0])
        self.assertEqual(part['content_hash'], digest('Laporan lama'))
        self.assertEqual(sending_body(part), {'chat_id': CHAT_ID, 'text': 'Laporan lama', 'content_hash': digest('Laporan lama')})
        await self.worker.flush_one()
        self.worker.recovered = False
        await self.worker.flush_one()
        self.assertEqual(self.first(manifest)['state'], 'sent')
        self.assertEqual(len(self.bot.sent), 1)

    async def test_photo_database_corruption_blocks_transport(self):
        manifest = self.freeze()
        self.worker.recover()
        with self.store.tx() as c:
            c.execute('UPDATE account_report_media SET photo=?', (png('black'),))
        with self.assertRaises(ValueError):
            await self.worker.flush_one()
        self.assertEqual(self.bot.sent, [])

    def test_photo_manifest_corruption_and_immutable_change_rejected(self):
        manifest = self.freeze()
        changed = copy.deepcopy(manifest)
        changed['parts'][0]['photo'] = png('black')
        with self.assertRaises(ValueError):
            self.worker.db.restore(changed)
        changed['parts'][0]['content_hash'] = digest({'text': changed['parts'][0]['text'], 'photo': changed['parts'][0]['photo']})
        with self.assertRaisesRegex(ValueError, 'beku'):
            self.worker.db.restore(changed)
        self.assertEqual(self.worker.db.parts(manifest['run']['id']), manifest['parts'])

    def test_photo_intent_must_match_frozen_manifest(self):
        manifest = self.freeze()
        part = self.first(manifest)
        body = sending_body(part)
        body['photo'] = png('black')
        self.worker.journal.begin('send:' + part['id'], body)
        with self.assertRaisesRegex(ValueError, 'manifest'):
            self.worker.recover()

    async def test_backup_restore_keeps_photo_and_accepted_delivery(self):
        manifest = self.freeze()
        await self.worker.flush_one()
        folder = backup.create(self.config, self.store, self.root / 'backups')
        backup.verify(folder)
        destination = self.root / 'restored'
        backup.restore(folder, destination)
        config = Config(destination, copy.deepcopy(self.config.data))
        config.require_live_action = Mock()
        store = Store(destination / 'runtime/workflow.sqlite3')
        self.assertFalse(store.get('account_report_enabled'))
        store.set('account_report_enabled', True)
        worker = AccountReports(config, store, None, self.bot, clock=lambda: self.at)
        worker.recover()
        self.assertEqual(worker.db.parts(manifest['run']['id']), manifest['parts'])
        self.assertEqual(worker.db.part(manifest['parts'][0]['id'])['message_id'], 201)
        await worker.flush_one()
        self.assertEqual([call[0] for call in self.bot.sent], ['sendPhoto', 'sendMessage'])

    async def test_native_photo_multipart_preserves_guard_and_error_decoder(self):
        calls = []
        marked = []
        async def handler(request):
            self.assertEqual(marked, [True])
            calls.append(request)
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 88}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://telegram.invalid/')
        bot = Telegram(self.config, self.store, None, client=client)
        try:
            raw = decode_photo(png())
            result = await bot.send_ui('sendPhoto', CHAT_ID, photo=raw, caption='<b>Target 600</b>',
                                       parse_mode='HTML', before_send=lambda: marked.append(True))
            self.assertEqual(result['message_id'], 88)
            self.assertIn('multipart/form-data', calls[0].headers['content-type'])
            self.assertIn(raw, calls[0].content)
            self.assertIn(b'name="caption"\r\n\r\n<b>Target 600</b>', calls[0].content)
            self.assertNotIn(b'link_preview_options', calls[0].content)
            self.assertNotIn(b'before_send', calls[0].content)
        finally:
            await client.aclose()
        async def reject(request):
            return httpx.Response(429, json={'ok': False, 'error_code': 429, 'description': 'retry', 'parameters': {'retry_after': 12}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reject), base_url='https://telegram.invalid/') as client:
            bot = Telegram(self.config, self.store, None, client=client)
            with self.assertRaises(TelegramError) as caught:
                await bot.send_ui('sendPhoto', CHAT_ID, photo=decode_photo(png()), caption='Target 600')
            self.assertEqual(caught.exception.retry_after, 12)

    def test_reject_invalid_oversized_and_extreme_png_or_caption(self):
        for value in ('not base64', '', base64.b64encode(b'plain text').decode('ascii'),
                      'A' * (4 * ((MAX_PHOTO_BYTES + 2) // 3) + 1), png(size=(1, 21))):
            with self.subTest(value=value[:30]), self.assertRaises(ValueError):
                decode_photo(value)
        run = self.worker.db.create('daily', self.at.date().isoformat(), self.at, self.at, self.at)
        with self.assertRaisesRegex(ValueError, 'Caption'):
            self.worker.db.manifest(run, [{'text': '😀' * 513, 'photo': png()}], self.at)
