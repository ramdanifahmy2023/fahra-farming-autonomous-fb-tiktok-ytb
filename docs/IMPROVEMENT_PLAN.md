# Rencana perbaikan Fahra Automation

Diperbarui 18 Oktober 2026 berdasarkan [audit ulang](AUDIT_20261018.md). Pengguna kemudian meminta implementasi di worktree dan push main. Kode perbaikan tersedia pada branch tugas; bukti merge, CI dan penerapan akhir dicatat pada worklog dan runtime/verification/audit-fixes-20261018/. A01–A11 merujuk [audit 16 Oktober](AUDIT_IMPROVEMENT_20261016.md).

## Hasil implementasi

| Bagian | Perubahan yang dibuat | Batas bukti |
| --- | --- | --- |
| Pengiriman | Guard ulang setelah limiter; intent yang belum terkirim ditutup sebagai not_sent | Fixture race menghasilkan nol POST; tidak membuat posting uji |
| Media | Error kelompok dibuka; retry dihitung per asset; probe sebelum AI | Preview nyata 720×1280, H.264/AAC, 30 fps, tanpa mirror, hook hilang pada detik 3 |
| Telegram | Grup menerima tanda terima umum; progres/hasil pindah ke chat pengirim | Pesan detail lama yang belum terkirim ke grup dikarantina; bot harus dibuka untuk mengaktifkan kembali DM |
| Pemulihan | Inspect/reconcile/resolve dan retry lokal; jadwal gagal diperiksa ulang pada ID sama | Resolve berarti mencocokkan bukti, bukan memaksa status sukses; provider retry belum diaktifkan |
| Akun | Koneksi, identitas, bukti publish dan bukti reply dicatat terpisah | Capability yang hilang menunggu bukti; histori publish berlaku tujuh hari dan harus cocok identitas |
| Operasi | Health, durasi tahap/limiter, penggunaan token, fairness pengirim, disk sebelum persiapan | Pengamatan 48 jam masih diperlukan; biaya uang tidak ditebak dari token |
| Backup | Bundle database/jurnal/config nonrahasia/ownership beserta checksum; restore shadow | Kekurangan jurnal reply legacy dicatat terbuka; restore tidak memperoleh hak kirim |
| Cleanup | Bank/render legacy dicocokkan dengan manifest, snapshot dan mapping asal | Pemeriksaan nyata 79 asset masih ditahan karena referensi lama; nol file dihapus |
| Hasil konten | Collector GET opsional pada 24/72/168 jam sejak verifikasi published | Default nonaktif sampai operasi stabil; lewat jendela satu jam dicatat missed, tidak diisi angka masa kini |

Versi Python minimum dipersempit menjadi 3.14 sesuai runtime dan CI yang diuji. Seluruh konfigurasi file memerlukan restart; reload parsial di worker dihapus. Perintah status dan inspect membuka SQLite hanya-baca. Aturan tujuan, batas kuota, bank Drive, orientasi, audio, kredit sumber dan ownership tetap berlaku.

Sasarannya: bot tahu kapan harus bekerja, kapan harus menunggu, dan bagaimana melanjutkan pekerjaan gagal tanpa mengirim video atau balasan dua kali. AI tidak mengerjakan sumber yang sudah pasti ditolak oleh profil aplikasi.

## 1. Perbaiki batas pengiriman dan pemeriksaan video

Prioritas pertama mencakup A01, A03, A05 dan error downloader baru.

- Periksa ulang generation snapshot, akun, kuota, binding dan pilot persis sebelum request publikasi, sesudah antrean limiter. Jika belum dikirim, tutup intent sebagai not_sent secara konsisten. Jika transport mungkin sudah terjadi, tetap tahan sebagai unknown dan rekonsiliasi.
- Buka penyebab ExceptionGroup secara rekursif. Pertahankan arti ConfigurationError sebagai penantian dan DownloadUncertain sebagai penahanan. Kelompok campuran/hasil ambigu memilih tindakan konservatif; rahasia dan URL tetap disamarkan.
- Bedakan sumber tanpa video, akses terbatas, respons kosong, timeout dan gangguan sementara. Kegagalan bersama ditangani pada asset; tiga tujuan tidak menghabiskan masing-masing empat percobaan untuk manifest gagal yang sama. Jangan mengulang start yang belum pasti.
- Validasi durasi, ukuran dan rasio segera sesudah probe, sebelum transkripsi/frame/AI. Satu alasan sumber ditampilkan konsisten di tiga tujuan. Default tetap menolak format di luar profil. Crop, padding, upscale atau pemotongan durasi memerlukan keputusan format terpisah.
- Pisahkan chat asal dari laporan privat. Input publik tetap diterima; grup hanya mendapat tanda terima umum. Rincian/tautan hasil menuju pengirim/admin yang berhak. Jika DM belum tersedia, simpan laporan dan beri alur membuka bot tanpa membuka rincian di grup.

File utama: `pipeline.py`, `downloader.py`, `media.py`, `util.py`, `repliz.py`, `db.py`, `telegram.py` dan tes terkait.

Lulus jika tiga race limiter menghasilkan nol POST tiruan; penantian metadata tidak menghabiskan retry; download belum pasti masuk hold tanpa start kedua; sumber tidak valid berhenti sebelum AI; dan callback sah di grup tidak membocorkan rincian. Uji juga error kelompok bertingkat/campuran, crash pada batas transport dan restart. Perubahan media memerlukan preview nyata terisolasi tanpa publikasi.

## 2. Beri jalan pemulihan untuk kiriman dan balasan

Meliputi A02, A04 dan A09, sesudah kontrak pengiriman tahap pertama stabil.

- Sediakan inspect/reconcile/resolve untuk failed-with-remote dan reply sent_unverified. Tampilkan ID jadwal, hasil provider, jurnal, kuota, bukti post/reply dan tindakan yang diperbolehkan. Jangan menghapus ID atau mengulang tujuan yang sudah berhasil.
- Jadwal error yang kemudian success pada ID sama harus dapat menjadi published dan memberi satu notifikasi. Pemeriksaan ulang diberi interval/batas beban. Retry provider hanya setelah kontrak, akses paket, identitas dan hasil sebelumnya terbukti; keberadaan endpoint bukan bukti retry aman.
- Pisahkan kesiapan koneksi, identitas, posting dan komentar per akun. Field capability yang tidak ada tidak otomatis berarti siap. Gunakan bukti provider dan histori yang cocok serta masih berlaku. Akun bermasalah ditahan secara spesifik, bukan mematikan pool sehat.
- Kelompokkan reply menurut umur, platform, hubungan ke posting terkelola dan alasan penahanan. Cocokkan ulang 20 hasil baru yang belum terbukti tanpa resend. Jangan memaksa komentar ambigu menjadi balasan otomatis. Buktikan kesempatan dan hasil yang memenuhi syarat sebelum menyimpulkan Facebook/YouTube sehat atau rusak.
- Periksa kasus caption gagal parsing dan konsistensi checkpoint asset/delivery. Satu tujuan bisa tertinggal failed meski persiapan bersama kemudian berhasil melalui tujuan lain. Pemulihan mempertahankan checkpoint valid dan tidak mengulang hasil yang sudah terbit.

File utama: `pipeline.py`, `db.py`, `repliz.py`, `replies.py`, `reply_store.py`, `comment_profiles.py`, `journal.py` dan antarmuka operator. Perubahan antarmuka memakai UI UX Pro Max dan antislop saat implementasi.

Lulus jika error→success jadwal sama terpulihkan tanpa create baru; hasil ambigu tetap ditahan; partial success tiga platform dipertahankan; akun parsial belum masuk rotasi sampai lengkap; reconnect tidak mereset kuota; serta setiap reply memiliki status bukti yang jelas. Enam kegagalan provider nyata adalah kasus pemeriksaan, bukan instruksi langsung mengulang semuanya.

## 3. Buat keadaan bot mudah dilihat dan dipulihkan

Meliputi A06–A08, dasar A11 dan cleanup migrasi baru.

- Buat health hanya-baca tiap loop: sukses terakhir, kegagalan beruntun, usia pekerjaan tertua, jumlah target unik, alasan menunggu dan tindakan berikutnya. Error per posting tidak hilang karena loop global selesai. Bedakan pemeriksaan ulang dari kiriman baru.
- Ringkasan memakai istilah jelas: diterima, dikerjakan, menunggu jam tayang, ditolak format, gagal penyedia dan selesai. Pisahkan sumber dari jumlah kiriman, serta legacy dari pekerjaan baru. Alert admin dideduplikasi.
- Paketkan backup database, batas jurnal konsisten, ownership dan konfigurasi nonrahasia dengan manifest/checksum. Credential tetap privat. Latihan restore memakai fixture, mulai shadow, dan menolak bukti yang hilang sebelum hak kirim diberikan.
- Cocokkan 79 kasus cleanup legacy dengan bank asal dan manifest migrasi. Referensi lama hanya diterima bila kepemilikan, file Bank/Drive dan checksum terbukti. Jika kurang bukti tetap hold. Tidak ada penghapusan manual/rekursif.
- Ukur waktu tiap tahap, antrean limiter, penggunaan AI yang tersedia dan disk. Tambahkan fairness pengirim serta pemeriksaan disk sebelum download/render. Uji beban sebelum menaikkan konkurensi.
- Tambahkan migrasi schema eksplisit, konfigurasi tervalidasi, dan CLI status tanpa inisialisasi Store tulis. Jelaskan field yang perlu restart dan hilangkan reload sebagian yang berbeda dengan klien aktif.

Lulus jika laporan menemukan loop macet walau PID hidup; restore fixture menghasilkan nol kiriman ganda dan menolak backup tidak lengkap; cleanup melindungi referensi lama/baru serta retensi; uji 20/100/1.000 sumber menjaga kuota/deduplikasi. Amati operasi sekurangnya 48 jam setelah penerapan dengan penyebut sumber/kiriman dan waktu snapshot jelas. Target latency/pemulihan mengikuti pengukuran, bukan janji tanpa baseline.

## 4. Ukur biaya dan hasil konten setelah alur stabil

A10 adalah pengembangan lanjutan, bukan penyebab kegagalan saat ini. Simpan metrik yang tersedia pada umur posting yang sama, misalnya 24 jam, 72 jam dan 7 hari. Hubungkan ke platform, akun, sumber, topik, persona dan versi hook. Nilai tidak tersedia tetap kosong, bukan nol.

Bandingkan dengan jumlah sampel memadai; jangan posting ulang hanya untuk eksperimen. Rotasi akun, format editorial, avatar, audio dan target monetisasi tidak berubah otomatis. Tidak menjanjikan viral, follower atau pendapatan dari perbaikan teknis. Selesaikan pengujian versi Python minimum atau persempit dukungan berdasarkan bukti.

## Aturan yang tetap utuh

Satu link baru tetap satu asset dan tiga tujuan wajib; legacy tidak mendapat backfill. Batas lokal maksimal 20 video/akun/hari Asia/Jakarta dengan batas provider lebih rendah dihormati. Kuota reply terpisah. TikTok iq7fanspage tetap dikecualikan. Input Telegram publik, kontrol admin, dan rincian privat dipertahankan.

Orientasi asli, zoom 110%, audio asli, hook Barlow [0,3), serta `source: @username` terverifikasi tetap berlaku. Drive Desktop menjadi satu jalur sinkronisasi; semua checksum diperiksa sebelum publikasi. Tidak ada render/posting ulang untuk mengganti gaya. Cleanup minimal 24 jam setelah tujuan wajib terakhir published dan semua referensi terlindungi.

Implementasi memakai branch/worktree dan venv/runtime fixture sendiri. Jangan menyalin DB aktif atau menjalankan bot kedua. Jalankan verifier full `--base origin/main --secrets-from PATH_CREDENTIAL`, preview nyata bila media/AI/Drive berubah, CI pada SHA branch tepat, merge ff-only dari main bersih, pemeriksaan gabungan, push main dan CI/SHA remote. Restart hanya layanan yang kodenya berubah. Arsipkan bukti sebelum menghapus worktree tanpa force.

Rencana selesai sebagai dokumen ketika bukti dan pemeriksaannya diarsipkan. Perbaikan baru selesai setelah kriteria terkait lulus dan diterapkan. Tes offline, preview, jadwal diterima, hasil publik dan aksi komentar nyata tetap dibedakan.
