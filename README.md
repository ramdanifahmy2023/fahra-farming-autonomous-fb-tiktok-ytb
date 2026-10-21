# Fahra Automation

Kirim Reel Instagram ke **@Fahra_autonomousbot**. Satu video diunduh dan diedit, lalu dibuat caption sesuai persona akun dan dijadwalkan ke satu TikTok, satu Facebook Page, dan satu YouTube Shorts. Bot melaporkan progres serta tautan ketika setiap tujuan benar-benar terbit.

- Maksimal **20 video per akun per hari WIB**. Rotasi bertahan antarbatch/restart; akun baru ikut otomatis setelah snapshot lengkap. Akun terputus dilewati.
- Satu asset, tiga antrean mandiri. YouTube penuh tidak mengulang TikTok/FB yang sudah berhasil.
- Orientasi asli tanpa mirror, zoom 110%, hook Indonesia tiga detik pertama, audio asli. Format bersama 9:16, 3–90 detik, H.264/AAC. Caption/deskripsi mencantumkan `source: @username` Instagram yang terverifikasi.
- Bank konten disinkronkan Drive Desktop. Seluruh checksum diverifikasi lagi sebelum setiap pengiriman.
- [Hook, caption, dan persona](docs/COPY_AND_PERSONA.md) memakai bahasa Indonesia santai berdasarkan isi video; balasan memakai konteks video dan komentar induk. Like/moderasi mengikuti kemampuan platform; YouTube tidak diberi like komentar.
- File kerja dibersihkan paling cepat 24 jam setelah semua tujuan wajib terbit. Bank, Drive, konteks, frame, dan histori tetap disimpan.

TikTok **iq7fanspage** selalu dikecualikan. Paket ini menggantikan kedua workflow lama setelah verifikasi dan cutover; konfigurasi bawaan tetap `shadow` sampai kepemilikan diberikan.

Mulai dari [tools.md](tools.md) dan [operasi](docs/OPERATIONS.md). Keputusan tetap ada di [AGENTS.md](AGENTS.md), status/bukti di [memory.md](memory.md) dan [worklog.md](worklog.md). Rancangan dan matriks uji: [PLAN.md](docs/PLAN.md), [ACCEPTANCE.md](docs/ACCEPTANCE.md), [MIGRATION.md](docs/MIGRATION.md).

Audit waktu terbaru: [audit mendalam 21 Oktober 2026](docs/LATENCY_AUDIT_20261021.md), dari penerimaan link sampai hasil publikasi terdeteksi. Berisi pengamatan produksi, empat probe offline, prioritas optimasi, dan batas buktinya; optimasi baru belum diterapkan. [Audit ulang 18 Oktober](docs/AUDIT_20261018.md), [rencana perbaikannya](docs/IMPROVEMENT_PLAN.md), dan [audit 16 Oktober](docs/AUDIT_IMPROVEMENT_20261016.md) adalah arsip; status implementasi masing-masing mengikuti worklog.

Credential, runtime, media, dan konfigurasi mesin tidak masuk Git. Repo harus tetap private.

Laporan TikTok pukul 08.00 WIB memantau semua akun Repliz yang diizinkan: sudah mencapai **600 followers**, belum mencapai, dan data belum tersedia. Ringkasan PNG menampilkan bar menuju target serta tren snapshot harian; daftar teks mencakup seluruh akun, kekurangan followers, dan perubahan harian/7 hari bila baseline tersedia. Akun tepat 600 termasuk tercapai. Pengambilan tetap 07.50, kirim 08.00 Asia/Jakarta, melalui worker yang sama ke private admin. [Operasi laporan](docs/OPERATIONS.md#laporan-tiktok-harian) dan [desain laporan](docs/FOLLOWER_DASHBOARD.md) menjelaskan preview, status, dan pengiriman. Default config tetap disabled; produksi sudah aktif. Bukti baca 21 Oktober menunjukkan run otomatis 18–21 Oktober diterima Telegram; statistik 18 Oktober parsial. [Audit 18 Oktober](docs/AUDIT_TIKTOK_REPORT_20261018.md) dan [rencana awal](docs/TIKTOK_DAILY_REPORT_PLAN.md) tetap arsip kebijakan lama >600.
