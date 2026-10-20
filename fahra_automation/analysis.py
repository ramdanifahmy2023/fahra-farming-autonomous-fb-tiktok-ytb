from __future__ import annotations

import asyncio
import base64
import json
import math
import re

import httpx

from .config import ConfigurationError
from .captioning import DELIVERY_PROMPT, EDITOR_PROMPT, POLICY_VERSION, SYSTEM_PROMPT, editorial_issues
from .hook import layout_hook
from .ai_usage import post


class LowConfidenceError(ValueError):
    pass


class Analyzer:
    def __init__(self, config, media, store=None):
        self.config, self.media = config, media
        self.store = store

    def ready(self):
        cfg = self.config.section("ai")
        if not cfg["base_url"] or not cfg["model"]:
            raise ConfigurationError("Endpoint dan model AI belum diatur di config.local.toml.")
        if cfg["transcription_enabled"]:
            if cfg["transcription_backend"] == "local":
                if not self.config.path(cfg["whisper_model"]).is_file():
                    raise ConfigurationError("Model Whisper lokal belum tersedia. Jalankan setup-whisper.")
            elif not cfg["transcription_model"]:
                raise ConfigurationError("Model transkripsi belum diatur.")
        self.config.ai_key()

    @staticmethod
    def validate(data):
        if not isinstance(data, dict) or not all(k in data for k in ("hook", "caption", "hashtags")):
            raise ValueError("Respons AI tidak memuat hook, caption, dan hashtags.")
        if not isinstance(data["hook"], str) or not isinstance(data["caption"], str):
            raise ValueError("Hook dan caption harus teks.")
        confidence = data.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("Confidence harus angka 0 sampai 1.")
        if confidence < .5:
            raise LowConfidenceError("AI tidak cukup yakin memahami konten video.")
        hook = data["hook"].strip()
        caption = data["caption"].strip()
        if not 1 <= len(hook) <= 60 or len(hook.split()) > 10 or "\n" in hook:
            raise ValueError("Teks pembuka harus satu kalimat pendek, maksimal 60 karakter dan 10 kata.")
        if not 1 <= len(caption) <= 360:
            raise ValueError("Caption harus ringkas, satu atau dua kalimat, maksimal 360 karakter.")
        if re.search(r"(?<!\w)[#@]\w+|https?://|www\.", hook + " " + caption, re.I):
            raise ValueError("Hook/caption tidak boleh memuat hashtag, mention, atau tautan.")
        if not isinstance(data["hashtags"], list):
            raise ValueError("Hashtag harus berupa daftar.")
        tags = []
        for tag in data["hashtags"]:
            if not isinstance(tag, str):
                raise ValueError("Hashtag harus teks.")
            tag = "#" + tag.strip().lstrip("#").casefold()
            if not re.fullmatch(r"#[\w]{1,50}", tag, re.UNICODE):
                raise ValueError("Format hashtag tidak valid.")
            if tag.casefold() not in [t.casefold() for t in tags]:
                tags.append(tag)
        if not 1 <= len(tags) <= 5:
            raise ValueError("Gunakan satu sampai lima hashtag relevan.")
        return {"hook": hook, "caption": caption, "hashtags": tags}

    async def generate(self, client, job, frames, transcript, folder, recent_captions=()):
        recent = [{"hook": c.get("hook", "")[:60], "caption": c["caption"][:360]}
                  for c in recent_captions if isinstance(c.get("caption"), str)][:8]
        content = [{"type": "text", "text": json.dumps({
            "source_caption": job["payload"].get("source_caption", "")[:6000],
            "transcript": transcript[:16000], "recent_captions": recent,
            "task": "Tulis hook, caption, dan hashtag baru berdasarkan bukti konten ini.",
        }, ensure_ascii=False)}]
        content.extend({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()}}
                       for p in frames)
        trace = {"policy_version": POLICY_VERSION, "model": self.config.section("ai")["model"], "attempts": []}
        folder.mkdir(parents=True, exist_ok=True)
        evidence = None
        observation_snapshot = None
        for stage in ("observe", "write"):
            if stage == "write":
                content = json.dumps({"observations": evidence["observations"], "tone": evidence["tone"],
                                      "draft": generated, "recent_captions": recent}, ensure_ascii=False)
            messages = [{"role": "system", "content": SYSTEM_PROMPT if stage == "observe" else EDITOR_PROMPT},
                        {"role": "user", "content": content}]
            for attempt in range(2):
                r = await post(client, store=self.store, operation='caption:' + stage, subject=job.get('id'),
                    payload={"model": trace["model"], "messages": messages,
                    "response_format": {"type": "json_object"}})
                r.raise_for_status()
                response = r.json()
                raw = response["choices"][0]["message"]["content"]
                entry = {"stage": stage, "attempt": attempt + 1, "model": response.get("model"), "response": raw}
                trace["attempts"].append(entry)
                try:
                    data = json.loads(raw)
                    if stage == "write" and isinstance(data, dict):
                        data["confidence"] = evidence["confidence"]
                    if stage == "observe" and isinstance(data, dict):
                        if observation_snapshot is not None:
                            confidence = data.get('confidence')
                            valid_score = (type(confidence) in (int, float) and math.isfinite(confidence)
                                           and 0 <= confidence <= 1)
                            data.update(observation_snapshot)
                            if valid_score:
                                data['confidence'] = min(confidence, observation_snapshot['confidence'])
                        else:
                            confidence = data.get('confidence')
                            if (type(confidence) in (int, float) and math.isfinite(confidence) and .5 <= confidence <= 1
                                    and isinstance(data.get('observations'), str) and data['observations'].strip()
                                    and data.get('tone') in {'ringan', 'hangat', 'praktis', 'serius', 'netral'}):
                                observation_snapshot = {key: data[key] for key in ('observations', 'tone', 'confidence')}
                    generated = self.validate(data)
                    # Repair new shared hooks here; delivery-only edits keep their already rendered hook.
                    layout_hook(generated['hook'], 540, 960)
                    if stage == "observe":
                        if not isinstance(data.get("observations"), str) or not data["observations"].strip():
                            raise ValueError("Catat bukti konten pada observations sebelum menulis caption.")
                        if data.get("tone") not in {"ringan", "hangat", "praktis", "serius", "netral"}:
                            raise ValueError("Pilih tone sesuai konteks: ringan, hangat, praktis, serius, atau netral.")
                    issues = editorial_issues(generated, recent, tone=evidence['tone']) if stage == "write" else []
                except LowConfidenceError as exc:
                    # The editor must never turn uncertain evidence into a passing confidence score.
                    entry["issues"] = [str(exc)]
                    self.save_review(folder, trace)
                    raise
                except (ValueError, TypeError) as exc:
                    issues = [str(exc)]
                entry["issues"] = issues
                self.save_review(folder, trace)
                if not issues:
                    if stage == "observe":
                        evidence = data
                    break
                if attempt == 0:
                    messages.extend([{"role": "assistant", "content": raw}, {"role": "user", "content":
                        "Perbaiki JSON/gaya sekali berdasarkan bukti yang sama. Jangan menambah fakta atau menaikkan confidence. "
                        + json.dumps({"perbaikan": issues, "frozen_evidence": observation_snapshot}, ensure_ascii=False)}])
            if issues:
                raise ValueError("Caption belum lolos pemeriksaan setelah satu perbaikan: " + "; ".join(issues))
        return {"generated": generated, "caption_policy_version": POLICY_VERSION,
                "caption_review": {"attempts": len(trace["attempts"]), "confidence": evidence["confidence"],
                    "models": [a["model"] for a in trace["attempts"]],
                    "observations": evidence["observations"][:1200], "tone": evidence["tone"]}}

    @staticmethod
    def save_review(folder, trace):
        target = folder / "caption-review.json"
        partial = target.with_suffix(".partial")
        partial.write_text(json.dumps(trace, ensure_ascii=False, indent=2))
        partial.chmod(0o600)
        partial.replace(target)

    async def analyze(self, job, folder, recent_captions=()):
        self.ready()
        cfg = self.config.section("ai")
        frames, audio = await asyncio.to_thread(self.media.analysis_assets, job["payload"]["source_path"], folder,
                                                6, cfg["transcription_enabled"])
        transcript = ""
        headers = {"Authorization": "Bearer " + self.config.ai_key()}
        async with httpx.AsyncClient(base_url=cfg["base_url"].rstrip("/") + "/", headers=headers, timeout=180) as client:
            if audio and cfg["transcription_backend"] == "local":
                output = folder / "transcript"
                await asyncio.to_thread(self.media.run, [cfg["whisper_binary"], "-m", str(self.config.path(cfg["whisper_model"])),
                    "-f", str(audio), "-l", "auto", "-otxt", "-of", str(output), "-np", "-nt"], 900)
                transcript = output.with_suffix(".txt").read_text().strip()
            elif audio:
                with audio.open("rb") as stream:
                    r = await client.post("audio/transcriptions", data={"model": cfg["transcription_model"]},
                                          files={"file": (audio.name, stream, "audio/wav")})
                r.raise_for_status()
                transcript = r.json().get("text", "")
            result = await self.generate(client, job, frames, transcript, folder, recent_captions)
        return {**result, "transcript": transcript, "analysis_model": cfg["model"],
                "analysis_frames": [str(p) for p in frames]}

    @staticmethod
    def validate_delivery_metadata(data, job, recent_captions=()):
        if not isinstance(data, dict):
            raise ValueError('Metadata tujuan harus object.')
        review = job['payload'].get('caption_review') or {}
        generated = Analyzer.validate({**data, 'hook': job['payload']['generated']['hook'],
                                       'confidence': review.get('confidence')})
        title = data.get('title', '')
        if not isinstance(title, str) or len(title.strip()) > 100 or '\n' in title:
            raise ValueError('Judul harus satu baris maksimal 100 karakter.')
        title = title.strip()
        if job['platform'] == 'youtube' and not title:
            raise ValueError('Judul YouTube wajib diisi.')
        if re.search(r'[#@]|https?://|www\.', title, re.I):
            raise ValueError('Judul tidak boleh memuat hashtag, mention, atau tautan.')
        # The hook is already burned into the video; only new delivery text can be repaired.
        issues = editorial_issues({**generated, 'title': title}, recent_captions,
                                  include_hook=False, tone=review.get('tone'))
        if issues:
            raise ValueError('; '.join(issues))
        tags = data.get('tags', [])
        if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() or
                re.search(r'[#@]|https?://|\n', t) for t in tags):
            raise ValueError('Tags harus berupa frasa pencarian tanpa tautan/hashtag.')
        tags = list(dict.fromkeys(t.strip() for t in tags))
        if len(','.join(tags)) > 500 or job['platform'] != 'youtube' and tags:
            raise ValueError('Tags hanya untuk YouTube, maksimal total 500 karakter.')
        description = generated['caption'] + '\n\n' + ' '.join(generated['hashtags'])
        return {'title': title, 'caption': generated['caption'], 'description': description,
                'hashtags': generated['hashtags'], 'tags': tags}

    async def delivery_metadata(self, job, persona, folder):
        cfg = self.config.section('ai')
        review = job['payload'].get('caption_review') or {}
        if not review.get('observations') or not isinstance(review.get('confidence'), (float, int)) or review['confidence'] < .5:
            raise LowConfidenceError('Metadata tujuan memerlukan bukti video dan confidence awal.')
        recent = self.store.recent_delivery_captions(job['account_id'], job['id'], limit=8) if self.store else []
        content = {'platform': job['platform'], 'persona': persona['profile'], 'observations': review['observations'],
                   'confidence': review['confidence'], 'tone': review.get('tone'),
                   'transcript': job['payload'].get('transcript', '')[:16000],
                   'shared_hook': job['payload']['generated']['hook'], 'draft': job['payload']['generated'],
                   'recent_captions': recent}
        messages = [{'role': 'system', 'content': DELIVERY_PROMPT},
                    {'role': 'user', 'content': json.dumps(content, ensure_ascii=False)}]
        trace = {'policy_version': POLICY_VERSION, 'model_requested': cfg['model'], 'attempts': []}
        folder.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(base_url=cfg['base_url'].rstrip('/') + '/',
                                    headers={'Authorization': 'Bearer ' + self.config.ai_key()}, timeout=180) as client:
            for attempt in range(2):
                response = await post(client, store=self.store, operation='delivery_metadata', subject=job['id'],
                    payload={'model': cfg['model'],
                    'messages': messages, 'response_format': {'type': 'json_object'}})
                response.raise_for_status()
                body = response.json()
                raw = body['choices'][0]['message']['content']
                entry = {'response': raw, 'model': body.get('model')}
                trace['attempts'].append(entry)
                try:
                    result = self.validate_delivery_metadata(json.loads(raw), job, recent)
                except (ValueError, TypeError, KeyError) as exc:
                    entry['error'] = str(exc)
                    self.save_review(folder, trace)
                    if attempt:
                        raise ValueError('Metadata tujuan belum lolos setelah satu perbaikan.') from exc
                    messages.extend([{'role': 'assistant', 'content': raw}, {'role': 'user',
                        'content': 'Perbaiki dengan bukti sama, tanpa fakta tambahan: ' + str(exc)}])
                    continue
                self.save_review(folder, trace)
                return {**result, 'model': body.get('model'), 'model_requested': cfg['model'],
                        'caption_policy_version': POLICY_VERSION, 'confidence': review['confidence']}
