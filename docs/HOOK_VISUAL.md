# Render tanpa hook dan arsip gaya lama

Permintaan terbaru 21 Oktober 2026 menghilangkan visual hook sepenuhnya. Renderer baru tidak menambah teks atau kartu pada detik berapa pun. Orientasi sumber tetap, zoom 110%, output 30 fps, H.264/AAC, serta audio asli dipertahankan. Caption/deskripsi mengikuti [rewrite caption Instagram](COPY_AND_PERSONA.md).

Render baru menyimpan `edits.hook_enabled=false` dan `edits.hook_seconds=0`. Default `video.hook_seconds=0`; renderer mengabaikan nilai hook lama yang mungkin masih ada pada konfigurasi lokal. Render baru tidak membuat `hook.txt`, `hook.png`, atau `hook-layout.json`.

Asset downloaded atau analyzed lama yang belum dirender memakai caption sumber sebelum render pertama. Video yang sudah dirender, masuk Bank/Drive, memiliki intent, terjadwal, atau terbit tidak direvisi atau dikirim ulang otomatis. Font Barlow, lisensi, dan kode pendukung arsip tetap disimpan karena laporan followers dan pengujian arsip masih memakainya.

Pemeriksaan baru membuktikan tidak ada input/filter overlay, hook yang dikirim pemanggil tidak tampil, serta orientasi, zoom, audio dan 30 fps tetap sesuai. Preview media nyata/sintetis terisolasi dan 10 tes media lulus tanpa publikasi. Bukti akhir kode, pengujian gabungan, pemasangan dan cleanup mengikuti `runtime/verification/caption-rewrite-no-hook-20261021/final-git.json`; preview tidak menjadi bukti deployment.

## Gaya 16 Oktober 2026 (arsip)

Permintaan pengguna 16 Oktober 2026: perbaiki font dan visual teks pembuka. Gaya `barlow-balanced-v1` mengikuti video singkat berbahasa Indonesia dengan nada santai yang sudah dipilih pengguna. Arah visual: huruf tegas, komposisi ringkas, tanpa animasi tambahan (energy 2, rhythm 2, motion 1). Penekanan berasal dari tipografi; gambar video tetap menjadi isi utama.

Bagian di bawah mencatat kontrak dan hasil pengujian versi tersebut. Instruksi hook tiga detik tidak berlaku untuk render baru setelah perubahan 21 Oktober.

## Aturan tampilan

- Font **Barlow Semi Condensed Bold**: bentuk agak ramping membantu kalimat pendek tetap besar pada layar HP. File font ikut paket, sehingga hasil tidak bergantung pada font Mac atau container.
- Warna putih pada kartu charcoal `#17191C`, opacity 236/255, sudut sedikit membulat. Latar menahan perubahan terang video tanpa stroke tebal pada huruf. Pada latar putih, hasil komposit terburuk dibulatkan menjadi `#292B2D`; rasio kontras teks 14,21:1 sebelum encoding.
- Pemenggalan memakai lebar glyph sebenarnya. Hook pendek tetap satu baris besar; berikutnya dicari dua baris seimbang, lalu maksimal tiga baris. Kata tidak dipotong, isi tidak diberi elipsis. Ukuran 6,1–8,1% lebar video, sekitar 22–29 px saat video ditampilkan selebar 360 px.
- Kartu maksimal 76% lebar video, pusat di 47% lebar, bagian atas di 18% tinggi. Ada ruang di kanan dan bawah untuk antarmuka platform. Posisi ini tetap, belum mengikuti wajah/objek secara otomatis.
- Hook tampil mulai detik 0 sampai sebelum 3. Orientasi asli, zoom 110%, durasi, H.264/AAC, audio sumber, dan kredit `source: @username` pada caption/deskripsi tetap mengikuti kontrak sebelumnya.

Hook tetap maksimal 60 karakter/10 kata. Karakter yang tidak ada pada font dan teks yang tidak muat ditolak sebelum menjadi video, sehingga kotak glyph kosong tidak terbit. Pemeriksaan yang sama dijalankan saat validasi AI agar kesempatan perbaikan yang sudah tersedia dapat memperbaiki hook. Emoji pada caption dan reply tetap diperbolehkan menurut kebijakannya.

## Jejak dan operasi

Setiap render menyimpan `hook.txt`, `hook.png`, dan `hook-layout.json` di folder kerja asset. JSON mencatat versi gaya, font, ukuran, baris, kanvas, dan posisi kartu. File final tetap diganti hanya setelah probe lulus. File hook adalah bukti kecil, bukan video duplikat; cleaner tidak diperluas pada tugas ini.

Perubahan berlaku saat asset masuk tahap render berikutnya. Video yang sudah dirender, masuk Bank/Drive, dijadwalkan, atau terbit tidak direvisi atau diposting ulang otomatis. Tidak ada bot/polling kedua untuk pengujian.

Pasang dependency dari `requirements.lock` sebelum restart layanan. Font dan lisensi harus ikut paket. Verifier menerima hanya file font dengan path dan SHA-256 yang ditetapkan; biner lain atau font yang berubah tetap ditolak. Jangan mengganti hash hanya agar verifier lolos tanpa memeriksa sumber/lisensinya.

## Pemeriksaan

- Tes unit mengukur teks dan batas piksel pada 540×960, 720×1280, dan 1080×1920; hook panjang, aksen, tanda baca, glyph hilang, dan kegagalan render diperiksa.
- Preview nyata memakai salinan media Reel `DbnsuCYybBP`, bukan publikasi baru. Preview sintetis berlatar putih/hitam memakai penanda kiri merah dan kanan biru untuk memeriksa orientasi dan waktu hilangnya hook.
- Bukti akhir disimpan di `runtime/verification/hook-visual-20261016/`. Lihat worklog untuk jumlah tes, CI, pemasangan, dan batas bukti aktual.
- Review visual: PASS keterbacaan 360 px dan ukuran asli; PASS teks tetap dalam kartu; PASS tujuan font/latar/posisi terdokumentasi; PASS tidak ada dekorasi atau klaim baru. Interaksi keyboard/tombol tidak berlaku pada overlay video.

Font memakai [SIL Open Font License 1.1](../fahra_automation/assets/fonts/OFL.txt). Sumber resmi: [Google Fonts, Barlow Semi Condensed](https://github.com/google/fonts/tree/main/ofl/barlowsemicondensed). Pengukuran menggunakan [Pillow ImageFont](https://pillow.readthedocs.io/en/stable/reference/ImageFont.html).
