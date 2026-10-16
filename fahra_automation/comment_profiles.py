"""Apply and verify Repliz comment settings without changing other automation modules."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path

from .config import Config, allowed_account
from .repliz import Repliz, ReplizError, MutationNotSent
from .journal import AttemptJournal
from .util import iso, safe_error

MODULES = ("delete", "reply", "like", "message", "chat", "story")


def object_id(value):
    return value.get("id", value.get("_id"))


def canonical(value):
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, dict):
        # Repliz regenerates postback payloads when saving a configuration.
        return {k: canonical(v) for k, v in value.items() if k != "payload"}
    return value


def defaults():
    keyword = {"isExactMatch": False, "values": []}
    condition = {"isActive": False, "isExactMatch": False, "keywords": []}
    delay = {"isActive": False, "value": 10, "type": "second"}
    text = {"isActive": False, "type": "text", "text": "", "prompt": "",
            "keyword": keyword, "delay": delay}
    button = {"type": "postback", "title": "", "payload": ""}
    opening = {"postback": {"isActive": False, "text": "", "image": "", "file": None, "button": button},
               "additional": {"isActive": False, "isPreventBeforeFollow": False, "text": "", "image": "", "file": None, "button": button},
               "webUrl": {"text": "", "image": "", "file": None, "buttons": []}}
    return copy.deepcopy({
        "delete": {"isActive": False, "type": "keyword", "keywords": [], "prompt": ""},
        "reply": {**text, "isIncludeContentContext": False, "condition": condition, "exception": condition},
        "like": {"isActive": False},
        "message": {**text, "opening": opening, "condition": condition},
        "chat": text,
        "story": {**text, "opening": opening, "condition": condition},
    })


def patch_config(profile):
    words = list(dict.fromkeys(w.strip().lower() for group in profile["categories"].values() for w in group))
    if not words or any(len(w) < 5 for w in words):
        raise ValueError("Profil memerlukan frasa spesifik dengan minimal lima karakter.")
    return {"like": {"isActive": True}, "delete": {"isActive": True, "type": "keyword", "keywords": words, "prompt": ""}}


def platform_patch(profile, platform):
    if platform not in {'facebook', 'tiktok', 'youtube'}:
        raise ValueError('Platform automasi tidak didukung.')
    patch = patch_config(profile)
    patch['like']['isActive'] = platform in {'facebook', 'tiktok'}
    return patch


def custom_reply_config(current, profile, platform):
    patch = platform_patch(profile, platform)
    wanted = merge_config(current, patch, content=True)
    wanted['reply']['isActive'] = False
    return wanted


def merge_config(current, patch, content=False):
    def fill_missing(base, existing):
        if not isinstance(base, dict) or not isinstance(existing, dict):
            return copy.deepcopy(base if existing is None else existing)
        result = copy.deepcopy(existing)
        for key, value in base.items():
            result[key] = fill_missing(value, existing.get(key))
        return result

    # Older Repliz configurations omit fields now required by the PUT schema.
    config = fill_missing(defaults(), current or {})
    config.update(copy.deepcopy(patch))
    if content:
        config.pop("chat", None)
    return config


def verify_config(actual, expected):
    def comparable(module, value):
        value = canonical(value)
        # Repliz reports execution status here; it is not part of either module's policy.
        if module in ("delete", "reply") and isinstance(value, dict):
            value.pop("status", None)
        return value

    return all(comparable(k, actual.get(k)) == comparable(k, v) for k, v in expected.items())


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    partial.chmod(0o600)
    partial.replace(path)


def account_snapshot(raw):
    return {k: raw[k] for k in ("id", "_id", "username", "type", "isConnected", "config", "updatedAt") if k in raw}


class CommentProfiles:
    def __init__(self, api, profile, folder, config=None):
        self.api, self.profile, self.folder = api, profile, folder
        self.config = config or getattr(api, 'config', None)
        self.patch = patch_config(profile)
        self.journal = {"started_at": iso(), "profile": profile["name"], "results": []}

    def require_live(self):
        if self.config is None:
            raise RuntimeError('Konfigurasi guard publikasi diperlukan sebelum mutasi automasi.')
        self.config.require_live_action()

    def record(self, **entry):
        self.journal["results"].append({"at": iso(), **entry})
        save(self.folder / "apply.json", self.journal)

    async def audit(self):
        listed = await self.api.pages("/public/account", {"types": ["tiktok", "facebook", "youtube"]})
        accounts = []
        for a in listed:
            if not allowed_account(a):
                continue
            raw = await self.api.request("GET", "/public/account/" + object_id(a))
            accounts.append(account_snapshot(raw))
        ids = [object_id(a) for a in accounts if allowed_account(a)]
        content = await self.api.pages("/public/automation", {"accountIds": ids}) if ids else []
        content = [{k: a[k] for k in ("id", "_id", "accountId", "contentId", "detail", "config", "updatedAt") if k in a}
                   for a in content if a.get("accountId") in ids]
        templates = await self.api.pages("/public/template")
        result = {"at": iso(), "accounts": accounts, "content_automations": content,
                  "templates": [{k: t[k] for k in ("id", "_id", "name", "config") if k in t} for t in templates]}
        save(self.folder / "before.json", result)
        return result

    async def ensure_template(self, platform='tiktok'):
        name = self.profile['name'] if platform == 'tiktok' else self.profile['name'] + '-' + platform
        wanted = merge_config({}, platform_patch(self.profile, platform))
        journal = AttemptJournal(self.config) if self.config else None
        action_id = 'template:' + name
        matches = [t for t in await self.api.pages("/public/template") if t.get("name") == name]
        if len(matches) > 1:
            raise RuntimeError("Nama template tidak unik; periksa sebelum menerapkan.")
        if matches:
            template = matches[0]
            if not verify_config(template.get("config", {}), wanted):
                raise RuntimeError("Template bernama sama memiliki konfigurasi berbeda; gunakan versi/nama baru.")
        else:
            self.require_live()
            journal.begin(action_id, {'name': name, 'config': wanted})
            self.record(kind="template_create_intent", name=name)
            post_accepted = False
            try:
                result = await self.api.request("POST", "/public/template", json={"name": name, "config": wanted})
                post_accepted = True
                template = await self.api.request("GET", "/public/template/" + object_id(result))
            except Exception as exc:
                definite = not post_accepted and (isinstance(exc, MutationNotSent) or
                    isinstance(exc, ReplizError) and exc.status in {400, 401, 403, 404, 422, 429})
                journal.mark(action_id, 'not_sent' if definite else 'unknown', error=safe_error(exc))
                if definite:
                    raise
                # A timed-out POST may have succeeded. Never blindly create a second template.
                matches = [t for t in await self.api.pages("/public/template") if t.get("name") == name]
                if len(matches) != 1:
                    raise RuntimeError("Hasil pembuatan template belum pasti; cocokkan daftar template sebelum mengulang.") from None
                template = matches[0]
        if not verify_config(template.get("config", {}), wanted):
            raise RuntimeError("Konfigurasi template hasil baca ulang tidak cocok.")
        if journal and journal.state(action_id):
            journal.mark(action_id, 'accepted', schedule_id=object_id(template))
        self.record(kind="template_verified", template_id=object_id(template))
        return object_id(template)

    async def update_one(self, kind, item, account, before):
        if not allowed_account(account):
            raise ValueError("Akun dikecualikan; perubahan ditolak.")
        identity = object_id(item)
        content = kind == "content"
        if account['type'] == 'youtube' and not content:
            self.record(kind=kind, id=identity, status='per_post_only')
            return
        get_path = f"/public/automation/{identity}" if content else f"/public/account/{identity}"
        path = get_path if content else get_path + "/automation"
        current = await self.api.request("GET", get_path)
        if content and current.get("accountId") != object_id(account):
            raise RuntimeError("Pemilik automasi konten berubah.")
        if not content and not allowed_account(current):
            raise ValueError("Identitas akun berubah atau dikecualikan.")
        if canonical(current.get("config", {})) != canonical(before.get("config", {})):
            raise RuntimeError("Konfigurasi berubah setelah audit; lakukan audit ulang agar perubahan lain tidak tertimpa.")
        wanted = merge_config(current.get("config", {}), platform_patch(self.profile, account['type']), content=content)
        if verify_config(current.get("config", {}), wanted):
            self.record(kind=kind, id=identity, account_id=object_id(account), status="already_matches")
            return
        self.require_live()
        self.record(kind=kind, id=identity, account_id=object_id(account), status="write_intent", before=current.get("config", {}), expected=wanted)
        try:
            await self.api.request("PUT", path, json={"config": wanted} if content else wanted)
        except Exception as exc:
            self.record(kind=kind, id=identity, status="write_response_error", error=safe_error(exc))
        actual = await self.api.request("GET", get_path)
        if not verify_config(actual.get("config", {}), wanted):
            raise RuntimeError(f"Baca ulang {kind} {identity} tidak cocok; hentikan perubahan berikutnya.")
        self.record(kind=kind, id=identity, account_id=object_id(account), status="verified", actual=actual["config"])
        print(json.dumps({"kind": kind, "username": account["username"], "id": identity, "status": "verified"}), flush=True)

    async def apply(self, before):
        accounts = {object_id(a): a for a in before["accounts"] if allowed_account(a)}
        templates = {platform: await self.ensure_template(platform) for platform in sorted({a['type'] for a in accounts.values()})}
        template_id = templates.get('tiktok')
        for a in accounts.values():
            await self.update_one("account", a, a, a)
        for a in before["content_automations"]:
            if a["accountId"] in accounts:
                await self.update_one("content", a, accounts[a["accountId"]], a)
        self.journal["template_id"] = template_id
        self.journal['templates'] = templates
        self.journal["completed_at"] = iso()
        save(self.folder / "apply.json", self.journal)
        return await self.verify(before, template_id)

    async def verify(self, before, template_id=None):
        results = []
        for a in before["accounts"]:
            actual = await self.api.request("GET", "/public/account/" + object_id(a))
            expected = (merge_config(a.get("config", {}), platform_patch(self.profile, a['type']))
                        if allowed_account(a) and a['type'] != 'youtube' else a.get("config", {}))
            results.append({"kind": "account", "id": object_id(a), "username": a["username"],
                            "excluded": not allowed_account(a), "matches": verify_config(actual.get("config", {}), expected)})
        accounts = {object_id(a): a for a in before["accounts"] if allowed_account(a)}
        for a in before["content_automations"]:
            if a["accountId"] not in accounts:
                continue
            actual = await self.api.request("GET", "/public/automation/" + object_id(a))
            expected = merge_config(a.get("config", {}), platform_patch(self.profile, accounts[a['accountId']]['type']), content=True)
            results.append({"kind": "content", "id": object_id(a), "matches": verify_config(actual.get("config", {}), expected)})
        report = {"at": iso(), "profile": self.profile["name"], "template_id": template_id,
                  "keyword_count": len(self.patch["delete"]["keywords"]), "results": results}
        save(self.folder / "verification.json", report)
        if not all(r["matches"] for r in results):
            raise RuntimeError("Sebagian konfigurasi tidak sesuai; lihat verification.json.")
        return report

    async def ensure_post(self, job, *, before_mutation=None):
        account_id, post_id = job['account_id'], job['payload'].get('post_id')
        if job['status'] != 'published' or not post_id:
            raise ValueError('Otomasi posting memerlukan published dan post_id terverifikasi.')
        account = await self.api.request('GET', '/public/account/' + account_id)
        account = account.get('data', account)
        if not allowed_account(account) or object_id(account) != account_id or not account.get('isConnected'):
            raise ValueError('Akun posting tidak eligible.')
        async def matching():
            rows = await self.api.pages('/public/automation', {'accountIds': [account_id]})
            return [a for a in rows if a.get('accountId') == account_id and
                    str(a.get('contentId') or (a.get('detail') or {}).get('id')) == str(post_id)]
        rows = await matching()
        if len(rows) > 1:
            raise RuntimeError('Override posting tidak unik.')
        current = rows[0].get('config', {}) if rows else (account.get('config') or {})
        wanted = custom_reply_config(current, self.profile, account['type'])
        journal = AttemptJournal(self.config) if self.config else None
        action_id = 'automation:' + account_id + ':' + str(post_id)
        if rows and verify_config(current, wanted):
            if journal and journal.state(action_id):
                journal.mark(action_id, 'accepted', schedule_id=object_id(rows[0]))
            return {'verified': True, 'automation_id': object_id(rows[0]), 'owner': 'custom'}
        if before_mutation is not None:
            before_mutation()
        self.require_live()
        if rows:
            identity = object_id(rows[0])
            fresh = await self.api.request('GET', '/public/automation/' + identity)
            fresh = fresh.get('data', fresh)
            if fresh.get('accountId') != account_id or canonical(fresh.get('config')) != canonical(current):
                raise RuntimeError('Override berubah sebelum PUT; perlu audit ulang.')
            try:
                await self.api.request('PUT', '/public/automation/' + identity,
                                       before_send=before_mutation, json={'config': wanted})
            except Exception as exc:
                self.record(kind='post_update_response_error', id=identity, error=safe_error(exc))
                if isinstance(exc, MutationNotSent) or isinstance(exc, ReplizError) and exc.status == 429:
                    raise
        else:
            journal.begin(action_id, {'account_id': account_id, 'content_id': post_id, 'config': wanted})
            try:
                await self.api.request('POST', '/public/automation',
                                       before_send=before_mutation,
                                       json={'accountId': account_id, 'contentId': post_id, 'config': wanted})
            except Exception as exc:
                definite = isinstance(exc, MutationNotSent) or isinstance(exc, ReplizError) and exc.status in {400, 401, 403, 404, 422, 429}
                journal.mark(action_id, 'not_sent' if definite else 'unknown', error=safe_error(exc))
                if definite:
                    raise
        rows = await matching()
        if len(rows) != 1 or not verify_config(rows[0].get('config', {}), wanted):
            raise RuntimeError('Otomasi belum terbukti via GET; jangan ulang create secara buta.')
        if journal.state(action_id):
            journal.mark(action_id, 'accepted', schedule_id=object_id(rows[0]))
        self.record(kind='post_custom_owner_verified', account_id=account_id, post_id=post_id,
                    expected=wanted, automation_id=object_id(rows[0]))
        return {'verified': True, 'automation_id': object_id(rows[0]), 'owner': 'custom'}


async def main():
    parser = argparse.ArgumentParser(description="Profil komentar Repliz: audit, apply, atau verify.")
    parser.add_argument("action", choices=["audit", "apply", "verify"])
    parser.add_argument("--profile", default="profiles/tiktok-comments-id.json")
    parser.add_argument("--folder", required=True, type=Path)
    args = parser.parse_args()
    config = Config.load()
    profile = json.loads(config.path(args.profile).read_text())
    api = Repliz(config)
    manager = CommentProfiles(api, profile, args.folder, config=config)
    try:
        if args.action == "audit":
            before = await manager.audit()
            print(json.dumps({"accounts": sum(allowed_account(a) for a in before["accounts"]),
                              "content_overrides": len(before["content_automations"]), "snapshot": str(args.folder / "before.json")}))
        else:
            before = json.loads((args.folder / "before.json").read_text())
            if args.action == "apply" and (args.folder / "apply.json").exists():
                raise RuntimeError("Folder ini sudah memiliki percobaan apply. Gunakan verify atau audit ke folder baru sebelum mengulang.")
            result = await manager.apply(before) if args.action == "apply" else await manager.verify(before)
            print(json.dumps({"verified": sum(r["matches"] for r in result["results"]), "template_id": result["template_id"]}))
    except Exception as exc:
        print(safe_error(exc))
        raise SystemExit(1) from None
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
