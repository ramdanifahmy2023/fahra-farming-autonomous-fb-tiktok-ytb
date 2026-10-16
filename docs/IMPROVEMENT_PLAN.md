# Rencana perbaikan Fahra Automation

Status 16 Oktober 2026: usulan setelah [audit kondisi aktif](AUDIT_IMPROVEMENT_20261016.md). Pengguna meminta fokus plan terlebih dahulu. Hasil tugas ini adalah dokumen audit dan urutan pengerjaan; perbaikan aplikasi belum dijalankan.

Sasaran pertama: delivery baru hanya dikirim ketika akun dan snapshot masih sah pada saat transport, kegagalan dapat dipulihkan tanpa posting ganda, dan operator bisa melihat penyebab pekerjaan tertahan. Sasaran berikutnya: mengurangi komputasi yang sia-sia dan mengukur kualitas hasil. Penambahan volume mengikuti bukti kestabilan.

## Batas yang tetap berlaku

Satu sumber baru tetap satu asset dan tiga tujuan wajib. Histori legacy tidak mendapat backfill. Batas 20 video/akun/hari WIB tetap menjadi batas lokal, dengan batas provider yang lebih rendah dihormati. TikTok iq7fanspage tetap dikecualikan. Input Telegram tetap publik dengan kendali admin dan rincian privat. Akun baru masuk otomatis sesudah bukti kesiapan lengkap, reconnect mempertahankan histori.

Orientasi sumber, zoom 110%, audio asli, hook Barlow [0,3), dan kredit Instagram terverifikasi dipertahankan. Drive Desktop tetap satu jalur sinkronisasi. Tidak ada render ulang/posting ulang otomatis untuk mengganti gaya. Cleanup tetap paling cepat 24 jam setelah tujuan terakhir terbit dan seluruh referensi terlindungi.

Angka 20 bukan target yang harus dipaksa tercapai. Pedoman [TikTok Direct Post](https://developers.tiktok.com/docs/en/content-sharing-guidelines) menyebut batas per kreator dalam jendela 24 jam yang dapat bervariasi. Jalur dan limit efektif Repliz perlu diukur; batas Direct Post tidak langsung diasumsikan sama untuk semua akun Repliz.

## Tahap 1 Ketepatan kirim dan akses informasi

Menutup A01, A02, A03. Perkiraan 2 sampai 4 hari pengembangan, bergantung kontrak readiness provider.

1. Tambahkan regresi dari tiga race limiter publikasi. Hubungkan pemeriksaan snapshot generation, akun, quota, binding dan pilot ke callback tepat sebelum transport. Jurnal/DB mencatat belum dikirim secara konsisten jika callback menolak. Crash setelah transport tetap unknown dan wajib rekonsiliasi.
2. Buat readiness per akun/operasi: identitas terverifikasi, status koneksi, capability, waktu pemeriksaan, alasan hold, dan cooldown error. Gunakan bukti yang tersedia dari provider; unknown tidak berubah menjadi ready hanya karena field tidak ada. Akun yang sudah bekerja dapat memakai bukti historis yang cocok dan masih sah sebagai bagian migrasi readiness.
3. Pisahkan asal pesan Telegram dari tujuan laporan. Grup hanya mendapat tanda terima umum; detail dan tautan hasil dikirim privat kepada pengirim yang telah membuka bot. Callback tetap memeriksa actor dan konteks. Layanan publik tidak diganti menjadi allowlist.

File utama: `pipeline.py`, `repliz.py`, `db.py`, `telegram.py`, tes scheduler/pipeline/Telegram dan migrasi readiness. Perubahan schema harus memiliki migration dan latihan upgrade fixture.

Kriteria lulus:

- Nol mock POST jika snapshot/akun/pilot menjadi tidak sah selama limiter, termasuk penundaan 429 dan antrean request lain.
- Timeout setelah transport tetap menghasilkan satu intent yang ditahan; restart/restore tidak menggandakan POST.
- Akun baru dengan identitas/capability parsial belum ikut rotasi; snapshot lengkap mengikutsertakannya otomatis. Disconnect/reconnect tidak mereset kuota/cutoff.
- Anggota grup selain pengirim/admin tidak melihat rincian, termasuk setelah callback sah milik pengirim. DM gagal ditahan tanpa membocorkan detail ke grup.
- Semua invariant lama dan verifier full tetap lulus. Bukti fixture tidak disebut sebagai publikasi nyata.

## Tahap 2 Pemulihan kegagalan dan validasi media lebih awal

Menutup A04 dan A05, serta bagian review A09. Mulai setelah kontrak pengiriman tahap 1 stabil. Perkiraan 3 sampai 5 hari.

1. Tambahkan klasifikasi `preflight`, `media`, `permission`, `rate_limit`, `provider_transient`, dan `unknown`, dengan reason yang dapat ditindak. Status failed-with-remote tetap dipantau secara terbatas dan dapat pulih berdasarkan bukti remote yang cocok.
2. Buat perintah inspect/reconcile/resolve untuk delivery dan reply. Setiap tindakan menampilkan schedule ID, post evidence, jurnal, quota dan keputusan yang akan diambil. Retry lokal hanya untuk belum terkirim; retry provider mempertahankan schedule ID dan membutuhkan kontrak endpoint yang sudah dibuktikan.
3. Validasi media setelah download/probe sebelum AI. Satu asset yang tidak sesuai menyimpan satu alasan yang ditampilkan pada semua tujuan. Cek ruang disk lagi sebelum ekstraksi dan render.
4. Kelompokkan review komentar berdasarkan alasan, umur, platform dan hasil terakhir. Jangan mengirim ulang 24 reply legacy yang masih sent_unverified atau memproses ulang 115 needs_review secara massal.

File utama: `db.py`, `pipeline.py`, `repliz.py`, `media.py`, `cli.py`, `reply_store.py`, `replies.py`. Tampilan kendali Telegram yang diperlukan mengikuti UI UX Pro Max dan antislop pada tahap implementasi.

Kriteria lulus:

- Repliz error yang kemudian menjadi success pada ID sama dapat menjadi published dan menghasilkan satu notifikasi, tanpa create baru.
- Retry ditahan bila remote/Content API/jurnal saling bertentangan atau hasil masih unknown.
- Izin akun bermasalah menahan pemilihan baru secara spesifik; akun sehat dan platform lain tetap berjalan.
- Fixture 480×854, durasi di luar profil, dan media rusak berhenti sebelum AI. Video valid tetap mengikuti alur normal; kebijakan penolakan tidak berubah diam-diam.
- Preview media nyata terisolasi wajib bila jalur media berubah; tidak dipublikasikan. Kasus Facebook yang ada ditangani hanya setelah penyebab dan hasil remote cocok, tanpa menghapus histori.

## Tahap 3 Kesehatan operasi, pemulihan dan beban

Menutup A06, A07, A08, sisa A09 serta dasar A11. Perkiraan 3 sampai 5 hari pengembangan, dilanjutkan sekurangnya 48 jam pengamatan normal. Durasi ini perkiraan kerja, bukan janji tanggal selesai.

1. Sediakan health read-only per loop: sukses terakhir, failure streak, latency, backlog tertua, next retry, dan alasan degraded. Bedakan snapshot wait dari error provider. Pisahkan event perubahan status dari pemeriksaan ulang.
2. Tambahkan alert admin yang dideduplikasi dan ringkasan harian. Tetapkan ambang awal dari baseline lalu evaluasi; interval kosong tanpa kiriman tidak dianggap kegagalan. Pengiriman alert baru menjadi bagian implementasi yang disetujui, bukan tindakan pada audit ini.
3. Buat backup bundle dengan manifest/checksum dan jurnal yang konsisten. Simulasikan restore DB tertinggal, jurnal hilang/rusak, disk penuh, kehilangan volume, dan crash di batas intent/response. Restore tetap shadow sampai state cocok. Tentukan target RPO/RTO sesudah rehearsal, lalu tampilkan pencapaiannya.
4. Uji antrean 20/100/1.000 sumber dengan fixture, kuota YouTube penuh, pergantian hari WIB, 429, timeout, serta akun putus. Ukur antrean limiter sebelum mengubah lock/concurrency. Pertahankan satu pemilik bot dan API budget bersama.
5. Tambahkan fairness per pengirim dan budget kerja/AI yang terukur. Catat bytes sementara, token bila provider mengembalikannya, model aktual dan waktu tiap tahap. Jangan menebak biaya dari jumlah request saja.
6. Amati komentar baru yang layak dan cocok dengan post terkelola; bedakan reply terverifikasi, like/moderasi terverifikasi, dan konfigurasi saja. Fitur nested tetap hold sampai kontrak parent terbukti.

Kriteria lulus:

- Health mengidentifikasi loop macet walau PID hidup; error per posting tidak hilang dari ringkasan karena loop global selesai.
- Sebuah kasus menghasilkan satu alert awal dan pembaruan yang bermakna; snapshot wait normal tidak membanjiri notifikasi.
- Restore latihan menghasilkan nol pengiriman ganda, menolak jurnal yang tidak lengkap, dan mempertahankan kuota serta ownership.
- Target backlog/latency disepakati berdasarkan hasil uji, dengan seluruh sumber/delivery tertelusur dan platform yang penuh tidak mengulang hasil platform lain.
- Laporan 48 jam memuat penyebut yang jelas: sumber diterima, delivery layak, jadwal diterima, published terverifikasi, failure dan hold. Tidak menetapkan target sukses 99% tanpa baseline.
- Bukti komentar per platform diberi status terpisah; tidak menunggu komentar lalu mengklaim seluruh automasi lulus dari heartbeat.

## Tahap 4 Pengukuran kualitas dan hasil konten

Menangani A10 dan penyelesaian A11 setelah tahap operasi stabil. Perkiraan 3 sampai 5 hari untuk instrumentasi awal, lalu 7 sampai 14 hari pengumpulan data bila volumenya memadai. Ini tahap lanjutan yang dapat dipilih terpisah.

1. Simpan metrik publikasi pada umur yang sama, misalnya 24 jam, 72 jam dan 7 hari, bersama platform, akun, sumber, topik, persona dan versi hook/caption. Gunakan endpoint yang aksesnya telah dibuktikan; nilai tidak tersedia tetap null.
2. Laporkan hasil per akun dan jenis konten dengan jumlah sampel. Bandingkan hook/caption melalui kiriman baru, tanpa menduplikasi satu video untuk eksperimen dan tanpa mengubah rotasi secara diam-diam.
3. Catat dasar penggunaan sumber yang tersedia serta evaluasi nilai editorial jika monetisasi menjadi sasaran. Kredit sumber tetap wajib. Usulan penambahan konteks/narasi atau perubahan format memerlukan brief tersendiri karena ketentuan sekarang mempertahankan audio asli dan tanpa avatar.
4. Pisahkan dokumen status terkini dari arsip, tambahkan migration schema eksplisit dan validasi konfigurasi. Tentukan versi Python yang benar-benar didukung serta hentikan reload parsial yang tidak konsisten dengan objek klien aktif.

Kriteria lulus: data metrik dapat ditelusuri ke ID post dan waktu pengambilan, tidak mengklaim field yang tidak tersedia, serta dapat menjelaskan biaya dan hasil per sumber. Tidak menjanjikan viral, follower, atau monetisasi dari perubahan teknis.

## Urutan keputusan dan pelaksanaan

Prioritas yang disarankan adalah tahap 1, tahap 2, lalu tahap 3. Tahap 4 menyusul setelah data operasi cukup. Untuk media kecil, default rencana mempertahankan minimum 540×960 dan memberikan alasan penolakan lebih awal. Dukungan upscale/padding menjadi pilihan terpisah dengan preview; menaikkan piksel tidak menjanjikan detail baru.

Setiap tahap implementasi memakai branch/worktree baru, venv/runtime fixture sendiri, dan credential hanya dari root yang ditetapkan. Tidak menyalin DB aktif ke worktree. Jalankan verifier full dengan `--base origin/main --secrets-from PATH_CREDENTIAL`, preview nyata jika media/AI/Drive berubah, CI pada SHA branch tepat, ff-only dari main bersih, pemeriksaan gabungan, push main, serta CI/SHA remote. Restart hanya layanan yang kodenya berubah. Arsipkan bukti sebelum membersihkan worktree tanpa force.

Rencana dinyatakan selesai ketika dokumen dan buktinya diperiksa. Perbaikan aplikasi baru dinyatakan selesai setelah kriteria tahap terkait terbukti; kelulusan dokumen ini tidak mengklaim celah A01 sampai A11 sudah ditutup.
