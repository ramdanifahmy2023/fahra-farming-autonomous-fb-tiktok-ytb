from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from datetime import timedelta
import time

import httpx

from .util import extract_urls, iso, now, safe_error
from . import telegram_views as views

HELP = (
    "<b>Reel masuk, progres terlihat</b>\n\n"
    "Kirim link Reel Instagram di chat ini. Untuk batch, letakkan satu link per baris.\n\n"
    "Bot mengunduh, membuat caption dan hashtag, mengedit video sekali, lalu menyimpan ke Drive. "
    "Setiap Reel dikirim ke satu akun TikTok, satu Facebook Page, dan satu channel YouTube secara bergiliran, "
    "maksimal 20 video per akun per hari.\n\n"
    "Progres tiga tujuan tampil terpisah dalam satu pesan. Link posting dikirim setelah terbit. "
    "Jika slot salah satu platform habis, kirimannya menunggu hari berikutnya."
)
ADMIN_HELP = ("\n\n<b>Pantau pekerjaan</b>\n/status · kuota dan pekerjaan\n/antrean · 20 pekerjaan terdekat\n"
              "/akun · akun tujuan\n/video ID · detail video\n\n<b>Kelola antrean</b>\n/jeda · jeda pekerjaan baru\n"
              "/lanjut · lanjutkan\n/ulang ID · coba lagi video gagal\n/batal ID · batalkan sebelum dikirim\n\n"
              "Gunakan minimal enam karakter ID video.")


class TelegramError(RuntimeError):
    def __init__(self, code, description, retry_after=0):
        super().__init__(f"Telegram {code}: {safe_error(description)}")
        self.code, self.retry_after = code, retry_after


class Telegram:
    def __init__(self, config, store, pipeline, client=None):
        self.config, self.store, self.pipeline = config, store, pipeline
        self.client = client or httpx.AsyncClient(base_url="https://api.telegram.org/bot" + config.telegram_token() + "/", timeout=40)
        self.reports = None
        self.ui_lock = asyncio.Lock()
        self.cooldown = 0
        self.chat_sent_at = {}
        with store.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS telegram_delivery_retry(outbox_id INTEGER PRIMARY KEY,next_attempt TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 1)")

    async def call(self, method, **data):
        r = await self.client.post(method, json=data)
        try:
            result = r.json()
        except ValueError:
            raise TelegramError(r.status_code, 'Respons Telegram bukan JSON.') from None
        if not result.get("ok"):
            code = result.get("error_code", r.status_code)
            if code == 409:
                raise RuntimeError("Polling Telegram konflik dengan proses lain. Hentikan proses yang memakai token sama.")
            retry_after = result.get("parameters", {}).get("retry_after", 0)
            if code == 429:
                self.cooldown = time.monotonic() + max(1, retry_after)
            raise TelegramError(code, result.get("description", "request failed"), retry_after)
        return result["result"]

    def authorized(self, user_id):
        return user_id in set(self.config.section("telegram")["allowed_user_ids"] + self.store.get("allowed_users", []))

    async def process(self, update):
        if update.get("callback_query"):
            await self.callback(update["callback_query"])
            return
        message = update.get("message")
        if not message or not message.get("from"):
            return
        text = message.get("text", "")
        user_id, chat_id = message["from"]["id"], message["chat"]["id"]
        private = message['chat'].get('type') == 'private'
        if private:
            self.store.register_private_chat(user_id, chat_id)
        report_chat = chat_id if private else user_id
        prefix = "update:" + str(update["update_id"])
        command, _, argument = text.strip().partition(" ")
        command = command.split("@")[0].lower()
        if not private and command.startswith('/'):
            self.store.notify(prefix + ':private', chat_id,
                              'Jawaban perintah dikirim lewat chat pribadi. Buka bot dan tekan Mulai jika belum terhubung.')
        if command == "/pair" and message["chat"]["type"] == "private":
            paired = self.store.pair(argument.strip(), user_id, chat_id)
            self.store.notify(prefix, chat_id, "Terhubung.\n\n" + HELP + ADMIN_HELP if paired else "Kode pairing salah atau kedaluwarsa. Buat kode baru dari CLI proyek.", views.html_options())
            return
        admin = self.authorized(user_id)
        public = self.config.section("telegram").get("public_ingest", False)
        if not admin and not public:
            if command == "/start":
                self.store.notify(prefix, chat_id, "Bot ini memerlukan pairing. Jalankan perintah pair-code dari proyek lalu kirim /pair KODE melalui chat pribadi.")
            return
        if command in {"/start", "/help", "/bantuan"}:
            self.store.notify(prefix, report_chat, HELP + (ADMIN_HELP if admin else ""), views.html_options())
            return
        if not admin and command.startswith("/"):
            self.store.notify(prefix, report_chat, "Bot menerima kiriman link Reel Instagram. Hasil batch dikirim lewat chat pribadi. Perintah kendali hanya untuk admin.")
            return
        options = None
        if command == "/status":
            answer, options = views.status(self.store.summary())
        elif command == "/antrean":
            rows = self.store.queue()
            answer = "<b>Antrean kiriman</b>\n\n" + ("\n\n".join(f"<code>{j['id'][:8]}</code> · {views.platform_name(j)} · {views.e(views.label(j))}\n{views.e(j['username'] or 'menunggu alokasi', 40)} · {views.day(j['slot_date'])}" for j in rows) or "Belum ada video dalam antrean. Kirim link Reel untuk memulai.")
            options = views.html_options()
        elif command == "/akun":
            accounts = self.store.accounts()
            answer = "\n".join(f"{i+1}. {views.platform_name(a)} · {(a['name'] or a['username'])[:50]} ({'aktif' if a['connected'] else 'terputus'})" for i, a in enumerate(accounts[:35])) or "Akun belum tersinkron."
            if len(accounts) > 35:
                answer += f"\n+{len(accounts)-35} akun lainnya. Daftar lengkap tersedia lewat CLI accounts."
        elif command in {"/jeda", "/lanjut"}:
            self.store.set("paused", command == "/jeda")
            self.store.record("operator_pause_changed", user_id=user_id, paused=command == "/jeda")
            answer = "Antrean dijeda. Pekerjaan yang sedang berjalan dan jadwal yang sudah dikirim tetap dilanjutkan." if command == "/jeda" else "Antrean dilanjutkan."
        elif command in {"/video", "/ulang", "/batal"}:
            job = self.store.find_job(argument.strip())
            if not job:
                answer = "ID tidak ditemukan atau belum unik. Gunakan minimal enam karakter."
            elif command == "/video":
                job["username"] = self.store.account_username(job["account_id"])
                answer, options = views.video(job, admin=True)
            elif command == "/ulang":
                answer = "Pekerjaan masuk antrean ulang." if self.store.retry(job["id"]) else "Pekerjaan tidak dapat diulang otomatis. Jadwal Repliz yang sudah dibuat perlu dicocokkan terlebih dahulu."
            else:
                answer = "Pekerjaan dibatalkan dan slot dilepas." if self.store.cancel(job["id"]) else "Pekerjaan sedang diproses atau sudah dikirim; tidak dapat dibatalkan dari antrean lokal."
        else:
            urls, invalid = extract_urls(text)
            if not urls:
                answer = "Kirim link langsung Reel Instagram. Gunakan /bantuan untuk melihat perintah."
            elif len(urls) > self.config.section("workflow")["max_batch_links"]:
                answer = f"Maksimal {self.config.section('workflow')['max_batch_links']} link per pesan. Pisahkan menjadi beberapa pesan."
            elif self.admission_error(len(urls)):
                answer = self.admission_error(len(urls))
            else:
                result = self.store.enqueue(chat_id, user_id, message["message_id"], urls, can_allocate=bool(self.pipeline.fresh_sync()),
                                            progress=True, invalid=len(invalid))
                if not private:
                    self.store.notify(prefix + ':receipt', chat_id,
                        'Link sudah dicatat. Progres dan hasil dikirim lewat chat pribadi. Buka bot dan tekan Mulai jika belum terhubung.')
                if self.store.has_progress(result["batch_id"]):
                    return
                if result["replayed"]:
                    answer = f"Batch {result['batch_id'][:8]} sudah tercatat."
                else:
                    answer = (f"Batch {result['batch_id'][:8]} diterima.\nVideo baru: {result['accepted']}\n"
                              f"Tujuan kiriman: {result['accepted'] * 3}\nDuplikat: {result['duplicates']}\nLink tidak valid: {len(invalid)}\nRotasi TikTok, Facebook, dan YouTube melanjutkan giliran masing-masing.")
                    answer += "\nSlot WIB: " + ", ".join(f"{day}: {count}" for day, count in self.store.batch_slots(result["batch_id"]).items())
                    if not self.pipeline.fresh_sync():
                        answer += "\nAlokasi akun menunggu sinkronisasi Repliz."
        self.store.notify(prefix, report_chat, answer, options)

    def admission_error(self, count):
        settings = self.config.section('workflow')
        with self.store.connect() as connection:
            pending = connection.execute("SELECT count(DISTINCT asset_id) FROM jobs WHERE status NOT IN ('published','failed','cancelled','duplicate')").fetchone()[0]
        if pending + count > settings.get('max_pending_assets', 1000):
            return 'Antrean sedang penuh. Link belum diterima; kirim lagi setelah antrean berkurang.'
        if shutil.disk_usage(self.config.runtime).free < settings.get('min_free_disk_gb', 10) * 1024**3:
            return 'Ruang kerja hampir penuh. Link belum diterima; admin perlu memeriksa penyimpanan.'
        return ''

    async def answer_callback(self, **data):
        try:
            return await asyncio.wait_for(self.call('answerCallbackQuery', **data), timeout=4)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.record('telegram_callback_ack_failed', error=safe_error(exc))

    async def callback(self, query):
        message = query.get("message", {})
        match = re.fullmatch(r"batch:([0-9a-f]{32}):(\d{1,3})", query.get("data", ""))
        snapshot = self.store.progress_snapshot(match[1]) if match else None
        if not snapshot:
            await self.answer_callback( callback_query_id=query["id"], text="Batch tidak ditemukan.")
            return
        batch, jobs = snapshot
        user = query.get("from", {}).get("id")
        if (message.get("chat", {}).get("id") != batch["report_chat_id"] or batch["report_chat_id"] <= 0
                or message.get("message_id") != batch["progress_message_id"]
                or (user != batch["user_id"] and not self.authorized(user))):
            await self.answer_callback( callback_query_id=query["id"], text="Rincian ini hanya untuk pengirim batch dan admin.", show_alert=True)
            return
        page = int(match[2])
        if page > (len(jobs) + 3) // 4:
            await self.answer_callback( callback_query_id=query["id"], text="Halaman tidak tersedia.")
            return
        await self.answer_callback( callback_query_id=query["id"])
        self.store.progress_page(batch['id'], page)

    async def send_ui(self, method, chat_id, *, before_send=None, **data):
        delay = max(self.cooldown, self.chat_sent_at.get(chat_id, 0) + 1.1) - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if before_send is not None:
            before_send()
        result = await self.call(method, chat_id=chat_id, link_preview_options={"is_disabled": True}, **data)
        self.chat_sent_at[chat_id] = time.monotonic()
        return result

    async def update_progress(self, batch_id):
        observed = iso()
        snapshot = self.store.progress_snapshot(batch_id)
        if not snapshot:
            return
        batch, jobs = snapshot
        chat_id = batch['report_chat_id']
        if chat_id <= 0:
            self.store.progress_defer(batch_id, disabled=True)
            return
        text, options, done = views.batch(batch, jobs, self.store.get("paused", False))
        signature = hashlib.sha256(json.dumps([text, options], sort_keys=True).encode()).hexdigest()
        message_id = batch["progress_message_id"]
        try:
            if signature != batch["signature"] or not message_id:
                if message_id:
                    try:
                        await self.send_ui("editMessageText", chat_id, message_id=message_id, text=text, **options)
                    except TelegramError as exc:
                        if exc.code == 400 and "message is not modified" in str(exc).lower():
                            pass
                        elif exc.code == 400 and any(s in str(exc).lower() for s in ("message to edit not found", "message can't be edited")):
                            message_id = None
                        else:
                            raise
                if not message_id:
                    thread = {"reply_parameters": {"message_id": batch["message_id"], "allow_sending_without_reply": True}} if chat_id == batch['chat_id'] else {}
                    result = await self.send_ui("sendMessage", chat_id, text=text, **thread, **options)
                    message_id = result["message_id"]
                    self.store.record("telegram_progress_created", batch_id=batch_id, message_id=message_id)
            self.store.progress_saved(batch_id, message_id, signature, done, observed, page=batch["page"])
        except Exception as exc:
            self.store.progress_defer(batch_id, max(30, getattr(exc, "retry_after", 0)), disabled=getattr(exc, "code", None) == 403)
            self.store.record("telegram_progress_failed", batch_id=batch_id, error=safe_error(exc))

    async def process_update(self, update):
        try:
            await self.process(update)
        except TelegramError as exc:
            if exc.code not in {400, 403, 404}:
                raise
            self.store.record('telegram_update_rejected', update_id=update['update_id'], error=safe_error(exc))
        self.store.set('telegram_offset', update['update_id'] + 1)

    async def polling_loop(self):
        webhook = await self.call("getWebhookInfo")
        if webhook.get("url"):
            raise RuntimeError("Bot memiliki webhook aktif. Konfigurasi deployment harus diselesaikan sebelum polling.")
        while True:
            try:
                updates = await self.call("getUpdates", offset=self.store.get("telegram_offset", 0), timeout=25, allowed_updates=["message", "callback_query"])
                for update in updates:
                    await self.process_update(update)
                self.store.health('telegram_poll', 'ok')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.record("telegram_poll_failed", error=safe_error(exc))
                self.store.health('telegram_poll', 'error', reason=safe_error(exc))
                if "konflik" in str(exc):
                    raise
                await asyncio.sleep(5)

    async def outbox_loop(self):
        self.store.track_active_batches()
        while True:
            await self.flush_ui()
            self.store.health('telegram_outbox', 'ok')
            await asyncio.sleep(1)

    async def flush_ui(self):
        async with self.ui_lock:
            with self.store.connect() as connection:
                items = [dict(row) for row in connection.execute("""SELECT o.* FROM outbox o
                    LEFT JOIN telegram_delivery_retry r ON r.outbox_id=o.id
                    WHERE o.sent=0 AND (r.next_attempt IS NULL OR r.next_attempt<=?) ORDER BY o.id LIMIT 10""", (iso(),))]
            for item in items:
                try:
                    options = json.loads(item['options'])
                    await self.send_ui('sendMessage', item['chat_id'], text=item['text'] if options else item['text'][:4000], **options)
                    self.store.mark_notified(item['id'])
                except Exception as exc:
                    self.store.record('telegram_send_failed', outbox_id=item['id'], error=safe_error(exc))
                    if getattr(exc, 'code', None) in {400, 403, 404}:
                        self.store.mark_notified(item['id'], delivered=False)
                        recoverable = getattr(exc, 'code', None) == 403 or 'chat not found' in str(exc).lower()
                        self.store.record('telegram_notification_unreachable', outbox_id=item['id'], recoverable=recoverable)
                        continue
                    with self.store.tx() as connection:
                        connection.execute("""INSERT INTO telegram_delivery_retry VALUES(?,?,1)
                            ON CONFLICT(outbox_id) DO UPDATE SET next_attempt=excluded.next_attempt,attempts=attempts+1""",
                            (item['id'], iso(now() + timedelta(seconds=max(30, getattr(exc, 'retry_after', 0))))))
                    if getattr(exc, 'code', None) == 429:
                        break
            if self.reports is not None:
                try:
                    await self.reports.flush_one()
                except Exception as exc:
                    from .account_reports import error_label
                    self.store.set('account_report_delivery_health', {'ok': False, 'at': iso(), 'error': error_label(exc)})
            for batch_id in self.store.progress_due():
                await self.update_progress(batch_id)
