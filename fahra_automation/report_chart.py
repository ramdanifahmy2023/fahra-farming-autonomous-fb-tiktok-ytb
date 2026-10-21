"""A bounded Telegram image: target status, nearest accounts, and dated history."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .report_views import TARGET, account_groups, daily_series, number, sample_window

WIDTH = 900
MAX_BARS = 8
PAPER = '#F6F4EE'
INK = '#202B28'
MUTED = '#52635C'
REACHED = '#15664E'
BELOW = '#98520C'
UNKNOWN = '#63716A'
TRACK = '#E1E4DC'
FONT = Path(__file__).parent / 'assets/fonts/BarlowSemiCondensed-Bold.ttf'


@lru_cache(maxsize=16)
def _font(size):
    return ImageFont.truetype(str(FONT), size=size)


def _text(draw, xy, text, size=32, fill=INK, anchor=None):
    draw.text(xy, text, font=_font(size), fill=fill, anchor=anchor)


def _fit(draw, text, size, width):
    if draw.textlength(text, font=_font(size)) <= width:
        return text
    while text and draw.textlength(text + '…', font=_font(size)) > width:
        text = text[:-1]
    return text + '…'


def _wrap(draw, text, width, size=28):
    lines, current = [], ''
    for word in text.split():
        candidate = f'{current} {word}'.strip()
        if current and draw.textlength(candidate, font=_font(size)) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _trend(draw, run, series, y):
    points = daily_series(run, series)
    _text(draw, (54, y), 'Akun mencapai ≥600', 38)
    _text(draw, (54, y + 48), 'Tren 7 hari · snapshot harian WIB', 28, MUTED)
    top, bottom = y + 112, y + 280
    # Partial observations are lower bounds, never joined to a complete series.
    maximum = max([p['reached'] for _, p in points if p] + [1])
    left, right = 104, 826
    plot_y = lambda value: bottom - value / maximum * (bottom - top)
    for value in sorted({0, maximum // 2, maximum}):
        py = plot_y(value)
        draw.line((left, py, right, py), fill=TRACK, width=2)
        _text(draw, (80, py - 18), number(value), 28, MUTED, anchor='ra')
    previous = None
    for index, (day, point) in enumerate(points):
        x = left + (right - left) * index / (len(points) - 1)
        _text(draw, (x, bottom + 25), date.fromisoformat(day).strftime('%d/%m'), 28, MUTED, anchor='ma')
        if point is None:
            _text(draw, (x, bottom - 38), '?', 28, MUTED, anchor='ma')
            previous = None
            continue
        py = plot_y(point['reached'])
        if point['complete']:
            if previous is not None:
                draw.line((previous[0], previous[1], x, py), fill=REACHED, width=5)
            draw.ellipse((x - 7, py - 7, x + 7, py + 7), fill=REACHED)
            _text(draw, (x, py - 43), number(point['reached']), 30, REACHED, anchor='ma')
            previous = (x, py)
        else:
            draw.polygon(((x, py - 9), (x + 9, py), (x, py + 9), (x - 9, py)), outline=BELOW, width=3)
            _text(draw, (x, py - 43), number(point['reached']) + '*', 30, BELOW, anchor='ma')
            previous = None
    legend_y = bottom + 76
    draw.ellipse((54, legend_y + 7, 66, legend_y + 19), fill=REACHED)
    _text(draw, (80, legend_y - 1), 'lengkap', 28, MUTED)
    draw.polygon(((280, legend_y + 4), (289, legend_y + 13), (280, legend_y + 22), (271, legend_y + 13)), outline=BELOW, width=3)
    _text(draw, (305, legend_y - 1), 'parsial*', 28, MUTED)
    _text(draw, (526, legend_y - 1), '? belum tersedia', 28, MUTED)
    _text(draw, (54, legend_y + 44), '*Jumlah teramati; cakupan akun belum lengkap.', 28, MUTED)
    if run['kind'] != 'daily':
        _text(draw, (54, legend_y + 82), 'Tren hanya memakai snapshot laporan harian.', 28, MUTED)
    elif not any(point for _, point in points):
        _text(draw, (54, legend_y + 82), 'Belum ada snapshot harian untuk periode ini.', 28, MUTED)
    return legend_y + 130


def render_chart(run, rows, series) -> bytes:
    reached, below, unavailable = account_groups(rows)
    selected = below[:MAX_BARS]
    bar_height = max(len(selected), 1) * 100
    image = Image.new('RGB', (WIDTH, 1300 + bar_height), PAPER)
    draw = ImageDraw.Draw(image)
    day = date.fromisoformat(run['day'])
    _text(draw, (54, 38), 'TikTok · target 600', 54)
    mode = 'Laporan harian' if run['kind'] == 'daily' else 'Preview' if run['kind'] == 'preview' else 'Uji laporan'
    _text(draw, (54, 108), f'{mode} · {day:%d/%m/%Y}', 30, MUTED)
    if not run['inventory_complete'] and not rows:
        _text(draw, (54, 186), 'Status target belum dapat dihitung', 42)
        _text(draw, (54, 253), 'Inventaris Repliz gagal.', 32, BELOW)
        _text(draw, (54, 299), 'Cakupan akun belum diketahui.', 30, MUTED)
    else:
        columns = ((54, len(reached), 'Sudah ≥600', REACHED),
                   (322, len(below), 'Belum 600', BELOW),
                   (590, len(unavailable), 'Belum dinilai', UNKNOWN))
        for x, count, label, color in columns:
            _text(draw, (x, 174), number(count), 72, color)
            _text(draw, (x, 267), label, 32)
        description = f'{len(rows)} akun di inventaris · {len(reached) + len(below)} terbaca'
        if not run['inventory_complete']:
            description = 'Inventaris belum lengkap · angka hanya yang teramati'
        _text(draw, (54, 321), description, 28, MUTED)
        if rows:
            cursor = 54
            for count, color in ((len(reached), REACHED), (len(below), BELOW), (len(unavailable), UNKNOWN)):
                length = 792 * count / len(rows)
                if length:
                    draw.rectangle((cursor, 369, cursor + length, 385), fill=color)
                    if cursor > 54:
                        draw.line((cursor, 368, cursor, 386), fill=PAPER, width=3)
                    cursor += length
    _text(draw, (54, 424), 'Paling dekat ke 600', 38)
    if selected:
        _text(draw, (54, 473), f'{len(selected)} dari {len(below)} akun di bawah target · urut follower', 28, MUTED)
    else:
        _text(draw, (54, 473), 'Daftar lengkap dan status tiap akun ada di pesan teks.', 28, MUTED)
    y = 530
    if not selected:
        if below:
            empty = 'Belum ada akun yang dapat ditampilkan.'
        elif unavailable or not run['inventory_complete']:
            empty = 'Akun tanpa statistik belum dapat dinilai.'
        elif reached:
            empty = 'Semua akun terbaca sudah mencapai target.'
        else:
            empty = 'Tidak ada akun TikTok dalam cakupan laporan.'
        for index, line in enumerate(_wrap(draw, empty, 792, 32)):
            _text(draw, (54, y + index * 42), line, 32, MUTED)
        y += 100
    else:
        for row in selected:
            followers = row['data']['followers']
            name = '@' + str(row['username']).strip().lstrip('@')
            _text(draw, (54, y), _fit(draw, name, 34, 560), 34)
            _text(draw, (846, y), f'{number(followers)} / 600', 34, anchor='ra')
            draw.rectangle((54, y + 52, 594, y + 68), fill=TRACK)
            end = 54 + min(followers, TARGET) / TARGET * 540
            if end > 54:
                draw.rectangle((54, y + 52, end, y + 68), fill=BELOW)
            draw.line((594, y + 46, 594, y + 74), fill=INK, width=3)
            _text(draw, (846, y + 42), f'kurang {number(TARGET - followers)}', 30, BELOW, anchor='ra')
            y += 100
    if selected:
        _text(draw, (54, y), '0', 28, MUTED)
        _text(draw, (594, y), '600 follower', 28, MUTED, anchor='ra')
    y = _trend(draw, run, series, y + 70)
    source = 'Repliz · ' + sample_window(rows)
    for line in _wrap(draw, source, 792, 28):
        _text(draw, (54, y), line, 28, MUTED)
        y += 38
    if run.get('excluded'):
        _text(draw, (54, y), f"{run['excluded']} akun dikecualikan sesuai pengaturan.", 28, MUTED)
        y += 38
    if y + 34 > image.height:
        raise ValueError('Tinggi chart melampaui kanvas.')
    image = image.crop((0, 0, WIDTH, y + 34))
    output = BytesIO()
    image.save(output, format='PNG', optimize=False, compress_level=6)
    payload = output.getvalue()
    if len(payload) > 5_000_000:
        raise ValueError('Ukuran chart melampaui batas laporan.')
    return payload
