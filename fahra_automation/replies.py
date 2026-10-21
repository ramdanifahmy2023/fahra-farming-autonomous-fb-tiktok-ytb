from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from .comment_profiles import CommentProfiles, ProfileSweep, object_id, save
from .config import Config, ConfigurationError, EXCLUDED_ACCOUNT_ID, allowed_account
from .db import Store
from .reply_ai import ReplyAI
from .reply_store import ReplyStore, ReplyWait
from .personas import PersonaRegistry
from .journal import AttemptJournal
from .repliz import Repliz, ReplizError, MutationNotSent, api_scope
from .util import iso, now, safe_error


def unwrap(data):
    return data.get('data', data) if isinstance(data, dict) else data


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Waktu komentar tidak memiliki zona waktu.')
    return parsed


def normalize_comment(raw):
    comment, content = raw['comment'], raw['content']
    item = {'account_id': str(raw['accountId']), 'inbox_id': str(object_id(raw)),
            'platform_id': str(comment['id']), 'post_id': str(content['id']),
            'text': comment.get('text', ''), 'author_id': str(comment.get('owner', {}).get('id', '')),
            'comment_at': iso(timestamp(comment.get('createdAt') or raw.get('createdAt'))),
            'remote_status': raw.get('status'), 'type': comment.get('type'),
            'has_media': bool(comment.get('medias')), 'parent_id': comment.get('parentId') or comment.get('replyToId')}
    if item['account_id'] == EXCLUDED_ACCOUNT_ID or any(not re.fullmatch(r'[A-Za-z0-9_.:-]{1,150}', item[k])
            or item[k] == 'None' for k in ('account_id', 'inbox_id', 'platform_id', 'post_id')):
        raise ValueError('Identitas komentar tidak valid/dikecualikan.')
    if not isinstance(item['text'], str):
        raise ValueError('Teks komentar tidak valid.')
    return item


def skip_reason(item, own_ids, keywords):
    if not item['author_id'] or item['author_id'] == 'None':
        return 'Identitas penulis belum tersedia.'
    if item['author_id'] in own_ids:
        return 'Komentar akun yang dikelola.'
    if item['remote_status'] != 'pending':
        return 'Komentar sudah ditangani/diabaikan di Repliz.'
    if item['type'] not in {'text', None} or item['has_media']:
        return 'Komentar media memerlukan pemeriksaan.'
    text = item['text'].casefold()
    if any(k.casefold() in text for k in keywords):
        return 'Cocok filter moderasi.'
    without_mentions = re.sub(r'@[^\s]+', '', text)
    without_labels = re.sub(r'\[[^\]]*\]', '', without_mentions)
    if not re.search(r'[^\W\d_]', without_labels, re.UNICODE):
        return 'Emoji/mention saja atau teks kosong.'
    if re.search(r'https?://|www\.|abaikan (?:semua )?(?:aturan|instruksi)|ignore (?:all |previous )?instructions', text):
        return 'Tautan/promosi/instruksi manipulatif.'
    return ''


class ReplyWorker:
    def __init__(self, config, store, api, ai=None):
        self.config, self.store, self.api = config, store, api
        self.db = ReplyStore(store)
        self.personas = PersonaRegistry(store)
        self.journal = AttemptJournal(config)
        self.ai = ai or ReplyAI(config, store)
        profile = json.loads((config.root / 'profiles/tiktok-comments-id.json').read_text())
        self.profiles = CommentProfiles(api, profile, config.runtime / 'replies/profiles', config=config)
        self.keywords = [w for words in profile['categories'].values() for w in words]
        self.own_ids = set()
        self.accounts = {}
        self.inventory_generation = None
        self.profile_lock = asyncio.Lock()
        self.empty_polls = 0
        self.poll_wakeup = asyncio.Event()

    def wake_poll(self, account_id=None):
        if not self.store.get('reply_enabled', False):
            return False
        if account_id is not None and account_id not in self.store.get('reply_account_ids', []):
            return False
        self.empty_polls = 0
        self.poll_wakeup.set()
        return True

    def own_ids_for(self, account_id):
        aliases = self.store.get('reply_owner_aliases', {})
        return self.own_ids | set(aliases.get(account_id, []))

    async def inventory(self):
        generation = self.db.require_snapshot()
        all_accounts = await self.api.pages('/public/account', {'types': ['tiktok', 'facebook', 'youtube']})
        with self.store.tx() as c:
            self.db.require_snapshot(generation, connection=c)
            approved = {r[0] for r in c.execute("SELECT id FROM accounts WHERE connected=1 AND username!=''")}
            own_ids = {str(a['generatedId']) for a in all_accounts if a.get('generatedId')}
            accounts = {object_id(a): a for a in all_accounts if allowed_account(a) and object_id(a) in approved and a.get('isConnected')}
            self.db.enroll_accounts(accounts, generation=generation, connection=c)
            for identity, account in accounts.items():
                self.personas.bind(identity, account['type'], connection=c)
        self.own_ids, self.accounts, self.inventory_generation = own_ids, accounts, generation
        return self.accounts

    async def ingest(self):
        if not self.store.get('reply_enabled', False):
            return
        since = timestamp(self.store.get('reply_since'))
        generation = self.db.require_snapshot()
        if self.inventory_generation != generation:
            await self.inventory()
        generation = self.inventory_generation
        if not self.accounts:
            raise ValueError('Tidak ada akun reply yang terhubung/diizinkan.')
        cfg = self.config.section('reply')
        group_size = max(1, min(30, cfg.get('inbox_group_size', 10)))
        max_pages = max(2, min(200, cfg.get('inbox_max_pages', 20)))
        page_positions = self.store.get('reply_inbox_pages', {})
        next_positions, rows, exhausted = {}, [], True
        ids = sorted(self.accounts)
        for offset in range(0, len(ids), group_size):
            group = ids[offset:offset + group_size]
            key = hashlib.sha256('\n'.join(group).encode()).hexdigest()
            if hasattr(self.api, 'comment_inbox'):
                batch = await self.api.comment_inbox(group, max_pages=max_pages,
                                                    start_page=page_positions.get(key, 1))
                rows.extend(batch['rows'])
                exhausted = exhausted and batch['exhausted']
                next_positions[key] = batch['next_page']
            else:
                rows.extend(row for row in await self.api.pages('/public/comment', {'accountIds': group, 'status': 'pending'})
                            if row.get('accountId') in group)
            self.db.require_snapshot(generation)
        # Pending children may refer to a resolved/ignored parent. Retain that evidence;
        # unresolved parent identity still follows the existing needs_review path.
        def missing_parents():
            comments = [(row.get('accountId'), row['comment']) for row in rows
                        if isinstance(row, dict) and isinstance(row.get('comment'), dict)]
            known = {(aid, str(comment.get('id'))) for aid, comment in comments}
            return {(aid, str(comment.get('parentId') or comment.get('replyToId')))
                    for aid, comment in comments if comment.get('parentId') or comment.get('replyToId')} - known
        if hasattr(self.api, 'comment_inbox'):
            for status in ('resolved', 'ignored'):
                missing = missing_parents()
                if not missing:
                    break
                parent_accounts = sorted({aid for aid, _ in missing if aid in self.accounts})
                for offset in range(0, len(parent_accounts), group_size):
                    batch = await self.api.comment_inbox(parent_accounts[offset:offset + group_size],
                                                        status=status, max_pages=max_pages)
                    rows.extend(r for r in batch['rows'] if isinstance(r.get('comment'), dict)
                                and (r.get('accountId'), str(r['comment'].get('id'))) in missing)
                    self.db.require_snapshot(generation)
        self.db.require_snapshot(generation)
        inserted = 0
        for raw in rows:
            if raw.get('accountId') not in self.accounts:
                continue
            try:
                item = normalize_comment(raw)
            except (ValueError, KeyError, TypeError):
                continue
            item['platform'] = self.accounts[item['account_id']]['type']
            cutoff = self.store.get('reply_account_since', {}).get(item['account_id'])
            old = timestamp(item['comment_at']) < max(since, timestamp(cutoff) if cutoff else since)
            reason = 'Sebelum aktivasi.' if old else skip_reason(item, self.own_ids_for(item['account_id']), self.keywords)
            inserted += self.db.ingest(item, 'skipped' if reason else 'queued', reason)
        self.store.set('reply_poll_at', iso())
        self.store.set('reply_inbox_pages', next_positions)
        self.store.set('reply_poll_error', None)
        self.store.record('reply_poll', comments=len(rows), inserted=inserted, accounts=len(self.accounts),
                          page_window_exhausted=exhausted, unresolved_parents=len(missing_parents()))
        return {'inserted': inserted, 'exhausted': exhausted}

    async def children(self, item, generation=None):
        result, token, seen = [], None, set()
        for _ in range(20):
            query = {'accountId': item['account_id'], 'commentId': item['platform_id']}
            if token:
                query['nextToken'] = token
            page = unwrap(await self.api.request('GET', '/public/content/' + item['post_id'] + '/comment', params=query))
            if generation is not None:
                self.db.require_snapshot(generation, account_id=item['account_id'])
            if not isinstance(page, dict) or not isinstance(page.get('docs'), list):
                raise ValueError('Respons thread tidak dikenal.')
            result.extend(page['docs'])
            token = page.get('nextToken')
            if not token:
                return result
            if token in seen:
                break
            seen.add(token)
        raise ValueError('Thread terlalu panjang atau cursor berulang; perlu pemeriksaan.')

    async def fresh(self, event, generation=None):
        item = event['payload']
        generation = self.db.require_snapshot(generation, account_id=item['account_id'])
        if item['account_id'] == EXCLUDED_ACCOUNT_ID:
            raise ValueError('Akun tidak diizinkan.')
        account = unwrap(await self.api.request('GET', '/public/account/' + item['account_id']))
        self.db.require_snapshot(generation, account_id=item['account_id'])
        if object_id(account) != item['account_id'] or not allowed_account(account) or not account.get('isConnected') or not account.get('generatedId'):
            raise ValueError('Akun tidak tersedia/dikecualikan.')
        if account.get('generatedId'):
            self.own_ids.add(str(account['generatedId']))
        config = account.get('config') or {}
        overrides = await self.api.pages('/public/automation', {'accountIds': [item['account_id']]})
        self.db.require_snapshot(generation, account_id=item['account_id'])
        matching = []
        for override in overrides:
            if override.get('accountId') != item['account_id']:
                continue
            detail = override.get('detail') or {}
            post_id = override.get('contentId') or detail.get('id')
            if not post_id:
                raise ValueError('Identitas override tidak diketahui.')
            if str(post_id) == item['post_id']:
                matching.append(override)
        if len(matching) > 1:
            raise ValueError('Lebih dari satu override posting; pemilik reply belum pasti.')
        if matching:
            config = matching[0].get('config') or {}
        if config.get('reply', {}).get('isActive'):
            return None, 'Native reply masih aktif pada posting ini.'
        raw = unwrap(await self.api.request('GET', '/public/comment/' + item['inbox_id']))
        self.db.require_snapshot(generation, account_id=item['account_id'])
        fresh = normalize_comment(raw)
        if any(fresh[k] != item[k] for k in ('account_id', 'inbox_id', 'platform_id', 'post_id', 'text', 'author_id', 'parent_id')):
            return None, 'Identitas/teks komentar berubah sejak diterima.'
        words = self.keywords[:]
        moderation = config.get('delete', {})
        if moderation.get('isActive') and moderation.get('type') == 'keyword':
            words += moderation.get('keywords', [])
        reason = skip_reason(fresh, self.own_ids_for(item['account_id']), words)
        if reason:
            return None, reason
        if any(r.get('isHidden') or r.get('isDeleted') for r in (raw, raw.get('comment', {}))):
            return None, 'Komentar disembunyikan/dihapus.'
        replies = await self.children(item, generation)
        if replies:
            return None, 'Thread sudah memiliki balasan; perlu pemeriksaan riwayat.'
        if item.get('parent_id'):
            # Nested targets differ between platform APIs; hold until a verified parent is supplied.
            return None, 'Komentar anak memerlukan konteks induk dan capability reply yang terverifikasi.'
        return fresh, ''

    async def process_one(self):
        if not self.store.get('reply_enabled', False):
            return
        try:
            generation = self.db.require_snapshot()
            if self.inventory_generation != generation:
                await self.inventory()
                generation = self.inventory_generation
            event = self.db.claim(generation=generation)
        except ReplyWait:
            return
        if not event:
            return
        item = event['payload']
        try:
            self.db.require_snapshot(generation)
            if now() - timestamp(item['comment_at']) > timedelta(hours=self.config.section('reply')['max_age_hours']):
                self.db.update(event['id'], 'skipped', 'Komentar melewati umur antrean.')
                return
            if item['account_id'] not in self.accounts:
                raise ValueError('Akun belum terhubung/diizinkan.')
            job = self.db.job_for(item['account_id'], item['post_id'])
            if not job:
                if now() - timestamp(event['created_at']) < timedelta(minutes=15):
                    self.db.update(event['id'], 'queued', 'Menunggu pemetaan publikasi.', delay=120)
                else:
                    self.db.update(event['id'], 'needs_review', 'Posting tidak memiliki job published yang unik.')
                return
            event['job_id'] = job['id']
            journal_id = 'reply:' + item['account_id'] + ':' + item['platform_id']
            prior = self.journal.state(journal_id)
            if prior and prior.get('state') in {'intended', 'unknown', 'accepted'}:
                self.db.restore_send(event, prior)
                return
            await self.ensure_profile(job, generation)
            self.db.require_snapshot(generation, account_id=item['account_id'])
            _, reason = await self.fresh(event, generation)
            if reason:
                self.db.update(event['id'], 'needs_review' if item.get('parent_id') else 'skipped', reason, job_id=job['id'])
                return
            if event['status'] != 'drafted':
                context = self.db.context(job)
                if not context:
                    context = await self.ai.context(job, self.config.runtime / 'replies/contexts' / job['id'])
                    self.db.require_snapshot(generation, account_id=item['account_id'])
                    self.db.save_context(job, context)
                event['persona'] = self.personas.bind(item['account_id'], job['platform'])
                draft = await self.ai.draft(event, context, self.db.recent(item['account_id']),
                                            self.config.runtime / 'replies/drafts' / event['id'])
                self.db.require_snapshot(generation, account_id=item['account_id'])
                if draft['action'] != 'reply':
                    self.db.update(event['id'], 'skipped' if draft['action'] == 'skip' else 'needs_review',
                                   draft['reason'], draft=draft, job_id=job['id'])
                    return
                self.db.update(event['id'], 'drafted', draft=draft, job_id=job['id'])
                event = self.db.get(event['id'])
            if not self.store.get('reply_enabled', False):
                return
            # Recheck the original comment and automation after the potentially slow AI calls.
            _, reason = await self.fresh(event, generation)
            if reason:
                self.db.update(event['id'], 'needs_review' if item.get('parent_id') else 'skipped', reason, job_id=job['id'])
                return
            self.config.require_live_action()
            def before_send():
                try:
                    reserved = self.db.reserve_send(event, self.config.section('reply')['daily_limit'], generation=generation)
                except ValueError as exc:
                    raise MutationNotSent(str(exc)) from None
                if not reserved:
                    raise ReplyWait('Reply ditunda oleh pause atau kuota; draft tetap disimpan.')
                self.journal.begin(journal_id, {'account_id': item['account_id'], 'post_id': item['post_id'],
                    'comment_id': item['platform_id'], 'inbox_id': item['inbox_id'], 'text': event['draft']['text'],
                    'started_at': iso(), 'job_id': job['id']})
            try:
                body = unwrap(await self.api.request('POST', '/public/comment/' + item['inbox_id'],
                    before_send=before_send, json={'text': event['draft']['text']}))
                reply_id = (body.get('commentId') or body.get('id') or body.get('_id')) if isinstance(body, dict) else None
                self.journal.mark(journal_id, 'accepted' if reply_id else 'unknown', schedule_id=str(reply_id) if reply_id else None)
                self.db.finish_send(event['id'], 'sent_unverified' if reply_id else 'send_unknown', str(reply_id) if reply_id else None)
            except ReplyWait:
                raise
            except MutationNotSent as exc:
                if self.journal.state(journal_id):
                    self.journal.mark(journal_id, 'not_sent', error=safe_error(exc))
                    self.db.finish_send(event['id'], 'failed', error=safe_error(exc))
                self.db.update(event['id'], 'drafted', 'Request belum dikirim; menunggu guard aktif.', delay=300)
            except ReplizError as exc:
                definite = exc.status in {400, 401, 403, 404, 422, 429}
                self.journal.mark(journal_id, 'not_sent' if definite else 'unknown', error=safe_error(exc))
                self.db.finish_send(event['id'], 'failed' if definite else 'send_unknown', error=safe_error(exc))
                if exc.status == 429:
                    self.db.update(event['id'], 'drafted', 'Batas provider; menunggu tanpa mengulang draft.', delay=max(900, exc.retry_after))
            except Exception as exc:
                prior = self.journal.state(journal_id)
                if self.db.get(event['id'])['status'] != 'sending':
                    raise
                if prior and prior.get('state') != 'accepted':
                    self.journal.mark(journal_id, 'unknown', error=safe_error(exc))
                if prior and prior.get('state') == 'accepted':
                    self.db.restore_send(event, prior)
                else:
                    self.db.finish_send(event['id'], 'send_unknown', error=safe_error(exc))
        except ConfigurationError as exc:
            current = self.db.get(event['id'])
            if current['status'] == 'sending':
                self.db.finish_send(event['id'], 'send_unknown', error=safe_error(exc))
            else:
                self.db.wait(event['id'], safe_error(exc))
        except Exception as exc:
            current = self.db.get(event['id'])
            if current['status'] in {'sending', 'send_unknown', 'sent_unverified', 'sent'}:
                # Never route a crash after the send intent back into the draft queue.
                if current['status'] == 'sending':
                    self.db.finish_send(event['id'], 'send_unknown', error=safe_error(exc))
            else:
                failures = self.db.failure(event['id']) if isinstance(exc, ValueError) else current['attempts']
                self.db.update(event['id'], 'needs_review' if isinstance(exc, ValueError) and failures >= 3 else
                               ('drafted' if current['draft'] else 'queued'), safe_error(exc), delay=300)
        finally:
            self.db.release(event['id'])

    async def reconcile(self, identity=None):
        for event, attempt in self.db.unresolved(identity):
            try:
                item = event['payload']
                if item['account_id'] == EXCLUDED_ACCOUNT_ID or item['account_id'] not in self.store.get('reply_account_ids', []):
                    continue
                account = unwrap(await self.api.request('GET', '/public/account/' + item['account_id']))
                if not allowed_account(account) or object_id(account) != item['account_id'] or not account.get('generatedId'):
                    raise ValueError('Pemilik balasan belum dapat diverifikasi.')
                owner_ids = {str(account['generatedId'])} | set(
                    self.store.get('reply_owner_aliases', {}).get(item['account_id'], []))
                replies = await self.children(item)
                matches = []
                for reply in replies:
                    if reply.get('text') != attempt['text']:
                        continue
                    if attempt['reply_id']:
                        # TikTok comment owner IDs can differ from the OAuth account generatedId.
                        # The returned POST ID plus text in this exact parent thread identifies our reply.
                        if str(reply.get('id')) == attempt['reply_id']:
                            matches.append(reply)
                    elif str(reply.get('owner', {}).get('id', '')) in owner_ids and reply.get('createdAt') and timestamp(reply['createdAt']) >= timestamp(attempt['started_at']) - timedelta(seconds=5):
                        matches.append(reply)
                if len(matches) == 1:
                    owner_id = str(matches[0].get('owner', {}).get('id') or '')
                    if attempt['reply_id'] and owner_id:
                        aliases = self.store.get('reply_owner_aliases', {})
                        aliases[item['account_id']] = sorted(set(aliases.get(item['account_id'], [])) | {owner_id})
                        self.store.set('reply_owner_aliases', aliases)
                    self.db.finish_send(event['id'], 'sent', str(matches[0]['id']))
                    action_id = 'reply:' + item['account_id'] + ':' + item['platform_id']
                    if self.journal.state(action_id):
                        self.journal.mark(action_id, 'accepted', schedule_id=str(matches[0]['id']))
                else:
                    self.db.update(event['id'], event['status'], 'Belum terverifikasi; tidak mengirim ulang.', delay=600)
            except Exception as exc:
                self.db.update(event['id'], event['status'], safe_error(exc), delay=600)

    async def polling_loop(self):
        while True:
            self.poll_wakeup.clear()
            base = self.config.section('reply')['poll_seconds']
            maximum = max(base, self.config.section('reply').get('poll_max_seconds', 300))
            delay = base
            try:
                with api_scope('reply'):
                    result = await self.ingest()
                if result:
                    self.empty_polls = (self.empty_polls + 1 if not result['inserted'] and result['exhausted'] else 0)
                    delay = min(maximum, base * 2 ** min(self.empty_polls, 6))
                self.store.health('reply_poll', 'ok')
            except ReplyWait as exc:
                self.store.set('reply_poll_error', None)
                self.store.health('reply_poll', 'waiting', reason=str(exc))
            except Exception as exc:
                self.store.health('reply_poll', 'error', reason=safe_error(exc))
                self.store.set('reply_poll_error', safe_error(exc))
                self.store.record('reply_poll_failed', error=safe_error(exc))
                if isinstance(exc, ReplizError) and exc.status == 429:
                    delay = max(base, exc.retry_after)
            try:
                await asyncio.wait_for(self.poll_wakeup.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def ensure_profile(self, job, generation=None, sweep=None):
        async with self.profile_lock:
            job = self.store.job(job['id'])
            generation = self.db.require_snapshot(generation, account_id=job['account_id'])
            retry_at = self.store.get('reply_profile_retry_at')
            if retry_at and timestamp(retry_at) > now():
                raise RuntimeError('Onboarding otomasi menunggu batas provider.')
            retry_at = job['payload'].get('comment_profile_retry_at')
            if retry_at and timestamp(retry_at) > now():
                raise RuntimeError('Onboarding posting menunggu pemeriksaan berikutnya.')
            self.config.require_live_action()
            def before_mutation():
                try:
                    self.db.require_snapshot(generation, account_id=job['account_id'])
                except (ConfigurationError, ValueError) as exc:
                    raise MutationNotSent(str(exc)) from None
            try:
                options = {'before_mutation': before_mutation}
                if sweep is not None:
                    options['sweep'] = sweep
                result = await self.profiles.ensure_post(job, **options)
                self.db.require_snapshot(generation, account_id=job['account_id'])
            except ConfigurationError:
                raise
            except Exception as exc:
                self.store.capability(job['account_id'], 'reply', 'held', 'profile_error', safe_error(exc))
                failures = job['payload'].get('comment_profile_failures', 0) + 1
                delay = min(86400, 300 * 2 ** min(failures - 1, 8))
                if isinstance(exc, ReplizError) and exc.status == 429:
                    delay = max(900, exc.retry_after)
                    self.store.set('reply_profile_retry_at', iso(now() + timedelta(seconds=delay)))
                self.store.checkpoint(job['id'], payload={'comment_profile_retry_at': iso(now() + timedelta(seconds=delay)),
                    'comment_profile_failures': failures, 'comment_profile_error': safe_error(exc)})
                raise
            self.store.checkpoint(job['id'], payload={'comment_profile': {**result, 'checked_at': iso()},
                'comment_profile_retry_at': None, 'comment_profile_failures': 0, 'comment_profile_error': None})
            self.store.capability(job['account_id'], 'reply', 'ready', 'profile_verified')
            return result

    async def profile_once(self):
        if not self.store.get('reply_enabled', False):
            return
        retry_at = self.store.get('reply_profile_retry_at')
        if retry_at and timestamp(retry_at) > now():
            return
        self.config.require_live_action()
        await self.inventory()
        with self.store.connect() as c:
            rows = c.execute("""SELECT id FROM jobs WHERE status='published'
                AND json_extract(payload,'$.post_id') IS NOT NULL
                ORDER BY json_extract(payload,'$.comment_profile.checked_at') IS NOT NULL,
                    julianday(json_extract(payload,'$.comment_profile.checked_at')),created_at,id""").fetchall()
        count, failures = 0, []
        batch_limit = max(1, min(20, self.config.section('reply').get('profile_batch_size', 5)))
        sweep = ProfileSweep()
        for row in rows:
            job = self.store.job(row['id'])
            if job['account_id'] not in self.accounts:
                continue
            checked = job['payload'].get('comment_profile', {}).get('checked_at')
            if checked and now() - timestamp(checked) < timedelta(hours=1):
                continue
            retry_at = job['payload'].get('comment_profile_retry_at')
            if retry_at and timestamp(retry_at) > now():
                continue
            count += 1
            try:
                await self.ensure_profile(job, sweep=sweep)
            except ReplyWait:
                raise
            except Exception as exc:
                failures.append(safe_error(exc))
                self.store.record('reply_profile_post_failed', job['id'], error=safe_error(exc))
                if isinstance(exc, ReplizError) and exc.status == 429:
                    break
            if count >= batch_limit:
                break
        self.store.set('reply_profile_at', iso())
        self.store.set('reply_profile_error', '; '.join(failures) if failures else None)
        self.store.health('reply_profiles', 'error' if failures else 'ok',
                          reason='; '.join(failures), details={'checked': count, 'failed': len(failures)})

    async def profile_loop(self):
        while True:
            try:
                with api_scope('background'):
                    await self.profile_once()
                if not self.store.get('reply_enabled', False):
                    self.store.health('reply_profiles', 'waiting')
            except ReplyWait as exc:
                self.store.set('reply_profile_error', None)
                self.store.health('reply_profiles', 'waiting', reason=str(exc))
            except Exception as exc:
                self.store.set('reply_profile_error', safe_error(exc))
                self.store.health('reply_profiles', 'error', reason=safe_error(exc))
                self.store.record('reply_profile_failed', error=safe_error(exc))
            await asyncio.sleep(self.config.section('reply').get('profile_seconds', 300))

    async def worker_loop(self):
        self.db.recover()
        while True:
            try:
                if self.store.get('reply_enabled', False):
                    with api_scope('reply'):
                        await self.process_one()
                        await self.reconcile()
                    self.store.set('reply_worker_at', iso())
                self.store.health('reply_worker', 'ok' if self.store.get('reply_enabled', False) else 'waiting')
            except ReplyWait as exc:
                self.store.health('reply_worker', 'waiting', reason=str(exc))
            except Exception as exc:
                self.store.health('reply_worker', 'error', reason=safe_error(exc))
                self.store.record('reply_worker_failed', error=safe_error(exc))
            await asyncio.sleep(15)


async def preview(config, job_id, text, folder):
    if folder.exists():
        raise ValueError('Folder preview harus baru.')
    with closing(sqlite3.connect(config.database_path.resolve().as_uri() + '?mode=ro', uri=True)) as c:
        c.row_factory = sqlite3.Row
        row = c.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('Job tidak ditemukan.')
        job = Store.decode(row)
    if job['account_id'] == EXCLUDED_ACCOUNT_ID or job['status'] != 'published' or not job['payload'].get('post_id'):
        raise ValueError('Preview membutuhkan job published yang diizinkan.')
    ai = ReplyAI(config)
    context = await ai.context(job, folder)
    event = {'id': 'preview:' + job_id, 'payload': {'text': text},
             'persona': {'profile': job['payload'].get('delivery_metadata', {}).get('persona_profile', {})}}
    draft = await ai.draft(event, context, [], folder)
    report = {'published': False, 'job_id': job_id, 'comment': text, 'context': context, 'draft': draft}
    save(folder / 'report.json', report)
    return {'report': str(folder / 'report.json'), 'quality': context['quality'], 'draft': draft, 'published': False}


def main():
    parser = argparse.ArgumentParser(description='Operasi auto-reply komentar kontekstual.')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('status', 'pause', 'resume', 'activate'):
        sub.add_parser(action)
    p = sub.add_parser('preview')
    p.add_argument('--job', required=True)
    p.add_argument('--text', required=True)
    p.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    config = Config.load()
    if args.action == 'preview':
        result = asyncio.run(preview(config, args.job, args.text, args.folder))
    else:
        store = Store(config.database_path, readonly=args.action == 'status')
        db = ReplyStore(store)
        if args.action in {'activate', 'resume'}:
            config.require_live_action()
            if args.action == 'resume' and not store.get('reply_since'):
                raise ValueError('Gunakan activate untuk aktivasi pertama.')
            async def activate():
                api = Repliz(config)
                try:
                    generation = db.require_snapshot()
                    accounts = await api.pages('/public/account', {'types': ['tiktok', 'facebook', 'youtube']})
                    db.require_snapshot(generation)
                    eligible = {a['id'] for a in store.accounts() if a['connected'] and a['username']}
                    ids = [object_id(a) for a in accounts if allowed_account(a) and a.get('isConnected') and object_id(a) in eligible]
                    if not ids:
                        raise ValueError('Tidak ada akun target.')
                    # Verify read access before enabling. Historical comments are marked on first poll.
                    await api.request('GET', '/public/comment', params=[('page', '1'), ('limit', '1')] + [('accountIds', a) for a in ids])
                    enrollment = db.enroll_accounts(ids, enable=True, generation=generation)
                    store.record('reply_activated', **enrollment)
                finally:
                    await api.close()
            asyncio.run(activate())
        elif args.action == 'pause':
            store.set('reply_enabled', False)
            store.record('reply_paused')
        result = db.summary()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
