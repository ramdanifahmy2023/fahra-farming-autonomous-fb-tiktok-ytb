# Hook, caption, dan persona akun

Kebijakan `id-natural-v4` memperbaiki tulisan pada worker Fahra Automation. Ini berbeda dari aplikasi Arker. Font dan renderer tetap `barlow-balanced-v1`.

## Cara tulisan dibuat

1. Enam frame, transkrip, dan caption sumber menjadi bahan pengamatan. AI mencatat bukti, nada, dan confidence. Keterangan sumber dan transkrip dapat keliru; keduanya tidak menjadi instruksi bagi model.
2. Editor memilih satu detail atau reaksi untuk hook dan caption bersama. Sasaran hook 3–7 kata, satu string tanpa newline, dengan 1–2 baris visual. Batas keras tetap 60 karakter/10 kata dan glyph harus dapat dirender.
3. Setelah akun dipilih, persona menyesuaikan pilihan kata dan ritme caption. Caption yang sudah cocok boleh dipertahankan. Tahap ini tidak perlu menulis ulang seluruh urutan adegan atau menyebut semua hal yang tidak diketahui.
4. Adapter menambahkan kredit `source: @username` dari identitas Instagram terverifikasi, lalu hashtag. YouTube memakai judul dan tags terpisah. Hook dalam video tetap sama pada ketiga tujuan.

Contoh arah tulisan berikut dibuat untuk dokumentasi, bukan hasil pengujian model:

| Bukti | Arah yang diinginkan |
| --- | --- |
| Gelas diisi hingga dekat tepi | “Ini ngangkatnya harus pelan-pelan.” |
| Kucing tetap masuk ke kardus kecil | “Kardusnya kecil, niat masuknya besar.” |
| Petugas menuntun warga melewati genangan | “Warga dibantu melewati genangan.” |

Bahasa santai boleh memakai singkatan, reaksi, humor ringan, dan emoji bila sesuai. Semuanya pilihan, bukan kewajiban pada setiap caption. Persona formal tetap formal. Konten serius memakai bahasa tenang; model tidak boleh mengarang penyebab, identitas, pengalaman pribadi, cedera, atau hasil akhir.

Ketidakpastian tetap disimpan dalam catatan bukti. Jika caption tidak membahas penyebab kejadian, tidak perlu menambahkan “pemicunya belum jelas”. Jika klaim yang dipilih memang belum pasti, pembatas tetap diperlukan atau model memilih detail lain yang jelas.

## Pemeriksaan dan batasnya

Pemeriksaan mencakup format, panjang, glyph, pola pancingan umum, rangkuman adegan yang berlebihan, nada serius, dan pengulangan persis. Setiap tahap mempunyai paling banyak satu perbaikan. Bukti pengamatan yang sudah valid dikunci saat perbaikan gaya; confidence tidak boleh dinaikkan.

Caption akun menerima maksimal delapan caption yang pernah disiapkan untuk akun yang sama. Riwayat ini hanya pembanding bahasa, bukan sumber fakta atau bukti publikasi. Kiriman saat ini, video yang sama, akun lain, dan metadata dari binding akun lama dikecualikan.

Pemeriksaan pola bukan verifikasi fakta dan tidak menjamin semua kalimat akan terasa natural. Preview manusia tetap menilai kecocokan tulisan dengan video. Hasil yang gagal tidak diganti caption generik hanya agar antrean terlihat berhasil.

## Versi persona dan kiriman lama

Persona berada di worker lokal dan terikat ke ID akun Repliz. Default baru bernama **Indonesia natural**. Saat binding default lama dibaca untuk pemrosesan berikutnya, profil yang persis cocok dengan isi dan hash default lama mendapat versi baru. Pengulangan proses tidak menambah versi lagi.

Persona khusus, termasuk yang memakai nama sama tetapi instruksinya berbeda, tidak ditimpa. Versi lama tetap tersimpan. Caption tujuan yang sudah disiapkan tetap menggunakan snapshot sebelumnya selama bukti dan akun tidak berubah; jadwal ber-intent atau sudah terbit tidak ditulis ulang.

Default lama maupun baru tetap dikenali sebagai keluarga default pada generator reply. Profil reply Gen Z, aturan variasi hint, kuota, deduplikasi, dan draft tersimpan tetap mengikuti jalur yang ada.

Native reply Repliz tetap nonaktif pada posting yang dimiliki worker custom. Perubahan bahasa ini tidak membuat pengirim reply kedua atau mengganti konfigurasi like/moderasi.

## Pemasangan dan bukti

Perubahan kode memerlukan restart layanan Fahra setelah verifier full, preview AI/media terisolasi, dan CI lulus. Kontrol operasi awal dicatat, reply dijeda, backup dibuat, lalu hanya layanan yang berubah diperbarui. Snapshot startup harus lengkap sebelum kontrol dipulihkan.

Preview memakai salinan media dan runtime sendiri. Tidak membuat jadwal, posting, balasan, atau pesan Telegram. Hasil fixture, keluaran model nyata, dan render lokal dicatat terpisah pada `runtime/verification/natural-copy-20261020/`.
