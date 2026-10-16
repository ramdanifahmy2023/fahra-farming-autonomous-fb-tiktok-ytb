"""Telegram-native views. Dynamic values are escaped before HTML formatting."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from html import escape
from math import ceil
from zoneinfo import ZoneInfo

TERMINAL = {"published", "failed", "cancelled", "duplicate"}
STAGES = ("Unduh Instagram", "Caption & hashtag", "Edit video", "Simpan ke Drive", "Publikasi")
STAGE_INDEX = {"queued": 0, "downloaded": 1, "analyzed": 2, "rendered": 3,
               "drive_ready": 4, "submitting": 4, "submit_unknown": 4, "scheduled": 4, "published": 5}
LABELS = {"queued": "Menunggu giliran", "downloaded": "Analisis konten", "analyzed": "Edit video",
          "rendered": "Sinkronisasi Drive", "drive_ready": "Siap dikirim", "submitting": "Mengirim ke Repliz",
          "submit_unknown": "Memeriksa pengiriman", "scheduled": "Menunggu publikasi", "published": "Sudah terbit",
          "failed": "Perlu diperiksa", "cancelled": "Dibatalkan", "duplicate": "Duplikat"}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")
ZONE = ZoneInfo("Asia/Jakarta")


def platform_name(job):
    return {"tiktok": "TikTok", "facebook": "Facebook", "youtube": "YouTube"}.get(job.get("platform"), "Tujuan")


def e(value, limit=200):
    text = str(value or "")
    return escape(text if len(text) <= limit else text[:limit - 1] + "…")


def day(value):
    if not value:
        return "Menunggu alokasi"
    d = date.fromisoformat(value)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def clock_time(value):
    return datetime.fromisoformat(value).astimezone(ZONE).strftime("%H:%M WIB") if value else ""


def label(job):
    state = job["status"]
    if state == "queued" and job.get("leased"):
        return "Mengunduh Instagram"
    if state not in TERMINAL and job.get("last_error") and not job.get("leased"):
        return "Menunggu Drive" if "Menunggu sinkronisasi Bank konten" in job["last_error"] else "Menunggu pemeriksaan" if not job.get("attempts") else "Menunggu percobaan ulang"
    return LABELS.get(state, "Status belum dikenali")


def note(job):
    state, error = job["status"], job.get("last_error") or ""
    if state == "failed":
        stage = job.get("payload", {}).get("resume_status", state)
        cause = {"queued": "Video belum berhasil diunduh.", "downloaded": "Analisis konten belum berhasil.",
                 "analyzed": "Edit video belum berhasil.", "rendered": "Salinan Drive belum dapat diverifikasi."}.get(stage, "Publikasi belum berhasil dikonfirmasi.")
        return cause + " Admin dapat memeriksa detail dengan /video " + job["id"][:8] + "."
    if state == "cancelled":
        return "Pekerjaan dibatalkan sebelum dikirim."
    if state == "duplicate":
        return "Isi video sama dengan pekerjaan lain; pengiriman dilewati."
    if state == "published":
        return "Tautan posting sedang diambil. Bot akan mengirimkannya otomatis." if not job["payload"].get("post_url") else ""
    if state == "submit_unknown":
        return "Bot mencocokkan hasil pengiriman sebelum melanjutkan."
    if state == "scheduled":
        return "Jadwal diterima. Bot menunggu konfirmasi bahwa video sudah terbit."
    if "Menunggu sinkronisasi Bank konten" in error:
        return "Video selesai diedit. Menunggu salinan Google Drive, lalu bot melanjutkan otomatis."
    if error and not job.get("leased"):
        if job.get("attempts"):
            return f"Tahap ini akan dicoba lagi mulai {clock_time(job.get('next_attempt'))}."
        return "Ada layanan yang belum siap. Bot memeriksa kembali otomatis; admin dapat melihat /video " + job["id"][:8] + "."
    if state == "drive_ready" and job.get("slot_date") and job["slot_date"] > datetime.now(ZONE).date().isoformat():
        return "Video siap. Menunggu tanggal slot akun."
    return ""


def steps(job):
    state = job["status"]
    if state in {"cancelled", "duplicate"}:
        return ""
    effective = ("scheduled" if job.get("schedule_id") else job["payload"].get("resume_status", state)) if state == "failed" else state
    index = STAGE_INDEX.get(effective, 0)
    rows = []
    for i, name in enumerate(STAGES):
        if i < index:
            rows.append(f"✓ {e(name)}")
        elif i == index:
            suffix = "perlu diperiksa" if state == "failed" else "dibatalkan" if state == "cancelled" else "dilewati" if state == "duplicate" else label(job).lower()
            rows.append(f"<b>{i + 1}. {e(name)}</b> · {e(suffix)}")
        else:
            rows.append(f"{i + 1}. {e(name)}")
    return "\n".join(rows)


def html_options(rows=None):
    return {"parse_mode": "HTML", "reply_markup": {"inline_keyboard": rows or []}}


def publication(job):
    p = job["payload"]
    title = e(p.get("generated", {}).get("hook"), 100)
    text = f"<b>Video sudah terbit di {platform_name(job)}</b>\n{e(p.get('published_username'), 60)}\n"
    if title:
        text += "\n" + title + "\n"
    text += f"\nVideo <code>{job['id'][:8]}</code>\n{e(p['post_url'], 300)}"
    return text, html_options([[{"text": "Buka " + platform_name(job), "url": p["post_url"]}]])


def video(job, admin=False):
    text = f"<b>{e(label(job))}</b>\n{platform_name(job)} · Kiriman <code>{job['id'][:8]}</code>"
    username = job.get("username") or job["payload"].get("published_username")
    text += f"\n{e(username, 60)}" if username else "\nAkun menunggu alokasi"
    text += f"\nSlot {day(job.get('slot_date'))} · {clock_time(job.get('planned_at')) or 'waktu belum dialokasikan'}\n\n{steps(job)}"
    if note(job):
        text += "\n\n" + e(note(job), 400)
    if admin and job.get("last_error"):
        text += "\n\n<b>Detail untuk admin</b>\n" + e(job["last_error"], 550)
    rows = []
    if job["status"] == "published" and job["payload"].get("post_url"):
        url = job["payload"]["post_url"]
        text += "\n\n" + e(url, 300)
        rows.append([{"text": "Buka " + platform_name(job), "url": url}])
    return text, html_options(rows)


def batch(batch, jobs, paused=False):
    count = Counter(j["status"] for j in jobs)
    finished = all(j["status"] in TERMINAL for j in jobs)
    page = min(batch.get("page", 0), ceil(len(jobs) / 4))
    title = "Batch selesai" if finished else "Antrean dijeda" if paused else "Sedang diproses"
    if not finished and not paused:
        if len(jobs) == 1:
            title = label(jobs[0])
        elif all(j["status"] == "queued" and not j.get("leased") for j in jobs):
            title = "Batch masuk antrean"
    if count["failed"]:
        title = "Batch perlu diperiksa" if finished else "Proses berjalan, ada kendala"
    if not jobs:
        title = "Tidak ada video baru"
    reel_count = len({j.get("asset_id", j["id"]) for j in jobs})
    lines = [f"<b>{title}</b>", f"Batch <code>{batch['id'][:8]}</code> · {reel_count} Reel · {len(jobs)} kiriman"]
    meta = batch.get("receipt", {})
    skipped = []
    if meta.get("duplicates"):
        skipped.append(f"{meta['duplicates']} duplikat")
    if meta.get("invalid"):
        skipped.append(f"{meta['invalid']} link tidak valid")
    if skipped:
        lines.append("Dilewati: " + " · ".join(skipped))
    rows = []
    callback = lambda text, p: {"text": text, "callback_data": f"batch:{batch['id']}:{p}"}
    if not jobs:
        lines.append("\nKirim link Reel lain untuk menambah video.")
    elif page:
        lines.append(f"\n<b>Rincian kiriman · halaman {page}/{ceil(len(jobs) / 4)}</b>")
        for j in jobs[(page - 1) * 4:page * 4]:
            lines.append(f"\n<code>{j['id'][:8]}</code> · {platform_name(j)} · {e(label(j))}\n{e(j.get('username') or 'menunggu alokasi', 40)} · {day(j.get('slot_date'))}")
            if note(j):
                lines.append(e(note(j), 220))
            if j["status"] == "published" and j["payload"].get("post_url"):
                rows.append([{"text": "Buka " + platform_name(j) + " · " + j["id"][:8], "url": j["payload"]["post_url"]}])
        nav = []
        if page > 1:
            nav.append(callback("Sebelumnya", page - 1))
        if page * 4 < len(jobs):
            nav.append(callback("Berikutnya", page + 1))
        if nav:
            rows.append(nav)
        rows.append([callback("Kembali ke ringkasan", 0)])
    elif reel_count == 1:
        j = jobs[0]
        hook = j["payload"].get("generated", {}).get("hook")
        if hook:
            lines.append("\n" + e(hook, 100))
        lines.append("\n" + "\n".join(steps(j).splitlines()[:4]))
        for target in jobs:
            lines.append(f"\n<b>{platform_name(target)} · {e(label(target))}</b>\n"
                         f"{e(target.get('username') or 'Akun menunggu alokasi', 50)} · {day(target.get('slot_date'))}")
            if note(target):
                lines.append(e(note(target), 320))
            if target["status"] == "published" and target["payload"].get("post_url"):
                rows.append([{"text": "Buka " + platform_name(target), "url": target["payload"]["post_url"]}])
        rows.append([callback("Rincian semua tujuan", 1)])
        rows.append([{"text": "Lihat Reel sumber", "url": j["url"]}])
    else:
        active = len(jobs) - sum(count[s] for s in TERMINAL) - count["queued"]
        lines.extend([f"\n<b>{count['published']} dari {len(jobs)} kiriman terbit</b>",
                      f"Menunggu giliran: {count['queued']}\nDalam proses: {active}"])
        for key in ("failed", "cancelled", "duplicate"):
            if count[key]:
                lines.append(f"{LABELS[key]}: {count[key]}")
        for platform in ("tiktok", "facebook", "youtube"):
            targets = [j for j in jobs if j["platform"] == platform]
            lines.append(f"{platform_name({'platform': platform})}: {sum(j['status'] == 'published' for j in targets)}/{len(targets)} terbit")
        working = [j for j in jobs if j["status"] not in TERMINAL and (j["status"] != "queued" or j.get("leased"))]
        if working:
            lines.append("\n<b>Sedang ditangani</b>")
            for j in working[:3]:
                lines.append(f"{platform_name(j)} · {e(label(j))} · {e(j.get('username') or 'menunggu alokasi', 40)}")
            if len(working) > 3:
                lines.append(f"+{len(working) - 3} kiriman lain dalam proses")
        slots = Counter(j.get("slot_date") for j in jobs)
        lines.append("\nSlot WIB: " + "; ".join(f"{day(d)} ({n})" for d, n in list(slots.items())[:3]))
        if len(slots) > 3:
            lines.append(f"+{len(slots) - 3} tanggal lainnya di rincian video")
        rows.append([callback("Rincian semua kiriman", 1)])
    if jobs and not finished:
        lines.append("\nPesan ini diperbarui otomatis. Link dikirim saat video terbit.")
    if paused and not finished:
        lines.append("Pengambilan pekerjaan baru dijeda; jadwal Repliz tetap dipantau.")
    return "\n".join(lines), html_options(rows), finished


def status(summary):
    lines = ["<b>Status workflow</b>", day(summary["date"]) + " · WIB",
             f"\nAntrean {'dijeda' if summary['paused'] else 'aktif'} · {summary['accounts']} akun",
             f"Slot hari ini: {summary['reserved_or_published']}/{summary['daily_capacity']}",
             "Mencakup reservasi dan publikasi."]
    for platform, values in summary.get("platforms", {}).items():
        lines.append(f"{platform_name({'platform': platform})}: {values['accounts']} akun · "
                     f"{values['reserved_or_published']}/{values['daily_capacity']} slot")
    lines.append("\n<b>Seluruh kiriman</b>")
    lines += [f"{LABELS.get(s, s)}: {n}" for s, n in summary["statuses"].items()]
    if not summary["statuses"]:
        lines.append("Belum ada video. Kirim link Reel untuk memulai.")
    return "\n".join(lines), html_options()
