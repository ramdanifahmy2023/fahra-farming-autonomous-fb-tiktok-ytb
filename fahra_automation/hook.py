"""Measured, centered typography for the opening three seconds of a Reel."""
from __future__ import annotations

import itertools
import math
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = Path(__file__).parent / 'assets/fonts/BarlowSemiCondensed-Bold.ttf'
STYLE = 'barlow-balanced-v1'


def _font(size):
    return ImageFont.truetype(str(FONT), size, layout_engine=ImageFont.Layout.BASIC)


def _width(font, text):
    left, _, right, _ = font.getbbox(text)
    return math.ceil(max(right - left, font.getlength(text)))


def _balanced_lines(text, font, available, count):
    words = text.split()
    if len(words) < count:
        return None
    choices = []
    for cuts in itertools.combinations(range(1, len(words)), count - 1):
        stops = (0, *cuts, len(words))
        lines = [' '.join(words[a:b]) for a, b in zip(stops, stops[1:])]
        widths = [_width(font, line) for line in lines]
        if max(widths) <= available:
            choices.append((max(widths) - min(widths), lines))
    return min(choices, key=lambda item: item[0])[1] if choices else None


def layout_hook(text, width, height):
    if not isinstance(text, str):
        raise ValueError('Hook harus berupa teks.')
    text = ' '.join(unicodedata.normalize('NFC', text).split())
    if not text or len(text) > 60 or len(text.split()) > 10:
        raise ValueError('Hook harus berisi 1–60 karakter dan maksimal 10 kata.')
    if width < 540 or height < 960 or abs(width / height - 9 / 16) > .005:
        raise ValueError('Layout hook memerlukan kanvas vertikal 9:16 minimal 540 × 960.')

    padding_x, padding_y = round(width * .033), round(width * .022)
    available = round(width * .76) - 2 * padding_x
    maximum, minimum = round(width * .081), round(width * .061)
    font = _font(maximum)
    missing = bytes(font.getmask('\uffff'))
    if any(unicodedata.category(char).startswith('C') or
           (not char.isspace() and bytes(font.getmask(char)) == missing) for char in text):
        raise ValueError('Hook memuat karakter yang tidak tersedia pada font; gunakan teks Indonesia tanpa emoji.')
    lines = None
    # Keep short hooks large; balance longer hooks before reducing their type size.
    for count in (1, 2, 3):
        sizes = (maximum,) if count == 1 else range(maximum, minimum - 1, -1)
        for size in sizes:
            font = _font(size)
            lines = _balanced_lines(text, font, available, count)
            if lines:
                break
        if lines:
            break
    if not lines:
        raise ValueError('Hook terlalu padat untuk tiga baris yang terbaca; pendekkan teks hook.')

    widths = [_width(font, line) for line in lines]
    boxes = [font.getbbox(line) for line in lines]
    line_height = max(box[3] - box[1] for box in boxes)
    gap = round(size * .24)
    card_width = max(widths) + 2 * padding_x
    card_height = len(lines) * line_height + (len(lines) - 1) * gap + 2 * padding_y
    left = round(width * .47 - card_width / 2)
    top = round(height * .18)
    return {'style': STYLE, 'font': FONT.name, 'font_size': size, 'lines': lines,
            'canvas': [width, height], 'card': [left, top, card_width, card_height],
            'text_widths': widths, 'padding': [padding_x, padding_y],
            'line_height': line_height, 'line_gap': gap}


def create_hook(text, width, height, target):
    layout = layout_hook(text, width, height)
    font = _font(layout['font_size'])
    left, top, card_width, card_height = layout['card']
    image = Image.new('RGBA', (width, height))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((left, top, left + card_width - 1, top + card_height - 1),
                           radius=round(width * .018), fill=(23, 25, 28, 236))
    for index, line in enumerate(layout['lines']):
        box = font.getbbox(line)
        x = left + (card_width - (box[2] - box[0])) / 2 - box[0]
        y = top + layout['padding'][1] + index * (layout['line_height'] + layout['line_gap']) - box[1]
        draw.text((round(x), round(y)), line, font=font, fill='white')
    image.save(target)
    return layout
