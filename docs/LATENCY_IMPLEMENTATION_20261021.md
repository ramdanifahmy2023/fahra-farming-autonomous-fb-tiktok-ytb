# Implementasi perbaikan latensi, 21 Oktober 2026

Perubahan pada branch `work/20261021-latency-implementation`, base `4b648b1`, menindaklanjuti [audit latensi](LATENCY_AUDIT_20261021.md). Kode dikembangkan di worktree terpisah, lulus 572 tes penuh, lalu dipasang dari commit `4a2b3d1`. CI branch/main pada SHA yang sama lulus. Belum ada pengukuran kohort antrean produksi sesudah deployment pada laporan ini.

Waktu input sampai siap kirim, waktu menerima jadwal, waktu jadwal tayang, dan waktu published terdeteksi tetap dihitung terpisah. Perubahan ini tidak menambah kuota provider atau menjamin tiga platform terbit bersamaan.

## Pemetaan audit ke perubahan

| Audit | Perubahan yang tersedia | Pemeriksaan dan batas |
| --- | --- | --- |
| L01: monitor dan anggaran API | Selector mendahulukan unknown dan jadwal dekat, dengan giliran untuk pekerjaan latar. Poll gagal memakai jeda membesar. Limiter membagi giliran menurut kelas kerja, maksimal dua request dan satu mutasi bersamaan; cooldown 429 berlaku bersama. | `test_latency_db`, `test_latency_api`, dan `test_runtime_flow` menguji prioritas, giliran latar, cancellation, dan guard setelah antre. 404/unknown tidak menjadi izin mengirim ulang. |
| L02: batas unggahan YouTube | Pesan `uploadLimitExceeded` dan frasa provider yang dikenal dicatat sebagai `provider_upload_limit`. Akun ditahan enam jam secara bawaan. Ledger mengikat jadwal dan error agar sync berulang tidak memperpanjang jeda; kegagalan historis memakai waktu kegagalan pertama. | `test_latency_db` menguji restart, sync berulang, pesan generik, platform lain, dan reservasi failed yang tetap dihitung. Enam jam adalah kebijakan menunggu, bukan bukti waktu reset kuota YouTube. |
| L03: consumer bercampur | Satu layanan menjalankan maksimal dua preparer dan satu sender dengan selector tahap terpisah. Lease asset tetap bersama untuk tiga delivery. | `test_latency_db` menguji klaim paralel dan kuota atomik; `test_pipeline` menguji asset bersama, intent, cancellation, dan unknown. Tidak ada bot kedua. |
| L04: dependency dan tanggal tujuan | Penantian snapshot/Drive/kapasitas disimpan. Snapshot lengkap membangunkan penantian snapshot dan kapasitas. Sender melewati tanggal mendatang kecuali pool perlu diperiksa ulang. Persiapan asset tetap boleh maju jika satu tujuan masuk horizon. | Tes meliputi restart, race pemulihan snapshot, pause, intent, dan tujuan YouTube masa depan. Drive memakai fallback 15 detik; belum ada watcher Drive Desktop. Jarak aman jadwal memakai nilai config yang sama saat alokasi, pemeriksaan slot, intent, dan guard sebelum request. |
| L05: sync, inbox, dan profil | Dua kelompok akun independen dapat dibaca bersamaan; halaman tiap kelompok tetap berurutan dan snapshot diterapkan sekali setelah lengkap. Inbox memakai kelompok lebih kecil, dedupe, batas halaman, dan jeda adaptif. Baca profil dibagi dalam satu putaran dan diulang sebelum mutasi. | `test_latency_api` menguji gagal satu kelompok, cancellation, perubahan jumlah komentar, induk resolved, invalidasi cache, dan wakeup webhook. Kontrak snapshot kuota tidak dilonggarkan; tidak ada cursor incremental yang diasumsikan. |
| L06: calon persona hilang | Hasil AI disimpan sebagai calon metadata dengan akun, platform, versi/hash persona, dan hash bukti. Percobaan berikutnya hanya memakai calon yang binding-nya masih sama. | Preview nyata membuktikan calon bertahan saat snapshot diinvalidasi dan dipakai ulang tanpa panggilan AI kedua. Metadata yang sudah memiliki intent tidak ditulis ulang. |
| L07: transaksi kecil dan lokasi DB | Statistik nonkritis dibuffer lalu digabung dalam transaksi melalui thread. `Config.database_path` mendukung relokasi offline dengan pointer, pemeriksaan seluruh tabel, arsip sumber, dan tombstone yang menolak worker lama. Runtime media tetap di proyek; lokasi jurnal mengikuti konfigurasi sebelumnya. | `test_latency_api` menguji flush, gagal tulis, dan cancellation. `test_storage_location` menguji lease aktif, crash, symlink, WAL, dan stale writer. Durability jurnal intent tidak diturunkan. Statistik yang masih di memori dapat hilang bila proses dimatikan paksa. |
| L08: query scheduler | Pemakaian akun dihitung dengan tiga query agregat. Slot terpakai dibaca bersama per hari/platform. Migrasi schema v3 menambah indeks jadwal eksternal, event, asset, dan lookup webhook. Pencarian tetap dibatasi cakupan snapshot dan maksimal 366 hari. | `test_latency_db` memeriksa jumlah query, dedupe kuota lokal/remote, alokasi bersamaan, migrasi v2, dan rencana query webhook. Angka benchmark audit lama belum menjadi ukuran kinerja versi ini. |
| L09: slot terdekat | Di antara akun dengan beban terendah, allocator memilih slot tersedia paling awal; cursor memecahkan seri. | Tes memisahkan pemerataan beban, pilihan waktu, dan rotasi saat waktu sama. Batas 20 per akun/hari WIB serta jendela jadwal tetap berlaku. Mode tayang segera tidak ditambahkan. |
| L10: biaya verifikasi Drive | Koneksi dipakai kembali dan verifikasi dibatasi maksimal dua bersamaan. Slot diperiksa sebelum pekerjaan preflight mahal dan sesudahnya. | `test_latency_drive` dan `test_drive` menguji seluruh byte, ekor file berubah, cancellation, batas konkurensi, dan penutupan koneksi. Setiap delivery tetap memeriksa identitas serta checksum penuh sebelum POST; tidak ada cache checksum atau upload API kedua. |
| L11: webhook | Receiver loopback menerima token header melalui proxy HTTPS, menyimpan inbox durable, melakukan dedupe, dan mencocokkan akun/jadwal/post yang dikelola. Consumer memicu GET terarah atau membangunkan polling komentar. | `test_webhook` dan preview HTTP lokal menguji autentikasi, framing/body, replay, lease, retry, dan mapping berubah. Isi webhook tidak langsung mengubah status menjadi published. Polling tetap tersedia, termasuk YouTube dan event yang hilang. |

## Bukti yang sudah tersedia

Suite fokus DB, scheduler, foundation, migrasi, dan pipeline lulus 121 tes. Setelah integrasi relokasi cleanup dan audit caption, suite cleanup, captioning, dan lokasi penyimpanan lulus 61 tes. Jumlah ini berasal dari run terpisah dan tidak dijumlahkan sebagai hasil suite gabungan.

Preview HTTP lokal pada `runtime/verification/latency-implementation/webhook-preview.json` lulus. Tiga request menghasilkan 401, 202, dan 202; event duplikat menjadi satu item inbox dan satu GET provider tiruan. Event mengaku sukses, tetapi GET menunjukkan pending, sehingga job tetap scheduled. Preview ini tidak membaca produksi atau credential dan tidak memakai jaringan eksternal.

Pemeriksaan endpoint HTTPS publik pada `runtime/verification/latency-public-webhook.json` juga lulus untuk event testing: token salah mendapat 401, lalu dua pengiriman event testing yang sama mendapat 200 dan hanya menghasilkan satu item inbox selesai. Event testing memang memakai respons 200; event schedule yang masuk antrean memakai 202. Laporan mencatat nol mutasi produksi dan nol request publikasi. Pemeriksaan ini membuktikan jalur HTTPS dan autentikasi receiver, belum membuktikan event otomatis dari akun Repliz telah diterima.

Preview AI/Drive nyata pada `runtime/verification/latency-real-preview/report.json`, tercatat 21 Oktober 2026 pukul 03.55 UTC, lulus untuk satu manifest media yang dibaca secara read-only. Satu generasi persona memerlukan 5,702 detik; pemakaian ulang calon memerlukan 0,001 detik tanpa generasi tambahan. Tiga verifikasi Drive penuh selesai dalam 15,244 detik dengan batas dua verifikasi bersamaan. Laporan mencatat nol request publikasi, nol reply, dan nol mutasi produksi. Ini satu sampel fungsi, bukan median, P95, atau bukti seluruh alur input sampai tayang.

Fixture pascaimplementasi pada `runtime/verification/latency-implementation/perf.json` lulus dengan socket diblokir dan DB sementara dibersihkan. Runner [preview_latency_perf.py](../scripts/preview_latency_perf.py) memakai fixture audit yang sama tanpa mengubah script arsip. Kiriman siap B mulai memproses persona saat unduhan A masih ditahan; satu consumer campuran tetap menunggu A. Pada kedua kasus hanya ada satu unduhan A. Snapshot yang pulih pada detik satu langsung membuka klaim, dan percobaan ulang setelah perubahan snapshot tetap memakai satu hasil persona.

Untuk tiga asset dan sembilan delivery dengan delay adapter palsu 30 ms, hasil satu run pada kode baru adalah:

| Pembagian consumer | Waktu fixture |
| --- | ---: |
| Satu consumer campuran | 3,831 detik |
| Satu preparer dan satu sender | 2,871 detik |
| Dua preparer dan satu sender | 3,095 detik |

Semua variasi menghasilkan tiga unduhan, tiga analisis, sembilan persona, sembilan verifikasi Drive, dan sembilan jadwal tiruan. Dua preparer tidak selalu lebih cepat pada batch kecil ini. Waktu wall dapat terpengaruh verifikasi lain dan disk; hasil ini bukan perkiraan throughput produksi.

Matrix 100/500/1.000 delivery pada 63 dan 64 akun juga lulus. Pada 1.000 delivery/64 akun, jumlah statement enqueue turun dari 108.724 pada audit menjadi 21.787, sekitar 80%; pembacaan pemakaian akun menjadi tiga query dari 129. CPU enqueue run baru 0,924 detik, sedangkan audit mencatat 0,832 detik, sehingga penurunan query belum membuktikan penurunan CPU untuk kasus itu. Empat puluh delivery YouTube bertanggal mendatang dilewati sebelum satu TikTok siap: nol klaim YouTube dan 84 SELECT, dibanding 1.010 SELECT pada jalur campuran audit.

Stress 365 hari penuh memakai 1.114 statement dan CPU 293 ms, dibanding 4.046 statement dan CPU 2.287 ms pada baseline audit. Eksperimen index-only pada audit pernah mencapai CPU 38 ms; agregasi baru belum mengungguli eksperimen itu untuk kasus ekstrem ini. Pengukuran snapshot pada harness memanggil Store langsung, sehingga lag yang direkam bukan pengukuran jalur sync Repliz yang kini memakai thread. Tidak ada klaim kecepatan produksi berdasarkan run ini.

## Relokasi dan cleanup

Relokasi harus dijalankan offline dengan proses, reply, dan laporan dijeda serta tanpa lease atau pengiriman aktif. `storage status` menunjukkan lokasi yang dipakai; `storage migrate` memindahkan DB dengan lock worker yang sama. Backup tetap membawa database dan jurnal intent. Pemulihan harus menggunakan backup konsisten yang baru, bukan mengarahkan pointer kembali ke arsip lama setelah ada penulisan baru.

Cleanup membaca DB aktif melalui pointer yang diverifikasi, termasuk DB peer yang sudah direlokasi. Pemindaian preview dan DB lama tetap berlaku. Arsip `before-move` juga ikut melindungi referensi file. Akibatnya media dapat tertahan lebih lama meskipun semua tujuan sudah published dan melewati 24 jam. Tugas ini tidak menghapus arsip atau melonggarkan perlindungan referensi. Direktori tombstone hanya dilewati jika `MOVED.json` cocok; DB hilang, hardlink, symlink, atau penanda yang tidak cocok menahan cleanup.

HTTPS publik `https://repliz.fahra.my.id/repliz/webhook` juga lulus: token salah 401, event testing sah 200, replay 200 tetapi tetap satu record. Receiver sementara memakai DB fixture, kemudian ditutup. Aktivasi endpoint pada akun Repliz masih terpisah dari uji sambungan ini.

Pemeriksaan ruang kosong mencakup volume DB, runtime media, bank, dan downloader. Relokasi DB tidak boleh menutupi kondisi disk media yang hampir penuh.

## Bukti yang masih harus ditutup

Verifikasi gabungan 572 tes, CI pada SHA tepat, merge ff-only, push main, relokasi DB, dan startup produksi sudah lulus. Aktivasi endpoint/token pada akun Repliz masih menunggu Save oleh pengguna. Penutupan tercatat di worklog dan manifest runtime. Verifier penuh yang dipakai:

```sh
./scripts/verify.sh --mode full --base origin/main --secrets-from PATH_CREDENTIAL
```

Preview nyata di atas sudah memeriksa jalur persona dan Drive yang berubah. Ia belum menguji batch media baru dari input sampai published, gangguan jaringan produksi berkepanjangan, atau distribusi waktu setelah deployment. Setelah aktivasi yang terkontrol, bandingkan antrean ringan dan batch dengan timestamp Telegram, penerimaan durable, tiap tahap, alasan menunggu, intent, jadwal diterima, waktu jadwal, published terverifikasi, dan notifikasi. Target lima menit pada audit tetap sasaran pengukuran untuk kondisi yang dijelaskan di sana, bukan hasil yang sudah tercapai.


## Pemasangan produksi

Commit `4a2b3d14e5a6dd2559cd3c5e3878f90b7752814a` lulus [CI branch](https://github.com/ramdanifahmy2023/fahra-farming-autonomous-fb-tiktok-ytb/actions/runs/39541965377) dan [CI main](https://github.com/ramdanifahmy2023/fahra-farming-autonomous-fb-tiktok-ytb/actions/runs/39542238861). Main digabung ff-only, diverifikasi penuh lagi, dan SHA remote cocok. Backup bundle sebelum/sesudah relokasi lulus. Fingerprint semua tabel sumber dan tujuan cocok; 1.505 jobs, 607 assets, 583 batches, 2.271 comment events, 427 reply attempts, dan 19 report parts tidak berubah oleh relokasi.

DB aktif berada pada penyimpanan internal melalui pointer; schema v3 aktif. Runtime media, bank, jurnal, ownership, dan lock mempertahankan lokasi operasional sebelumnya. Startup siap pada 11:09:35 WIB, PID 12528. Kontrol dikembalikan ke paused=false, reply/report aktif. Pemeriksaan awal menunjukkan snapshot segar dan tidak ada event failure baru sejak resume. Uji endpoint produksi menghasilkan 401/200. Ini belum menjadi bukti bahwa Repliz sudah mengirim event nyata atau bahwa durasi input-ke-published sudah memenuhi target audit.
