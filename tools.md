# Tools dan akses

Root proyek: `/Volumes/DATA KERJA /Project Dev/Fahra-automation-tiktok-fb-ytb-downloader`. Nama volume DATA KERJA memiliki spasi di akhir; kutip seluruh path.

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

File AI baru memakai label `base-url`, `api-key`, `model-combo`. Parser lama memakai underscore dan nama model berbeda. Adapter baru harus menormalisasi label yang didukung, menolak nilai ganda yang bertentangan, dan melaporkan nama field tanpa nilainya. Endpoint dan model dibaca dari file; jangan menyimpan hostname tunnel sementara ke source sebagai default permanen. Pilihan model tetap cx/gpt-6.1-sol untuk caption/persona adalah usulan agar keluaran tidak berubah karena rotasi combo.

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
