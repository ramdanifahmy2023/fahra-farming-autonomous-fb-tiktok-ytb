"""Native Telegram HTML with dated evidence and a complete account list."""
from __future__ import annotations

import html
import re
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Jakarta')
LIMIT = 3500
TARGET = 600
ACCOUNTS_PER_PAGE = 8


def number(value):
    return f'{value:,}'.replace(',', '.') if type(value) is int else 'belum tersedia'


def stamp(value):
    return datetime.fromisoformat(value).astimezone(ZONE).strftime('%d/%m %H:%M WIB')


def username(value):
    name = str(value).strip().lstrip('@')[:80]
    label = '@' + html.escape(name)
    return f'<a href="https://www.tiktok.com/@{name}">{label}</a>' if re.fullmatch(r'[A-Za-z0-9_.]{1,40}', name) else label


def account_groups(rows):
    """Only fresh successful counters can establish target status."""
    valid = [r for r in rows if r['status'] == 'ok']
    order = lambda r: (-r['data']['followers'], r['username'], r['account_id'])
    return (sorted((r for r in valid if r['data']['followers'] >= TARGET), key=order),
            sorted((r for r in valid if r['data']['followers'] < TARGET), key=order),
            sorted((r for r in rows if r['status'] != 'ok'), key=lambda r: (r['username'], r['account_id'])))


def target_percent(followers):
    value = min(followers, TARGET) * 100 / TARGET
    return (f'{value:.1f}'.rstrip('0').rstrip('.') if value % 1 else str(int(value))).replace('.', ',') + '%'


def delta(row, base, days):
    label = 'Harian' if days == 1 else '7 hari'
    if not base:
        return f'{label}: baseline D−{days} belum tersedia'
    current, previous = row['data']['followers'], base['data']['followers']
    difference = current - previous
    change = ('+' if difference > 0 else '−' if difference < 0 else '') + number(abs(difference))
    percent = f' ({difference / previous * 100:+.2f}%)'.replace('.', ',') if previous else ' (baseline 0; % tidak dihitung)'
    hours = (datetime.fromisoformat(row['fetched_at']) - datetime.fromisoformat(base['fetched_at'])).total_seconds() / 3600
    interval = 'sekitar 24 jam' if days == 1 and 23 <= hours <= 25 else f"{stamp(base['fetched_at'])} → {stamp(row['fetched_at'])}"
    return f'{label}: <b>{change}</b>{percent} · {interval}'


def daily_series(run, series, *, days=7):
    """Use calendar positions; missing and partial days remain visible gaps."""
    end = date.fromisoformat(run['day'])
    by_day = {point['day']: point for point in series}
    return [(day.isoformat(), by_day.get(day.isoformat()))
            for day in (end - timedelta(days=offset) for offset in reversed(range(days)))]


def sample_window(rows):
    times = sorted((r['fetched_at'] for r in rows if r['status'] == 'ok' and r.get('fetched_at')),
                   key=datetime.fromisoformat)
    if not times:
        return 'pengambilan belum tersedia'
    return 'diambil ' + (stamp(times[0]) if times[0] == times[-1] else f'{stamp(times[0])} sampai {stamp(times[-1])}')


def _account_text(row, yesterday, week, history, previous_connections, show_daily, show_week):
    identity, data = row['account_id'], row['data']
    followers = data['followers']
    progress = 'tercapai' if followers >= TARGET else f'kurang {number(TARGET - followers)}'
    lines = [f"{username(row['username'])} · <b>{number(followers)} follower</b>",
             f'{target_percent(followers)} target · {progress}']
    previous = yesterday.get(identity)
    if previous and previous['data']['followers'] < TARGET <= followers:
        lines.append('Baru mencapai 600 follower')
    elif previous and followers < TARGET <= previous['data']['followers']:
        lines.append('Turun ke bawah 600 follower')
    if identity in history and previous_connections is not None and previous_connections.get(identity) in {None, 'disconnected'}:
        lines.append('Terhubung kembali · histori ID yang sama dipertahankan')
    if identity in history and history[identity]['username'] != row['username']:
        lines.append(f"Username sebelumnya: {username(history[identity]['username'])}")
    if show_daily:
        lines.append(delta(row, previous, 1))
    if show_week:
        lines.append(delta(row, week.get(identity), 7))
    return '\n'.join(lines)


def render_report(run, rows, yesterday, week, history, *, missed=0, previous_connections=None, series=()):
    reached, below, unavailable = account_groups(rows)
    connected = [r for r in rows if r['status'] != 'disconnected']
    valid = reached + below
    valid_ids = {r['account_id'] for r in valid}
    show_daily, show_week = bool(valid_ids.intersection(yesterday)), bool(valid_ids.intersection(week))
    partial = not run['inventory_complete'] or bool(unavailable)
    summary = [f'Target <b>≥{TARGET} follower</b>',
               f'Sudah mencapai: <b>{len(reached)} akun</b> · belum: <b>{len(below)} akun</b>',
               f'Belum bisa dinilai: <b>{len(unavailable)} akun</b>']
    if not run['inventory_complete']:
        if not rows:
            summary = [summary[0], 'Status target belum dapat dihitung.']
        summary.append('Inventaris Repliz gagal; cakupan akun belum diketahui.')
    else:
        summary.append(f'{len(valid)}/{len(connected)} akun terhubung terbaca · {len(rows)} akun di inventaris')
        if not valid and connected:
            summary.append(f'Statistik gagal untuk {len(connected)} akun; hasil follower belum tersedia.')
        if not rows:
            summary.append('Tidak ada akun TikTok dalam cakupan laporan.')
    if partial:
        summary.append('<b>Data parsial. Akun tanpa statistik terbaru belum dapat dinilai.</b>')
    if run.get('excluded'):
        summary.append(f"{run['excluded']} akun dikecualikan sesuai pengaturan.")
    if missed:
        summary.append(f'{missed} jadwal sebelumnya terlewat; tidak dikirim ulang.')
    if valid and not show_daily:
        summary.append('Harian: baseline D−1 belum tersedia.')
    if valid and not show_week:
        summary.append('7 hari: baseline D−7 belum tersedia.')
    warnings = Counter(w for r in valid for w in set(r['data'].get('warnings', [])))
    for warning, count in sorted(warnings.items())[:3]:
        summary.append(f'Catatan Repliz ({count} akun): {html.escape(warning)}')
    if warnings:
        summary.append('Tanggal catatan mengikuti Repliz; zona waktunya tidak dinyatakan.')

    entries = []
    for label, group in ((f'Sudah mencapai ≥600 · {len(reached)} akun', reached),
                         (f'Belum mencapai 600 · {len(below)} akun', below)):
        entries.extend((label, _account_text(row, yesterday, week, history, previous_connections, show_daily, show_week)) for row in group)
    labels = {'pending': 'belum selesai saat batas waktu', 'error': 'statistik gagal',
              'stale': 'data sudah terlalu lama', 'disconnected': 'terputus dari Repliz'}
    for row in unavailable:
        text = f"{username(row['username'])} · {labels.get(row['status'], 'statistik belum tersedia')}"
        if row['account_id'] in history:
            old = history[row['account_id']]
            text += f"\nTerakhir diketahui {number(old['data']['followers'])} follower · {stamp(old['fetched_at'])}"
        entries.append((f'Belum bisa dinilai · {len(unavailable)} akun', text))
    if run['inventory_complete']:
        ids = {r['account_id'] for r in rows}
        for identity, old in sorted(yesterday.items()):
            if identity not in ids:
                text = (f"{username(old['username'])} · tidak ada pada inventaris Repliz hari ini"
                        f"\nTerakhir {number(old['data']['followers'])} follower · {stamp(old['fetched_at'])}")
                entries.append(('Perubahan inventaris', text))

    title = 'Laporan TikTok' if run['kind'] == 'daily' else 'Preview TikTok' if run['kind'] == 'preview' else 'Uji laporan TikTok'
    late = (datetime.fromisoformat(run['started_at']) > datetime.fromisoformat(run['due_at'])
            or (run.get('frozen_at') and datetime.fromisoformat(run['frozen_at']) > datetime.fromisoformat(run['due_at']) + timedelta(minutes=1)))
    day = date.fromisoformat(run['day'])
    month = ('Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun', 'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des')[day.month - 1]
    timing = '08.00 WIB' if run['kind'] == 'daily' else 'Snapshot uji · tidak menjadi baseline pagi'
    heading = f'<b>{title} · {day.day:02d} {month} {day.year}</b>\n' + timing + (' · terlambat' if late and run['kind'] == 'daily' else '')
    footer = 'Sumber: Repliz · ' + sample_window(rows)
    pages, current, count, section = [], ['\n'.join(summary)], 0, None

    def fits(blocks):
        text = heading + '\nBagian 9999/9999\n\n' + '\n\n'.join(blocks) + '\n\n' + footer
        return len(text.encode('utf-16-le')) // 2 <= LIMIT

    for label, text in entries:
        block = (f'<b>{label}</b>\n\n' if section != label else '') + text
        if count >= ACCOUNTS_PER_PAGE or not fits(current + [block]):
            pages.append(current)
            current, count = [], 0
            block = f'<b>{label}</b>\n\n' + text
        if not fits([block]):
            raise ValueError('Blok laporan melebihi batas Telegram.')
        current.append(block)
        count, section = count + 1, label
    if series:
        values = []
        for day_string, point in daily_series(run, series):
            label = date.fromisoformat(day_string).strftime('%d/%m')
            if point is None:
                values.append(f'{label}: belum tersedia')
            elif not point['complete']:
                values.append(f"{label}: {point['reached']} teramati (data parsial)")
            else:
                values.append(f"{label}: {point['reached']} dari {point['total']} akun")
        trend = '<b>Tren harian · akun mencapai ≥600</b>\n' + '\n'.join(values)
        if run['kind'] != 'daily':
            trend += '\nTren hanya memakai snapshot laporan harian.'
        if not fits(current + [trend]):
            pages.append(current)
            current = []
        current.append(trend)
    if current:
        pages.append(current)
    return [heading + f'\nBagian {i}/{len(pages)}\n\n' + '\n\n'.join(page) + '\n\n' + footer for i, page in enumerate(pages, 1)]
