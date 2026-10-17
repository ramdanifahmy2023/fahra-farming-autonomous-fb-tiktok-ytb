# Menjalankan dan merawat paket

Semua perintah dari root proyek memakai `.venv/bin/python -m fahra_automation`. `config.example.toml` berisi default aman; penyesuaian mesin pada config.local.toml yang diabaikan Git. Jangan mencetak credential. Baca status/worklog terbaru sebelum mengubah layanan.

## Urutan normal

Mulai versi perbaikan audit 18 Oktober, gunakan Python 3.14. Perubahan config file baru berlaku setelah restart semua klien layanan terkait. Schema v1 dimigrasikan ke v2 pada pembukaan tulis; status/health/inspect memakai SQLite read-only dan tidak mengubah schema.

1. `init`, lalu `doctor`. Doctor membaca bot/Repliz/AI/Drive/MCP tanpa membuat posting. `sync` memperbarui snapshot akun dan jadwal lengkap secara atomik.
2. `preview URL --sync-drive` membuktikan download, AI, render, dan checksum Drive di runtime/previews. Preview tidak memanggil create schedule. Frame, transkrip, review caption, dan report.json menjadi bukti.
3. Jalankan verifier full dan CI pada commit tugas. Media/AI/Drive membutuhkan preview selain fixture.
4. Setelah migrasi final dan rekonsiliasi, `migrate activate` membuat ownership. Aktifkan mode live, publish_enabled, cleanup.enabled pada config.local.toml. Buka pause setelah pilot siap; resume reply lewat CLI setelah pilot terverifikasi. Sakelar reply disimpan di SQLite melalui `reply pause|resume`, bukan pada konfigurasi.
5. `service install`, `service start`. Jangan menjalankan `run` terpisah saat LaunchAgent hidup. Service baru mengambil lock kedua workflow lama dan menolak mutasi jika salah satu LaunchAgent lama dimuat kembali.

`service status|stop|restart`, `status`, `accounts`, `events --limit 50`, `reply status|pause|resume`, `backup`, `cleanup --status` tersedia. `cleanup` tanpa opsi hanya rencana; `cleanup --apply` memeriksa ulang bukti sebelum menghapus file yang memenuhi syarat.

Untuk restart terencana, pause reply dahulu, buat backup, lalu stop/deploy/start layanan. Periksa snapshot akun sudah lengkap setelah startup sebelum `reply resume`. Worker juga menahan proses ketika snapshot sedang diperbarui atau generation berubah, tanpa menghabiskan failure budget draft. Jangan mengubah status unknown menjadi queued/drafted untuk melewati penantian.

## Pemeriksaan dan pemulihan setelah audit

- `health`: heartbeat tiap loop, usia heartbeat, kegagalan beruntun, sumber/kiriman baru versus legacy, hold, kesiapan akun, token AI yang dilaporkan provider, durasi tahap dan limiter. Unknown berarti belum ada bukti dari versi ini. Stale/error perlu diperiksa meskipun PID hidup. Tiga kegagalan berturut-turut membuat satu alert admin per rangkaian kegagalan.
- `delivery inspect ID` dan `reply inspect ID`: status, hasil provider yang tersimpan, jurnal, serta tindakan yang tersedia. `reconcile` atau `resolve` mencocokkan bukti dengan GET; tidak memaksa sukses dan tidak membuat kiriman/balasan baru. Jadwal error diperiksa berkala pada ID yang sama. Hasil yang tetap ambigu tetap ditahan.
- `delivery retry-local ID`: hanya kegagalan lokal tanpa remote schedule/intent dan tanpa asset bersama yang sedang dikerjakan. Checkpoint valid dipakai kembali. Sumber yang sudah dinyatakan gagal downloader ditahan untuk pemeriksaan sumber; tidak otomatis memulai download yang sama berulang.
- Input grup tetap publik; detail masuk DM pengirim. Pengirim membuka bot/Mulai untuk mengaktifkan laporan DM yang tertahan. Binding message ID dari grup dibuang saat migrasi, pesan detail lama yang masih pending dikarantina dengan sent=-2. Tidak menghapus pesan grup yang sudah terlanjur terkirim pada versi sebelumnya.
- Readiness publish memerlukan identitas, koneksi dan bukti capability eksplisit atau histori sukses cocok dalam tujuh hari. Respons tanpa capability tidak dianggap true. Penolakan 401/403 menahan publish akun itu sampai bukti baru tersedia; readiness reply dibuktikan sendiri melalui profile dan preflight komentar. Jangan mengatasi hold dengan mengubah eligible langsung.

`backup` menghasilkan folder bundle dengan database konsisten, seluruh jurnal publikasi/reply/laporan, bukti ownership dan konfigurasi nonrahasia. Credential tidak ikut. Setiap file memiliki ukuran/hash dalam manifest. Jurnal reply legacy yang memang tidak pernah diimpor dicatat sebagai legacy_reply_gaps; jangan menganggapnya terverifikasi. Jurnal intent baru yang hilang membuat pemeriksaan backup gagal.

`restore FOLDER_BACKUP FOLDER_BARU` menolak folder tujuan yang sudah ada serta checksum/bukti yang tidak cocok. Hasil restore selalu paused, reply/report nonaktif, snapshot stale, dan tanpa ownership aktif. File config.safe.json adalah catatan konfigurasi, bukan aktivasi. Setelah menyiapkan checkout/config shadow, cocokkan intent dengan provider dan lengkapi bukti legacy; restore_requires_reconciliation harus tetap true sampai audit rekonsiliasi operator selesai. Jangan memindahkan ownership lama untuk melewati pemeriksaan.

Sampling metrik konten terpisah dari laporan akun TikTok harian. Default `metrics.enabled=false`; aktifkan setelah pengamatan operasi stabil minimal 48 jam dan kemampuan GET statistik terbukti. Collector dibatasi lima kandidat per lima menit, pada 24/72/168 jam sejak published terverifikasi, dengan jendela satu jam. Nilai hilang tetap null; jendela terlewat disimpan missed_window. Data tidak memicu repost atau perubahan persona. Kontrak GET dan tingkat aksesnya mengikuti [dokumentasi statistik konten Repliz](https://docs.repliz.com/api/content/get-statistic.html).

## Migrasi dan aktivasi

`migrate snapshot` membuat snapshot konsisten kedua sumber ke runtime/migrations/rehearsal-… . `migrate import FOLDER --staging` mengimpor ke runtime/migration-staging/workflow.sqlite3. `migrate reconcile --staging` menguji snapshot remote untuk rehearsal; ready tetap false karena belum snapshot final. Jangan mengimpor rehearsal ke database produksi yang akan menerima input.

Sesudah rehearsal/preview lulus, hentikan dua LaunchAgent lama dan buktikan lock bebas. `migrate snapshot --final` mengambil snapshot baru; `migrate import FOLDER` mengisi database baru yang kosong. Import idempotent untuk manifest sama dan menolak target berisi pekerjaan/migrasi berbeda. `migrate reconcile` mengambil snapshot remote lengkap, menjaga slot lama/unknown, memeriksa journal, lalu mencatat kelayakan ownership. `migrate activate` menolak jika tahap tersebut belum siap atau layanan lama masih loaded.

Importer mempertahankan ID job/schedule/post, payload/path media, status/quota, tujuan asli, cursor, event, konteks dan dedupe reply. Asset sumber sama dari dua arsip memakai namespace terpisah sehingga hasil render lama tidak saling menimpa. Mengirim ulang URL lama tidak menambah tujuan baru. Sumber tidak diubah. Offset Telegram lama tidak disalin. Batch lama memiliki chat netral; outbox lama ditahan pada snapshot dengan jumlah tercatat, bukan dikirim menggunakan bot baru ke penerima yang belum terpetakan.

Aktivasi pilot memakai satu Reel baru pilihan pengguna. Pastikan sumber belum ada pada jobs/assets legacy sebelum cutover. Set SQLite pilot_source_id ke sumber baru tersebut; gate ini membatasi POST publikasi ke satu sumber dengan satu akun eligible per platform. Pertahankan reply_enabled=false selama pilot karena gate sumber tidak membatasi worker reply. Hapus gate dan aktifkan reply hanya sesudah bukti pilot. Simpan tiga URL terbit, ID jadwal/post, checksum Drive, notifikasi, serta status automasi yang dibaca ulang. Posting YouTube dengan spesifikasi Shorts tetap perlu bukti hasil publik; jadwal diterima saja belum cukup.

## Penjadwalan dan ruang kerja

Default 08:00 sampai sebelum 23:00 WIB, 20 slot tersebar dengan offset akun stabil. Snapshot remote mencakup jadwal eksternal dan perpindahan tanggal. Slot lokal yang sudah memiliki remote ID dihitung sekali. Saat quota/HTTP429, akun ditunda dan worker menghormati retry; jumlah 20 adalah batas lokal, bukan jaminan provider.

Input publik maksimum100 link/pesan, backlog1000 asset, minimal10GiB ruang bebas. Download/render dibatasi horizon24jam. Pengirim mendapat alasan bila link belum diterima; admin dapat melihat detail error. Progres menampilkan tahap bersama dan status masing-masing tujuan, dengan tombol tautan hasil publik. Callback kedaluwarsa atau satu chat tidak terjangkau tidak menghalangi kiriman chat lain.

## Persona dan komentar

Registry persona dibuat otomatis per akun terhubung, setiap perubahan menyimpan versi immutable. Caption delivery menyimpan versi persona/model dan evidence; hook bersama tidak berubah per tujuan. Reply menggunakan konteks enam frame/transkrip, identitas posting, komentar, dan komentar induk. Induk belum ditemukan, fakta lemah, atau balasan sudah ada masuk hold/review. Kuota reply20/akun/hari terpisah dari video.

Worker lokal menjadi pemilik reply pada posting yang dikelola. Profil native per posting mempertahankan seluruh konfigurasi efektif, mematikan native reply untuk mencegah balasan ganda, mengaktifkan like hanya TT/FB, serta moderasi keyword. Worker profil merekonsiliasi timeout lewat GET sebelum retry. Aksi yang statusnya tidak pasti tidak dikirim ulang. Konfigurasi tersimpan bukan bukti komentar nyata telah dibalas/ditangani.

## Restore dan rollback

Hentikan service sebelum mengganti DB. Simpan DB melalui `backup` dan seluruh attempt journal, ownership, serta manifest migrasi di penyimpanan internal. Journal dibuat sebelum intent SQLite/POST dan difsync; response remote dicatat sebelum status DB. Saat restore, startup memulihkan intent menjadi submit_unknown atau schedule ID diterima. Attempt tanpa job menahan semua mutasi sampai mapping dipulihkan. Journal reply/download/profil juga mencegah pengulangan setelah DB tertinggal.

Jangan menghapus journal, ID remote, counts_quota, atau status unknown agar job bisa dikirim lagi. Reconcile dahulu. Jangan menghidupkan bot lama dengan snapshot pra-cutover setelah bot baru mengirim; delta schedule/reply dan kuota harus dipetakan balik sebelum ownership dipindahkan. Jika rollback belum dapat dibuktikan aman, tahan kedua pengirim dan biarkan jadwal remote yang sudah ada dipantau.

Cleanup hanya file kerja yang memiliki bukti semua tujuan wajib published, waktu publikasi terakhir+24jam, checksum Bank/Drive lengkap, enam frame, manifest MCP completed, dan tidak ada referensi lain. Ketiga workspace termasuk preview/arsip diperiksa. Arsip legacy di luar batas runtime baru tetap dilindungi; jangan menghapusnya manual untuk merapikan.

Uji yang dimasukkan dari sesi operator memakai batch chat_id=0/user_id=0 dan message_id negatif unik, dengan event operator_pilot_enqueued berisi actor, authorization, dan intake_channel=operator_cli. Jangan mengarang update Telegram. Batch operator tetap chat0 selama aktif agar tidak membentuk reply-thread ke message ID negatif. Setelah getChat admin berhasil, laporan dapat dikirim eksplisit tanpa thread; kiriman Telegram asli tetap memakai ID update/message asli.

## Laporan TikTok harian

Fitur memakai worker, limiter Repliz, dan outbox Telegram yang sama. Tidak ada bot, polling getUpdates, atau LaunchAgent tambahan. Sakelar laporan terpisah dari `/jeda` dan reply; default config contoh tetap disabled. Jadwal tetap 07.50 pengambilan, 08.00 kirim Asia/Jakarta, catch-up hari berjalan sebelum 12.00. Mac/volume/jaringan harus tersedia. Setelah 12.00 hari yang belum selesai ditandai terlewat.

```sh
.venv/bin/python -m fahra_automation report preview --credentials-from '/Volumes/DATA KERJA /Project Dev/Fahra-automation-tiktok-fb-ytb-downloader/Credential'
.venv/bin/python -m fahra_automation report status
.venv/bin/python -m fahra_automation report enable
.venv/bin/python -m fahra_automation report pilot
.venv/bin/python -m fahra_automation report disable
```

Preview membuat DB, jurnal, evidence.json, dan telegram.html sendiri di runtime/report-previews; hanya GET Repliz, tanpa token/pengiriman Telegram atau perubahan pool/kuota publikasi. `--credentials-from` hanya untuk preview. `enable` memverifikasi ownership produksi serta identitas bot/private admin melalui getMe/getChat; tujuan tetap admin 265732518. `pilot` meminta worker yang sedang berjalan mengumpulkan data baru dan mengirim satu laporan uji per tanggal. CLI tidak menjalankan worker/getUpdates kedua. Pilot tidak dipakai sebagai baseline snapshot pagi.

Tabel account_report_runs, account_metric_snapshots, dan account_report_parts menyimpan tanggal WIB, semua akun dalam lingkup, data numerik, status, hash, dan message_id. Progress hanya selisih ID yang sama pada D−1/D−7; hari yang hilang tidak diinterpolasi. Metrik harian provider selalu diberi tanggal terpisah. Filter follower >600 dan perlindungan iq7fanspage diterapkan sebelum GET statistik. Rename/reconnect mempertahankan histori ID.

`report status` menunjukkan health startup/collector/delivery dan status per bagian. Startup Repliz yang gagal di-retry tiap 30 detik; bot dan laporan tetap hidup, sedangkan worker publikasi/reply/cleanup baru dimulai sesudah snapshot lengkap, rekonsiliasi jurnal, dan guard ownership lulus. Rekonsiliasi/recovery disk startup berjalan pada thread terpisah agar tidak menahan timer/outbox. Snapshot publikasi diinvalidasi sebelum loop Telegram dimulai. Gagal startup tidak membuka guard publikasi.

Jurnal laporan berada di subfolder `account-reports` dalam attempt journal eksternal. Backup/restore harus mempertahankan jurnal tersebut bersama DB. Manifest isi dibekukan dan difsync sebelum pengiriman; intent transport dicatat setelah throttle dan guard terakhir. `sent` berarti Telegram mengembalikan message_id, bukan bukti dibaca. `send_unknown` berarti Telegram mungkin menerima pesan; jangan menghapus jurnal atau mengembalikannya ke queued. Tanpa message_id, Bot API tidak menyediakan pencarian riwayat pesan untuk membuktikan kirim. Periksa chat secara manual jika akses tersedia. Error 400/403/404 menjadi failed; 429 diulang sesuai retry_after. Bagian lain dan notifikasi pipeline tetap diproses.
