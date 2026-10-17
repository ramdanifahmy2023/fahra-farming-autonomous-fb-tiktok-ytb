import copy
import asyncio
from datetime import timedelta
import sqlite3
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx

from fahra_automation.config import Config
from fahra_automation.db import Store
from fahra_automation.telegram import Telegram, TelegramError
from fahra_automation import telegram_views as views
from fahra_automation.util import iso, now


class ValidHTML(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.plain = ""
        self.feed(text)
        assert not self.stack

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "code"}
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, data):
        self.plain += data


class TelegramUITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "ui.db")
        self.store.apply_snapshot([{"id": p, "username": "akun_satu", "name": "Akun", "type": p, "isConnected": True, "publishEligible": True} for p in ("tiktok", "facebook", "youtube")], [], (now()-timedelta(days=1)).date().isoformat(), (now()+timedelta(days=366)).date().isoformat())
        self.config = Config(Path(self.temp.name), copy.deepcopy(Config.load().data))
        self.config.runtime.mkdir(exist_ok=True)
        self.config.data["workflow"]["min_free_disk_gb"] = 0
        self.config.data["telegram"].update(allowed_user_ids=[1], public_ingest=True)
        self.pipeline = Mock()
        self.pipeline.fresh_sync.return_value = True
        self.bot = Telegram(self.config, self.store, self.pipeline, client=Mock())
        self.bot.send_ui = AsyncMock(return_value={"message_id": 51})
        self.bot.call = AsyncMock(return_value=True)

    def enqueue(self, count=1, message=1):
        result = self.store.enqueue(77, 77, message, [(f"https://instagram.com/reel/test{message}_{n}/", f"test{message}_{n}") for n in range(count)], progress=True)
        return result["batch_id"]

    async def test_one_message_edits_across_stages_and_restart(self):
        bid = self.enqueue()
        await self.bot.update_progress(bid)
        self.assertEqual(self.bot.send_ui.call_args.args[0], "sendMessage")
        jid = self.store.progress_snapshot(bid)[1][0]["id"]
        self.store.checkpoint(jid, "downloaded", leased=1)
        await self.bot.update_progress(bid)
        self.assertEqual(self.bot.send_ui.call_args.args[0], "editMessageText")
        self.assertEqual(self.bot.send_ui.call_args.kwargs["message_id"], 51)
        self.assertIn("Caption &amp; hashtag", self.bot.send_ui.call_args.kwargs["text"])
        new_bot = Telegram(self.config, Store(self.store.path), self.pipeline, client=Mock())
        new_bot.send_ui = AsyncMock()
        await new_bot.update_progress(bid)
        new_bot.send_ui.assert_not_awaited()
        self.store.checkpoint(jid, "published", payload={"post_url": "https://www.facebook.com/reel/123/", "published_username": "Akun"})
        await new_bot.update_progress(bid)
        self.assertEqual(new_bot.send_ui.call_args.args[0], "editMessageText")
        self.assertNotIn("Batch selesai", new_bot.send_ui.call_args.kwargs["text"])
        for remaining in self.store.progress_snapshot(bid)[1][1:]:
            self.store.checkpoint(remaining["id"], "published")
        await new_bot.update_progress(bid)
        self.assertIn("Batch selesai", new_bot.send_ui.call_args.kwargs["text"])
        self.store.batch_reports()
        self.assertFalse(self.store.notifications())

    async def test_replayed_input_keeps_one_card_and_duplicate_receipt(self):
        msg = {"update_id": 1, "message": {"message_id": 1, "from": {"id": 77}, "chat": {"id": 77, "type": "private"},
               "text": "https://instagram.com/reel/a/ https://instagram.com/reel/a/ https://wrong.example"}}
        await self.bot.process(msg)
        await self.bot.process(msg)
        self.assertEqual(len(self.store.progress_due()), 1)
        self.assertFalse(self.store.notifications())
        await self.bot.flush_ui()
        text = self.bot.send_ui.call_args.kwargs["text"]
        self.assertIn("1 duplikat", text)
        self.assertIn("1 link tidak valid", text)
        self.assertEqual(self.store.summary()["reserved_or_published"], 3)
        self.assertIn("1 Reel · 3 kiriman", text)

    async def test_callback_owner_admin_scope_and_pagination(self):
        bid = self.enqueue(20)
        await self.bot.update_progress(bid)
        query = {"id": "q1", "from": {"id": 77}, "message": {"chat": {"id": 77}, "message_id": 51}, "data": f"batch:{bid}:2"}
        await self.bot.callback(query)
        await self.bot.flush_ui()
        self.assertIn("halaman 2/15", self.bot.send_ui.call_args.kwargs["text"])
        count = self.bot.send_ui.await_count
        for change in ({"from": {"id": 99}}, {"message": {"chat": {"id": 99}, "message_id": 51}},
                       {"message": {"chat": {"id": 77}, "message_id": 50}}, {"data": f"batch:{bid}:99"}):
            await self.bot.callback({**query, **change})
            self.assertEqual(self.bot.send_ui.await_count, count)
        await self.bot.callback({**query, "from": {"id": 1}, "data": f"batch:{bid}:0"})
        self.assertEqual(self.store.progress_snapshot(bid)[0]["page"], 0)

    async def test_deleted_card_recovers_and_not_modified_is_success(self):
        bid = self.enqueue()
        await self.bot.update_progress(bid)
        jid = self.store.progress_snapshot(bid)[1][0]["id"]
        self.store.checkpoint(jid, "downloaded")
        self.bot.send_ui.side_effect = [TelegramError(400, "message to edit not found"), {"message_id": 52}]
        await self.bot.update_progress(bid)
        self.assertEqual(self.store.progress_snapshot(bid)[0]["progress_message_id"], 52)
        self.store.checkpoint(jid, "analyzed")
        self.bot.send_ui.side_effect = TelegramError(400, "message is not modified")
        await self.bot.update_progress(bid)
        self.bot.send_ui.reset_mock()
        await self.bot.update_progress(bid)
        self.bot.send_ui.assert_not_awaited()

    async def test_rate_limit_defers_without_marking_progress_delivered(self):
        bid = self.enqueue()
        self.bot.send_ui.side_effect = TelegramError(429, "Too Many Requests", 90)
        await self.bot.update_progress(bid)
        self.assertIsNone(self.store.progress_snapshot(bid)[0]["progress_message_id"])
        self.assertFalse(self.store.progress_due())
        self.assertEqual(self.store.summary()["statuses"], {"queued": 3})

    async def test_outbox_html_button_and_unreachable_chat(self):
        self.store.notify("first", 77, "<b>Siap</b>", views.html_options())
        self.bot.send_ui.side_effect = TelegramError(403, "bot was blocked by the user")
        await self.bot.flush_ui()
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT sent FROM outbox").fetchone()[0], -1)
        self.assertFalse(self.store.notifications())

    async def test_snapshot_race_and_retry_reactivate_completed_card(self):
        bid = self.enqueue()
        jid = self.store.progress_snapshot(bid)[1][0]["id"]
        self.store.checkpoint(jid, "failed", payload={"resume_status": "downloaded"})
        observed = iso()
        self.store.retry(jid)
        self.store.progress_saved(bid, 1, "stale", True, observed)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT done FROM batch_progress").fetchone()[0], 0)
        self.store.checkpoint(jid, "failed", payload={"resume_status": "downloaded"})
        await self.bot.update_progress(bid)
        self.store.retry(jid)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT done FROM batch_progress").fetchone()[0], 0)

    def test_html_safety_and_all_states_with_large_batch(self):
        bid = self.enqueue(100)
        batch, jobs = self.store.progress_snapshot(bid)
        self.assertEqual(len(jobs), 300)
        for state in views.LABELS:
            job = copy.deepcopy(jobs[0])
            job.update(status=state, username='<a href="evil">&' * 20, last_error='<script>&' * 150)
            job["payload"]["generated"] = {"hook": "<b>&" * 30}
            job["payload"]["resume_status"] = "downloaded"
            for admin in (True, False):
                text, options = views.video(job, admin=admin)
                self.assertLess(len(ValidHTML(text).plain.encode("utf-16-le")) // 2, 4096)
                self.assertNotIn("<script>", text)
            for page in (0, 1, 50):
                text, options, done = views.batch({**batch, "page": page}, [job] + jobs[1:])
                self.assertLess(len(ValidHTML(text).plain.encode("utf-16-le")) // 2, 4096)
                for row in options["reply_markup"]["inline_keyboard"]:
                    for button in row:
                        self.assertLessEqual(len(button.get("callback_data", "").encode()), 64)

    def test_pending_and_drive_wait_do_not_claim_published(self):
        bid = self.enqueue()
        batch, jobs = self.store.progress_snapshot(bid)
        jobs[0].update(status="scheduled", payload={"post_url": "https://www.facebook.com/reel/123/"})
        text, options, done = views.batch(batch, jobs)
        self.assertFalse(done)
        self.assertNotIn("✓ Publikasi", text)
        self.assertFalse(any("facebook.com" in b.get("url", "") for row in options["reply_markup"]["inline_keyboard"] for b in row))
        jobs[0].update(status="rendered", last_error="Menunggu sinkronisasi Bank konten ke Google Drive.")
        text, _, _ = views.batch(batch, jobs)
        self.assertIn("Menunggu salinan Google Drive", text)
        self.assertNotIn("konfigurasi", text)

    def test_empty_duplicate_batch_and_paused_state(self):
        self.enqueue()
        result = self.store.enqueue(77, 77, 2, [("https://instagram.com/reel/test1_0/", "test1_0")], progress=True)
        text, _, done = views.batch(*self.store.progress_snapshot(result["batch_id"]))
        self.assertIn("Tidak ada video baru", text)
        self.assertIn("1 duplikat", text)
        self.assertTrue(done)
        self.assertIn("Belum ada video", views.status({"date": "2026-10-14", "accounts": 0, "reserved_or_published": 0, "daily_capacity": 0, "statuses": {}, "paused": False})[0])
        self.assertIn("Antrean dijeda", views.batch(*self.store.progress_snapshot(self.enqueue(message=3)), paused=True)[0])

    def test_old_outbox_migration_preserves_messages(self):
        path = Path(self.temp.name) / "old.db"
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE outbox(id INTEGER PRIMARY KEY,dedupe_key TEXT UNIQUE,chat_id INTEGER,text TEXT,sent INTEGER DEFAULT 0,created_at TEXT)")
            c.execute("INSERT INTO outbox VALUES(1,'old',77,'pesan lama',0,'today')")
        c.close()
        store = Store(path)
        self.assertEqual(store.notifications()[0]["options"], "{}")
        self.assertEqual(store.notifications()[0]["text"], "pesan lama")
        Store(path)

    async def test_actual_telegram_error_contract(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 17}}))
        async with httpx.AsyncClient(transport=transport, base_url="https://example.test") as client:
            bot = Telegram(self.config, self.store, self.pipeline, client=client)
            with self.assertRaises(TelegramError) as caught:
                await bot.call("sendMessage", text="test")
            self.assertEqual(caught.exception.retry_after, 17)

    async def test_expired_callback_does_not_block_later_input(self):
        bid=self.enqueue()
        await self.bot.update_progress(bid)
        self.bot.call.side_effect=TelegramError(400,'query is too old')
        await self.bot.process_update({'update_id':9,'callback_query':{'id':'expired','from':{'id':77},
            'message':{'chat':{'id':77},'message_id':51},'data':f'batch:{bid}:0'}})
        self.assertEqual(self.store.get('telegram_offset'),10)
        await self.bot.process_update({'update_id':10,'message':{'message_id':10,'from':{'id':77},
            'chat':{'id':77,'type':'private'},'text':'https://www.instagram.com/reel/next/'}})
        self.assertEqual(self.store.get('telegram_offset'),11)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM batches').fetchone()[0],2)

    async def test_bad_outbox_does_not_block_other_chat(self):
        self.store.notify('bad',1,'bad')
        self.store.notify('good',2,'good')
        self.bot.send_ui.side_effect=[TelegramError(400,'chat not found'),{'message_id':4}]
        await self.bot.flush_ui()
        with self.store.connect() as c:
            states=[r[0] for r in c.execute('SELECT sent FROM outbox ORDER BY id')]
        self.assertEqual(states,[-1,1])

    async def test_transient_outbox_failure_defers_persistently_and_continues(self):
        self.store.notify('slow',1,'slow')
        self.store.notify('good',2,'good')
        self.bot.send_ui.side_effect=[TimeoutError(),{'message_id':4}]
        await self.bot.flush_ui()
        self.bot.send_ui.reset_mock()
        self.bot.send_ui.side_effect=None
        await self.bot.flush_ui()
        self.bot.send_ui.assert_not_awaited()
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM telegram_delivery_retry').fetchone()[0],1)

    async def test_backlog_budget_does_not_silently_accept(self):
        self.config.data['workflow']['max_pending_assets']=0
        await self.bot.process_update({'update_id':12,'message':{'message_id':12,'from':{'id':77},
            'chat':{'id':77,'type':'private'},'text':'https://www.instagram.com/reel/no_space/'}})
        self.assertFalse(self.store.queue())
        self.assertIn('Link belum diterima',self.store.notifications()[0]['text'])

    async def test_callback_does_not_wait_for_outbox_lock(self):
        bid=self.enqueue()
        await self.bot.update_progress(bid)
        async with self.bot.ui_lock:
            await asyncio.wait_for(self.bot.callback({'id':'q','from':{'id':77},
                'message':{'chat':{'id':77},'message_id':51},'data':f'batch:{bid}:1'}),timeout=.2)
        self.assertEqual(self.store.progress_snapshot(bid)[0]['page'],1)

    def test_page_changed_during_send_stays_due(self):
        bid=self.enqueue()
        for job in self.store.progress_snapshot(bid)[1]:
            self.store.checkpoint(job['id'],'published')
        observed=iso()
        self.store.progress_page(bid,1)
        self.store.progress_saved(bid,51,'page-zero',True,observed,page=0)
        self.assertIn(bid,self.store.progress_due())
        self.assertEqual(self.store.progress_snapshot(bid)[0]['page'],1)
