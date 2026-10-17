from .journal import AttemptJournal
from .reply_store import ReplyStore
from .replies import ReplyWorker
from .pipeline import Pipeline
from .repliz import Repliz


def inspect(config, store, kind, identity):
    journal = AttemptJournal(config, readonly=True)
    if kind == 'delivery':
        job = store.find_job(identity)
        if not job:
            raise ValueError('ID kiriman tidak ditemukan atau tidak unik.')
        action = job['id']
        remote = job['schedule_id']
        allowed = ['reconcile'] if remote or job['payload'].get('submission') else (
            ['retry-local'] if job['status'] == 'failed' else [])
        result = {key: job[key] for key in ('id','asset_id','status','platform','account_id','schedule_id','counts_quota','last_error')}
        result['post_id'] = job['payload'].get('post_id')
        result['post_url'] = job['payload'].get('post_url')
        result['asset_status'] = store.asset(job['asset_id'])['status']
    else:
        event = ReplyStore(store).get(identity)
        if not event:
            raise ValueError('ID komentar tidak ditemukan.')
        action = 'reply:' + event['account_id'] + ':' + event['platform_id']
        result = {key: event[key] for key in ('id','status','account_id','post_id','reason','created_at')}
        with store.connect() as c:
            row = c.execute('SELECT status,reply_id,started_at,error FROM reply_attempts WHERE event_id=?', (identity,)).fetchone()
        result['attempt'] = dict(row) if row else None
        allowed = ['reconcile'] if event['status'] in {'send_unknown','sent_unverified'} else []
    entry = journal.state(action)
    result['journal'] = {k: entry.get(k) for k in ('state','at','schedule_id','error')} if entry else None
    result['allowed_actions'] = allowed
    result['rule'] = 'Hasil ambigu ditahan. Reconcile membaca bukti provider; tidak mengirim ulang.'
    return result


async def reconcile(config, store, kind, identity):
    inspect(config, store, kind, identity)
    api = Repliz(config)
    try:
        if kind == 'delivery':
            job = store.find_job(identity)
            worker = Pipeline(config, store, api, None, None, None, None, personas=object())
            await worker.monitor(job['id'])
        else:
            await ReplyWorker(config, store, api).reconcile(identity)
        return inspect(config, store, kind, identity)
    finally:
        await api.close()
