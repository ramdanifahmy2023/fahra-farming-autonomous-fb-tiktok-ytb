"""Daily reporting coverage, without filling gaps from pilot or provider metrics."""
from __future__ import annotations

import json
from datetime import date, timedelta


def point(run, rows):
    valid = [row for row in rows if row['status'] == 'ok']
    reached = sum(row['data']['followers'] >= 600 for row in valid)
    return {'day': run['day'], 'reached': reached, 'below': len(valid) - reached,
            'unknown': len(rows) - len(valid), 'total': len(rows),
            'complete': bool(run['inventory_complete']) and len(valid) == len(rows)}


def daily_series(store, day, *, current_run=None, current_rows=()):
    end = date.fromisoformat(day)
    start = (end - timedelta(days=6)).isoformat()
    with store.connect() as connection:
        runs = connection.execute(
            "SELECT * FROM account_report_runs WHERE kind='daily' AND state='ready' "
            'AND day>=? AND day<=? ORDER BY day', (start, day)).fetchall()
        series = {}
        for run in runs:
            rows = connection.execute('SELECT status,data FROM account_metric_snapshots WHERE run_id=?',
                                      (run['id'],)).fetchall()
            parsed = [{**dict(row), 'data': json.loads(row['data']) if row['data'] else None} for row in rows]
            series[run['day']] = point(run, parsed)
    if current_run and current_run['kind'] == 'daily' and current_run['day'] == day:
        series[day] = point(current_run, current_rows)
    return [series[key] for key in sorted(series)]
