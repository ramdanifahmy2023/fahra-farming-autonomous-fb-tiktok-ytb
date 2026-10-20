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

Audit terbaru dengan penjelasan sederhana: [audit ulang 18 Oktober 2026](docs/AUDIT_20261018.md) dan [rencana perbaikan](docs/IMPROVEMENT_PLAN.md). [Audit 16 Oktober](docs/AUDIT_IMPROVEMENT_20261016.md) tetap menjadi arsip. Permintaan terbaru fokus audit/plan; perbaikan dalam dokumen tersebut belum diimplementasikan.

Credential, runtime, media, dan konfigurasi mesin tidak masuk Git. Repo harus tetap private.

Permintaan laporan TikTok >600 follower pukul 08.00 WIB: [audit Repliz nyata 18 Oktober](docs/AUDIT_TIKTOK_REPORT_20261018.md) dan [rencana laporan beserta format/pengujian](docs/TIKTOK_DAILY_REPORT_PLAN.md). Laporan sudah tersedia melalui worker yang sama: pengambilan 07.50, kirim 08.00 Asia/Jakarta, hanya private admin. [Operasi laporan](docs/OPERATIONS.md#laporan-tiktok-harian) mencakup preview, status, enable/disable, dan pilot. Default config tetap disabled; produksi telah diaktifkan dan pilot message_id 416 diterima Telegram pada 18 Oktober. Run otomatis 08.00 belum dibuktikan oleh pilot.
