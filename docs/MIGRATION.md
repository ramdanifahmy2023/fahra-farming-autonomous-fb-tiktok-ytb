# Mengganti dua workflow lama

Keputusan pengguna: paket baru menggantikan TikTok dan FB/YT lama setelah uji lulus. Runbook ini menjadi urutan implementasi yang disetujui pada 16 Oktober. Status pelaksanaan ada di memory/worklog dan bukti runtime; jangan menganggap langkah di bawah sudah dijalankan hanya karena kode tersedia.

## Batas yang harus dipertahankan

Satu akun/post hanya boleh memiliki satu pemilik publikasi dan satu pemilik reply. Native like/moderasi dapat tetap berjalan bila memang ditetapkan sebagai pemilik aksi tersebut. Cutover harus mencakup worker, bot intake, scheduler, reply, cleanup, dan konfigurasi remote; mengganti token Telegram saja tidak cukup.

Target 20 berlaku untuk paket baru. Pemakaian hari berjalan dari workflow lama dan jadwal eksternal tetap mengurangi jatah 20; tidak dimulai dari nol. Identitas akun tidak diganti, iq7fanspage tetap dikecualikan. File Bank lama tetap dirujuk pada lokasi aslinya; tidak mengunggah atau menyalin massal arsip ke Bank baru.

Tiga tujuan berlaku untuk input baru. Asset/pending legacy mempertahankan required_destinations asli: TikTok saja dari proyek TikTok, Facebook+YouTube dari proyek FB/YT. Import tidak membuat backfill platform yang tidak pernah diminta. Jika sumber sama ada pada kedua DB, pertahankan mapping dan revisi artifact masing-masing; gabungkan referensi sumber hanya bila tidak menimpa histori atau memicu kiriman baru. Mengirim ulang URL legacy menampilkan histori/dedupe, bukan otomatis menambahkan tujuan yang belum ada. Backfill membutuhkan pekerjaan terpisah yang memang diminta.

## Tahap migrasi

1. **Inventaris read-only.** Rekam source commit, DB schema, akun/platform, seluruh job/asset/status/slot, remote schedule/post ID, reply pending/unknown, cutoff, context, native account/post overrides, serta semua lokasi media yang masih dirujuk. Cocokkan jadwal remote berhalaman lengkap. Stop pada snapshot ambigu; jangan menganggap kosong.
2. **Simulasi import.** Buat snapshot konsisten dengan SQLite backup API, bukan copy file aktif. Rehearsal berada di runtime staging terlindungi pada checkout baru yang hak kirimnya nonaktif; worktree pengembangan tetap memakai fixture. Import ke DB baru dengan schema version dan mapping `(workflow_asal, legacy_id)`. Jangan menautkan runtime atau menulis DB lama.
3. **Validasi tanpa kirim.** Bandingkan jumlah dan hash record penting, pemakaian kuota per akun/hari, mapping post/context/reply, source/render/bank paths, serta seluruh status unknown. Jalankan shadow scheduler/reconciler yang hanya membaca remote dan menghasilkan usulan. Semua mutation endpoint ditolak oleh mode ini, bukan sekadar janji operator.
4. **Bekukan penerimaan dan owner lama.** Pada tahap cutover yang dijalankan, hentikan intake serta pembentukan intent baru kedua workflow. Tunggu operasi yang bisa selesai; untuk request yang tidak pasti, rekam sebagai unknown dan rekonsiliasi. Bootout dan disable persisten dua LaunchAgent lama agar tidak bangun kembali saat login, lalu verifikasi proses/lease polling serta worker lama benar-benar berhenti. Ambil snapshot final yang konsisten; rehearsal sebelumnya bukan data cutover final.
5. **Import final dan ambil ownership.** Ulang import/delta dengan checksum, rekonsiliasi remote, lalu peroleh lock dan generation pemilik akun paket baru. DB baru tetap menahan submit_unknown/send_unknown yang belum terjawab. Jangan menghapus marker atau membuat jadwal pengganti. Audit remote native reply sebelum memilih custom owner; perubahan profil harus mempertahankan modul lain dan didukung snapshot/diff.
6. **Aktifkan pilot terkendali.** Mulai satu polling bot baru. Cakupan kirim awal dibatasi pilot satu akun eligible per platform, tetap memperhitungkan kuota lama. Gunakan satu link pengguna untuk membuktikan tiga hasil publik dan notifikasi. Konfigurasi komentar yang belum terbukti tetap menunggu capability/pilot; tidak membuat klaim semua platform mendukung aksi identik.
7. **Perluas dan observasi.** Setelah pilot dan tes pemulihan lulus, perluas eligible pool otomatis. Akun tambahan berikutnya mengikuti registry otomatis, tanpa perlu mendaftarkan manual. Simpan laporan health, quota, ownership, dan hasil rekonsiliasi. Daftar root cleanup mencakup ketiga workspace/arsip selama ada referensi lama.

Pilot memakai link baru yang dipilih pengguna. Tidak ada approval manual berulang per video. Eksekusi aktual tetap dicatat dengan hasil verifikasi dan status kepemilikan.

## Data yang ikut dan yang dipisahkan

| Data | Perlakuan |
| --- | --- |
| Account ID, cursor, pemakaian hari berjalan dan jadwal eksternal | Pertahankan dan hitung ulang terhadap snapshot remote terbaru; jangan reset |
| Published jobs, post URLs, schedule IDs, marker historis | Import sebagai histori/delivery legacy; jangan menjadi queued baru |
| Submitting/submit_unknown/scheduled | Pertahankan kepemilikan akun, intent dan slot; rekonsiliasi sebelum tindakan baru |
| Failed dengan remote schedule atau counts_quota | Tetap memblokir dedup/kuota sampai hasil pasti; bukan otomatis retry |
| Reply sent/unverified/unknown, comment IDs dan owner aliases | Pertahankan dedupe dan namespace platform; polling baru tidak membalas ulang |
| Cutoff reply, video contexts, transcript, frames dan persona | Pertahankan konteks; label versi legacy untuk prompt global lama |
| Telegram bot token dan offset | Token baru dibaca dari Credential baru; offset lama tidak disalin ke bot baru |
| Outbox lama yang belum terkirim | Catat kepemilikan bot/chat; drain terkontrol sebelum stop atau tandai hold, jangan kirim via bot baru tanpa pemetaan penerima |
| Worker locks, PID, lease aktif | Tidak diimpor sebagai pemilik hidup; reconcile lease setelah old owner berhenti |
| Media/Bank/Drive lama | Referensi tetap, tidak dipindahkan/dihapus; cleanup baru memeriksa semua registry |

Telegram bot baru tidak otomatis memiliki hubungan chat dengan seluruh pengirim bot lama. Pengguna perlu memulai percakapan dengan bot baru untuk input berikutnya. Rencana notifikasi transisi dan penyelesaian outbox harus dibuat sebelum cutover; audit ini tidak mengirim pesan ke siapa pun.

## Pemulihan jika pilot gagal

Hentikan pembentukan intent baru, simpan DB/jurnal/konfigurasi paket baru, lalu rekonsiliasi semua schedule dan reply yang mungkin sudah terkirim. Kembalikan ownership hanya setelah tidak ada dua pengirim aktif dan pemakaian/delta baru telah dimasukkan pada pemilik yang akan dilanjutkan. Jangan menyalakan kembali kedua service lama dengan backup pra-cutover tanpa mengimpor delta; itu dapat membuat posting/reply ganda.

Jika kepastian remote belum tersedia, tahan tindakan terkait dan tampilkan alasan. Restore kehilangan intent harus masuk mode rekonsiliasi, bukan normal run. Penggantian remote automation memakai snapshot sebelum perubahan dan diff modul, bukan restore seluruh objek yang dapat menimpa perubahan sah lain.

## Bukti penutupan cutover

Simpan waktu freeze/import/activation, PID dan identitas pemilik, backup quick_check, source/target counts dan checksum, kuota per akun/hari, daftar unknown/hold, perubahan remote config yang terbukti via GET, tiga post URL pilot, hasil komentar uji, serta prosedur rollback yang teruji. Jangan menyebut migrasi selesai hanya karena proses running atau create schedule diterima.
