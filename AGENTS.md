# Aturan agent Fahra Automation

Baca [memory.md](memory.md), [rencana](docs/PLAN.md), [audit](docs/AUDIT.md), [tools.md](tools.md), lalu [worklog.md](worklog.md) sebelum bekerja. Periksa kondisi aktual; angka pada audit adalah snapshot bertanggal.

## Status dan ruang lingkup

Pengguna menyetujui **implementasi seluruh paket** pada 16 Oktober 2026 setelah audit. Kode ada pada paket `fahra_automation`; jangan memperlakukan permintaan ini sebagai audit saja. Baca [operasi](docs/OPERATIONS.md) dan status aktual di memory/worklog. Migrasi final dan ownership produksi sudah aktif; status pause, pilot gate, publikasi, dan reply terbaru mengikuti memory/worklog. Default config contoh tetap shadow. Bedakan kode tersedia, preview lulus, jadwal diterima, publikasi terverifikasi, dan aksi komentar nyata.

## Keputusan tetap pengguna

- Input baru lewat bot Telegram baru, satu link Reel menjadi satu asset dan tiga delivery wajib: satu TikTok, satu Facebook Page, satu YouTube Shorts. Histori/pending legacy mempertahankan tujuan aslinya; jangan membuat backfill platform lain otomatis. Target output tidak mencakup Instagram; kata ig pada nama bank adalah nama folder pilihan pengguna.
- Batas paket baru maksimal 20 video per akun per hari Asia/Jakarta, terpisah dari kuota reply. Angka lima pada workflow lama tidak boleh diwarisi atau diubah pada proyek lama pada workspace lama. Batas provider yang lebih rendah tetap dihormati.
- Akun baru ketiga platform otomatis ikut rotasi setelah koneksi, identitas, capability, dan snapshot kuota lengkap. Disconnect mengeluarkan akun dari pilihan berikutnya; reconnect memakai ID/kuota lama. Snapshot parsial tidak boleh mengosongkan atau mengaktifkan pool.
- Jangan menggunakan atau mengubah TikTok iq7fanspage, ID `6a9ec26462caae1e0415af16`, posting, atau profilnya. Facebook Page bernama IQ7 adalah identitas berbeda.
- Paket baru menggantikan dua workflow lama setelah tes lulus dan tahap cutover dijalankan. Satu akun/post hanya punya satu pemilik kirim/reply. Token bot berbeda tidak menyelesaikan konflik antarworker.
- Sumber akses hanya `Credential/` pada root baru. Nama file aktual: `repliz.md`, `9router.md`, `token-bot-telegram.md`, `google-drive-link.md`. Jangan menyalin nilai akses ke source, log, argumen shell, output, atau repo.
- Rencana mewarisi input Telegram publik dari keputusan pengguna sebelumnya; kendali admin tetap ID `265732518`, rincian batch hanya pengirim/admin. Batas batch/backlog boleh menunda penerimaan; jangan diam-diam mengganti mode publik menjadi allowlist.
- Bank lokal adalah `Bank-konten-fahra-automation-tiktok-ig-fb-ytb/`. Pengguna menyatakan folder disinkronkan Drive Desktop. Jangan menambahkan upload API kedua; sebelum publikasi buktikan ID, ukuran, dan seluruh checksum Drive.
- Instruksi pengguna terbaru 21 Oktober 2026: **hilangkan visual hook sepenuhnya** pada render baru. Pertahankan orientasi sumber tanpa mirror, zoom 110%, H.264/AAC, audio asli, dan output 30 fps. Simpan `edits.hook_enabled=false` dan `edits.hook_seconds=0`; nilai hook lama pada config tidak mengaktifkan overlay. [HOOK_VISUAL.md](docs/HOOK_VISUAL.md) menyimpan arsip gaya lama. Font dan lisensi tetap dibundel untuk laporan followers dan kompatibilitas arsip.
- Caption/deskripsi baru memakai `id-source-rewrite-v1`: rewrite ringan hanya dari caption Instagram asli yang cocok dengan URL, shortcode, pemilik, dan hash. Satu hasil bersama dipakai TikTok, Facebook, dan YouTube tanpa rewrite persona per akun. Caption yang benar-benar kosong menghasilkan kredit sumber saja; emoji atau hashtag saja tidak menjadi cerita buatan. Metadata caption hilang/rusak ditahan, tidak diganti judul extractor atau tebakan dari frame. Ketiga platform tetap mencantumkan `source: @username` dari pemilik terverifikasi. Baca [COPY_AND_PERSONA.md](docs/COPY_AND_PERSONA.md).
- Asset downloaded atau analyzed lama yang belum dirender mengikuti kebijakan baru sebelum render pertama. Asset yang sudah dirender, masuk Bank/Drive, memiliki intent, terjadwal, atau terbit tidak ditulis ulang atau diposting ulang otomatis. Persona dan gaya Gen Z tetap berlaku pada reply dengan konteks video/percakapan; jangan mengubah confidence kesetiaan caption menjadi bukti pemahaman video. Konten serius tetap tenang dan fakta tidak boleh dikarang.
- Auto-reply harus memakai konteks video dan percakapan serta deduplikasi. YouTube tidak mendukung like komentar melalui Repliz. Native override posting mengganti seluruh config akun; pertahankan modul lain dan jangan menjalankan dua pengirim reply.
- Cleanup minimal 24 jam setelah semua tujuan wajib published terverifikasi, dihitung dari tujuan terakhir. Simpan Bank/Drive, frame, transkrip, caption, konteks, dan histori. Lindungi semua referensi lama/baru sebelum menghapus file kerja MCP. Tidak ada penghapusan rekursif/manual.

## Disiplin kerja dan GitHub

Repo tujuan privat `ramdanifahmy2023/fahra-farming-autonomous-fb-tiktok-ytb`. Pengguna mengizinkan pembuatan repo, commit/push hasil PASS, dan implementasi dan dokumentasi dalam lingkup permintaan ini. Tidak perlu meminta ulang izin Git. Jangan push paket baru ke repo lama.

Pekerjaan berat memakai branch `work/YYYYMMDD-topik` dan worktree `/Volumes/DATA KERJA /Project Dev/worktrees/fahra-farming-autonomous-fb-tiktok-ytb/YYYYMMDD-topik`, tambah akhiran bila sudah terpakai. Worktree memakai fixture, runtime dan venv sendiri. Jangan memasang dua paket Python lama bernama peternakan ke satu venv, menyalin DB aktif, menautkan runtime produksi, menjalankan bot kedua, atau menerbitkan preview.

Setiap tugas meninggalkan worklog bahasa Indonesia: permintaan, perubahan, alasan, file, base/branch/worktree, pemeriksaan, hasil, batas bukti, merge/push, dan status pembersihan. Update memory jika keputusan berubah dan tools jika cara operasi berubah. Bedakan rencana, mock, preview nyata, request diterima, dan hasil publik terverifikasi.

Jalankan `./scripts/verify.sh --mode full --base origin/main --secrets-from PATH_CREDENTIAL`. CI memakai verifier sama dan suite dengan jaringan dinonaktifkan. Perubahan media/AI/Drive juga perlu preview nyata di runtime terpisah, tanpa publikasi. `scripts/verify_plan.py` tetap pemeriksa arsip rencana saja; bukan bukti runtime. Baca [ACCEPTANCE.md](docs/ACCEPTANCE.md).

Alur: PASS lokal, commit/push branch, CI success pada SHA tepat, main bersih dan selaras remote, merge ff-only, periksa hasil gabungan, push main, periksa CI dan SHA remote. Jangan force-push. Restart hanya layanan yang kodenya berubah setelah layanan itu benar-benar diimplementasikan/diaktifkan. Hapus worktree tanpa force dan branch lokal dengan `git branch -d` setelah bukti diarsipkan, merge/push/CI lulus. Bukti hash akhir boleh disimpan di runtime supaya tidak menambah commit hanya untuk merekam hash sendiri.

Untuk UI/UX, gunakan UI UX Pro Max dan antislop serta companion yang relevan dari `/Users/fahra/.codex/skills/`. Pengguna memilih antislop selama pekerjaan dan review. Backend/infrastruktur tidak membutuhkan alur desain visual. Antislop core/copywriting digunakan untuk membedakan fakta, usulan, dan bukti pada dokumen ini.
