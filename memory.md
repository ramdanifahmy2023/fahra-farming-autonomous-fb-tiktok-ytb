# Memori proyek

Diperbarui 16 Oktober 2026. Status: pengguna meminta implementasi seluruh paket. Implementasi sedang diuji pada branch work/20261016-implementation; aktivasi dicatat terpisah setelah bukti cutover. Hasil Git/CI akhir tercatat di runtime/verification/planning-20261016/final.json.

## Keputusan yang tidak perlu ditanyakan ulang

1. Paket berada di `/Volumes/DATA KERJA /Project Dev/Fahra-automation-tiktok-fb-ytb-downloader`, repo privat fahra-farming-autonomous-fb-tiktok-ytb.
2. Bot Telegram berbeda: @Fahra_autonomousbot. Credential ada pada folder Credential proyek ini, bukan menggunakan token dua bot lama.
3. Satu link Reel baru menuju satu akun TikTok, satu Facebook Page, dan satu channel YouTube Shorts. Satu asset dipakai tiga delivery; keberhasilan sebagian tidak boleh mengulang tujuan yang sudah terbit. Histori/pending legacy tetap mempunyai tujuan asli, tanpa backfill otomatis ke platform lain.
4. Pengguna mengubah batas awal lima menjadi maksimal 20 video per akun per hari WIB pada percakapan 16 Oktober. Berlaku untuk paket baru. Kuota reply terpisah; 20 bukan janji semua platform mengizinkan 20 kiriman nyata.
5. Akun baru pada ketiga platform otomatis masuk rotasi setelah snapshot/koneksi/kuotanya lengkap. Akun putus dilewati; reconnect mempertahankan histori. Tidak perlu enrollment manual per akun untuk posting.
6. TikTok iq7fanspage dikecualikan mutlak. Facebook Page IQ7 tidak sama dengan akun tersebut.
7. Paket menggantikan dua workflow lama setelah tes lulus. Layanan lama berjalan sampai migrasi final; periksa status aktual sebelum aktivasi.
8. Bank konten bernama `Bank-konten-fahra-automation-tiktok-ig-fb-ytb/`, disinkronkan Drive Desktop menurut pengguna. Link folder ada di Credential/google-drive-link.md. Instagram adalah sumber, bukan tujuan posting tambahan.
9. Edit dan gaya caption/reply mengikuti workflow sebelumnya. Input publik dan kendali admin ID 265732518 diwarisi dari pilihan pengguna sebelumnya; batas batch/backlog dirancang tanpa mengganti mode publik diam-diam.
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

Tahap berjalan: suite, preview nyata, rehearsal migrasi, lalu cutover dan pilot. Link pilot pilihan pengguna: https://www.instagram.com/reel/DaLK4M_JU9W/. Tahap, dependensi, serta bukti lulus ada di [PLAN.md](docs/PLAN.md), [ACCEPTANCE.md](docs/ACCEPTANCE.md), dan [MIGRATION.md](docs/MIGRATION.md).
