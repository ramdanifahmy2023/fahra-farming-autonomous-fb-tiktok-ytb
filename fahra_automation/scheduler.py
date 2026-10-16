from __future__ import annotations

import hashlib
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Jakarta')


def parse_time(value):
    stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.utcoffset() is None:
        raise ValueError('Waktu harus memiliki zona waktu.')
    return stamp


def offset_for(account_id, daily_limit, start_hour=8, end_hour=23):
    interval = (end_hour - start_hour) * 3600 // daily_limit
    return int.from_bytes(hashlib.sha256(account_id.encode()).digest()[:8], 'big') % interval


def daily_slots(day, daily_limit, offset, start_hour=8, end_hour=23):
    start = datetime.combine(day, time(start_hour), ZONE)
    seconds = (end_hour - start_hour) * 3600
    interval = seconds // daily_limit
    return [start + timedelta(seconds=i * interval + offset % interval) for i in range(daily_limit)]
