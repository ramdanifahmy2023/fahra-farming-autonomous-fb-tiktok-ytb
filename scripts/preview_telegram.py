"""Render actual bot messages with fixture data; never connects to Telegram."""
from pathlib import Path
import copy
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fahra_automation import telegram_views as views
from fahra_automation.telegram import HELP


def main():
    target = Path(sys.argv[1] if len(sys.argv) > 1 else 'runtime/verification/bot-ui-preview')
    target.mkdir(parents=True, exist_ok=True)
    batch = {'id': 'b' * 32, 'page': 0}
    jobs = [dict(id=str(i) * 32, asset_id='a' * 32, platform=platform, status='rendered',
                 username='akun_contoh_' + platform, payload={'generated': {'hook': 'Kucingnya malah ikut duduk'}},
                 slot_date='2026-10-18', planned_at=None, attempts=0,
                 last_error='Menunggu sinkronisasi Bank konten ke Google Drive.',
                 url='https://www.instagram.com/reel/contoh/')
            for i, platform in enumerate(('tiktok', 'facebook', 'youtube'), 1)]
    summary = dict(date='2026-10-18', paused=False, accounts=64, reserved_or_published=234,
                   daily_capacity=1280, sync_in_progress=True, sync_last_success_at='2026-10-17T19:10:00+00:00',
                   statuses={'queued':63,'scheduled':9,'published':39,'failed':3},
                   source_groups=[dict(origin='baru',complete=13,processing=24,attention=1)])
    accounts = [dict(platform=('tiktok','facebook','youtube')[i % 3], username=f'akun_contoh_{i + 1:02d}',
                     name=f'Halaman contoh {i + 1:02d}' if i % 3 == 1 else f'Channel contoh {i + 1:02d}',
                     connected=i != 3, eligible=i != 5, blocked_until=None) for i in range(64)]
    failure = copy.deepcopy(jobs[0])
    failure.update(status='failed', last_error='Unsupported get request. Missing permission.', payload={})
    cases = [('Mulai', (HELP, views.html_options(views.menu()))), ('Status', views.status(summary)),
             ('Satu Reel menunggu', views.batch(batch,jobs)[:2]), ('Kiriman terkendala', views.video(failure,admin=True)),
             ('Daftar akun',views.accounts(accounts)), ('Antrean',views.queue(jobs * 2,60))]
    unknown = copy.deepcopy(jobs[2])
    unknown.update(status='submit_unknown',last_error='TimeoutError')
    cases.append(('Hasil belum pasti',views.video(unknown,admin=True)))
    artifacts = []
    for title, (text, options) in cases:
        rows = ''.join('<div class="buttons">' + ''.join('<button>' + views.e(b['text']) + '</button>' for b in row) + '</div>'
                       for row in options['reply_markup']['inline_keyboard'])
        artifacts.append('<section><h2>' + views.e(title) + '</h2><article><div class="message">' + text.replace('\n','<br>') + '</div>' + rows + '</article></section>')
    css = '''*{box-sizing:border-box}body{margin:0;padding:16px;background:#dfe8e3;color:#17212b;font:16px/1.45 -apple-system,BlinkMacSystemFont,sans-serif}
    h1{font-size:20px;margin:0 0 8px}header p{margin:0 0 24px;max-width:65ch}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:24px;align-items:start}
    h2{font-size:15px;font-weight:600;margin:0 0 8px}article{background:#fff;border-radius:10px;padding:16px;overflow-wrap:anywhere}
    .message{margin-bottom:16px}code{font-size:.95em}.buttons{display:flex;gap:6px;margin-top:6px}
    button{flex:1;min-width:0;border:0;border-radius:5px;padding:10px 8px;min-height:44px;background:#e6eff5;color:#225b80;font:inherit;line-height:1.3}
    @media(prefers-color-scheme:dark){body{background:#17212b;color:#f5f5f5}article{background:#243241}button{background:#344c62;color:#d7ebff}}
    body.large{font-size:32px}.large h2{font-size:24px}.large main{grid-template-columns:1fr}'''
    document = '<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Preview pesan bot</title><style>' + css + '</style><body><header><h1>Preview pesan Fahra Automation</h1><p>Simulasi dengan data uji. Isi dan tombol berasal dari fungsi bot. Font, warna, dan ukuran asli mengikuti aplikasi Telegram pengguna.</p></header><main>' + ''.join(artifacts) + '</main></body></html>'
    (target / 'index.html').write_text(document)
    (target / 'messages.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2))
    print(target / 'index.html')


if __name__ == '__main__':
    main()
