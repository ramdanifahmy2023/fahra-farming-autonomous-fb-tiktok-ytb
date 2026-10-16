# Fahra Automation TikTok, Facebook, dan YouTube

Status 16 Oktober 2026: audit dan rencana implementasi. Paket layanan belum dibuat atau diaktifkan.

Tujuan: satu link Reel Instagram menghasilkan satu video untuk satu akun TikTok, satu Facebook Page, dan satu channel YouTube Shorts. Kuota yang dipilih pengguna adalah maksimal 20 video per akun per hari WIB. Paket baru akan menggantikan dua workflow lama setelah pengujian lulus.

Satu paket berarti satu bot, satu model antrean, satu registry akun/persona, dan satu layanan pengendali. Downloader MCP dan Drive Desktop yang sudah tersedia tetap menjadi dependensi eksternal; ini belum merupakan installer mandiri.

Akun baru pada ketiga platform otomatis ikut rotasi setelah snapshot akun dan kuotanya lengkap. Akun putus dilewati tanpa menghapus histori. Akun TikTok iq7fanspage tetap dikecualikan.

Mulai membaca dari [rencana implementasi](docs/PLAN.md), kemudian [audit dan bukti](docs/AUDIT.md). Dokumen pendukung:

- [Kemampuan per platform](docs/PLATFORM_CAPABILITIES.md), termasuk batas native automation dan 9Router.
- [Tes penerimaan](docs/ACCEPTANCE.md) dan [migrasi dua workflow lama](docs/MIGRATION.md).
- [Aturan agent](AGENTS.md), [tools dan akses](tools.md), [keputusan tersimpan](memory.md), [jejak kerja](worklog.md).

Pemeriksaan dokumen lokal: `python3 scripts/verify_plan.py`. CI hanya memeriksa paket rencana ini, bukan menguji worker publikasi yang belum dibuat.

Credential dan bank konten lokal sudah disiapkan pengguna dan diabaikan Git. Tidak ada token, database produksi, atau video di repo. Layanan lama tetap beroperasi; paket ini belum menjalankan polling, membuat jadwal, atau mengubah automasi akun.
