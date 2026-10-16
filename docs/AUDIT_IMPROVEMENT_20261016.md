# Audit perbaikan Fahra Automation setelah aktivasi

Audit 16 Oktober 2026 atas commit `5eeccfbbfbe0fc27e4447a00539210724efe4f47`. Prioritas berikutnya adalah menutup celah validasi sebelum pengiriman, kesiapan akun, privasi Telegram, dan pemulihan delivery gagal. Fondasi satu asset dengan tiga delivery, jurnal intent, deduplikasi, serta pemeriksaan Drive sudah tersedia; membangun ulang pipeline tidak diperlukan.

Pengguna meminta audit dan plan terlebih dahulu. Dokumen ini tidak mengaktifkan implementasi perbaikannya. Urutan kerja dan kriteria lulus ada di [rencana perbaikan](IMPROVEMENT_PLAN.md). [Audit sebelumnya](AUDIT.md) merupakan arsip sebelum paket baru diimplementasikan.

## Kondisi yang diperiksa

Pembacaan SQLite dilakukan dengan `mode=ro`, `query_only=ON`, dan transaksi baca, tanpa membuka `Store` produksi. CLI `status` tidak dipakai karena jalur inisialisasinya juga menulis schema/settings. Tidak ada polling Telegram kedua, request publikasi/reply, perubahan konfigurasi, restart, atau pemulihan job produksi oleh audit ini.

| Cakupan | Hasil dan batas bukti |
| --- | --- |
| Layanan pada 15:14 WIB | Worker baru PID 26788 berjalan; kedua LaunchAgent lama unloaded. `paused=false`, reply aktif, pilot gate kosong, snapshot lengkap 64 akun |
| Akun | 46 TikTok, 14 Facebook, 4 YouTube ditandai connected dan eligible dalam snapshot lokal. Status ini belum membuktikan izin setiap operasi platform |
| Histori seluruh delivery | 133 published, 33 failed, 1 scheduled. Angka ini mencampur histori migrasi dan paket baru; bukan tingkat keberhasilan paket baru |
| Tiga sumber paket baru pada 15:17 WIB | Sembilan delivery: 4 published, 4 failed, 1 scheduled. Pilot pertama lengkap tiga platform. Sumber kedua: TikTok published, Facebook failed, YouTube scheduled. Sumber ketiga gagal validasi media pada ketiga tujuan |
| Reply | 115 komentar needs_review dan 24 sent_unverified. Tidak ada reply attempt dengan `started_at` sesudah cutoff cutover 12:02 WIB pada query audit. Status sent historis tidak menjadi bukti reply baru |
| Kesehatan satu jam pengamatan | 11 event Telegram ReadTimeout; 44 event profile wait pada 14 posting; 144 event sent_unverified hanya mencakup 24 komentar. Angka event tidak boleh dibaca sebagai jumlah aksi kirim |
| Penyimpanan | Ruang bebas sekitar 826 GiB; DB sekitar 4,6 MB. Belum ada bukti tekanan disk saat snapshot |
| Verifier baseline | Full PASS, 334 tes offline tanpa skip, nol temuan verifier. Lima skenario probe tambahan mengungkap tiga kelompok celah di bawah |

Status published di tabel merupakan hasil rekonsiliasi yang tersimpan di DB. Audit ini tidak membuka ulang seluruh halaman publik. Bukti pilot publik terdahulu tetap tercatat di [penerimaan](ACCEPTANCE.md).

Bukti lokal ada di `runtime/verification/audit-improvement-20261016/`: `runtime-inventory.json`, `runtime-detail.json`, `runtime-classification.json`, reproduksi fixture, dan laporan verifier. Query runtime bertanggal 15:14 sampai 15:17 WIB; layanan tetap memproses antrean secara mandiri selama audit.

## Temuan berprioritas

P1 perlu didahulukan karena memengaruhi ketepatan pengiriman atau akses informasi. P2 memperbaiki keandalan operasi. P3 meningkatkan pengukuran hasil dan kemudahan pengembangan. Tidak ditemukan bukti insiden P0 pada cakupan audit ini.

### A01 P1 Validasi publikasi tidak diulang setelah menunggu limiter

Pada [pipeline.py](../fahra_automation/pipeline.py), `Pipeline.step` menyelesaikan preflight dan membuat intent sebelum memanggil `Repliz.create`. [repliz.py](../fahra_automation/repliz.py), `request`, kemudian menunggu lock/limiter. Callback `create` hanya memeriksa apakah waktu jadwal sudah lewat. Generation snapshot, koneksi akun, dan pilot source tidak diperiksa ulang di batas transport tersebut.

Reproduksi memakai `httpx.MockTransport` dan fixture terpisah: invalidasi snapshot, disconnect akun, atau perubahan pilot gate ketika limiter menunggu masing-masing masih menghasilkan satu mock POST dan status scheduled. Tidak ada POST nyata. Tes yang ada mencakup perubahan saat preflight akun/persona/Drive, tetapi belum menutup jeda di limiter publikasi.

Perbaikan: pemeriksaan terakhir dalam callback tepat sebelum transport, dengan pengelolaan intent yang membedakan belum dikirim dari hasil tidak pasti. Pertahankan binding, jurnal, dan kuota ketika transport mungkin sudah terjadi. Semantik `/jeda` saat ini membiarkan pekerjaan berjalan selesai; perubahan semantik pause harus dinyatakan tersendiri.

### A02 P1 Kesiapan akun terlalu bergantung pada status connected

[db.py](../fahra_automation/db.py), `_sync_accounts`, memakai `a.get('publishEligible', True)` dan menerima nama/username kosong. `Repliz.check_account` hanya menguji identitas record, jenis platform, serta koneksi. Tidak ada rekaman capability per operasi beserta umur bukti dan alasan penahanan.

Probe snapshot berisi hanya `id`, `type=facebook`, dan `isConnected=true` menghasilkan `eligible=1` meski username kosong dan tidak ada bukti capability. Di produksi, satu delivery Facebook baru memiliki schedule ID lalu gagal dengan pesan objek tidak ditemukan/tidak dapat diakses/izin tidak sesuai. Pesan itu memiliki beberapa kemungkinan penyebab; audit ini belum memastikan penyebabnya adalah izin.

Perbaikan: pisahkan connected, identitas platform terverifikasi, siap posting, dan siap komentar. Akun dengan bukti belum lengkap menunggu pemeriksaan otomatis. Error izin/objek yang pasti harus menahan pemilihan baru pada akun terkait sampai pemeriksaan ulang, tanpa mengalihkan delivery yang sudah punya intent. Kontrak resmi [Get One Account Repliz](https://docs.repliz.com/api/account/get-one-account) memberikan detail akun dan koneksi; jangan menganggap `publishEligible` selalu disediakan provider.

### A03 P1 Rincian batch dapat terlihat oleh anggota grup

[telegram.py](../fahra_automation/telegram.py), `process`, menerima link publik dari grup dan menyimpan `message.chat.id`. `update_progress` mengirim/mengedit kartu ke chat yang sama. Callback memeriksa pengirim/admin, tetapi ketika pengguna yang berhak membuka rincian, pesan grup yang sama berubah menjadi halaman rincian. Notifikasi hasil pada [db.py](../fahra_automation/db.py), `notify_publication`, juga memakai chat batch.

Probe grup sintetis menerima satu asset, lalu callback sah dari pengirim mengubah pesan grup menjadi `Rincian kiriman`. Seluruh panggilan Telegram dimock. Database produksi memiliki nol batch dengan chat ID negatif saat audit, sehingga belum ada bukti kebocoran aktual melalui grup.

Perbaikan: pisahkan chat asal dan tujuan laporan privat. Input tetap publik; kiriman dari grup mendapat tanda terima umum, sedangkan rincian dan hasil menuju DM pengirim yang sudah terhubung. Jika DM belum dapat dihubungi, simpan laporan dan berikan alur membuka bot, tanpa memasang allowlist global.

### A04 P1 Delivery gagal dengan jadwal remote belum punya alur pemulihan lengkap

[db.py](../fahra_automation/db.py), `retry`, dengan benar menolak job yang memiliki schedule ID atau submission. Namun `pending_publications` hanya memilih submitting/submit_unknown/scheduled. Setelah [pipeline.py](../fahra_automation/pipeline.py), `monitor`, menyimpan remote error menjadi failed, polling status delivery itu berhenti. Sync global masih membaca jadwal/kuotanya melalui `_record_remote`, tetapi tidak mengubah failed menjadi published jika remote belakangan pulih.

Dampaknya, kegagalan Facebook baru dan timeout upload TikTok legacy perlu jalur rekonsiliasi operator yang jelas. Mengosongkan schedule ID lalu mengulang bukan pemulihan aman.

Perbaikan: antrean kasus failed-with-remote, pemeriksaan ulang status dan identitas, klasifikasi error, serta tindakan yang terikat pada jadwal yang sama. Provider mendokumentasikan [Retry Schedule](https://docs.repliz.com/api/schedule/retry-schedule) melalui PUT pada ID jadwal, tier Premium+. Ketersediaan paket dan perilaku saat timeout harus dibuktikan sebelum fitur retry dipakai; endpoint ini tidak menjadi alasan mengulang hasil ambigu.

### A05 P2 Media ditolak setelah biaya analisis sudah dikeluarkan

[pipeline.py](../fahra_automation/pipeline.py), `prepare`, melakukan probe setelah download, tetapi `validate_shared` baru dipanggil oleh [media.py](../fahra_automation/media.py), `render`. Pada [analysis.py](../fahra_automation/analysis.py), transkripsi dan generasi AI sudah berjalan sebelumnya.

Snapshot nyata menunjukkan video 480×854 berdurasi sekitar 36,2 detik, dengan dua respons generasi dalam caption review, lalu tiga delivery failed karena minimum 540×960. Penolakan sesuai kebijakan sekarang; urutannya membuang komputasi dan membuat satu masalah sumber terlihat sebagai tiga kegagalan edit.

Perbaikan: validasi durasi, dimensi, orientasi dan kebutuhan adaptasi segera setelah probe, sebelum ekstraksi frame/transkripsi/AI. Satu alasan pada asset diturunkan ke semua delivery. Kebijakan untuk resolusi rendah tetap perlu pilihan eksplisit; jangan diam-diam mengubah crop, durasi, mirror, atau standar kualitas.

### A06 P2 Health belum menjelaskan apakah tiap loop berhasil bekerja

`Store.summary` melaporkan antrean dan snapshot; `ReplyStore.summary` memiliki waktu poll/worker. Tidak ada satu laporan kesehatan yang memuat waktu sukses Telegram intake/outbox, publikasi, backup, profil, Drive, backlog tertua, dan alasan degraded. Pada `ReplyWorker.profile_loop`, error global kembali kosong ketika `profile_once` selesai meskipun ada kegagalan per posting. `ReplyStore.update` mencatat event bernama status bahkan ketika hanya memeriksa ulang status yang sama.

Akibatnya, heartbeat yang baru dan error global kosong dapat bersamaan dengan pekerjaan yang tertahan. Sebelas ReadTimeout Telegram perlu diukur terhadap waktu keberhasilan polling; audit tidak membuktikan pesan hilang. Sebanyak 144 event sent_unverified adalah pemeriksaan berulang atas 24 reply lama, bukan 144 reply baru.

Perbaikan: health per loop, waktu sukses terakhir, latensi, usia antrean, klasifikasi wait/error, jumlah target unik, dan alert admin yang dideduplikasi. Alarm harus dipicu oleh kemacetan atau kegagalan berulang, bukan setiap sync normal.

### A07 P2 Paket pemulihan belum mencakup seluruh state secara otomatis

Loop backup pada [cli.py](../fahra_automation/cli.py) hanya memanggil `Store.backup`; fungsi itu menyimpan SQLite dan mempertahankan 30 salinan. [journal.py](../fahra_automation/journal.py) menyimpan intent di lokasi internal terpisah. Ownership dan manifest migrasi juga terpisah. Dokumentasi operasi sudah memperingatkan bahwa backup DB saja tidak cukup, tetapi belum tersedia paket restore terpadu dan latihan pemulihan operasional lengkap.

Perbaikan: manifest backup ber-checksum yang mencakup DB, batas jurnal konsisten, ownership, konfigurasi nonrahasia dan referensi media. Backup credential tetap memakai mekanisme privat tersendiri. Restore menolak bukti jurnal yang hilang, berjalan shadow, dan merekonsiliasi hasil remote sebelum hak kirim. Latihan awal memakai fixture; DB aktif tidak disalin ke worktree.

### A08 P2 Admission belum membatasi beban per pengirim dan tiap tahap

[telegram.py](../fahra_automation/telegram.py), `admission_error`, membatasi batch, backlog global, dan ruang disk saat intake. Jalur download/render tidak memeriksa ulang ambang disk sebelum setiap tahap. Belum ada fairness per pengirim, budget penggunaan AI, atau pencatatan token/biaya. [repliz.py](../fahra_automation/repliz.py), `sync`, mengambil rentang jadwal sekitar setahun setiap putaran; request memakai satu lock bersama sampai respons selesai.

Belum ada bukti overload aktual; ruang disk sekarang masih longgar. Namun input publik dapat memenuhi backlog melalui beberapa batch, dan satu request lambat dapat memperpanjang antrean limiter bersama.

Perbaikan: ukur durasi setiap tahap/request dan antrean limiter, beri fairness pengirim, periksa disk sebelum pekerjaan besar, serta hitung budget AI yang benar-benar tersedia dari respons provider. Uji 20/100/1.000 sumber dengan fixture sebelum mengubah konkurensi atau cakupan snapshot.

### A09 P2 Penyelesaian kasus reply dan bukti lintas platform masih terbatas

Snapshot memperlihatkan 115 needs_review dan 24 sent_unverified; tidak ada attempt kirim baru sesudah cutover pada query audit. [replies.py](../fahra_automation/replies.py), `fresh`, menahan semua komentar anak, sehingga pembacaan konteks induk belum menjadi fitur balasan nested yang aktif. Penahanan ini aman, tetapi klaim umum konteks thread harus mengikuti kemampuan yang benar-benar tersedia.

Perbaikan: ringkasan review berdasarkan alasan/umur/platform, tindakan resolve yang menjaga draft/cutoff/jurnal, rekonsiliasi reply lama tanpa kirim ulang, dan pengamatan komentar baru yang memenuhi syarat. Buktikan reply/like/moderasi per platform melalui hasil nyata yang sah. Fitur balasan anak baru dibuka jika identitas induk dan capability sudah teruji; YouTube like tetap tidak masuk lingkup.

### A10 P3 Hasil konten dan biaya belum dipakai untuk evaluasi

Kode mencatat status delivery, persona dan review AI, tetapi belum memiliki penyimpanan statistik performa posting atau evaluasi hook/caption berdasarkan hasil. Rotasi tetap mengutamakan pemerataan beban. Itu sesuai rancangan awal, tetapi belum menjawab konten mana yang berhasil menarik penonton.

Perbaikan: pengambilan metrik terjadwal, perbandingan pada umur posting yang sama, serta evaluasi berdasarkan topik/akun/versi hook. [Get Content Statistic Repliz](https://docs.repliz.com/api/content/get-statistic) menyediakan field berbeda per platform dan membutuhkan tier Gold+; akses aktual belum diuji. Jangan menjanjikan retention atau watch time jika field itu tidak tersedia.

Jika monetisasi menjadi sasaran, evaluasi nilai editorial perlu menjadi pekerjaan tersendiri. Kebijakan [monetisasi YouTube](https://support.google.com/youtube/answer/1311392?hl=en) membedakan reused content dari copyright dan menilai nilai tambah yang berarti. Kesimpulan audit: zoom, hook singkat dan kredit sumber yang diterapkan sekarang tidak cukup menjadi bukti kelayakan monetisasi. Ini bukan temuan pelanggaran pada akun pengguna. Rencana tidak menambah avatar atau mengganti audio asli.

### A11 P3 Kontrak konfigurasi dan dokumentasi perlu dirapikan

Schema memakai versi 1 dengan beberapa ALTER/CREATE saat objek diinisialisasi. `Config.load` memvalidasi sebagian field, sedangkan `worker_loop` memuat ulang config tanpa membangun ulang semua klien/Store. [OPERATIONS.md](OPERATIONS.md) sudah meminta restart setelah perubahan config, tetapi jalur reload parsial tetap menyulitkan diagnosis. `pyproject.toml` menyatakan Python >=3.11, sementara bukti operasi terpusat pada 3.14.

Perbaikan: schema migration eksplisit, kontrak config bertipe, daftar field yang perlu restart, dan status yang benar-benar read-only. Uji versi Python minimum atau persempit dukungan berdasarkan bukti. Pisahkan halaman status terkini dari arsip checkpoint agar pembaca tidak mengira fase awal implementasi masih menunggu.

## Yang perlu dipertahankan

Deduplikasi URL/hash, tiga delivery mandiri, larangan iq7fanspage, limit lokal 20/WIB, preservation tujuan legacy, jurnal sebelum mutasi, single ownership, kredit sumber terverifikasi, checksum Drive penuh, hook Barlow tepat [0,3), orientasi asli, dan cleanup seluruh tujuan +24 jam tetap menjadi invariant. Full PASS tidak menutup celah tambahan yang baru direproduksi; probe tambahan juga tidak membatalkan bukti pilot yang sudah ada.

Audit copy antislop PASS untuk cakupan dokumen: angka memiliki snapshot/sumber, reproduksi fixture dibedakan dari insiden produksi, usulan tidak dinyatakan sudah terpasang, serta tidak ada klaim performa/keamanan/monetisasi tanpa bukti. Pemeriksaan visual UI tidak dilakukan pada tugas backend dan operasi ini.
