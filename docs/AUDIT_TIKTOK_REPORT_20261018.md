# Audit laporan follower TikTok

Audit 18 Oktober 2026 untuk permintaan notifikasi harian @Fahra_autonomousbot pukul 08.00 tentang username TikTok dengan follower di atas 600 dan progress-nya. Data total follower dapat dibaca dari Repliz, tetapi pengumpulan histori, jadwal laporan, dan pencatatan hasil kirim khusus laporan belum tersedia. [Rencana implementasi](TIKTOK_DAILY_REPORT_PLAN.md) memisahkan pekerjaan tersebut dari automasi publikasi yang sudah aktif.

## Metode dan batas pemeriksaan

Base kode `4e92897c450a5ba1f66bec489ce78467aef33ad8`. Kode, konfigurasi contoh, worker, schema, renderer Telegram, dan outbox diperiksa. DB aktif dibaca dengan SQLite `mode=ro`, `PRAGMA query_only=ON`, dalam transaksi baca, tanpa membuat Store aplikasi. LaunchAgent baru terbaca running PID 26788. Audit tidak menyimpulkan seluruh loop sehat dari PID saja.

GET Repliz dan Telegram dilakukan pada **18 Oktober 00.38.58 sampai 00.40.29 WIB**. Kredensial dibaca dalam proses lokal dari Credential root baru. Evidence hanya menyimpan field yang dipilih, tanpa token, auth header, respons akun mentah, avatar, atau demografi. Tidak ada getUpdates kedua, sendMessage, POST/PUT/DELETE Repliz, perubahan DB/config produksi, restart layanan, atau aktivasi laporan. Worktree audit lain yang sudah ada tidak disentuh.

Evidence di `runtime/verification/tiktok-report-plan-20261018/` pada root produksi sesudah pengarsipan: `access.json`, `statistics.json`, script probe, hasil verifier/CI dan `final-git.json`. Bukti runtime tidak masuk Git. Pada tahap pengembangan, file berada di runtime worktree `20261018-tiktok-report-plan` dengan nama folder `tiktok-report-plan`.

## Hasil akses nyata

| Pemeriksaan | Hasil |
| --- | --- |
| Inventaris GET `/public/account` dengan filter TikTok | Satu halaman lengkap, 47 akun. Satu akun terlarang dikeluarkan sebelum request statistik |
| Akun dalam lingkup | 46, semuanya `isConnected=true` pada snapshot ini |
| Statistik | 46/46 GET statistik akun HTTP 200, semuanya memiliki integer `followersCount` yang sah |
| Di atas 600 | 2 akun; tepat 600: 0 akun |
| Bot | getMe HTTP 200, username `Fahra_autonomousbot` |
| Tujuan usulan | getChat admin 265732518 HTTP 200, type private |
| Webhook | getWebhookInfo HTTP 200, URL webhook kosong; tidak memanggil getUpdates |
| Jumlah request audit | 48 GET Repliz: satu inventaris, satu sampel statistik, 46 statistik lengkap; tiga GET Telegram |

| Username | Follower | Total like | Jumlah video | Waktu GET statistik WIB |
| --- | ---: | ---: | ---: | --- |
| @clayrafathia | 9.471 | 12.939 | 76 | 18 Oktober 00.40.29 |
| @almeera_278 | 694 | 2.621 | 166 | 18 Oktober 00.40.25 |

Angka berasal dari respons Repliz pada waktu tersebut, bukan contoh buatan dan bukan jaminan nilai yang sama pada pukul 08.00. Audit tidak membuka profil publik TikTok untuk membandingkan angka. Repliz tidak menyertakan timestamp pembaruan total follower dalam field statistik yang diterima; waktu GET adalah waktu pengambilan oleh audit.

## Temuan dan prioritas

| ID | Prioritas | Bukti | Dampak dan arah perbaikan |
| --- | --- | --- | --- |
| R01 | P1 | `accounts` di db.py menyimpan identitas/koneksi/kuota, tanpa follower atau histori metrik. `_sync_accounts` tidak menyimpan field statistik | Perubahan harian/mingguan belum dapat dihitung dari snapshot pagi. Tambahkan histori khusus laporan dengan ID akun stabil |
| R02 | P1 | Semua akun memiliki tanggal `metrics[]` terbaru 16 Oktober. Untuk kedua akun >600, seluruh field numerik baris 16 Oktober yang diperiksa bernilai nol, termasuk follower, sedangkan total akun positif | Nol harian tidak cukup untuk klaim stagnan atau follower hilang. Pisahkan total akun dari histori provider, deteksi anomali, tampilkan tanggal nyata dan status belum terverifikasi |
| R03 | P1 | `outbox` hanya menyimpan dedupe key, teks, opsi dan sent; `flush_ui` membuang respons message_id, retry exception generik | Deduplikasi antrean belum mencegah pesan ganda sesudah timeout/crash. Butuh intent/response/journal per bagian dan send_unknown |
| R04 | P2 | config contoh, TaskGroup `cli.run`, dan pencarian source tidak memuat jadwal laporan harian | Perlu jadwal WIB persisten dan catch-up restart, bukan sekadar sleep 24 jam |
| R05 | P2 | Inventaris akun tidak membawa follower; GET statistik terpisah berhasil 46/46 | Filter >600 harus dilakukan setelah statistik seluruh akun dibaca. Mengandalkan daftar lama dapat melewatkan akun baru yang naik |
| R06 | P2 | `/akun` berupa daftar identitas/aktif; renderer belum memiliki format statistik, delta, kualitas data, atau pemecahan laporan panjang | Perlu renderer HTML native, urutan akun stabil, pemecahan per blok, serta state kosong/sebagian/error |
| R07 | P2 | Bot menerima input publik, tetapi admin tersedia. `cli.run` menunggu sync Repliz sebelum memulai Telegram | Tujuan laporan wajib private admin. Kegagalan startup dapat mencegah laporan gangguan; rencana perlu mencakup kondisi ini tanpa melonggarkan guard publikasi |

Endpoint akun dan statistik termasuk Standard+ menurut [Get Account](https://docs.repliz.com/api/account/get-account) dan [Get Statistic Account](https://docs.repliz.com/api/account/get-statistic-account). Dokumentasi menyediakan contoh field TikTok `followersCount`, `totalLikes`, `videosCount`, dan `metrics` harian. Akses aktual pada akun pengguna dibuktikan oleh GET di atas, bukan oleh tier dokumentasi saja. Endpoint ini cukup untuk laporan akun; Content Statistic per posting tidak dibutuhkan.

## Progress yang bisa dan belum bisa dinyatakan

Histori yang benar-benar dikembalikan Repliz memberi pembanding bertanggal:

| Username | Follower tanggal Repliz 10 Oktober | Follower tanggal Repliz 15 Oktober | Selisih dua baris bertanggal |
| --- | ---: | ---: | ---: |
| @clayrafathia | 9.480 | 9.474 | -6 |
| @almeera_278 | 693 | 694 | +1 |

Ini perbandingan histori provider **10 ke 15 Oktober**, bukan progress 24 jam atau tujuh hari pada 18 Oktober. Baris 16 Oktober memerlukan pemeriksaan kualitas. Jangan menggeser tanggal 15 Oktober lalu menamainya “kemarin”. Urutan array tidak kronologis: pada dua akun ini 16 Oktober muncul sebelum 10 sampai 15 Oktober.

Ada perbedaan antarfield: follower harian @almeera_278 berubah 693 ke 694 pada 14 Oktober, tetapi `dailyTotalFollowers` tanggal tersebut bernilai 0. Untuk @clayrafathia, perubahan dua baris follower juga tidak selalu sama dengan `dailyTotalFollowers`. Penyebab dan waktu pembaruan masing-masing field belum dibuktikan. Rancangan tidak mencampurkan kedua definisi itu sebagai satu metrik pertumbuhan.

Snapshot audit ini bukan baseline laporan pagi 08.00. Baseline pertama perlu diambil oleh implementasi pada jendela yang ditentukan, dengan informasi waktu. Sampai baseline pagi D-1/D-7 tersedia, laporan wajib menulis “belum tersedia”. Histori provider yang dipakai sebagai pelengkap tetap menyebut rentang tanggal dan sumbernya.

## Contoh tampilan, memakai angka audit nyata

Berikut mockup teks untuk review, **belum dikirim ke Telegram**. Waktu pada contoh adalah waktu audit, bukan klaim pengiriman otomatis pukul 08.00.

> **Laporan TikTok · Fahra**
>
> 18 Oktober 2026 · contoh audit 00.40 WIB<br>
> **2 akun di atas 600 follower**<br>
> Statistik berhasil: 46/46 akun
>
> **1. @clayrafathia**<br>
> **9.471 follower** · Terhubung<br>
> Perubahan sejak laporan kemarin: belum tersedia<br>
> Perubahan 7 hari: belum tersedia<br>
> Histori Repliz 10 ke 15 Okt: 9.480 ke 9.474 (-6)<br>
> Total: 12.939 like · 76 video
>
> **2. @almeera_278**<br>
> **694 follower** · Terhubung<br>
> Perubahan sejak laporan kemarin: belum tersedia<br>
> Perubahan 7 hari: belum tersedia<br>
> Histori Repliz 10 ke 15 Okt: 693 ke 694 (+1)<br>
> Total: 2.621 like · 166 video
>
> **Catatan data**<br>
> Metrik harian 16 Okt berisi nol yang belum terverifikasi. Progress pagi menunggu baseline.
>
> Sumber: Repliz · diambil 00.39 sampai 00.40 WIB

Pada implementasi, username menjadi tautan profil yang tervalidasi dan angka diperbarui dari run harian. Tabel audit digunakan untuk membaca dokumen desktop; pesan Telegram memakai blok vertikal. Format asli mengikuti tema/font Telegram dan menghindari tabel lebar, gambar berteks kecil, serta progress bar tanpa target. Bot API mendukung HTML dan [sendMessage](https://core.telegram.org/bots/api#sendmessage) membatasi teks sampai 4.096 karakter setelah entity parsing.

## Review dokumen dan batas kelulusan

- **PASS fakta/copy:** dua username dan seluruh angka mockup cocok dengan evidence; selisih 10 ke 15 Oktober dihitung dari dua tanggal yang disebut. Tidak ada delta 24 jam/tujuh hari buatan.
- **PASS rancangan hierarki:** judul/cakupan, blok per akun, catatan kualitas, dan waktu sumber memiliki fungsi masing-masing; visual mengikuti arah laporan ringkas untuk ponsel, ENERGY 1 / RHYTHM 2 / MOTION 1.
- **PASS cakupan state dalam plan:** kosong sah, pengambilan, sebagian, gagal total, baseline kosong, dan disconnect dirancang terpisah. Pertumbuhan tidak ditandai dengan warna saja.
- **PASS ruang lingkup audit:** probe GET dan query DB baca saja; fitur dan notifikasi belum diaktifkan. Pengiriman menjadi tahap implementasi setelah diminta.

Review antislop dan UI UX Pro Max ini berlaku pada dokumen/rancangan. Tampilan Telegram native, pembesaran teks, tema, tautan, pengiriman, deduplikasi, restore, serta jadwal 08.00 belum diuji sebagai fitur. Hasil verifier full dan CI membuktikan kelulusan checkout dokumentasi serta suite aplikasi yang sudah ada, bukan kelulusan fitur laporan baru.
