# Rencana laporan TikTok pukul 08.00 WIB

Status lanjutan 18 Oktober 2026: pengguna meminta implementasi di worktree dan push ke main. Kode, fixture dan preview GET nyata telah dibuat; aktivasi/pilot serta bukti jadwal otomatis dicatat terpisah pada worklog. Dasarnya adalah [audit kode dan GET Repliz nyata](AUDIT_TIKTOK_REPORT_20261018.md). Permintaan lanjutan ini menyetujui implementasi dan tahap aktivasi dalam rencana ini.

## Hasil yang diminta

Setiap hari pukul **08.00 Asia/Jakarta**, @Fahra_autonomousbot mengirim laporan ke chat pribadi admin **265732518**. Pemilihan chat mengikuti admin proyek yang sudah ada; GET getChat pada audit mengonfirmasi chat private dapat diakses. Bot adalah pengirim, bukan alamat tujuan. Pengguna meminta akun TikTok dengan **followersCount > 600**; tepat 600 tidak masuk daftar utama.

Laporan menampilkan semua username yang lolos, jumlah follower, perubahan follower yang dapat dibuktikan, status koneksi, dan waktu data diambil. TikTok iq7fanspage dikecualikan berdasarkan ID dan username sebelum request statistik. Akun yang putus koneksi tidak dihilangkan tanpa keterangan. Angka berasal dari Repliz; perhitungan dan kalimat status deterministik, tanpa AI.

## Kontrak data dan arti progress

| Isi | Sumber dan aturan |
| --- | --- |
| Identitas | GET `/public/account?types[0]=tiktok`, seluruh halaman tervalidasi; kunci histori adalah Repliz account ID, username disimpan bersama snapshot agar rename tetap dapat ditelusuri |
| Jumlah follower | `followersCount` tingkat akun dari GET `/public/account/{accountId}/statistic`; integer nonnegatif, bukan boolean/string/null. Field hilang berarti tidak tersedia, bukan nol |
| Perubahan sejak laporan kemarin | Selisih snapshot pagi tanggal D dan D-1 untuk ID yang sama. Tampilkan interval pengambilan sebenarnya; sebut sekitar 24 jam hanya untuk interval 23 sampai 25 jam. Snapshot catch-up di luar rentang tetap memakai label waktu sebenarnya |
| Perubahan 7 hari | Selisih snapshot pagi D dan D-7, bukan jumlah follower dari tujuh baris harian. Baseline hilang berarti belum tersedia, tanpa interpolasi |
| Persentase pertumbuhan | `(sekarang - baseline) / baseline * 100`; baseline nol menampilkan selisih absolut saja. Penurunan tetap negatif |
| Statistik harian Repliz | `metrics[]` disortir berdasarkan `date`, bukan urutan array. `dailyNewFollowers`, `dailyLostFollowers`, `dailyTotalFollowers`, `videoViews`, `profileViews` boleh ditampilkan dengan tanggal provider eksplisit |
| Like dan video | `totalLikes` dan `videosCount` adalah total akun saat dibaca. Selisih jumlah video bukan otomatis jumlah upload berhasil karena penghapusan juga dapat mengubah total |
| Kualitas data | Waktu GET, waktu snapshot pembanding, tanggal metrik provider, jumlah akun berhasil/target, serta status lengkap/sebagian/gagal |

Snapshot pagi adalah sumber utama progress. Data harian provider menjadi informasi pelengkap saat histori lokal belum ada. Jangan menyamakan `dailyTotalFollowers` dengan selisih dua snapshot pagi, atau menjumlahkannya sebagai perubahan tujuh hari tanpa jendela dan kelengkapan yang terbukti. Audit belum membuktikan zona waktu agregasi harian Repliz; label tanggal tetap “tanggal Repliz”, bukan “kemarin WIB”.

Audit nyata menemukan baris terbaru kedua akun >600 berisi nol pada 16 Oktober meski total akun positif, serta `dailyTotalFollowers` yang tidak selalu cocok dengan perubahan `metrics[].followersCount`. Baris serba nol dengan diskontinuitas ini berstatus anomali/belum terverifikasi, tidak menjadi baseline atau klaim stagnasi. Jangan menghapus nol secara umum: nol yang valid tetap sah, tetapi kasus yang bertentangan membutuhkan label kualitas. Histori lebih lama boleh ditampilkan dengan tanggalnya sendiri, bukan diam-diam menggantikan tanggal terbaru. Perbandingan provider 10 ke 15 Oktober pada mockup adalah informasi pelengkap bertanggal.

Contoh label untuk hari pertama: `Perubahan sejak laporan kemarin: belum ada baseline`. Setelah baseline D-1 tersedia, label berubah menjadi `Sejak laporan kemarin: +[selisih nyata]`. Angka nol yang sah tetap nol; null, tanggal hilang, dan tanggal mendatang tidak diubah menjadi nol. Metrik yang terlambat diberi tanggal nyata. Waktu GET bukan jaminan data di TikTok diperbarui pada detik yang sama.

Pantau seluruh akun TikTok dalam lingkup, termasuk yang belum melewati 600, agar kenaikan melintasi batas dapat diketahui. Urutkan daftar utama menurut follower terbesar, lalu username dan ID untuk hasil stabil. Bagian perubahan keanggotaan memuat “baru melewati 600” dan “turun ke 600 atau kurang” hanya jika kedua snapshot sah. Akun baru terhubung dengan follower tinggi dilabeli “baru dipantau”, bukan “baru tumbuh”. Disconnect, akun hilang dari inventaris lengkap, dan kegagalan GET memiliki label berbeda; kegagalan baca bukan penurunan follower.

Jangan membuat target 1.000 follower, perkiraan monetisasi, skor kesehatan, atau klaim penyebab pertumbuhan tanpa keputusan dan bukti terpisah. Fitur ini tidak memerlukan Content Statistic per posting.

## Jadwal dan pengiriman

Parameter berikut adalah usulan implementasi yang dapat ditinjau sebelum aktivasi:

1. Mulai pengambilan pukul **07.50 WIB**. Ambil inventaris lengkap, lalu statistik seluruh akun yang terhubung melalui limiter Repliz bersama worker yang ada. Batas konkurensi awal satu. Rancang waktu tunggu per akun, anggaran total, serta retry 429/5xx/timeout agar pekerjaan posting dan reply tetap mendapat giliran.
2. Tutup snapshot untuk laporan pukul **08.00 WIB**. Snapshot total follower dianggap baru untuk laporan bila GET dilakukan dalam 20 menit terakhir. Kesegaran pengambilan dan tanggal metrik provider ditampilkan terpisah. Akun dengan GET gagal masuk hitungan belum terverifikasi, bukan dianggap di bawah 600.
3. Bekukan isi laporan dan seluruh bagiannya secara atomik. Kunci unik run adalah `(jenis_laporan, tanggal_WIB, chat_id)`; kunci bagian menambah nomor bagian. Restart atau loop berulang memakai run yang sama. Tidak mengubah angka di tengah pengiriman beberapa bagian.
4. Kirim melalui bot dan loop outbox yang sudah ada, tanpa getUpdates kedua, bot kedua, atau LaunchAgent pengirim kedua. Semua bagian memuat tanggal dan nomor bagian. Laporan tidak mengikuti jeda antrean video; sakelar laporan tersendiri mengendalikan fitur ini.
5. Bila Mac bangun/restart antara 08.00 dan 12.00, kirim satu laporan hari berjalan dengan label waktu kirim terlambat dan waktu pengambilan aktual. Setelah 12.00, tandai hari itu terlewat; jangan membanjiri chat dengan laporan hari-hari sebelumnya. Sebut laporan terlewat pada laporan berikutnya. Tidak ada jaminan notifikasi tepat waktu ketika Mac, volume, jaringan, atau Telegram tidak tersedia.
6. Pada 08.00, jika semua statistik gagal, kirim status kegagalan pengambilan. Bila sebagian berhasil, kirim laporan sebagian dengan daftar akun yang belum terverifikasi. Jangan mengirim ringkasan “tidak ada akun >600” dari snapshot kosong akibat gangguan.

Status Telegram harus membedakan queued, sending, sent, failed, dan send_unknown per bagian. Simpan intent sebelum transport, respons `message_id`, waktu diterima API, serta hash isi. Gunakan jurnal durable yang ikut prosedur backup/restore; backup DB lama tidak boleh memicu kirim ulang bagian yang sudah tercatat terkirim. Telegram menerima request tidak membuktikan pengguna membaca pesan.

Outbox saat ini belum cukup untuk jaminan bebas duplikat: timeout atau crash sesudah sendMessage diterima dapat terjadi sebelum `sent=1`. Untuk laporan, hasil ambigu masuk `send_unknown` dan tidak otomatis dikirim ulang. `message_id` yang sudah diketahui dapat dipakai untuk rekonsiliasi/edit pesan yang sama; tanpa ID jangan mengklaim dapat menemukannya lewat getUpdates. Kebijakan ini mengutamakan tidak menggandakan laporan, dengan konsekuensi ada kasus yang perlu pemeriksaan operator. 429 memakai retry_after, error pasti sebelum transport boleh diulang, 400 format dicatat sebagai kegagalan format, 403 menandai tujuan tidak terjangkau. Pengiriman lain tetap berjalan.

Startup sekarang menunggu sync Repliz dan guard produksi sebelum loop Telegram aktif. Implementasi harus memberi jalur retry startup dan status laporan tanpa melonggarkan guard publikasi/reply, atau secara eksplisit melaporkan batas ini pada health. Fitur belum lulus penerimaan gangguan hanya karena loop laporan dapat berjalan setelah startup sehat.

## Format visual Telegram

Arah desain: laporan pagi untuk admin yang membaca di ponsel, ringkas dan mudah dibandingkan antarhari. ENERGY 1 / RHYTHM 2 / MOTION 1. Format memakai HTML Telegram native dengan heading tebal, angka utuh format Indonesia, username sebagai tautan profil, dan jarak satu baris antarblok. Rancangan meneruskan bahasa visual bot yang sudah memakai HTML dan tautan hasil.

| Keputusan | Tujuan |
| --- | --- |
| Judul singkat, tanggal, lalu jumlah akun yang lolos | Pembaca langsung mengetahui cakupan laporan |
| Satu blok vertikal per akun | Username dan angka tetap terbaca pada layar sempit; tidak membutuhkan tabel lebar atau perataan dengan spasi |
| Follower ditebalkan, perubahan memakai tanda + atau kata turun | Nilai utama mudah dipindai, arah perubahan tidak bergantung warna |
| Font, ukuran, tema, dan warna mengikuti Telegram pengguna | Teks dapat diperbesar serta dibaca dalam tema terang/gelap; tidak memaksakan gambar dengan teks kecil |
| Tautan hanya pada username profil yang tervalidasi | Pembaca dapat membuka akun terkait; tidak menambah tombol dekoratif |
| Footer sumber, waktu GET, dan tanggal statistik | Angka dapat ditelusuri dan keterlambatan data terlihat |

Setiap pesan ditargetkan maksimal 3.500 unit UTF-16 secara konservatif, serta diuji terhadap batas resmi Telegram 4.096 karakter setelah entity parsing. Pecah pada batas blok akun, tutup tag HTML di setiap bagian, escape semua string eksternal, validasi username sebelum membentuk URL `https://www.tiktok.com/@username`, dan nonaktifkan preview tautan. Maksimal enam blok akun per bagian sebagai awal; hitungan panjang selalu menentukan batas akhir. Seluruh akun yang lolos tetap dikirim, bukan hanya top lima. Setiap bagian tetap dapat dibaca sendiri.

Contoh dengan angka audit nyata terdapat di dokumen audit. Pada implementasi, tanggal header berasal dari jadwal laporan, bukan tanggal contoh. Mockup bukan bukti tampilan sudah diperiksa pada aplikasi Telegram.

Keadaan yang wajib dirancang:

- Kosong sah: `Belum ada akun dengan follower di atas 600. Statistik berhasil diperiksa: [jumlah nyata]/[target nyata].`
- Pengambilan berjalan: status preview/admin menyebut `Mengambil statistik akun TikTok`. Tidak mengirim pesan loading harian tambahan.
- Sebagian: `Data belum lengkap: [berhasil]/[target] akun berhasil diperiksa`, lalu akun yang berhasil dan daftar yang belum terverifikasi. Angka terakhir akun bermasalah hanya muncul sebagai histori dengan waktu lamanya.
- Gagal total: `Statistik Repliz belum berhasil diambil untuk laporan hari ini`; tidak menampilkan angka lama sebagai data baru.
- Baseline belum ada: `Perubahan belum tersedia; ini snapshot pagi pertama.`
- Disconnect: `Koneksi terputus; angka terakhir [tanggal/jam]`, terpisah dari daftar follower yang terverifikasi saat ini.

Riset UI UX Pro Max memberi kecocokan untuk keterbacaan mobile dan empty states. Rekomendasi 16 px dan kontras menjadi sasaran review, bukan CSS yang bisa dipaksakan melalui Bot API. Hasil pencarian bulk edit/form label tidak cocok dan tidak diterapkan. Antislop menyaring ornamen, angka contoh palsu, dan progress bar tanpa target. Tidak diperlukan gambar AI, grafik berwarna, avatar baru, atau font video Barlow untuk laporan teks Telegram.

## Perubahan yang akan diimplementasikan

| Area | Pekerjaan yang diusulkan |
| --- | --- |
| `repliz.py` | Pembacaan statistik akun dengan whitelist field, validasi tipe, limiter bersama, timeout/backoff dan perlindungan akun terlarang |
| Modul baru `account_reports.py` | Pengumpulan, validasi kelengkapan, pemilihan >600, perhitungan progress, deadline WIB, catch-up, dan health laporan |
| `db.py` atau store laporan baru | Migrasi idempotent untuk report_runs, account_metric_snapshots, provider_daily_metrics, dan report_parts/attempts; histori append-only, foreign key/unique key yang sesuai |
| `telegram_views.py` | Renderer laporan deterministik, semua state, pemecahan pesan dan escape HTML |
| `telegram.py` dan jurnal | Pengiriman bagian laporan dengan respons message_id dan send_unknown, tanpa mengubah semantik notifikasi lama tanpa uji regresi |
| `cli.py` | Task di worker pemilik bot saat ini; preview lokal dan status report. Perintah usulan belum tersedia sampai diimplementasikan |
| `config.example.toml` | Bagian laporan disabled secara bawaan, timezone Asia/Jakarta, jam 08:00, threshold 600 dengan operator >, tujuan private admin, jendela persiapan/catch-up |
| Dokumen operasi | Cara preview, enable/disable, diagnosis metrik terlambat, send_unknown, backup/restore, serta bukti kirim |

Snapshot laporan dipisah dari snapshot akun/kuota publikasi. Jangan memanggil `sync(store)` untuk sekadar mengumpulkan follower karena fungsi itu memulai generation dan memperbarui state scheduler. Simpan status per akun; kegagalan satu statistik tidak membatalkan akun lain atau mengubah eligible/connected pool. Snapshot gagal tidak menjadi baseline. Reconnect mempertahankan histori ID yang sama.

Metadata minimal: run ID, tanggal/jadwal WIB, started/completed/fetched timestamps, status inventaris lengkap, daftar ID target, excluded count, status per akun, username saat dibaca, field numerik yang sah, tanggal provider, versi parser, payload hash, dan error class/status yang sudah disanitasi. Simpan hanya field yang diperlukan, tanpa credential, header auth, demografi, avatar URL, atau respons akun mentah. Usulan retensi snapshot numerik 400 hari dan metadata kirim/jurnal mengikuti arsip operasional; pengurangan retensi menjadi pekerjaan terpisah, bukan penghapusan media.

## Tahap dan kriteria penerimaan

1. **Data dan baseline.** Implementasi collector/store terisolasi. Preview GET nyata seluruh akun, tanpa menulis DB aktif atau mengirim Telegram. Cocokkan setiap angka pada laporan dengan ID dan waktu evidence; validasi rename, akun baru, disconnect, reconnect, dan pengecualian.
2. **Renderer dan jadwal.** Uji waktu dengan clock fixture. Periksa hasil teks pada lebar ponsel, teks besar, tema terang/gelap, daftar panjang, semua state, dan karakter HTML. Preview angka nyata harus membedakan snapshot sekarang dari tanggal metrik provider.
3. **Ketahanan pengiriman.** Uji crash/restart/restore sebelum dan sesudah intent/response, send_unknown, retry 429, chat terblokir, bagian kedua gagal, inventaris parsial, Repliz startup gagal, dan Mac bangun terlambat. Buktikan tidak ada worker/getUpdates kedua.
4. **Verifikasi dan aktivasi.** Jalankan verifier full, commit/push branch, CI pada SHA tepat, main bersih/selaras, merge ff-only, verifikasi hasil gabungan, push main dan periksa CI. Sesudah implementasi diminta dan preview lulus, aktivasi hanya laporan; restart layanan yang kodenya berubah sesuai prosedur pause/backup/start/snapshot/reply proyek. Kirim satu pilot laporan ke chat admin dan simpan message_id, lalu buktikan run otomatis pukul 08.00 berikutnya. Pilot manual tidak membuktikan jadwal otomatis.

| Kasus | Bukti lulus |
| --- | --- |
| 599, 600, 601; null/string/bool/negatif | Hanya integer 601 masuk, nilai invalid diberi status tidak tersedia |
| Inventaris lebih dari satu halaman, ID duplikat/total berubah | Validasi seluruh halaman; partial tidak dianggap seluruh populasi |
| Baseline D-1/D-7 hilang, nol, turun, rename | Selisih benar atau belum tersedia; tidak ada angka buatan/division by zero |
| Array metrics tidak urut, tanggal ganda/hilang, hari provider terlambat | Urutan tanggal dinormalisasi, konflik ditahan, tanggal nyata dilabeli |
| Akun tidak terhubung, hilang, baru terhubung, kembali | Histori tetap ada, label berbeda, tidak melabeli error sebagai turun ke <=600 |
| Bot publik/nonadmin/grup | Laporan otomatis hanya tujuan private admin; fitur manual/callback bila ditambahkan wajib private dan admin |
| 07.59/08.00, timezone mesin lain, restart dua kali, ganti hari | Tepat satu logical report per tanggal WIB; catch-up dan keterlambatan terlabel |
| Timeout setelah Telegram menerima, crash sebelum DB ack, restore backup | Tidak ada retry buta; status unknown, journal dan message_id dipertahankan |
| Pesan panjang, nama Unicode/HTML, lebih dari enam akun | Semua akun termuat, HTML sah, batas karakter terpenuhi, tautan aman |
| Gangguan metrik atau laporan | Input bot, publikasi, reply, kuota dan pemilik worker tidak berubah akibat state laporan |

Kriteria selesai adalah data nyata cocok, format diperiksa di Telegram, pilot terkirim, dan run terjadwal 08.00 terbukti. Pada tahap audit awal butir tersebut belum dijalankan. Status implementasi terbaru mengikuti worklog; preview dan pilot tidak menggantikan bukti run otomatis pukul 08.00. Akses Computer Use untuk Safari dan Telegram ditolak saat implementasi, sehingga review visual native tetap belum terverifikasi.
