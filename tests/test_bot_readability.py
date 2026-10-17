import unittest

import test_telegram_ui as ui
from test_telegram_ui import ValidHTML
from fahra_automation import telegram_views as views
from fahra_automation.journal import AttemptJournal
from fahra_automation.telegram import TelegramError


class ReadableBotTest(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TelegramUITest.setUp
    enqueue = ui.TelegramUITest.enqueue

    def query(self, data, user=1, chat=1):
        return {'id': 'readable', 'from': {'id': user}, 'message': {'chat': {'id': chat}, 'message_id': 51}, 'data': data}

    async def test_read_only_navigation_reaches_every_account(self):
        account = self.store.accounts()[0]
        items = [dict(account, id=str(i), username=f'account_{i:02d}',name=f'account_{i:02d}') for i in range(64)]
        seen = []
        for page in range(8):
            text, options = views.accounts(items, page)
            seen.extend(name for name in [a['username'] for a in items] if name in text)
            self.assertIn(f'halaman {page + 1}/8', text)
            self.check_message(text, options)
        self.assertEqual(seen, [a['username'] for a in items])
        before = self.store.summary()
        for kind in ('accounts', 'status', 'help', 'queue'):
            await self.bot.callback(self.query(f'panel:{kind}:0'))
            self.assertEqual(self.bot.send_ui.call_args.args[0], 'editMessageText')
        self.assertEqual(before, self.store.summary())

    async def test_private_admin_required_for_every_panel_and_details(self):
        job = self.store.progress_snapshot(self.enqueue())[1][0]
        for data in ['panel:status:0', 'panel:accounts:1', 'panel:queue:0', 'panel:help:0', f"video:{job['id']}:0", f"video:{job['id']}:1"]:
            for user, chat in ((77,77),(1,-20),(1,77),(77,1)):
                await self.bot.callback(self.query(data,user,chat))
        self.bot.send_ui.assert_not_awaited()

    async def test_error_details_are_explicit_and_escaped(self):
        job = self.store.progress_snapshot(self.enqueue())[1][0]
        self.store.checkpoint(job['id'], 'failed', last_error='Unsupported get request <script>internal-stack</script>')
        await self.bot.callback(self.query(f"video:{job['id']}:0"))
        text = self.bot.send_ui.call_args.kwargs['text']
        self.assertIn('izin akun', text)
        self.assertNotIn('internal-stack', text)
        await self.bot.callback(self.query(f"video:{job['id']}:1"))
        text = self.bot.send_ui.call_args.kwargs['text']
        self.assertIn('&lt;script&gt;internal-stack', text)
        ValidHTML(text)

    async def test_queue_pages_do_not_drop_jobs_and_clamp_stale_page(self):
        self.enqueue(5)
        rows = []
        for page in range(3):
            text, options = self.bot.panel('queue', page)
            self.check_message(text, options)
            rows.extend(b['callback_data'].split(':')[1] for row in options['reply_markup']['inline_keyboard']
                        for b in row if b['callback_data'].startswith('video:'))
        self.assertEqual(len(rows),15)
        self.assertEqual(len(set(rows)),15)
        self.assertIn('halaman 3/3',self.bot.panel('queue',999)[0])

    async def test_expired_ack_does_not_undo_saved_page(self):
        batch = self.enqueue(3)
        await self.bot.update_progress(batch)
        async def ack(*args, **kwargs):
            self.assertEqual(self.store.progress_snapshot(batch)[0]['page'], 2)
            raise TimeoutError()
        self.bot.call.side_effect = ack
        await self.bot.callback(self.query(f'batch:{batch}:2',77,77))
        self.assertEqual(self.store.progress_snapshot(batch)[0]['page'],2)

    async def test_panel_not_modified_is_normal_and_no_duplicate_message(self):
        self.bot.send_ui.side_effect = TelegramError(400,'message is not modified')
        await self.bot.callback(self.query('panel:status:0'))
        self.assertEqual(self.bot.send_ui.await_count,1)
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE kind='telegram_panel_failed'").fetchone()[0],0)

    async def test_retry_cannot_bypass_unknown_journal(self):
        job = self.store.progress_snapshot(self.enqueue())[1][0]
        journal = AttemptJournal(self.config)
        journal.begin(job['id'], {'accountId':'tiktok'})
        journal.mark(job['id'],'unknown')
        self.store.checkpoint(job['id'],'failed')
        await self.bot.process({'update_id':3,'message':{'message_id':3,'from':{'id':1},
            'chat':{'id':1,'type':'private'},'text':f"/ulang {job['id']}"}})
        self.assertEqual(self.store.job(job['id'])['status'],'failed')
        self.assertIn('belum aman', self.store.notifications()[0]['text'])

    def check_message(self, text, options):
        self.assertLess(len(ValidHTML(text).plain.encode('utf-16-le')) // 2,4096)
        for row in options['reply_markup']['inline_keyboard']:
            self.assertLessEqual(len(row),2)
            for button in row:
                self.assertLessEqual(len(button.get('callback_data','').encode()),64)

    def test_long_unicode_states_remain_readable_and_safe(self):
        batch, jobs = self.store.progress_snapshot(self.enqueue(100))
        for job in jobs:
            job.update(username='<>&😀' * 100, last_error='TimeoutError '+ '😀' * 3000)
            job['payload']['generated'] = {'hook':'<>&😀' * 100}
        for state in views.LABELS:
            jobs[0]['status'] = state
            for page in (0,1,75):
                text, options, _ = views.batch(dict(batch,page=page),jobs)
                self.check_message(text,options)
            for technical in (False,True):
                self.check_message(*views.video(jobs[0],admin=True,technical=technical))

    def test_shared_wait_is_stated_once_and_scheduled_never_looks_published(self):
        batch, jobs = self.store.progress_snapshot(self.enqueue())
        for job in jobs:
            job['last_error'] = 'Snapshot akun belum lengkap.'
        text, _, _ = views.batch(batch,jobs)
        self.assertEqual(text.count('Data akun sedang diperbarui'),1)
        jobs[0].update(status='scheduled',last_error='old error')
        self.assertEqual(views.label(jobs[0]),'Terjadwal')
        self.assertIn('menunggu konfirmasi',views.note(jobs[0]))
        jobs[0]['status'] = 'submit_unknown'
        self.assertIn('tidak terkirim dua kali',views.note(jobs[0]))

    def test_status_distinguishes_sources_from_deliveries_and_stopped(self):
        batch = self.enqueue(3)
        jobs = self.store.progress_snapshot(batch)[1]
        for job in jobs[:3]:
            self.store.checkpoint(job['id'],'published')
        for job in jobs[3:6]:
            self.store.checkpoint(job['id'],'cancelled')
        self.store.checkpoint(jobs[6]['id'],'failed')
        summary = self.store.summary()
        group = summary['source_groups'][0]
        self.assertEqual((group['complete'],group['attention'],group['processing'],group['stopped']),(1,1,0,1))
        self.store.begin_sync()
        text, options = views.status(self.store.summary())
        self.assertIn('Pengiriman baru menunggu data lengkap',text)
        self.assertIn('Histori lama',views.status(dict(summary,source_groups=[dict(group,origin='legacy')]))[0])
        self.check_message(text, options)

    def test_stale_account_pool_does_not_claim_ready(self):
        text, _ = views.accounts(self.store.accounts(),fresh=False)
        self.assertNotIn('Terhubung',text)
        self.assertIn('Menunggu data lengkap',text)

    def test_page_name_is_readable_without_changing_identity(self):
        with self.store.tx() as c:
            c.execute("UPDATE accounts SET username='Writer',name='Halaman Contoh' WHERE platform='facebook'")
        self.enqueue()
        text, _ = views.accounts(self.store.accounts())
        self.assertIn('Halaman Contoh',text)
        self.assertNotIn('Writer',text)
        fb = next(j for j in self.store.queue() if j['platform']=='facebook')
        self.assertEqual(fb['username'],'Halaman Contoh')
        self.assertEqual(self.store.account_username('facebook',identity=True),'Writer')

    def test_health_alert_keeps_raw_error_in_log_only(self):
        for _ in range(3):
            self.store.health('reply_poll','error',reason='Permission denied internal-stack')
        message = self.store.notifications()[0]['text']
        self.assertIn('Pemeriksaan komentar perlu diperiksa',message)
        self.assertNotIn('internal-stack',message)
        with self.store.connect() as c:
            self.assertIn('internal-stack',c.execute("SELECT reason FROM health WHERE component='reply_poll'").fetchone()[0])
