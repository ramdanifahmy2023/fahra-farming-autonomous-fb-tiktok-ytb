# Audit workflow TikTok untuk paket tiga platform

Tanggal: 16 Oktober 2026. Tahap: audit dan rencana, tanpa deployment.

Sumber yang diperiksa: `/Volumes/DATA KERJA /Project Dev/Peternakan-akun`, commit `9c452a93e84e0b1257a778c6eab15edd75e1e962`. Working tree sumber bersih sebelum dan sesudah audit. Nomor baris di bawah merujuk commit tersebut. Audit membaca AGENTS, memory, tools, WORKFLOW_PLAN, worklog terbaru, kontrak reply/cleanup, source, dan tes. Tidak mengubah antrean, konfigurasi, layanan, akun, atau posting produksi.

Keputusan paket baru: satu Reel menghasilkan satu tujuan TikTok, satu Facebook Page, dan satu YouTube Short; maksimum 20 video per akun per hari WIB. Paket menggantikan dua workflow lama setelah pemeriksaan lulus. Kuota lima pada source lama tetap dibiarkan selama tahap ini. `iq7fanspage` tetap dikecualikan.

Snapshot GET account oleh koordinator pada 10.26 WIB membaca lengkap 136 record; target yang memenuhi filter ialah 46 TikTok, 14 Facebook, dan empat YouTube. Jumlah ini bukan jaminan koneksi atau kapasitas yang tetap. Credential Repliz paket baru sama dengan kedua workflow lama, sehingga pemisahan bot tidak memisahkan risiko publikasi ke akun yang sama.

## Komponen yang dapat digunakan kembali

| Komponen | Bukti kode saat ini | Batas penggunaan kembali |
| --- | --- | --- |
| Antrean, reservasi atomik, rotasi persisten, event, outbox, backup SQLite | `peternakan/db.py:34`, `:95`, `:134`, `:140`, `:206` | Model sekarang satu sumber untuk satu tujuan TikTok. |
| Intent sebelum POST dan rekonsiliasi tanpa kirim ulang buta | `peternakan/pipeline.py:74`; `peternakan/repliz.py:128`; `peternakan/db.py:134` | Pertahankan `submit_unknown` dan marker historis saat migrasi. |
| MCP, analisis, render, Drive dengan checksum penuh | `peternakan/downloader.py:39`; `peternakan/pipeline.py:35`; `peternakan/drive.py:161` | Preview nyata tetap diperlukan untuk integrasi paket baru. |
| Konteks video, draft reply persisten, kuota kirim, preflight dan rekonsiliasi | `peternakan/replies.py:131`; `peternakan/reply_store.py:90`, `:113` | Baru terbukti untuk kontrak TikTok; bukan adapter komentar tiga platform. |
| Cleanup dengan retensi, checksum, referensi lintas workspace dan shared lock | `peternakan/cleanup.py:167`, `:332` | Struktur tujuan harus diperluas menjadi tiga dan seluruh root relevan didaftarkan. |

## Celah teruji dengan fixture offline

Ketiga reproduksi memakai database sementara terpisah, tanpa credential dan tanpa API nyata. Fixture dihapus otomatis. Temuan membuktikan perilaku kode, bukan bahwa pelanggaran sudah terjadi pada produksi.

### P1: akun baru dapat dialokasikan sebelum kuota remote diketahui

`peternakan/repliz.py:75–85` menyimpan akun sebelum GET jadwal selesai. `peternakan/pipeline.py:18–20` masih menganggap timestamp sinkronisasi sebelumnya fresh selama 600 detik.

Reproduksi: mulai dari satu akun dan timestamp fresh; GET akun berikutnya menambahkan akun kedua; GET jadwal ditahan lalu dibuat gagal. Dua link dimasukkan selama jeda tersebut. Hasil: akun kedua memperoleh reservasi dan `fresh_sync` tetap true sesudah kegagalan jadwal.

Usulan: commit akun, jadwal, dan timestamp sebagai satu snapshot setelah semua pembacaan berhasil. Akun baru belum eligible sebelum kuotanya diketahui. Lulus bila kegagalan atau keterlambatan salah satu GET tidak membuka alokasi berdasarkan snapshot campuran.

### P1: perubahan tanggal jadwal remote menyebabkan kuota kurang dihitung

`peternakan/db.py:200–202` mengabaikan jadwal remote yang cocok ID/title tanpa mencocokkan tanggal lokal. Monitor pada `peternakan/pipeline.py:136–145` tidak menyelaraskan `slot_date` dengan perubahan remote.

Reproduksi: satu job lokal mempunyai slot kemarin; schedule remote dengan ID sama dipindah ke hari ini. Hasil: beban hari ini terbaca nol, kemudian lima job baru mendapat slot hari ini. Total remote dan lokal menjadi enam pada batas lama lima.

Usulan: rekonsiliasi tanggal reservasi terhadap jadwal remote, lalu hitung satu kali pada hari yang benar. Lulus bila pemindahan tanggal, pergantian WIB, dan publikasi tertunda tidak membuka slot berlebih pada batas baru 20. Bedakan waktu jadwal dari waktu publikasi aktual bila API menyediakan keduanya.

### P1: hash duplikat melewati job gagal yang masih mempunyai jadwal remote

`peternakan/db.py:328–331` mengabaikan semua status failed. Sebaliknya, `peternakan/pipeline.py:143–146` mempertahankan kuota remote error sampai operator memastikan hasilnya.

Reproduksi: job failed masih memiliki `schedule_id`, hash sumber, dan `counts_quota=1`; job kedua berasal dari URL berbeda dengan hash sama. Hasil: `duplicate_hash=False`.

Usulan: pertahankan deduplikasi untuk media dengan intent/jadwal remote belum selesai direkonsiliasi. Lulus bila URL alternatif tidak dapat melewati penahanan tersebut; pelepasan memerlukan hasil remote yang pasti.

## Penyesuaian paket baru yang belum diimplementasikan

Nama file singkat pada bagian ini berada di direktori `peternakan/` sumber audit.

- **Kuota dan model data:** angka lima tertanam pada `config.py:37–38`, `db.py:210`, `:296`, `:343`, serta `telegram.py:18`. Jadikan batas 20 satu sumber konfigurasi, dihitung per platform/akun/hari WIB. Pisahkan asset dari tiga tujuan; `db.py:48–56` sekarang menyimpan `source_id UNIQUE` dan satu akun. Keberhasilan satu tujuan tidak boleh memicu pengulangan tujuan lain.
- **Akun baru dan disconnect:** video memakai seluruh akun yang lolos filter (`db.py:156–171`, `config.py:84–89`), sedangkan reply memakai daftar aktivasi (`replies.py:83–87`). Tentukan satu kebijakan enrollment yang mencakup koneksi, pengecualian, persona, serta kesiapan automasi.
- **Persona:** seluruh akun membaca satu profil Gen Z (`reply_ai.py:193–198`). Persona berbeda memerlukan pemetaan akun ke profil dan versi; simpan pilihannya bersama draft. Fakta tetap berasal dari video tersebut.
- **Nested reply:** `replies.py:39` menyimpan parent_id, tetapi filter tidak memakainya; input AI pada `reply_ai.py:194–195` hanya menerima teks komentar. Fixture komentar anak lolos prefilter. Baca riwayat induk atau tahan komentar anak secara deterministik bila riwayat belum tersedia.
- **Retry reply:** counter bertambah saat claim termasuk menunggu kuota (`reply_store.py:79`, `:124–126`); exception setelah tiga claim masuk review (`replies.py:237–238`). Pisahkan penundaan kuota, gangguan jaringan, dan kegagalan kualitas isi.
- **Cleanup:** `cleanup.py:170–171` mensyaratkan tepat dua tujuan Facebook/YouTube pada asset. Paket baru harus menunggu seluruh tiga tujuan published dan retensi sejak tujuan terakhir. Periksa referensi paket baru serta kedua arsip lama; pertahankan shared MCP lock, enam frame, transkrip, konteks, Bank konten, dan Drive.
- **Diagnosis:** `util.py:53–60` hanya memakai string exception; timeout dapat menghasilkan error kosong, seperti worklog 16 Oktober. Simpan tipe exception, subsistem, keberhasilan terakhir, dan retry berikutnya tanpa rahasia.

## Pemeriksaan dan gerbang penggantian

Perintah baseline: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v`. Hasil: 124 tes dijalankan, 123 lulus dan satu skipped karena tes pasangan cleanup khusus FB/YouTube. Ada ResourceWarning koneksi SQLite fixture tidak tertutup; tidak menggagalkan suite. Kelulusan suite tidak menutup tiga celah yang direproduksi terpisah di atas.

Sebelum cutover, wajib lulus regresi untuk batas 20/21, batch bersamaan, restart, akun baru/disconnect, perubahan tanggal remote, partial success tiga tujuan, dan semua status unknown. Jalankan preview nyata media/AI/Drive tanpa publikasi, serta uji konteks/persona/thread dan rekonsiliasi reply.

Migrasi harus mempertahankan histori, deduplikasi, reservasi, intent, marker, post mapping, konteks, dan waktu aktivasi reply. Rekonsiliasi jadwal lama; hentikan dua pengirim lama sebelum paket baru memperoleh hak kirim ke akun yang sama. Sesudah cutover, bedakan jadwal diterima, publikasi terverifikasi tiap platform, notifikasi, dan kuota. Tidak menambahkan persetujuan manual rutin per video. Seluruh perubahan di bagian usulan masih rencana; audit ini tidak melakukan deployment.
