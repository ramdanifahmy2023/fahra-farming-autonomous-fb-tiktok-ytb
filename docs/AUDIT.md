# Audit paket terpadu, 16 Oktober 2026

Kesimpulan: model satu asset dengan delivery per platform dari proyek FB/YT layak menjadi dasar, ditambah caption dan reply kontekstual TikTok. Dua service lama tidak cukup digabung dalam satu proses. Schema, kuota, batas transaksi, migrasi, capability platform, dan kepemilikan automasi perlu dirancang ulang sebelum aktivasi.

## Sumber dan metode

Audit source dilakukan pada TikTok commit `9c452a93e84e0b1257a778c6eab15edd75e1e962` dan FB/YT `9cf41dcad06e97b9c4ec3ffdb4a1b5a55f5dc940`. Kedua checkout bersih. Audit membaca dokumen/kode, snapshot SQLite mode read-only, CLI status, event terpilih, LaunchAgent, serta GET integrasi. Tiga pemeriksa bekerja paralel untuk TikTok, FB/YT+MCP, dan dokumentasi resmi platform. Tidak ada perubahan source/config/DB/layanan lama dari audit ini.

Laporan rinci dan batas bukti:

- [Audit TikTok](AUDIT_TIKTOK.md): komponen reusable, tiga regresi yang direproduksi pada fixture, serta suite baseline.
- [Audit FB/YT dan downloader](AUDIT_FB_YT_DOWNLOADER.md): status audit lama, satu reproduksi baru preflight, kontrak MCP/Drive/media.
- [Kemampuan platform](PLATFORM_CAPABILITIES.md): sumber resmi, perbedaan native automation, batas media dan jadwal.

## Akses baru dan kondisi operasi

GET integrasi 16 Oktober 10:26 WIB berhasil untuk Telegram, Repliz, 9Router, dan halaman folder Drive. Repliz memakai dua halaman dengan total 136 record; 46 TikTok, 14 Facebook Page, dan 4 YouTube terhubung dan termasuk lingkup. iq7fanspage ada di inventaris tetapi dikecualikan. Record platform lain tidak ikut rotasi.

Bot baru @Fahra_autonomousbot berbeda dari dua bot lama, webhook kosong, pending update nol saat probe. Tidak memanggil getUpdates. Model combo codexx dan cx/gpt-6.1-sol terdaftar di 9Router; belum ada request generasi. Format kredensial hyphen tidak cocok dengan parser lama. Drive HTTP 200 dan ID folder cocok pada HTML, tetapi belum ada uji sinkronisasi, file MP4 publik, atau checksum paket baru.

Snapshot layanan lama sekitar 10:22–10:24 WIB: TikTok running PID 1636, 129 published dan 29 failed; FB/YT running PID 1506, nol job. Polling Telegram masih mencatat kegagalan, sementara sinkronisasi Repliz FB/YT kembali mencatat sukses. Jangan menganggap maintenance pagi masih berlangsung atau sudah pulih seluruhnya hanya dari satu probe GET.

Bukti akses tanpa token: runtime/verification/planning-20261016/access.json pada root baru. Angka akun, kapasitas, dan status adalah snapshot yang harus dibaca ulang sebelum implementasi/cutover.

## Temuan yang menentukan urutan implementasi

| Prioritas | Masalah | Dampak dan keputusan rencana |
| --- | --- | --- |
| P1 | Akun diterapkan sebelum snapshot jadwal lengkap | Akun baru bisa dialokasikan tanpa kuota eksternal; gunakan satu snapshot atomik dan generation |
| P1 | Jadwal remote berpindah tanggal tidak dihitung benar | Batas harian bisa terlampaui; rekonsiliasi slot berdasarkan waktu remote dan publikasi aktual |
| P1 | GET kedua setelah status submitting gagal sebelum POST | Pekerjaan dapat tertahan unknown tanpa pernah dikirim; pisahkan preflight, intent, dan transport |
| P1 | Backup lama dapat kehilangan intent; dedup failed terlalu longgar | Posting ulang setelah restore/hasil remote error; perlu jurnal attempt dan rekonsiliasi sebelum kirim |
| P1 | Drive hanya diperiksa sebagian sebelum POST | File berubah dapat lolos; checksum penuh sebelum setiap tujuan dikirim |
| P1 | Telegram 400 menahan update/outbox berikutnya | Satu pesan gagal bisa menahan batch lain; pisahkan error permanen, backoff, dan penerimaan durable |
| P1 | Dua pemilik posting/reply saat migrasi | Akun sama dapat menerima aksi ganda; cutover terukur dan satu pemilik per akun/post |
| P2 | Kuota lima, platform, dan jumlah tujuan hardcoded | Ganti model menjadi data-driven 20 dan daftar tujuan asset; uji seluruh summary/HELP/cleanup |
| P2 | Persona global, thread induk belum dibaca | Balasan anak dapat tidak nyambung; snapshot persona serta konteks video/thread wajib |
| P2 | Auto-enrollment posting dan reply berbeda | Akun baru mendapat posting tetapi automasi tertinggal; registry onboarding dengan capability dan status terpisah |
| P2 | Input/media dan disk belum dibatasi memadai | Stok video serta backlog YouTube dapat membesar; admission budget dan render mendekati jadwal |
| P2 | Error timeout string kosong | Proses terlihat hidup tanpa kesehatan loop; simpan kelas error dan waktu sukses terakhir |

Tes tambahan mereproduksi celah pada fixture, bukan membuktikan kejadian pada akun produksi. Audit FB/YT lama tidak dijalankan ulang seluruhnya; source terkait identik dan bukti lama dirujuk dengan label historis. Retensi cleanup telah diperbaiki pada dua proyek lama, tetapi masih perlu digeneralisasi untuk tiga tujuan dan arsip migrasi.

## Konsekuensi limit 20 dan akun otomatis

Kapasitas nominal pada inventaris sekarang adalah TikTok 920, Facebook 280, YouTube 80 delivery per hari. Batas ini menghitung 20 dikali akun eligible, tanpa menganggap provider selalu menerima sebanyak itu. TikTok Direct Post mendokumentasikan batas bervariasi yang umumnya sekitar 15 per 24 jam; jalur dan batas aktual akun Repliz perlu dibuktikan. Gunakan configured_limit=20 dan penundaan ketika provider memberi batas lebih rendah. [Panduan TikTok](https://developers.tiktok.com/docs/en/content-sharing-guidelines).

Satu link wajib tiga platform berarti laju penyelesaian lengkap dibatasi YouTube. Akun baru menambah kapasitas setelah snapshot lengkap; disconnect menguranginya. Antrean per tujuan, perhitungan tanggal WIB, dan ringkasan backlog diperlukan agar angka 20 tidak dianggap target yang selalu tercapai.

## Yang belum dibuktikan

Belum ada worker baru, generasi persona, preview render, checksum Drive baru, publikasi tiga platform, hasil Shorts, atau aksi komentar melalui paket baru. Dukungan native Repliz pada dokumen tidak membuktikan akses paket/permission akun pengguna. Read-only audit ini tidak mengubah profil automasi remote. Fase implementasi dan bukti yang dibutuhkan ada di [PLAN.md](PLAN.md) dan [ACCEPTANCE.md](ACCEPTANCE.md).
