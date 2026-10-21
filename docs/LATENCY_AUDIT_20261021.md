# Audit waktu worker, 21 Oktober 2026

Status: **audit dan rancangan**, belum implementasi optimasi produksi. Base kode `1f7e811`. Pemeriksaan memakai SQLite produksi read-only, event historis, empat harness offline dengan database buatan, dan dokumentasi resmi provider. Arker, worker produksi, akun, kuota, jadwal, credential, serta media produksi tidak diubah.

Kesimpulan: antrean satu consumer, pekerjaan pemeriksaan berulang, timer penantian, dan kebijakan slot saling menambah waktu. Menambah consumer saja belum menyelesaikan pembagian API, keterbatasan YouTube, dan penjadwalan. Besarnya penghematan total masih perlu dibuktikan setelah implementasi; angka speedup fixture tidak boleh dipakai sebagai janji produksi.

## Waktu yang benar-benar tercatat

Kohort: 68 sumber nonlegacy yang diterima 20 Oktober, pukul 00.00 sampai sebelum 24.00 Asia/Jakarta. Semua masuk sebelum deployment generator baru pada malam 20 Oktober. Lima belas asset gagal; statistik tahap berikutnya hanya mencakup asset yang mencapai tahap itu.

| Pengukuran | Sampel | Median | P90 |
| --- | ---: | ---: | ---: |
| Diterima sampai intent unduhan MCP | 68 | 36 menit 29 detik | 62 menit 31 detik |
| Intent unduhan sampai downloaded | 54 | 15,32 detik | 27,07 detik |
| Downloaded sampai rendered | 53 | 45,32 detik | 206,28 detik |
| Rendered sampai Drive siap | 53 | 1,49 menit | 3,29 menit |
| Diterima sampai Drive siap | 53 | 43,08 menit | 72,83 menit |
| Diterima sampai TikTok terdeteksi published | 49 | 68,74 menit | 102,67 menit |
| Diterima sampai Facebook terdeteksi published | 48 | 90,50 menit | 129,30 menit |

Median antarbaris tidak boleh dijumlahkan: sampel dan titik tengahnya berbeda. Waktu status memakai event pertama `status_changed`, bukan `jobs.updated_at` yang dapat berubah saat sync. Intent MCP belum mengukur jeda pesan Telegram sebelum masuk database. Selisih checkpoint dapat memuat antrean/retry, sedangkan published adalah waktu worker mendeteksi hasil, bukan waktu persis video muncul di platform.

Contoh sumber terakhir: diterima 17.18.05 WIB; intent download 18.12.22; downloaded 18.12.36; analyzed 18.13.05; rendered 18.13.16; Drive siap 18.14.42. TikTok dijadwalkan 18.23.13 dan terdeteksi published 18.26.13; Facebook dijadwalkan 18.52.13 dan terdeteksi published 18.54.22. Ada event penahanan snapshot sebelum unduhan; bukti ini tidak memisahkan seluruh 54 menit tunggu menjadi persentase antrean dan sync.

## Temuan dan tindakan yang diusulkan

### L01. Pemeriksaan jadwal lama berebut API dengan pekerjaan baru

Snapshot audit menemukan 219 kandidat monitor: 165 failed dengan ID remote, 52 scheduled, dan dua unknown. Failed mencakup 25 TikTok, 18 Facebook, dan 122 YouTube, sebagian dibuat sejak 14 Oktober. Mereka tetap dipilih oleh `Store.pending_publications()`. Monitor memasang `next_poll` 90 detik sebelum GET; jika respons memastikan status error, jedanya diganti menjadi 15 menit. Jika GET gagal dengan 404/exception, jeda 90 detik tetap berlaku. Limiter bersama hanya menyediakan dua request bersamaan.

Ini berarti 75,3% **kandidat** monitor adalah kegagalan lama; bukan bukti 75,3% seluruh GET benar-benar menuju kegagalan. Angka 219 mencakup seluruh baris dengan status yang memenuhi syarat; selector mengambil maksimal sepuluh yang sudah jatuh tempo per putaran. Observasi perubahan `next_poll` disimpan terpisah sebagai batas bawah jumlah job yang diperiksa, bukan penghitung raw GET. Pada 20 Oktober ada 265 event pemeriksaan yang mendapat 404. Tidak ada alasan untuk menganggap 404 sebagai izin mengirim ulang.

Usulan: bedakan hasil unknown yang mendesak, jadwal mendekati tayang, jadwal jauh, dan failed lama. Failed berulang/404 memakai jeda yang membesar serta jalur review; jadwal jauh diperiksa mendekati waktunya. Semua tetap bisa direkonsiliasi pada ID yang sama. Berikan prioritas API untuk sync dan pemeriksaan sebelum pengiriman, dengan jatah minimum bagi reply agar tidak kelaparan.

Bukti kode: `db.py:903`, `pipeline.py:263`, `repliz.py:73`. Ukur jumlah GET per komponen, umur hasil unknown, dan waktu jadwal sampai published terdeteksi.

### L02. Kuota lokal YouTube penuh sebagian karena kegagalan provider

Pemakaian tersimpan untuk 20 Oktober adalah 80 slot: **37 published dan 43 failed yang masih menghitung kuota**. Ada 16 failed lain dengan kuota nol. Dari 43 slot yang ditahan, 34 memiliki pesan batas unggahan dan sembilan memiliki timeout koneksi. Angka ini berasal dari snapshot pada 21 Oktober, bukan salinan keadaan persis saat alokasi.

Pesan batas unggahan cocok dengan kategori `uploadLimitExceeded` pada [dokumentasi YouTube](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert#errors). Ini tidak membuktikan batas numerik tiap akun, waktu reset, atau bahwa semua kegagalan sudah aman dicoba lagi. Batas lokal 20 bukan jaminan provider mengizinkan 20 unggahan.

Usulan: klasifikasikan kegagalan hasil remote; tahan alokasi baru untuk akun yang terbukti dibatasi dan jadwalkan pemeriksaan ulang. Kendala akun ditampilkan berbeda dari keterlambatan worker. Slot failed/unknown jangan dilepas atau dikirim ulang otomatis tanpa bukti hasil remote dan aturan kuota. Perubahan ini terutama mengurangi pekerjaan sia-sia dan kegagalan tambahan, bukan menaikkan kapasitas YouTube.

### L03. Consumer persiapan dan pengiriman masih satu jalur

`Pipeline.worker_loop()` menunggu satu `handle()` selesai sebelum klaim berikutnya. Fixture actual Store/Pipeline menahan download A: dengan satu consumer, persona untuk delivery B yang sudah siap tidak dimulai; dua consumer membuat B maju sebelum A selesai. Lease tetap mencegah unduhan asset A ganda pada kasus normal itu.

Fixture tiga asset/sembilan delivery: satu consumer 5,778 detik, dua consumer 3,801 detik. Kedua run memiliki tiga download, tiga analisis, sembilan persona, dan sembilan pemeriksaan Drive. Rasio 1,52 hanya berlaku untuk delay palsu dan disk fixture ini; CPU render, lock MCP, bandwidth, dan perilaku provider belum dimodelkan.

Usulan: selector tahap terpisah, maksimal dua persiapan dan satu pengirim, tetap di satu layanan/bot. Menyalakan tiga `worker_loop` yang sama belum memberi pembagian tersebut. Uji cancellation, recovery, alokasi bersamaan, satu render per asset, dan satu intent per tujuan sebelum mengaktifkan paralelisme.

### L04. Layanan pulih tetapi pekerjaan masih menunggu timer

Penahanan `ConfigurationError`, termasuk snapshot belum siap, menunda asset 60 detik. Fixture memulihkan snapshot pada detik satu; job tetap tidak dapat diklaim sampai detik 60. Tambahan 59 detik ini terbukti tanpa jaringan. Empat puluh delivery YouTube bertanggal mendatang juga mendahului satu TikTok siap: 1.010 SELECT dan 3,055 detik untuk satu putaran fixture.

Koreksi hipotesis: tujuan bertanggal mendatang **tidak berputar tanpa henti**. Setelah ditunda ke tanggal tujuan, klaim berikutnya mengabaikannya. FFmpeg melalui `local_stage` juga bukan pemblokir seluruh event loop: probe 80 ms masih memberi 16 heartbeat; yang tertahan adalah consumer pemanggil.

Usulan: bangunkan pekerjaan yang menunggu dependency saat snapshot/Drive siap, dengan satu antrean dedupe dan jitter terbatas; filter tanggal/tahap sejak klaim. Filter tanggal pengiriman jangan menahan persiapan asset bersama jika minimal satu tujuannya sudah masuk horizon 24 jam. Timer tetap menjadi fallback, dan guard final tetap dijalankan sebelum intent/pengiriman.

### L05. Sync menutup kesiapan dan pembacaan komentar sering gagal lengkap

`begin_sync()` menginvalidasi freshness sebelum jaringan. Kelompok jadwal per 30 akun dibaca berurutan dengan rentang kemarin sampai sekitar satu tahun ke depan. Snapshot audit terbaru menunjukkan sync sukses sekitar 71 sampai 100 detik. Pada 20 Oktober terdapat 66 timeout sync, 174 kegagalan pagination komentar karena jumlah record berubah, dan 476 kegagalan profil yang menunggu rekonsiliasi hasil sebelumnya. Jumlah event bukan durasi kumulatif atau jumlah POST ulang.

Usulan: dua kelompok sync independen berjalan bersama melalui limiter yang sama, lalu terapkan satu snapshot lengkap. Tetap serial dalam pagination suatu kelompok. Dua kelompok tidak otomatis dua kali cepat ketika limiter/beban API lain mendominasi. Jangan menggunakan snapshot parsial atau sekadar memperpanjang TTL agar pengiriman lolos.

Pada 20 poll sukses terakhir, 21 Oktober 04.30–09.51 WIB, event mencatat **43.829 baris komentar diterima dan hanya 120 insert baru** (0,274%). Dengan limit 100 per halaman, batas bawahnya 468 GET termasuk inventory, belum percobaan gagal. Ini perhitungan dari event sukses, bukan log HTTP lengkap. Snapshot 24 jam tersendiri berisi 204 kegagalan pagination; jendelanya berbeda dari hitungan 174 pada tanggal 20 Oktober WIB.

Fixture memperlihatkan peluang berikut:

| Kasus offline | Perilaku sekarang | Kandidat optimasi | Batas bukti |
| --- | --- | --- | --- |
| Sync 13 request, limiter dominan | 0,1081 detik | 0,1073 detik | Hampir tidak lebih cepat; interval fixture 8 ms |
| Sync 13 request, respons lambat | 0,3661 detik | 0,2059 detik | Maksimal dua request bersama; Store stub, bukan DB produksi |
| Akun sepi satu jam virtual | 60 GET | 26 GET | Penemuan komentar dapat melambat dari 120 menjadi 300 detik |
| Lima profil satu akun, 250 otomasi | 21 GET per sweep | 5 GET | Cache baca hanya dalam satu sweep; konflik tetap ditahan |

Backoff profil **sudah ada**, mulai 300 detik dan membesar sampai maksimal 86.400 detik. Probe membuktikan retry sebelum deadline ditahan, tetapi ketika jatuh tempo pembacaan akun/daftar otomasi identik diulang untuk setiap posting. Cache kandidat harus terikat akun/generation, dibuang setelah mutasi, dan tidak menggantikan guard akhir.

Untuk inbox komentar, [Get Comment Repliz](https://docs.repliz.com/api/comment/get-comment) mendokumentasikan filter status dan akun, tetapi tidak mendokumentasikan cursor atau parameter sejak timestamp. Pisahkan pembacaan inbox dari kontrak snapshot kuota yang ketat; uji filter pending, kelompok akun lebih kecil, dedupe, komentar berubah, dan pengambilan induk resolved. Jangan mengarang dukungan incremental cursor. Penggunaan konfigurasi otomasi bersama per akun juga dapat dibaca sekali per siklus, dengan pemeriksaan ulang sebelum mutasi. Benchmark fixture pada volume yang sama dapat menambah beban disk: health selama audit bukan baseline produksi tanpa beban tambahan.

### L06. Snapshot berubah dapat membuang hasil persona yang sudah selesai

Pada fixture, generation berganti saat AI persona selesai: validasi berikutnya gagal sebelum metadata tersimpan, sehingga retry memanggil AI lagi. Kegagalan Drive setelah checkpoint berbeda: metadata tetap ada dan dipakai ulang. Catatan produksi 20 Oktober menunjukkan tujuh panggilan delivery tambahan atas 186 subject; tidak semua panggilan tambahan dapat dinyatakan terbuang karena repair/alih akun bisa sah.

Usulan: simpan hasil sebagai calon metadata yang terikat akun, platform, versi persona, dan hash bukti; pakai kembali hanya bila binding masih sama. Validasi snapshot terbaru tetap wajib sebelum membuat intent. Jangan menulis ulang caption, draft, atau payload yang sudah dibekukan.

### L07. Database operasional berada pada volume USB exFAT

`diskutil` mengonfirmasi database produksi 27 MB berada pada USB exFAT; volume internal memakai APFS. SQLite menggunakan DELETE journal dan synchronous FULL. Tiap request Repliz melakukan dua transaksi statistik secara sinkron sambil memegang kapasitas limiter; pencatatan ini dapat menahan event loop.

Probe kecil setelah instalasi dependency selesai, 20 transaksi dengan durability yang sama:

| Ukuran fixture | USB exFAT | Temporary internal APFS |
| --- | ---: | ---: |
| Median satu transaksi statistik | 7,323 ms | 0,541 ms |
| 20 request palsu 5 ms, dengan statistik | 0,4608 s | 0,1470 s |
| P90 keterlambatan heartbeat pada request palsu | 20,118 ms | 3,470 ms |

Tanpa observer, 20 request palsu selesai 0,1134 detik. Run awal berbarengan instalasi dependency jauh lebih lambat dan disimpan sebagai contoh beban, bukan baseline. Fixture kecil ini tidak membuktikan pemindahan DB mempercepat seluruh worker 13 kali.

Usulan: gabungkan pencatatan statistik nonkritis dan keluarkan I/O sinkron dari event loop. Evaluasi lokasi DB operasional pada penyimpanan internal melalui perubahan path yang didukung, backup, ownership, restore, dan rollback; bank konten tetap pada lokasi pilihan pengguna. Jangan mengganti synchronous menjadi OFF atau melemahkan jurnal intent. Pemindahan DB bukan perubahan konfigurasi kecil: root runtime saat ini dibatasi di dalam proyek.

### L08. Scheduler melakukan banyak query kecil dan pemindaian berulang

Fixture 64 akun dengan 1.000 delivery menghasilkan 108.724 statement saat enqueue, CPU sekitar 0,832 detik. Untuk 63 akun: 107.388 statement dan CPU 0,781 detik. Ini membuktikan N+1, tetapi tidak menjelaskan antrean 36 menit dengan CPU saja. Waktu wall dapat dipengaruhi commit dan disk.

Stress khusus empat akun YouTube penuh 365 hari: satu alokasi melakukan 4.046 statement, CPU 2,287 detik. Index fixture pada jadwal eksternal menurunkannya menjadi sekitar 38 ms, tanpa mengurangi jumlah statement. Ini kondisi ekstrem, bukan kapasitas produksi saat audit.

Fixture tambahan 40.000 event, 1.000 job, dan 64 akun dengan fallback bukti published dipaksa: snapshot CPU sekitar 287 ms, wall 371 ms. Index event yang sesuai menurunkannya ke CPU 37,5 ms, wall 67,5 ms. Event campuran produksi belum direplikasi.

Usulan: index terarah setelah EXPLAIN, agregasi pemakaian/slot sekali per hari/platform, dan batasi pencarian horizon sesuai kapasitas yang diketahui. Transaksi reservasi tetap atomik. Jangan mengganti database atau menambah Redis hanya berdasarkan jumlah query ini.

### L09. Pemilihan akun belum memilih waktu tayang terdekat

Konfigurasi aktif 20 slot pada 08.00 sampai sebelum 23.00 WIB membentuk jarak 45 menit per akun. Allocator memilih beban minimum lalu cursor, bukan waktu slot paling awal. Setelah jadwal diterima, median tunggu menuju waktu terjadwal pada kohort adalah 17,47 menit untuk TikTok dan 33,92 menit untuk Facebook.

Usulan awal: di antara akun dengan beban minimum, pilih slot paling dekat lalu cursor sebagai pemecah seri. Mode tayang segera adalah pilihan kebijakan terpisah dengan batas per akun, jarak antarkiriman, dan kuota provider tetap. Kuota 20 tidak perlu dinaikkan. Scheduled tetap berarti jadwal diterima, bukan published.

### L10. Verifikasi Drive memang mengulang byte, tetapi punya kontrak keselamatan

Satu asset diverifikasi penuh setelah sinkronisasi bank, kemudian diperiksa penuh lagi sebelum tiap tujuan. Fixture file 2 MiB dengan lima tingkat folder menghasilkan 20 GET direktori dan empat GET file penuh (8 MiB) sampai tiga delivery diverifikasi; satu preflight ulang menambah lima GET direktori dan 2 MiB. File fixture hanya byte berawalan MP4, bukan video nyata. Seluruh SHA-256 diperiksa. Pemeriksaan direktori dan hash bank juga diulang.

Usulan: periksa lebih awal apakah slot masih cukup sebelum memulai preflight mahal, lalu tetap ulangi guard sesudahnya. Pool koneksi, cache pencarian direktori yang tetap memvalidasi identitas, serta paralelisme verifikasi yang dibatasi bandwidth dapat mengurangi overhead. Seluruh checksum sebelum tiap POST tetap sesuai M05. Jangan mengganti verifikasi penuh dengan HEAD, ukuran saja, cache checksum tanpa identitas revisi yang terbukti, atau upload API kedua yang bertentangan dengan penggunaan Drive Desktop.

### L11. Webhook dapat mengurangi polling, dengan fallback yang tetap ada

[Dokumentasi webhook Repliz](https://docs.repliz.com/tutorial/webhook) menyebut hasil schedule sukses/gagal untuk semua platform dan komentar untuk platform yang didukung; komentar YouTube tidak mengirim webhook. Receiver membutuhkan HTTPS dan pemeriksaan token header. Ini kemampuan dokumentasi, belum bukti konfigurasi akun pengguna tersedia/aktif.

Rancangan tahap berikutnya: terima event ke inbox durable, dedupe, cocokkan akun/ID jadwal, lalu verifikasi hasil dengan GET terarah. Polling lambat tetap untuk event hilang, startup, unknown, serta komentar YouTube. Webhook mempercepat deteksi hasil dan membebaskan jatah API; tidak membuat YouTube menerbitkan video lebih cepat atau memberi kuota tambahan. Belum memasang endpoint atau mengubah webhook akun pada audit ini.

## Urutan implementasi yang disarankan

1. Kurangi pemeriksaan failed lama/jadwal jauh dan pemindaian komentar/profil berulang; prioritaskan pekerjaan jatuh tempo dan unknown. Bangunkan dependency yang pulih. Tambahkan klasifikasi penolakan batas unggahan tanpa melepaskan slot secara buta.
2. Pisahkan selector persiapan/pengiriman, dua preparer dan satu sender; simpan calon persona; baca kelompok sync dengan konkurensi terbatas. Uji semua race sebelum mengaktifkan.
3. Tambahkan index yang terbukti berguna dan gabungkan telemetri nonkritis. Evaluasi migrasi penyimpanan DB dengan backup/restore teruji jika hasil pengukuran produksi mendukung.
4. Pilih slot lebih dekat. Jika pengguna ingin tayang segera, tetapkan kebijakannya terpisah dari pemerataan jadwal.
5. Pasang webhook setelah receiver dan rekonsiliasi teruji; pertahankan fallback polling dan kepemilikan tunggal.

## Ukuran keberhasilan dan batas bukti

Sasaran uji awal, belum hasil optimasi: antrean ringan satu sampai tiga link, sumber valid, akun/kuota tersedia, dan Drive sehat menghasilkan video siap dikirim dalam lima menit. Untuk batch 20/100, laporkan throughput, median/P95 waktu tunggu, dan lama drain; jangan memakai sasaran antrean ringan sebagai janji seluruh batch. Waktu terbit ketiga platform bergantung kapasitas tujuan paling lambat.

Tambahkan timestamp pesan Telegram, penerimaan durable, mulai/selesai tiap tahap, alasan menunggu beserta durasi, mulai/akhir preflight, intent, accepted, waktu jadwal, deteksi published, dan notifikasi terkirim. Label metrik request cukup komponen/rute umum; jangan menyimpan token, caption, URL bertoken, atau isi komentar sebagai label.

Guard penerimaan: nol jadwal/unduhan ganda, nol pemakaian akun terlarang, kuota tidak kurang hitung, snapshot parsial tidak diterapkan, checksum Drive penuh, payload lama tetap beku, unknown tidak dikirim ulang, dan hanya satu pemilik kirim/reply. Gunakan replay fixture dan preview terisolasi sebelum deployment; audit ini tidak melakukan publikasi, reply, atau pesan uji.

## Reproduksi dan arsip

Empat harness membuat fixture sendiri; tidak menyalin DB aktif atau membaca Credential. Perintah bawaan di bawah tidak membuka DB produksi. Jalankan pada worktree dengan venv terpisah, tanpa mengaktifkan layanan:

```sh
.venv/bin/python scripts/audit_pipeline_perf.py
.venv/bin/python scripts/audit_scheduler_perf.py --index-experiment
.venv/bin/python scripts/audit_io_perf.py
.venv/bin/python scripts/audit_runtime_perf.py --output runtime/verification/latency-deep/runtime.json
```

Gunakan `--output` baru saat mengulang script yang mendukungnya agar bukti tidak tertimpa; script I/O menolak output yang sudah ada. Opsi `audit_io_perf.py --read-only-db PATH` dipakai untuk ringkasan audit produksi, memakai SQLite `mode=ro` dan transaksi baca singkat; opsi itu tidak diperlukan untuk fixture. Benchmark sebaiknya dijalankan saat beban rendah atau di mesin terpisah agar I/O fixture tidak bersaing dengan produksi.

Laporan mesin tersimpan di `runtime/verification/latency-deep/` pada worktree, lalu diarsipkan ke runtime produksi `verification/latency-deep-20261021/probes/`. Database fixture sementara dibersihkan otomatis. Pengamatan awal ada di `verification/latency-audit-20261021/observations.json`. Bukti raw tidak dimasukkan Git. Verifier, review, commit/CI, merge/push dan cleanup dicatat dalam worklog serta manifest penutupan. Kelulusan suite membuktikan artefak audit tidak merusak regresi yang ada, bukan implementasi optimasi telah selesai.
