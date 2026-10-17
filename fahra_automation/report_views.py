"""Native Telegram HTML, with dated evidence and no inferred growth."""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Jakarta')
LIMIT = 3500


def number(value):
    return f'{value:,}'.replace(',', '.') if type(value) is int else 'belum tersedia'


def stamp(value):
    return datetime.fromisoformat(value).astimezone(ZONE).strftime('%d/%m %H:%M WIB')


def username(value):
    name = str(value).lstrip('@')[:80]
    label = '@' + html.escape(name)
    return f'<a href="https://www.tiktok.com/@{name}">{label}</a>' if re.fullmatch(r'[A-Za-z0-9_.]{1,40}', name) else label


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


def provider_history(data):
    metrics = data.get('daily', [])
    warnings = data.get('warnings', [])
    text = []
    if len(metrics) >= 2:
        first, last = metrics[0], metrics[-1]
        diff = last['followers'] - first['followers']
        sign = '+' if diff > 0 else '−' if diff < 0 else ''
        text.append(f"Histori Repliz {first['day']}–{last['day']}: {sign}{number(abs(diff))}")
    if warnings:
        text.append('Catatan data: ' + html.escape('; '.join(warnings[:3])))
    return '\n'.join(text)


def render_report(run, rows, yesterday, week, history, *, missed=0, previous_connections=None):
    valid = [r for r in rows if r['status'] == 'ok']
    eligible = sorted((r for r in valid if r['data']['followers'] > 600), key=lambda r: (-r['data']['followers'], r['username'], r['account_id']))
    connected = [r for r in rows if r['status'] != 'disconnected']
    partial = not run['inventory_complete'] or len(valid) != len(connected)
    if not run['inventory_complete']:
        quality = 'Inventaris Repliz gagal; cakupan akun belum diketahui.'
    elif not valid and connected:
        quality = f'Statistik gagal untuk {len(connected)} akun; hasil follower belum tersedia.'
    else:
        quality = f"{len(valid)}/{len(connected)} akun terhubung terbaca · {len(eligible)} akun >600 follower"
    blocks = [quality + ('\n<b>Data parsial — akun yang gagal belum dapat dinilai.</b>' if partial else '')]
    if missed:
        blocks[0] += f'\n{missed} jadwal sebelumnya terlewat; tidak dikirim ulang.'
    if not eligible and valid and not partial:
        blocks.append('Tidak ada akun terhubung dengan follower >600 pada pengambilan ini.')
    if not connected and run['inventory_complete']:
        blocks.append('Tidak ada akun TikTok terhubung dalam cakupan laporan.')
    for row in eligible:
        identity, data = row['account_id'], row['data']
        lines = [f"{username(row['username'])} · <b>{number(data['followers'])} follower</b>"]
        if identity not in history:
            lines.append('Baru dipantau' if run['kind'] == 'daily' else 'Snapshot uji · tidak menjadi baseline pagi')
        elif previous_connections is not None and previous_connections.get(identity) in {None, 'disconnected'}:
            lines.append('Terhubung kembali · histori ID yang sama dipertahankan')
        elif identity in yesterday and yesterday[identity]['data']['followers'] <= 600:
            lines.append('Baru melewati 600 follower')
        if identity in history and history[identity]['username'] != row['username']:
            lines.append(f"Username sebelumnya: {username(history[identity]['username'])}")
        lines.extend([delta(row, yesterday.get(identity), 1), delta(row, week.get(identity), 7),
                      f"Total likes {number(data.get('likes'))} · video {number(data.get('videos'))}"])
        provider = provider_history(data)
        if provider:
            lines.append(provider)
        blocks.append('\n'.join(lines))
    for row in sorted(rows, key=lambda r: (r['username'], r['account_id'])):
        identity = row['account_id']
        if row['status'] == 'ok' and row['data']['followers'] <= 600 and identity in yesterday and yesterday[identity]['data']['followers'] > 600:
            blocks.append(f"<b>Turun ke ≤600</b>\n{username(row['username'])} · {number(row['data']['followers'])} follower\n" + delta(row, yesterday[identity], 1))
        elif row['status'] != 'ok':
            labels = {'pending': 'belum selesai saat batas waktu', 'error': 'statistik gagal', 'stale': 'data sudah terlalu lama', 'disconnected': 'terputus dari Repliz'}
            text = f"{username(row['username'])} · {labels[row['status']]}"
            if identity in history:
                old = history[identity]
                text += f"\nTerakhir diketahui {number(old['data']['followers'])} follower · {stamp(old['fetched_at'])}"
            blocks.append(text)
    if run['inventory_complete']:
        ids = {r['account_id'] for r in rows}
        # Only yesterday's missing identities are surfaced, avoiding an ever-growing archive.
        for identity, old in sorted(yesterday.items()):
            if identity not in ids:
                blocks.append(f"{username(old['username'])} · tidak ada pada inventaris Repliz hari ini\nTerakhir {number(old['data']['followers'])} follower · {stamp(old['fetched_at'])}")
    times = [r['fetched_at'] for r in valid]
    footer = ('Sumber: Repliz · diambil ' + (f'{stamp(min(times))}–{stamp(max(times))}' if times else 'belum tersedia')
              + '\nHistori provider memakai tanggal Repliz; zona waktunya tidak dinyatakan.')
    title = 'LAPORAN TIKTOK' if run['kind'] == 'daily' else 'PREVIEW TIKTOK' if run['kind'] == 'preview' else 'UJI LAPORAN TIKTOK'
    late = (datetime.fromisoformat(run['started_at']) > datetime.fromisoformat(run['due_at'])
            or (run.get('frozen_at') and datetime.fromisoformat(run['frozen_at']) > datetime.fromisoformat(run['due_at']) + timedelta(minutes=1)))
    day = datetime.fromisoformat(run['day'])
    month = ('Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun', 'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des')[day.month - 1]
    timing = '08.00 WIB' if run['kind'] == 'daily' else 'Jadwal rutin 08.00 WIB · ini uji, bukan run terjadwal'
    heading = f'<b>{title} · {day.day:02d} {month} {day.year}</b>\n' + timing + (' · terlambat' if late and run['kind'] == 'daily' else '')
    groups, current = [], []
    def length(text):
        return len(text.encode('utf-16-le')) // 2
    for block in blocks:
        # The reserve includes the page counter; lengths include markup, stricter than Bot API.
        if length(heading + '\nBagian 9999/9999\n\n' + block + '\n\n' + footer) > LIMIT:
            raise ValueError('Blok laporan melebihi batas Telegram.')
        if current and (len(current) >= 6 or length(heading + '\nBagian 9999/9999\n\n' + '\n\n'.join(current + [block]) + '\n\n' + footer) > LIMIT):
            groups.append(current)
            current = []
        current.append(block)
    if current:
        groups.append(current)
    return [heading + f'\nBagian {i}/{len(groups)}\n\n' + '\n\n'.join(group) + '\n\n' + footer for i, group in enumerate(groups, 1)]
