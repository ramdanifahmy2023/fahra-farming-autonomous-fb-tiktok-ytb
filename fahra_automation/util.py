from __future__ import annotations

import fcntl
import hashlib
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def now():
    return datetime.now(timezone.utc)


def iso(value=None):
    return (value or now()).astimezone(timezone.utc).isoformat()


def normalize_url(value):
    try:
        p = urlsplit(value.strip())
        valid = (p.scheme in {"http", "https"} and p.hostname in
                 {"instagram.com", "www.instagram.com", "m.instagram.com"}
                 and p.port in {None, 443} and not p.username and not p.password)
        match = re.fullmatch(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]{1,100})/?", p.path)
        if not valid or not match:
            raise ValueError
        return f"https://www.instagram.com/reel/{match[1]}/", match[1]
    except (ValueError, AttributeError):
        raise ValueError("Gunakan link langsung Reel atau posting video Instagram.") from None


def extract_urls(text):
    found, invalid = [], []
    for raw in re.findall(r"https?://[^\s<>]+", text):
        raw = raw.rstrip(").,;!?]}")
        try:
            found.append(normalize_url(raw))
        except ValueError:
            invalid.append(raw[:160])
    return found, invalid


def digest_file(path: Path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_error(error):
    if isinstance(error, BaseExceptionGroup):
        error = '; '.join(dict.fromkeys(safe_error(leaf) for leaf in exception_leaves(error)))
    text = str(error) or type(error).__name__
    text = re.sub(r"\b\d{6,15}:[A-Za-z0-9_-]{25,}\b", "[telegram-token]", text)
    text = re.sub(r"\b(?:sk|pk)-[A-Za-z0-9_-]+", "[api-key]", text)
    text = re.sub(r"https?://\S+", "[URL]", text)
    text = re.sub(r"(?i)(bearer|basic)\s+\S+", r"\1 [redacted]", text)
    text = re.sub(r"(?i)(api[_-]?key|secret|token|password)[\s:=]+\S+", r"\1=[redacted]", text)
    return text[:800]


def exception_leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in exception_leaves(child)]
    return [error]


@contextmanager
def process_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Proses workflow lain masih berjalan.") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
