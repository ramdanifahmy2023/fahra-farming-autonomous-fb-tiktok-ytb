# Kemampuan platform dan batas integrasi

Tanggal pemeriksaan: 16 Oktober 2026. Fase: **AUDIT/PLAN**. Dokumen ini tidak mengaktifkan worker, membuat jadwal, atau mengubah akun. “Terkonfirmasi dokumentasi” berarti sumber resmi menyebut kemampuan tersebut; keberhasilan pada akun proyek tetap harus dibuktikan melalui pengujian nyata.

Rancangan yang disepakati: **satu Reel menghasilkan satu tujuan TikTok, satu Facebook Page, dan satu YouTube Short**, dengan batas internal maksimal **20 video per akun per hari WIB**. Paket baru menggantikan dua paket lama setelah pengujian lulus dan perpindahan dilakukan sesuai [rencana](PLAN.md), [kriteria penerimaan](ACCEPTANCE.md), dan [migrasi](MIGRATION.md).

## Matriks kemampuan melalui Repliz

| Kemampuan | TikTok | Facebook Page | YouTube |
|---|---|---|---|
| Publikasi terjadwal | `type: video` | `type: video` atau `reel` | `type: video`; tidak ada tipe `shorts` |
| Native reply teks, keyword, AI | Per akun atau posting | Per akun atau posting | **Hanya per posting** |
| Native like komentar | Didukung | Didukung | **Tidak didukung** |
| Native moderasi keyword/AI | “Delete” berarti **hide** | Dokumentasi menyebut delete | Per posting; semantik penghapusan perlu diuji |
| Webhook komentar | Ada | Ada | **Tidak ada** |
| Webhook hasil jadwal | Ada | Ada | Ada |
| Uji nyata di paket baru | Belum dilakukan | Belum dilakukan | Belum dilakukan |

Sumber publikasi: [TikTok](https://docs.repliz.com/tutorial/specification/tiktok), [Facebook](https://docs.repliz.com/tutorial/specification/facebook), [YouTube](https://docs.repliz.com/tutorial/specification/youtube). Sumber otomasi: [reply](https://docs.repliz.com/tutorial/automation/reply-comment), [like](https://docs.repliz.com/tutorial/automation/like-comment), [moderasi](https://docs.repliz.com/tutorial/automation/delete-comment), [webhook](https://docs.repliz.com/tutorial/webhook).

## Media, caption, dan hashtag

| Tujuan | Kontrak dokumentasi yang harus divalidasi |
|---|---|
| TikTok melalui Repliz | Video 3 detik–10 menit, maksimal 1 GB; 9:16 disarankan; caption maksimal 2.200 karakter termasuk hashtag/mention. Durasi yang benar-benar diizinkan dapat berbeda per akun. |
| Facebook Reel melalui Repliz | MP4/MOV, H.264/AAC, 3–90 detik, 9:16, 24–60 fps; resolusi 1080 × 1920 disarankan, minimal 540 × 960. Caption maksimal 5.000 karakter. |
| YouTube melalui Repliz | MP4 H.264/AAC disarankan; judul wajib maksimal 100 karakter, deskripsi 5.000 karakter; `additionalInfo.tags` maksimal total 500 karakter. |
| Klasifikasi YouTube Shorts | Video persegi atau vertikal sampai tiga menit untuk unggahan baru pada kanal standar. Video vertikal saja tidak cukup untuk menjamin klasifikasi Shorts. |

Sumber: spesifikasi Repliz [TikTok](https://docs.repliz.com/tutorial/specification/tiktok), [Facebook](https://docs.repliz.com/tutorial/specification/facebook), [YouTube](https://docs.repliz.com/tutorial/specification/youtube), serta [aturan Shorts YouTube](https://support.google.com/youtube/answer/15424877?hl=en).

Rencana: hasil analisis sumber dapat dipakai bersama, tetapi payload caption/judul/hashtag divalidasi per tujuan. Tags YouTube berbeda dari hashtag dalam teks. Pilihan untuk video yang melebihi batas tujuan, misalnya memotong atau melewati tujuan, harus dinyatakan dalam rencana sebelum implementasi; jangan diam-diam menerbitkannya sebagai jenis konten lain.

Meta mengumumkan penyatuan video menjadi Reels tanpa batas panjang/format, tetapi dokumentasi Repliz masih memisahkan `video` dan `reel` dengan batas 90 detik. Batas adapter tidak boleh dinaikkan hanya berdasarkan pengumuman produk Meta; perlu uji atau konfirmasi integrator. [Pengumuman Meta](https://about.fb.com/news/2025/06/making-it-easier-create-videos-facebook/).

## Persona dan pemilik eksekusi reply

Native Repliz menyediakan `reply.type: ai`, `reply.prompt`, dan `isIncludeContentContext`. Konteks yang dijelaskan adalah caption/detail posting, bukan bukti bahwa sistem menonton atau mentranskripsikan seluruh video. AI memerlukan provider sendiri atau saldo token Repliz. Dalam dokumentasi dan schema yang diperiksa, belum ditemukan resource persona tersendiri maupun kontrak konfigurasi `base_url` untuk 9Router. [Automation Configuration](https://docs.repliz.com/api/guides/automation-config), [penggunaan AI](https://docs.repliz.com/tutorial/automation/before-start#using-ai), [schema publik](https://api.repliz.com/public-json).

**Persona 9Router dalam paket baru adalah rancangan custom:** profil versi lokal dan konteks video menjadi masukan model; teks hasilnya dikirim melalui API Repliz. Dukungan 9Router langsung sebagai provider native Repliz belum terverifikasi. Jangan mengaktifkan native reply dan worker custom untuk posting yang sama.

Override otomasi per posting menggantikan seluruh otomasi akun, bukan menggabungkannya. Perubahan reply harus mempertahankan modul like/moderasi yang masih diinginkan. Native hanya memproses komentar baru dan mengabaikan komentar akun sendiri. YouTube diperiksa setiap 30 menit; opsi delay native tidak berlaku. [Aturan otomasi Repliz](https://docs.repliz.com/tutorial/automation/before-start).

## Jadwal, waktu, dan rekonsiliasi

`POST /public/schedule` menerima satu `accountId` dan `scheduleAt` ISO 8601; contoh memakai UTC `Z`, tanpa field timezone terpisah. `templateId` dapat melampirkan template otomasi. Field `replies` adalah thread lanjutan untuk Threads/X, bukan auto-reply atau komentar pertama universal. [Create Schedule](https://docs.repliz.com/api/schedule/create-schedule).

Rencana: kuota harian dihitung dengan `Asia/Jakarta`, sedangkan waktu kirim dikonversi ke UTC. Satu sumber memiliki tiga catatan tujuan dengan status masing-masing agar keberhasilan sebagian tidak memicu publikasi ulang tujuan yang sudah berhasil.

Daftar jadwal mendukung pagination, filter akun/tanggal, dan status `pending`, `process`, `error`, `success`. Tidak ditemukan parameter pencarian marker maupun kontrak idempotency key pada create. Rekonsiliasi marker seperti `pw:<job_id>` berarti membaca halaman relevan dan mencocokkan isinya di klien. Timeout create harus menjadi status tidak pasti sampai rekonsiliasi, bukan langsung diulang. [Get Schedule](https://docs.repliz.com/api/schedule/get-schedule), [schema publik](https://api.repliz.com/public-json).

Webhook hasil jadwal berlaku untuk semua platform, tetapi Repliz mengirim event sekali tanpa retry otomatis. Event dapat tidak berurutan; event saat URL webhook kosong tidak dikirim belakangan. Rancangan perlu verifikasi `x-token`, penyimpanan event, deduplikasi, serta polling sebagai cadangan. Komentar YouTube memerlukan polling. [Webhook Repliz](https://docs.repliz.com/tutorial/webhook).

## Like, moderasi, dan bukti tindakan

API reply antrean memakai ID komentar tersimpan Repliz; Content API memakai ID konten/komentar platform dan `accountId`. Endpoint like hanya mendukung Facebook, TikTok, dan LinkedIn. `GET /public/report` adalah log eksekusi otomasi dengan hasil `success/error`, bukan endpoint melaporkan komentar ke moderator platform. Endpoint Repliz untuk “report spam/abuse” belum ditemukan. [Reply API](https://docs.repliz.com/api/comment/reply-comment), [Like API](https://docs.repliz.com/api/content/like-comment), [Report API](https://docs.repliz.com/api/report/get-report).

Referensi Google membedakan `comments.delete` untuk komentar penulis sendiri dari `setModerationStatus=rejected` untuk menyingkirkan komentar orang lain pada kanal/video milik sendiri. `markAsSpam` tidak lagi didukung. Istilah “delete” Repliz pada YouTube harus diuji terhadap hasil nyata, bukan diasumsikan penghapusan permanen. [Moderasi Google](https://developers.google.com/youtube/data/reference/rest/v3/comments/setModerationStatus?hl=en), [Comments API](https://developers.google.com/youtube/v3/docs/comments).

## Kuota dan batas otomatisasi

**20 video/akun/hari WIB adalah batas internal maksimum, bukan kapasitas yang dijamin provider.** TikTok Direct Post mendokumentasikan batas yang bervariasi, biasanya sekitar 15 posting per akun per 24 jam dan dibagi antar-klien; durasi aktual mengikuti kemampuan creator. Jalur integrasi TikTok yang digunakan Repliz belum dibuktikan dalam audit ini, sehingga angka tersebut tidak otomatis menjadi kontrak Repliz. Sistem harus menerima pembatasan provider yang lebih rendah. [TikTok Guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines), [Creator Info](https://developers.tiktok.com/docs/en/content-posting-api-reference-query-creator-info).

TikTok mensyaratkan kendali/consent pengguna pada Direct Post dan membatasi penggunaan yang sekadar menyalin konten arbitrer dari platform lain. YouTube melarang spam komentar berulang, produksi massal konten serupa, dan repost tanpa kontribusi bermakna. Mirror, zoom, dan hook bukan bukti otomatis bahwa konten memenuhi kebijakan atau syarat monetisasi. [TikTok Guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines), [YouTube Spam Policy](https://support.google.com/youtube/answer/2801973?hl=en), [kebijakan monetisasi](https://support.google.com/youtube/answer/1311392?hl=en).

## Bukti yang masih diperlukan

1. Audit read-only akun, permission, paket API, token, dan override otomasi. Schedule memerlukan Premium+; Content/Report/Automation API memerlukan Gold+ menurut [tingkatan API Repliz](https://docs.repliz.com/api/introduction#api-access-tiers).
2. Preview satu sumber dengan tiga payload valid; periksa media, judul, caption, hashtag, tags, serta batas durasi per tujuan.
3. Tes mock timeout, pagination rekonsiliasi, restart, kuota WIB, pembatasan provider, webhook hilang/duplikat, dan keberhasilan sebagian tujuan.
4. Pilot publikasi sesuai gerbang penerimaan: buktikan ID jadwal, ID konten, URL hidup, waktu terbit, dan klasifikasi Shorts.
5. Komentar uji untuk persona, pencegahan reply ganda, like pada platform pendukung, semantik hide/delete, dan komentar lama yang harus dilewati.
6. Catat konfigurasi tersimpan, request diterima, log sukses, dan hasil yang terlihat di platform sebagai bukti yang berbeda.

Belum ada pengujian nyata maupun aktivasi fitur paket baru dalam fase ini. Seluruh langkah berikutnya mengikuti [ACCEPTANCE.md](ACCEPTANCE.md).
