# Tools dan akses

Root proyek: `/Volumes/DATA KERJA /Project Dev/Fahra-automation-tiktok-fb-ytb-downloader`. Nama volume DATA KERJA memiliki spasi di akhir; kutip seluruh path.

## Cakupan verifikasi terbaru

Checkpoint 16 Oktober setelah perubahan pengguna: commit 26c7731, **280/280 tes tanpa skip PASS**, verifier full lokal PASS, [CI branch](https://github.com/ramdanifahmy2023/fahra-farming-autonomous-fb-tiktok-ytb/actions/runs/38858165665) dan [CI main](https://github.com/ramdanifahmy2023/fahra-farming-autonomous-fb-tiktok-ytb/actions/runs/38858464310) success pada SHA yang sama. Preview nyata DbnsuCYybBP membuktikan orientasi tanpa mirror, zoom 110%, hook [0,3), seluruh checksum Drive cocok, serta kredit `source: @nopri.adi.16144` pada payload ketiga platform. Bukti: `runtime/verification/source-credit-20261016/`. Status pilot produksi dicatat di memory/worklog.

## Checkpoint implementasi sebelum perubahan orientasi

Checkpoint 16 Oktober 2026 pada commit [54e4417](https://github.com/ramdanifahmy2023/fahra-farming-autonomous-fb-tiktok-ytb/commit/54e4417d01b031b472f541a4492418dcbfe58d05): **263/263 tes offline PASS, tanpa skip**, verifier full lokal PASS, serta [CI branch](https://github.com/ramdanifahmy2023/fahra-farming-autonomous-fb-tiktok-ytb/actions/runs/38853332246) dan [CI main](https://github.com/ramdanifahmy2023/fahra-farming-autonomous-fb-tiktok-ytb/actions/runs/38853445793) success. CI menjalankan pemeriksaan source dan seluruh suite dengan dependency terkunci, tanpa credential, jaringan aplikasi atau deployment. Bukti lokal: `runtime/verification/verify-20261016T045916808690Z/` dan `runtime/verification/implementation-20261016/git-initial-deploy.json`.

Preview nyata sudah membuktikan download MCP, transkripsi/enam frame, render 17,068 detik 720×1280 30 fps H.264/AAC dengan mirror/zoom 110%/hook [0,3), serta Drive dengan ukuran dan SHA-256 penuh yang cocok. Generasi metadata persona tiga akun fixture dan draft reply memakai model aktual `gpt-6.1-sol`; payload tiga platform lulus validasi offline tanpa request Repliz. Bukti pengembangan disalin ke `runtime/verification/implementation-20261016/development-artifacts/`.

Rehearsal migrasi mencocokkan 158 jobs/assets (129 published, 29 failed), 539 comment events dan 126 reply attempts, termasuk identitas, checkpoint, kuota dan cutoff. Missing active schedules dan journal unresolved sama-sama 0; staging tetap paused dan `ready=false` karena belum memakai snapshot final. Bukti: `runtime/verification/implementation-20261016/rehearsal-validation.json` dan `rehearsal-reconcile.json`.

Belum ada publikasi atau aksi komentar nyata dari paket ini pada checkpoint tersebut. Pengujian outage/rollback operasional, freeze/import final, pilot tiga platform, dan notifikasi nyata tetap dicatat terpisah. Matriks hasil dan batas buktinya ada di [ACCEPTANCE.md](docs/ACCEPTANCE.md). Tabel berikut adalah arsip akses audit sebelum implementasi, bukan status kemampuan yang belum diuji saat ini.

## Akses yang diaudit sebelum implementasi

| Komponen | Lokasi atau identitas | Bukti audit 16 Oktober 2026 |
| --- | --- | --- |
| Repliz | Credential/repliz.md, access_key dan secret_key | Sama dengan workspace lama; GET akun lengkap berhasil |
| Bot baru | Credential/token-bot-telegram.md, @Fahra_autonomousbot | getMe valid, webhook kosong, tanpa getUpdates |
| AI | Credential/9router.md | GET models berhasil; combo codexx dan cx/gpt-6.1-sol terdaftar; generasi belum diuji |
| Drive | Credential/google-drive-link.md | Halaman folder HTTP 200, ID cocok, tanpa redirect login; sinkronisasi/checksum belum diuji |
| Bank | Bank-konten-fahra-automation-tiktok-ig-fb-ytb/ | Folder ada, sinkronisasi dinyatakan pengguna |
| Downloader | /Users/fahra/Projects/instagram-downloader-mcp/start-mcp.sh | Kontrak stdio dari README/kode; tidak download baru pada audit ini |
| Repo privat | [fahra-farming-autonomous-fb-tiktok-ytb](https://github.com/ramdanifahmy2023/fahra-farming-autonomous-fb-tiktok-ytb) | GitHub CLI membuat repo; hash/CI akhir pada bukti lokal |
| Bukti lokal | runtime/verification/planning-20261016/ | Ringkasan akses tanpa token, hasil pemeriksaan, CI, dan final.json |

File AI baru memakai label `base-url`, `api-key`, `model-combo`. Parser implementasi mendukung normalisasi hyphen/underscore, menolak nilai ganda yang bertentangan, dan melaporkan nama field tanpa nilainya; perilaku ini diuji pada suite offline. Endpoint/kunci dibaca dari konfigurasi atau file credential; jangan menyimpan hostname tunnel sementara ke source sebagai default permanen. Konfigurasi caption/persona saat ini memakai `cx/gpt-6.1-sol`; model aktual pada preview tercatat `gpt-6.1-sol`.

Baca API key hanya dalam proses lokal. Hindari curl berisi token pada argumen, mencetak respons akun mentah, dump env, atau log exception URL Telegram. GET getMe/getWebhookInfo boleh untuk health; getUpdates hanya boleh dimiliki satu polling worker bot tersebut.

## Pemeriksaan repo rencana

```sh
python3 scripts/verify_plan.py
python3 scripts/verify_plan.py --credentials-dir '/Volumes/DATA KERJA /Project Dev/Fahra-automation-tiktok-fb-ytb-downloader/Credential'
git diff --check
```

Pemeriksa hanya membaca dokumen/source dan index, memeriksa tautan lokal, sintaks Python, whitespace, pola rahasia, nilai akses lokal bila diberikan, dan file yang dilarang tracked. Laporan JSON ditulis ke runtime/verification/. CI menjalankan pemeriksa yang sama tanpa credential, jaringan aplikasi, atau deployment. Ini bukan unit test workflow baru.

Git origin memakai HTTPS dan helper `gh auth git-credential` lokal repo. Jangan menyimpan token pada URL. Push awal sempat gagal melalui default SSH; HTTPS memperbaiki akses tanpa mengganti credential pengguna. GitHub visibility harus tetap private.

## Paket dan operasi

Paket Python `fahra_automation`, Python 3.14, SQLite schema v1. Dependency dikunci pada requirements.lock. LaunchAgent `id.fahra.automation.social`; database runtime/workflow.sqlite3. Petunjuk perintah, cutover, dan pemulihan ada di [OPERATIONS.md](docs/OPERATIONS.md).

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
./scripts/verify.sh --mode full --base origin/main --secrets-from '/Volumes/DATA KERJA /Project Dev/Fahra-automation-tiktok-fb-ytb-downloader/Credential'
.venv/bin/python -m fahra_automation doctor
.venv/bin/python -m fahra_automation status
.venv/bin/python -m fahra_automation events --limit 20
```

Verifier full memeriksa working tree/index, pola dan nilai rahasia, sintaks Python/JSON/TOML/shell, tautan Markdown, diff, lalu seluruh tes offline. CI tidak memiliki credential atau deployment. Artefak lokal disimpan di runtime/verification/; nilai rahasia tidak dicetak. Pemeriksa rencana di bagian sebelumnya hanya untuk arsip dokumen.

Backup database melalui API SQLite ke `~/Library/Application Support/FahraAutomation/backups`. Attempt journal terpisah di `~/Library/Application Support/FahraAutomation/attempts`; **backup database saja tidak cukup untuk restore**. Worktree/fixture otomatis memakai journal runtime sendiri. Jangan menghapus journal untuk memperbaiki antrean macet.

## Referensi lama dan kepemilikan

TikTok lama: `/Volumes/DATA KERJA /Project Dev/Peternakan-akun`, label `id.fahra.peternakan`. FB/YT lama: `/Volumes/DATA KERJA /Project Dev/Peternakan-fb-ytb`, label `id.fahra.peternakan.fb-ytb`. Migrasi mengikuti [MIGRATION.md](docs/MIGRATION.md), SQLite backup API, checksum, dan rekonsiliasi remote. Data aktif tidak disalin mentah ke worktree. Worker baru menahan lock kedua root lama selama hidup; setiap mutasi Repliz juga memeriksa ownership dan status kedua LaunchAgent lama.


## Kredit sumber Instagram dan orientasi video

Instruksi terbaru pengguna: hasil baru tidak di-mirror. Zoom110% dan hook [0,3) tetap. Renderer menyimpan edits.mirror=false; arsip lama dengan mirror=true tetap menjadi histori.

Downloader tetap MCP. Metadata pemilik dibaca read-only melalui yt-dlp pada container downloader yang sama, memakai lock `/tmp/instagram-mcp.lock`; source MCP tidak diubah. Hanya channel/owner.username/user.username dari extractor Instagram dan shortcode/URL yang cocok menjadi bukti username. Nama tampilan, caption, dan uploader_id bukan sumber kredit. Metadata yang hilang/bertentangan menahan pengiriman.

Adapter Repliz menambahkan tepat satu `source: @username` sebelum hashtag pada caption/deskripsi ketiga platform dan menghitung batas karakter sesudah kredit. Kredit bukan teks overlay tambahan. Job yang sudah ber-intent/terbit tetap memakai body tersimpan. Retry manual arsip yang sudah melewati download tanpa bukti atribusi membutuhkan revisi/enrichment terverifikasi; jangan menebak kredit atau menerbitkan ulang artifact mirror lama.


## Kendali reply dan batch

Gunakan CLI utama `.venv/bin/python -m fahra_automation reply status|pause|resume` untuk operasi rutin. Kedua jalur CLI dan inventory memakai pencatatan cutoff yang sama: akun migrasi mempertahankan tanggal lamanya, akun baru memakai waktu bergabung, dan reconnect tidak menggeser cutoff. CLI sekunder pada modul replies tetap memiliki preflight GET dan perintah activate untuk aktivasi pertama. Resume dapat melanjutkan backlog legacy yang masih memenuhi umur dan kuota. Setiap pengiriman tetap memerlukan konteks serta profil single owner yang sudah dibaca ulang.

Batch mendukung maksimal 100 link per pesan, backlog 1.000 asset pending, dengan rotasi yang berlanjut antarbatch/restart. Batas tersebut terpisah dari kuota 20 video per akun per hari WIB.
