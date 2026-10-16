# Tes penerimaan implementasi

Dokumen ini menetapkan tes yang harus dibuat/dijalankan pada fase implementasi. Daftar ini belum PASS. Pada tahap audit hanya suite baseline TikTok dan probe yang disebut dalam laporan audit telah dijalankan. CI repo sekarang memeriksa dokumen saja.

## Fondasi, akun dan publikasi

| ID | Skenario | Hasil wajib |
| --- | --- | --- |
| A01 | Credential hyphen/underscore, field duplikat, endpoint salah, key hilang | Field didukung terbaca, konflik ditolak, tidak ada key/token/URL Telegram bertoken pada log |
| A02 | Satu link, batch 20/100, update Telegram replay, URL parameter berbeda | Satu asset per sumber canonical; tepat tiga delivery wajib; replay tidak menggandakan batch/job |
| A03 | Callback/outbox HTTP 400/403, timeout, 429, crash sebelum/sesudah offset, kontrol oleh nonadmin | Pesan permanen diisolasi; update lain tetap diproses; offset maju sesudah ingest durable; input publik tidak memberi hak kontrol atau akses batch orang lain |
| A04 | Akun baru masing-masing platform, disconnect/reconnect, ID duplikat, snapshot 2 halaman terputus | Akun baru ikut rotasi otomatis hanya setelah snapshot lengkap; partial tidak mengganti pool; reconnect tidak reset kuota; iq7fanspage tak pernah eligible |
| A05 | GET akun sukses, GET jadwal gagal/menunggu, freshness/generation berubah pada setiap await | Akun+kuota+generation diterapkan atomik; tidak ada reservasi/intent pada snapshot tidak sah |
| A06 | Akun dengan 19/20 penggunaan, batch bersamaan, restart, jadwal eksternal | Akun yang sama maksimal 20; akun eligible lain yang belum penuh tetap dipakai hari ini; ke besok hanya jika pool platform penuh atau tidak ada slot sah; summary/capacity/HELP membaca konfigurasi 20 |
| A07 | Jadwal remote dipindah tanggal, publish terlambat melewati WIB, provider rolling window | Kuota tidak kurang hitung atau dihitung ganda; slot konservatif ditahan saat hasil belum pasti; batas provider dapat lebih rendah dari 20 |
| A08 | Disconnect sebelum intent, setelah intent, dan sesudah POST; persona akun berbeda | Sebelum intent boleh alih akun dan regenerate metadata persona; sesudah intent tidak dialihkan tanpa hasil rekonsiliasi |
| A09 | GET preflight pertama/kedua timeout versus POST timeout | Nol POST pada kegagalan preflight dapat diulang aman; POST yang mungkin terkirim menjadi submit_unknown dan tidak POST ulang buta |
| A10 | Satu tujuan published, tujuan lain failed/unknown/quota penuh | Hanya tujuan yang memang belum selesai dilanjutkan; tidak membuat ulang jadwal yang sudah berhasil |
| A11 | Source berbeda tetapi hash sama; failed masih memiliki remote schedule/intent | Dedup menahan media yang hasil remote-nya belum pasti; status failed bukan izin mengulang |
| A12 | Crash pada setiap batas intent/request/response/commit, restart, restore backup sebelum intent | Jurnal/marker rekonsiliasi menemukan remote existing atau menahan unknown; tidak ada create tambahan |

## Media, Drive dan komentar

| ID | Skenario | Hasil wajib |
| --- | --- | --- |
| M01 | Download start sukses lalu crash sebelum ID; orphan dapat/tidak dapat dipetakan; completed/partial/failed; MCP lock/timeout | Pemetaan terbukti dipakai ulang; start ambigu ditahan untuk pemulihan; komputasi lokal boleh diulang aman tanpa artifact resmi ganda; manifest cleanup konsisten |
| M02 | Landscape/portrait, <3 detik, 90/180/>180 detik, audio kosong, codec/ukuran tidak cocok | Validator per platform jelas; tidak diam-diam memotong atau mengklaim Shorts pada video tidak sesuai |
| M03 | Enam frame, speech/music/noisy, visual kabur, konten serius, prompt injection di caption/komentar | Fakta dan ketidakpastian disimpan; persona tidak mengarang; confidence tidak dinaikkan editor; data sumber tidak menjadi instruksi sistem |
| M04 | Preview nyata dari link yang diberikan pengguna, tanpa membuat schedule | Satu download/render/Drive, mirror+zoom110%, hook pada [0,3), metadata sesuai, payload ketiga platform lolos validasi |
| M05 | Google Drive pending sync, HTML/login, duplikat nama, ID cache usang, byte berubah dengan header MP4 sama | Seluruh checksum/ukuran sebelum tiap POST cocok atau nol POST; file untuk besok diperiksa kembali |
| C01 | Persona berbeda, restart, versi berubah, delivery berpindah akun sebelum intent | Versi tepat melekat ke draft/delivery; hasil lama tidak diam-diam ditulis ulang; hook bersama tetap konsisten |
| C02 | Top-level/nested comment, induk hilang, reply sudah ada, komentar edited/deleted | Konteks thread benar; induk hilang ditahan; komentar diri sendiri/duplikat tidak dibalas |
| C03 | Native reply akun/post aktif, override parsial, akun baru, template berubah | Satu pemilik reply; modul lain tetap sesuai snapshot; override lengkap diperiksa ulang via GET |
| C04 | Rate limit, delay kuota, timeout reply, send_unknown, owner ID berbeda | Penundaan kuota bukan error isi; hasil belum pasti direkonsiliasi; tidak mengirim ulang buta |
| C05 | Like TikTok/FB, like YouTube, hide/delete/moderasi per platform | YouTube like tidak dicoba; arti tindakan sesuai capability nyata; kritik biasa tidak dihapus karena sentimen |
| C06 | Native polling YouTube, webhook hilang/duplikat/terlambat/out of order | Polling tetap berfungsi; event terdedupe; tidak mengandalkan webhook yang tidak tersedia |

## Migrasi, operasi dan bukti nyata

| ID | Skenario | Hasil wajib |
| --- | --- | --- |
| O01 | Import snapshot dua workflow, termasuk sumber sama dengan artifact berbeda | Mapping jobs/assets/post/kuota/komentar/cutoff lengkap; tujuan legacy tetap TT saja atau FB+YT, tanpa backfill/publish ulang; tidak mengimpor secret, bot offset lama, atau lock sebagai data baru |
| O02 | Shadow mode dan proses lama masih running | Nol POST/mutasi automasi oleh paket baru; startup hak kirim ditolak jika owner lama masih aktif |
| O03 | Rollback setelah paket baru membuat jadwal/reply | Rekonsiliasi delta dan ownership dilakukan sebelum owner lama boleh aktif; restore DB lama saja tidak cukup |
| O04 | Disk hampir penuh, Drive offline, volume lepas, Docker mati, 9Router gagal | Ingest/backpressure dan retry beralasan; health membedakan proses hidup dari loop sehat; token tak bocor |
| O05 | Semua tiga tujuan published versus satu failed/unknown; frame hilang; peer/arsip rusak | Cleanup hanya setelah semua tujuan +24 jam, hash penuh dan referensi lengkap; bank/Drive/konteks tetap utuh |
| O06 | Shared MCP source dipakai preview/arsip dua proyek lama; crash saat unlink | File dilindungi; jurnal per file dan shared lock benar; tidak ada penghapusan rekursif |
| O07 | 64 akun snapshot awal, batas 20, backlog 100+ sumber, API lambat | Budget panggilan/CPU/disk terukur; TTL/limiter tidak saling mengunci semua subsistem; progres tetap informatif |
| O08 | Pilot satu sumber nyata setelah hak kirim dialihkan | Tiga schedule ID, tiga post ID/URL, bukti video benar-benar terbit dan YouTube Shorts, notifikasi Telegram diterima |
| O09 | Pilot komentar yang memang diotorisasi pada tiap platform | Draft sesuai konteks/persona, request/Report sukses dibedakan dari hasil terlihat, tidak ada native/custom ganda |

Suite offline memakai DB/file sementara, tidak credential produksi. Preview integrasi memakai runtime terpisah dan tidak POST publikasi. Pilot nyata hanya pada fase aktivasi yang dijalankan secara eksplisit setelah kriteria sebelumnya lulus. Sebelum cutover, ulang inventaris dan validasi batas provider yang bisa berubah.

Setiap hasil mencatat commit, versi dependency/model, input/fixture, waktu, hasil aktual, batas pengamatan, dan lokasi bukti. Jangan menyatakan seluruh matriks PASS dari config yang tersimpan atau CI dokumen hijau. Git/CI harus menguji commit tepat yang akan digabung/dijalankan.
