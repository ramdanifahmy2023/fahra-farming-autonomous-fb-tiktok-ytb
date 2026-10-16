# Audit Facebook, YouTube, dan downloader

Tanggal pemeriksaan: 16 Oktober 2026. Dokumen ini berisi audit dan rencana, bukan aktivasi fitur. Keputusan pengguna: satu Reel menjadi satu kiriman TikTok, satu Facebook Page, dan satu YouTube Short; maksimal 20 video per akun per hari WIB. Paket baru menggantikan dua workflow lama setelah pengujian lulus. Kuota dan layanan lama tidak diubah oleh audit ini.

## Baseline dan batas pemeriksaan

Rujukan kode di bawah memakai dua awalan:

- `FBYT`: `/Volumes/DATA KERJA /Project Dev/Peternakan-fb-ytb`, main bersih pada `9cf41dcad06e97b9c4ec3ffdb4a1b5a55f5dc940`.
- `MCP`: `/Users/fahra/Projects/instagram-downloader-mcp`, instalasi tanpa repositori Git. SHA-256 `mcp_service/jobs.py`: `6aa86fa2d3e9d057db393c5f92fe50e4cc52364fb6808e539f9460ec3069bb15`.

AGENTS, memory, tools, kontrak workflow, worklog terbaru, dan audit 15 Oktober telah dibaca. Pemeriksaan source, Git, LaunchAgent, dan SQLite memakai akses baca. Tidak ada polling kedua, publikasi, perubahan akun, atau perubahan konfigurasi produksi.

Snapshot DB pada sekitar 10:21 WIB: nol asset/job, outbox kosong, 14 Facebook dan empat YouTube connected. LaunchAgent running; ini belum membuktikan publikasi pertama. Sepuluh event kegagalan polling terbaru memiliki pesan error kosong. Sinkronisasi akun/jadwal tercatat selesai 10:18:59 WIB. Freshness sempat null saat dibaca; nilai tersebut juga dipakai selama sync berlangsung, sehingga bukan bukti kemacetan permanen.

Pemeriksaan GET akun oleh koordinator pada 10:26 WIB mengumpulkan 136 record lengkap. Pool terpilih: 46 TikTok, 14 Facebook, empat YouTube, semuanya connected; TikTok yang dikecualikan pengguna tidak termasuk. Kapasitas nominal paket baru: 920/280/80 kiriman per hari, atau paling banyak 80 Reel selesai pada ketiga platform per hari, sebelum pemakaian kuota lain.

Folder Drive baru baru terbukti memberi halaman HTML HTTP 200 dengan ID folder. Belum ada uji tulis, pencocokan file, checksum, atau tautan MP4 langsung untuk paket baru. Bukti preview Drive workflow lama tidak mengesahkan folder baru.

## Temuan audit lama yang masih berlaku

Diff dari baseline audit `e80c25c` sampai baseline sekarang hanya menambahkan cleanup, penyesuaian CLI, dokumentasi, dan tes terkait. Jalur Telegram, database inti, Repliz, pipeline, dan Drive tidak berubah. Delapan probe audit lama tidak dijalankan ulang pada pemeriksaan ini; bukti lamanya tersedia di `FBYT/docs/AUDIT_WORKFLOW_2026-10-15.md`.

| Temuan | Status dan akibat | Rujukan source |
| --- | --- | --- |
| A01, P1: error permanen Telegram | Belum diperbaiki. Callback HTTP 400 menahan kemajuan offset; outbox 400 menahan pesan berikutnya. | `FBYT/peternakan/telegram.py:197`, `:221` |
| A02, P1: snapshot tidak lengkap | Belum diperbaiki. Total pagination opsional; jadwal tanpa status dapat dilewati setelah snapshot lama dibuang. Pool/kuota dapat menjadi salah. | `FBYT/peternakan/repliz.py:73`, `:122`; `db.py:212` |
| A03, P1: akun berubah sebelum intent | Belum diperbaiki. Sync dapat melepas reservasi ketika preflight menunggu jaringan; payload lama lalu disimpan tanpa transaksi pemeriksaan ulang. | `FBYT/peternakan/pipeline.py:29`; `db.py:194`, `:204` |
| A04, P1: Drive berubah setelah persiapan | Belum diperbaiki. Pra-POST hanya memeriksa penanda MP4 awal, bukan checksum asset. | `FBYT/peternakan/pipeline.py:34`; `drive.py:218` |
| A05, P1: restore backup lama | Belum diperbaiki. Backup sebelum intent tidak mengetahui POST sesudah backup; recovery belum merekonsiliasi kehilangan tersebut. | `FBYT/peternakan/db.py:145`, `:151` |
| A06, P1: publikasi pertama | Belum ada bukti; DB operasional tetap kosong pada snapshot audit. | Snapshot read-only di atas |
| A07, P2: kesehatan dan penyelesaian kasus | Belum lengkap. Unknown dapat menunggu terus; error kosong menyulitkan diagnosis. | `FBYT/peternakan/pipeline.py:140`; `db.py:418`; `util.py:53` |
| A08, P2: input publik dan penyimpanan | Retensi sudah diimplementasikan. Batas pengirim, backlog global, dan disk belum tersedia. | `FBYT/peternakan/cleanup.py`; `telegram.py:115` |
| A09, P2: kelayakan media | Profil dan matriks belum lengkap. Sumber 3–600 detik dan berbagai rasio masih diterima. | `FBYT/peternakan/media.py:24`, `:55` |

P1 perlu ditutup sebelum cutover. A09 juga menjadi syarat cutover karena target pengguna kini secara eksplisit YouTube Shorts. Ketentuan platform terbaru dibahas dalam audit platform terpisah.

## Probe baru: unknown tanpa percobaan POST

`pipeline.py:52` menyimpan submitting, kemudian `repliz.py:170` mengulang GET pemeriksaan akun. POST baru terjadi pada baris 171. Timeout GET kedua ditangkap sebagai submit_unknown oleh `pipeline.py:69`, walaupun pengiriman belum dicoba.

Probe baru memakai `Pipeline.step` dan `Repliz.create` asli dengan objek tiruan di memori, tanpa jaringan atau DB. Hasil: dua GET preflight, nol POST, status tersimpan submit_unknown, payload submission ada. Ini reproduksi baru, bukan hasil publikasi nyata.

Perbaikannya harus membedakan kegagalan sebelum POST dari hasil POST yang tidak pasti. Tes penerimaan: GET gagal boleh dijadwalkan ulang secara aman; timeout setelah POST tetap direkonsiliasi tanpa retry buta.

## Kontrak downloader dan media

`MCP/README.md` dan implementasi menetapkan kontrak berikut:

- Server stdio lokal melalui `start-mcp.sh`; file berada pada Mac yang menjalankannya. API tersedia untuk health, inspect, start, dan status.
- Link langsung `/p/`, `/reel/`, `/reels/`, `/tv/`; Stories/profil tidak didukung. `max_items` 1–50; status dapat menunggu 0–20 detik.
- Job persisten di `downloads/mcp/<uuid>/`, dengan status completed/partial/failed, media, caption, dan log. Container menjalankan fungsi downloader tanpa memulai polling Telegram (`MCP/mcp_service/docker_worker.py:19`).
- Lock container membatasi satu ekstraksi/download MCP lintas sesi. Timeout download 600 detik termasuk menunggu lock; inspect 40 detik; job lewat 900 detik dianggap gagal (`MCP/mcp_service/jobs.py:136`, `:156`).
- Penyalinan memeriksa keberadaan, symlink, dan ukuran; belum membandingkan checksum asal dengan salinan (`MCP/mcp_service/jobs.py:163`).

Adapter proyek meminta satu file dan menerima partial bila tepat satu file tersedia (`FBYT/peternakan/downloader.py:43`). Crash sesudah start tetapi sebelum penyimpanan ID dapat membuat download kedua karena start belum idempotent. Cleaner hanya menerima manifest completed (`cleanup.py:232`), sehingga partial yang diterima pipeline tetap tertahan saat cleanup. Keduanya perlu kontrak eksplisit pada paket baru.

Render memakai mirror, zoom 110%, hook `[0,3)`, H.264/yuv420p, AAC bila sumber memiliki audio, dan faststart; dimensi sumber dipertahankan. Ukuran hasil dibatasi 1 GB (`FBYT/peternakan/media.py:55`). Target Shorts memerlukan validasi durasi/rasio terpisah, bukan sekadar mengganti label YouTube.

Dependensi: Python proyek minimal 3.11, MCP SDK versi 1, httpx, Google API/Auth; Docker/container dengan Python, timeout, yt-dlp, ffmpeg; host ffmpeg/ffprobe dan Whisper lokal. Versi container, model Whisper, serta instalasi MCP perlu manifest deployment. Cadangan `MCP/recovery/bot.py` bukan source MCP aktif; jangan rebuild dari proyek asal eksternal yang tercatat mengandung byte NUL.

## Drive dan cleanup

Mode synced_public menyalin ke Bank konten, mencari ID melalui folder publik, lalu membandingkan seluruh SHA-256 (`FBYT/peternakan/drive.py:161`). Pemeriksaan pra-POST hanya membaca bagian awal (`:218`); perbaikan A04 tetap diperlukan untuk setiap tujuan, termasuk yang menunggu besok.

Mode API alternatif benar-benar mengunggah file (`drive.py:123`); jangan mengaktifkannya sebagai jalur kedua untuk media yang sudah disinkronkan Desktop. Folder baru wajib preview nyata tanpa publikasi yang membuktikan satu file, ID, ukuran, checksum, dan URL MP4.

Cleaner memeriksa checksum remote penuh, retensi 24 jam sejak tujuan terakhir published, enam frame, dan referensi kedua workspace/preview. Referensi dibaca ulang setelah I/O jaringan (`cleanup.py:276`, `:347`). Namun jumlah tujuan masih diwajibkan tepat dua (`:170`).

## Migrasi asset bersama, tiga delivery, dan kuota 20

Basis yang layak dipakai ulang: asset bersama, checkpoint media, dedupe sumber/batch, cursor akun, reservasi kuota, intent, dan rekonsiliasi. Jangan memasang kedua paket lama dalam satu venv: keduanya memakai namespace Python `peternakan`.

Migrasi harus mencakup:

1. Platform dan limit menjadi konfigurasi tervalidasi. Saat ini `config.py:38` menolak selain lima; `db.py:254`, `:375`, `:377`, `:424`, `:427` memakai lima langsung.
2. Satu asset dengan tiga delivery dan lease asset tersendiri. Constraint platform masih hanya Facebook/YouTube (`db.py:42`, `:55`). Tujuan wajib disimpan saat ingest, termasuk alasan bila tujuan tertahan.
3. Antrean independen: untuk 100 Reel dengan kuota kosong, TikTok/FB dapat menerima 100 hari ini; YouTube 80 hari ini dan 20 berikutnya. Rotasi bertahan setelah restart.
4. Cleaner menunggu seluruh tujuan wajib yang tersimpan, bukan angka dua/tiga hardcoded. Histori, preview, intent, dan referensi kedua workflow lama tetap dilindungi selama migrasi.
5. Cutover menetapkan satu pemilik polling dan publikasi, mengimpor histori/kuota secara teruji, serta merekonsiliasi intent lama sebelum aktivasi. Restore/rollback tidak boleh mengulang POST.

## Gerbang penerimaan

Tambahkan regresi A01–A05 dan probe preflight baru; uji sync/disconnect/kuota eksternal/tengah malam pada batas await. Uji perubahan Drive setelah drive_ready, restore sebelum/sesudah intent, dan crash saat MCP start. Buktikan satu download/analisis/render/file Drive untuk tiga delivery, limit 20 bertahan setelah restart, akun yang dikecualikan tidak tersentuh, serta cleanup menunggu tujuan terakhir.

Sesudah suite, preview media/AI/Drive, dan latihan cutover lulus, ikuti satu Reel pengguna sampai tiga posting nyata beserta laporan Telegram. Jadwal diterima, preview berhasil, dan layanan running harus tetap dibedakan dari video terbit. Audit ini belum menjalankan gerbang implementasi tersebut.
