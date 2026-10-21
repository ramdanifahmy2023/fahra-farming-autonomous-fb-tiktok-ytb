# Caption sumber dan persona reply

Permintaan 21 Oktober 2026: deskripsi cukup menulis ulang caption Instagram asli supaya tidak terasa kaku, dan teks hook di video dihilangkan. Kode memakai kebijakan `id-source-rewrite-v1`. Perubahan ini berada pada worker Fahra Automation; aplikasi Arker terpisah.

## Alur deskripsi baru

1. Downloader mencocokkan metadata Instagram dengan URL, shortcode, dan pemilik Reel. Caption asli disimpan utuh bersama bukti sumber dan hash. Judul otomatis, nama tampilan, caption MCP yang belum dibuktikan, frame, dan transkrip tidak menjadi pengganti caption asli.
2. Satu tahap AI menulis ulang caption tersebut dengan perubahan ringan. Hasil yang sudah enak dibaca boleh dipertahankan. Maksud, nada, angka, nama, negasi, dan ketidakpastian sumber tetap dijaga; model tidak menambah reaksi, cerita, atau penjelasan adegan.
3. TikTok, Facebook, dan YouTube memakai hasil bersama. Pemilihan akun tidak memicu rewrite persona lagi. YouTube tetap memiliki judul tersendiri yang merangkum caption yang sama.
4. Adapter menambahkan `source: @username` dari pemilik terverifikasi, lalu hashtag sumber yang dipilih. Batas karakter diperiksa sesudah kredit ditambahkan.

Caption asli diperlakukan sebagai data, termasuk jika isinya memerintah AI mengabaikan aturan. Model tidak boleh mengaku sebagai pemilik, perekam, atau orang yang mengalami kejadian dalam caption sumber. Cerita orang pertama perlu ditulis sebagai cerita dari sumber bila identitas pembicara dapat tertukar. Pemakaian bahasa santai mengikuti nada caption, tanpa memaksakan slang, lelucon, atau emoji.

Caption kosong yang terkonfirmasi menghasilkan kredit sumber saja. Caption tanpa narasi, seperti emoji, hashtag, URL, mention, atau angka saja, diproses tanpa panggilan AI. Emoji, mention, dan angka dipertahankan; URL serta baris kredit lama dibuang, dan maksimal lima hashtag sumber dipisahkan. Jika tidak ada isi deskripsi yang tersisa, keluaran cukup kredit sumber beserta hashtag yang tersedia. Judul kasus ini adalah `Video dari Instagram`, bukan dugaan isi video. Metadata hilang, bentuk caption tidak valid, atau pemilik/shortcode yang berbeda menahan proses; kondisi ini tidak dianggap caption kosong.

Frame dan transkrip tetap dapat disiapkan serta disimpan untuk kebutuhan konteks reply. Kedua bahan itu tidak dikirim sebagai bahan rewrite deskripsi. `confidence_kind=source_fidelity` menilai kesetiaan tulisan pada caption, bukan kebenaran klaim sumber atau pemahaman video.

## Pemeriksaan dan batasnya

Caption hasil maksimal 1.800 karakter, judul satu baris maksimal 100 karakter. Hashtag boleh kosong, maksimal lima, dan hanya berasal dari caption Instagram. Validator menolak tambahan angka atau mention, angka sumber yang hilang, URL dan kredit sumber buatan, serta teks hook. Bukti caption terikat ke sumber yang sama dan hash diperiksa kembali sebelum metadata tujuan disiapkan.

AI mendapat paling banyak satu kesempatan memperbaiki format atau kesetiaan teks pada tahap yang sama. Confidence di bawah 0,8 ditahan; perbaikan tidak boleh menaikkannya. Kesalahan tidak ditutup dengan caption generik. Caption tanpa narasi menggunakan hasil deterministik sesuai sumber, sehingga tidak membutuhkan perbaikan AI.

Pemeriksaan struktur dan angka tidak membuktikan semua makna dipertahankan. Preview manusia tetap membandingkan caption sumber dengan hasil rewrite. Satu contoh yang lulus juga tidak menjamin setiap hasil berikutnya akan terasa natural.

## Video baru dan antrean lama

Renderer baru tidak menambah overlay pada detik berapa pun. Orientasi sumber tetap, zoom 110%, output 30 fps, H.264/AAC, dan audio asli dipertahankan. Catatan render menyimpan `hook_enabled=false` dan `hook_seconds=0`. Nilai hook lama pada config tidak mengaktifkan overlay. [HOOK_VISUAL.md](HOOK_VISUAL.md) menyimpan gaya lama sebagai arsip; font tetap dipakai laporan followers.

Asset yang sudah diunduh atau sudah dianalisis dengan kebijakan lama tetapi belum dirender mengambil bukti caption dan menghasilkan rewrite baru sebelum render pertama. Asset yang sudah dirender atau masuk Bank/Drive tidak dirender ulang otomatis. Intent, jadwal, publikasi, dan body pengiriman yang sudah tersimpan tetap dipertahankan. Perubahan ini tidak mengirim ulang posting lama.

Jalur `id-natural-v4` tetap tersedia untuk metadata dan arsip lama yang membutuhkannya. Pada jalur lama, caption tujuan dapat terikat ke snapshot persona akun dan riwayat caption menjadi pembanding bahasa. Aturan tersebut tidak menambah tahap persona pada rewrite sumber baru.

## Persona dan balasan

Registry persona lokal tetap terikat ke ID akun Repliz dengan versi immutable. Default Indonesia natural dan persona khusus tetap tersedia untuk reply. Caption baru tidak memakai instruksi persona akun untuk mengubah isinya.

Reply tetap memakai konteks video, transkrip, komentar, dan percakapan induk. Confidence kesetiaan rewrite tidak boleh dipakai sebagai confidence konteks reply. Profil default Gen Z, variasi hint, kuota, deduplikasi, dan draft tersimpan mengikuti jalur reply yang ada. Konten serius memakai bahasa tenang; fakta yang tidak diketahui tidak dikarang.

Native reply Repliz tetap nonaktif pada posting milik worker custom. Kepemilikan reply, like, moderasi, dan larangan like komentar YouTube tidak berubah.

## Pemeriksaan dan pemasangan

Perubahan kode memerlukan verifier full, preview AI/media terisolasi, CI pada SHA tepat, lalu pemasangan dan restart layanan yang berubah. Kontrol awal dicatat, lease diselesaikan, backup dibuat, dan snapshot startup diperiksa sebelum kontrol dipulihkan. Preview tidak membuat jadwal, posting, balasan, atau pesan Telegram.

Pada checkpoint dokumentasi ini, dua pembacaan metadata Instagram nyata dan 51 tes terarah attribution/downloader/foundation sudah lulus. Preview media nyata/sintetis beserta 10 tes media juga lulus; suite terarah gabungan mencapai 76 tes PASS. Preview AI memakai dua caption nyata: satu caption narasi selesai dalam 6,908 detik dengan satu panggilan model, sedangkan satu emoji dipertahankan dalam 0,013 detik tanpa AI. Ketiga payload fixture memakai deskripsi/kredit yang sama, dengan nol panggilan AI tambahan saat delivery. Kata ketidakpastian pada caption narasi tetap dipertahankan.

Preview ini membuat nol request publikasi, reply, atau Drive dan tidak mengakses DB produksi. Hasilnya membuktikan dua contoh rewrite dan payload fixture, bukan publikasi atau jaminan kualitas semua caption. Angka tes penuh serta status pemasangan tidak disimpulkan dari suite terarah. Bukti tugas disimpan di `runtime/verification/caption-rewrite-no-hook-20261021/`, dengan salinan bukti worktree pada `worktree-verification/`. Commit, CI, pemasangan, dan pembersihan mengikuti `final-git.json` di folder tersebut.

Bukti kebijakan sebelumnya tetap berada di `runtime/verification/natural-copy-20261020/`.
