# Memori proyek

Diperbarui 16 Oktober 2026 sekitar 13:49 WIB. Kode produksi pada main 5c0e3db sudah dipush; 292/292 tes tanpa skip serta CI branch/main lulus. Perubahan tanpa mirror, kredit Instagram, dan cutoff komentar akun baru sudah terpasang. Dua layanan lama tetap bootout dan disabled; layanan baru id.fahra.automation.social berjalan. Pilot DbnsuCYybBP sudah terbit di ketiga platform, termasuk tab Shorts publik. Gate pilot sudah dibuka, paused=false, cleanup aktif, reply dijeda sementara untuk memperbaiki penantian snapshot akun. Bukti terbaru: runtime/verification/source-credit-20261016/.

## Keputusan yang tidak perlu ditanyakan ulang

1. Paket berada di `/Volumes/DATA KERJA /Project Dev/Fahra-automation-tiktok-fb-ytb-downloader`, repo privat fahra-farming-autonomous-fb-tiktok-ytb.
2. Bot Telegram berbeda: @Fahra_autonomousbot. Credential ada pada folder Credential proyek ini, bukan menggunakan token dua bot lama.
3. Satu link Reel baru menuju satu akun TikTok, satu Facebook Page, dan satu channel YouTube Shorts. Satu asset dipakai tiga delivery; keberhasilan sebagian tidak boleh mengulang tujuan yang sudah terbit. Histori/pending legacy tetap mempunyai tujuan asli, tanpa backfill otomatis ke platform lain.
4. Pengguna mengubah batas awal lima menjadi maksimal 20 video per akun per hari WIB pada percakapan 16 Oktober. Berlaku untuk paket baru. Kuota reply terpisah; 20 bukan janji semua platform mengizinkan 20 kiriman nyata.
5. Akun baru pada ketiga platform otomatis masuk rotasi setelah snapshot/koneksi/kuotanya lengkap. Akun putus dilewati; reconnect mempertahankan histori. Tidak perlu enrollment manual per akun untuk posting.
6. TikTok iq7fanspage dikecualikan mutlak. Facebook Page IQ7 tidak sama dengan akun tersebut.
7. Paket menggantikan dua workflow lama setelah tes lulus. Kedua layanan lama telah dihentikan pada 16 Oktober sekitar 12:02 WIB. Jangan mengaktifkan kembali tanpa rekonsiliasi delta dan pemindahan ownership.
8. Bank konten bernama `Bank-konten-fahra-automation-tiktok-ig-fb-ytb/`, disinkronkan Drive Desktop menurut pengguna. Link folder ada di Credential/google-drive-link.md. Instagram adalah sumber, bukan tujuan posting tambahan.
9. Instruksi terbaru 16 Oktober: video **tanpa mirror**, tetap zoom 110%, hook [0,3), audio asli. Caption/deskripsi TikTok, Facebook, dan YouTube wajib memuat `source: @username` Instagram sumber yang terverifikasi. Gaya caption/reply mengikuti workflow sebelumnya. Input publik dan kendali admin ID 265732518 diwarisi dari pilihan pengguna sebelumnya; batas batch/backlog dirancang tanpa mengganti mode publik diam-diam.
10. Cleanup tetap minimal 24 jam setelah semua tujuan wajib published, melindungi bank/Drive, frame, transkrip, konteks, histori, dan referensi workspace lama.

## Bukti audit

GET baca pada 16 Oktober 10:26 WIB: Repliz mengembalikan 136 record dalam dua halaman lengkap. Target connected yang diizinkan: 46 TikTok, 14 Facebook, 4 YouTube. Token baru valid dan berbeda dari kedua token lama; webhook kosong. 9Router models tersedia, combo codexx dan model cx/gpt-6.1-sol tercantum. Folder Drive HTTP 200 dan ID ditemukan dalam halaman. Ini belum membuktikan download/edit, generasi AI, checksum Drive, posting, atau aksi komentar paket baru.

Source baseline TikTok 9c452a93e84e0b1257a778c6eab15edd75e1e962; FB/YT 9cf41dcad06e97b9c4ec3ffdb4a1b5a55f5dc940. Snapshot lokal sekitar 10:22–10:24 WIB: TikTok 129 published, 29 failed, 46 akun; FB/YT nol job dan 18 akun. Kedua proses running, tetapi event polling Telegram gagal berulang dengan pesan kosong. Status running tidak berarti semua loop sehat.

Audit menemukan bug snapshot/kuota, tanggal jadwal, dedup hasil remote yang belum pasti, preflight dan batas intent, Drive pra-POST, Telegram 400, dan restore. Baca [AUDIT.md](docs/AUDIT.md); menyalin dua proyek tanpa memperbaikinya tidak cukup. Tes baseline TikTok 124 kasus: 123 lulus, satu khusus pasangan FB/YT dilewati. Paket baru sekarang mempunyai suite offline, fixture integrasi, verifier full, dan guard shadow; bukti final ada di worklog dan runtime/verification.

## Default implementasi dari rencana yang disetujui

- Persona ditafsirkan sebagai profil gaya bahasa akun; avatar, TTS, perubahan wajah, DM, follow/unfollow, atau engagement otomatis di posting orang lain tidak masuk rancangan awal. Permintaan implementasi seluruh paket melanjutkan ruang lingkup rencana ini.
- Antrean per tujuan independen; YouTube penuh tidak menahan TikTok/FB. Kapasitas nominal 920/280/80 per hari, paling banyak 80 sumber lengkap tiga platform per hari jika semua kuota kosong. Pada 100 link, 20 tujuan YouTube bergeser setidaknya ke hari berikutnya.
- Jendela jadwal awal 08:00 sampai sebelum 23:00 WIB, 20 slot tersebar dengan jarak sekitar 45 menit, offset per akun tersimpan. Ini default usulan yang dapat diubah, bukan jam yang dipilih pengguna.
- Persona default Indonesia Gen Z sesuai konteks, diikat ke akun dan diberi versi. Kuota reply awal 20 per akun/hari sebagai usulan terpisah; YouTube tidak memiliki native like komentar.
- Satu pemilik reply custom 9Router pada posting yang dikelola, native like/moderasi hanya jika platform mendukung dan hasil pilot terbukti. Default automasi calon akun baru disiapkan otomatis; capability yang belum terbukti tidak diaktifkan secara spekulatif.
- Profil bersama awal 9:16, 3–90 detik untuk irisan dukungan ketiga adapter. Video lebih panjang/rasio lain memerlukan kebijakan adaptasi atau review; jangan diam-diam dipotong atau diterbitkan sebagai video YouTube biasa.

Pilot publikasi DbnsuCYybBP dan verifikasi profil ketiga platform selesai; posting normal aktif. Reply dijeda sementara saat perbaikan penantian sinkronisasi akun. Sumber sebelumnya DaLK4M_JU9W tetap ditahan deduplikasi karena memiliki pekerjaan legacy failed dengan jadwal Repliz; tidak dibuat ulang. Pengguna sudah membuka bot dan mengirim sumber baru pada 12:38 WIB. Onboarding dan kartu progres tercatat terkirim ke chat nyata; tidak perlu meminta Start atau link baru lagi. Rincian penerimaan ada di [ACCEPTANCE.md](docs/ACCEPTANCE.md).

## Status migrasi aktual

Manifest final 03bf0b76ff714313a4d8c7cb99e0005d: 158 pekerjaan/aset lama (129 published, 29 failed), 22 konteks, 541 komentar, 126 percobaan reply. Perbandingan seluruh baris penting PASS; missing active schedules dan journal unresolved nol. Tujuan lama tetap TikTok saja atau FB+YT; tidak ada backfill. Satu batch operator kosong tercatat dari percobaan pilot yang ditolak dedup. Konfigurasi lokal live/publish_enabled=true, SQLite paused=false; reply sempat aktif dan sekarang dijeda sejak 13:49 WIB untuk perbaikan snapshot. Pilot gate null sejak 13:44:51 WIB. Cleanup aktif, retensi minimal 24 jam setelah seluruh tujuan published; sweep pertama nol file eligible dan nol byte dihapus. Antrean normal terbuka untuk input Telegram berikutnya.

## Orientasi dan kredit sumber yang sudah diterapkan

Pengguna mengirim Reel DbnsuCYybBP lewat bot baru: batch 63f40e1579474fc48888f20de204e81e, asset 55e8d6f3bd3b4211ac757399484aa12a, tiga delivery. Preview orientasi asli, zoom 110%, hook tiga detik, serta checksum Drive sudah PASS tanpa publikasi. Username sumber terverifikasi nopri.adi.16144. Kredit berada di caption/deskripsi publik, bukan tambahan overlay video. Produksi memproses pekerjaan Telegram asli secara mandiri; preview tidak dipromosikan menjadi posting. Status jadwal/publikasi harus dibaca dari DB dan Repliz sebelum dilaporkan.


## Bukti pilot final

TikTok namira5751 terbit pada 13:13:02 WIB dengan post ID 7697144810318122261. Facebook Page Arunika terbit pada 13:24:13 WIB, post ID 113230217671155_1412797961067585, URL Reel publik 1445902694303604. Schedule, Content API, dan halaman publik cocok; kredit sumber terlihat. YouTube terbit 13:36:21 WIB dengan ID XVvnrT74448 di clara luna; Content API cocok dan video muncul pada tab Shorts/Terbaru publik. Ketiga notifikasi tautan Telegram sent=1, kartu batch message 4 done=1.

Preview reply terisolasi pada video TikTok yang terbit memakai enam frame, model aktual gpt-6.1-sol, confidence 0,94. Contoh komentar fixture tentang helm menghasilkan jawaban faktual; context tidak memuat fakta mirror palsu. Tidak ada komentar uji dikirim dan cache/DB konteks produksi tidak berubah. Profil native dua posting pilot telah dibaca ulang: reply mati, like dan moderasi keyword aktif. Profil YouTube berhasil dibuat satu kali, ID 6ad1c63b0f680841e48bf22c, tetapi pembanding konfigurasi menolak tambahan status runtime pending. GET readback menunjukkan native reply/like=false dan moderasi keyword=true. Perbaikan pembanding sudah dipasang: rekonsiliasi profil yang sama lulus melalui tiga GET dan nol mutasi remote. Semua profil pilot verified dengan owner custom. Reply diaktifkan kembali setelah bukti ini; konfigurasi profil belum menjadi bukti tindakan pada komentar nyata.


## Pemeriksaan startup reply

Polling pulih otomatis pada 13:48:01 WIB: 64 akun (46 TikTok/14 Facebook/4 YouTube), 46 cutoff migrasi dan 18 cutoff baru 16 Oktober 13:47:03 WIB. Error polling/profile kosong. Namun inventory startup yang bertepatan dengan snapshot sedang disinkronkan sempat membuat delapan draft legacy masuk needs_review karena akun dianggap tidak tersedia. Audit backup membuktikan draft tidak berubah, tidak ada attempt/journal kirim, dan akun sebenarnya eligible. Reply dipause 13:49 WIB untuk memperbaiki penantian snapshot; jangan memulihkan draft tanpa pemeriksaan tersebut. Detail di startup-review-recovery-audit.json. Cleanup dan publikasi tetap aktif.

Empat worktree sebelumnya sudah dihapus dengan git worktree remove tanpa force setelah merge/push/CI, arsip dan pilot lulus; branch lokal dihapus dengan -d. Arsip 106 file checksum PASS. Worktree baru reply-snapshot-wait menampung perbaikan startup.
