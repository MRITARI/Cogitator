"""Mod details from Nexus for the details panel: a tolerant parser, a 14-day JSON cache, a quota guard and a strict image fetch.

Everything the mod author wrote (name, summary, description, changelog) is untrusted: it is reduced to plain text here and the page
only ever sets it with textContent. Network use is minimal: at most one mod() call per mod per TTL, nothing at all when there is no
key or the remembered hourly quota is low, and images only when a panel is opened (never while scrolling)."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from .config import cache_dir, get_api_key
from .fsutil import atomic_write_bytes
from .nexus import NexusClient, NexusError, NotFound, RateLimited, make_download_client

TTL_S = 14 * 24 * 3600
MIN_HOURLY = 50  # below this the details panel makes no call (the update check needs the budget more)
QUOTA_FRESH_S = 3600  # a remembered quota older than the hourly window says nothing any more
FAIL_BACKOFF_S = 600  # after a failed call, do not ask again for this mod for a while
MAX_JSON_BYTES = 256 * 1024
MAX_CACHE_FILES = 400
MAX_IMAGE_BYTES = int(1.5 * 1024 * 1024)
MAX_IMAGE_DIR_BYTES = 40 * 1024 * 1024
MAX_REDIRECTS = 3
DESCRIPTION_CAP = 4000
HOST_SUFFIX = ".nexusmods.com"

OK, CACHED, NO_KEY, LOW_QUOTA, RATE_LIMITED, NOT_FOUND, OFFLINE, UNLINKED, ERROR = (
    "ok", "cached", "no_key", "low_quota", "rate_limited", "not_found", "offline", "unlinked", "error")

_failed: dict[int, float] = {}  # mod id -> time of the last failed call (this run only)


# ---------------------------------------------------------------- text from untrusted markup
_DROP_BLOCK = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>|\[(img|youtube|video|code)(=[^\]]*)?\].*?\[/\2\]", re.I | re.S)
_BREAK = re.compile(r"<\s*(br|/p|/div|/li|/h[1-6]|/tr)\b[^>]*>|\[/?(list|quote|center|heading|line|hr)(=[^\]]*)?\]", re.I)
_ITEM = re.compile(r"\[\*\]|<\s*li\b[^>]*>", re.I)
_URL_BB = re.compile(r"\[url=([^\]\s]{1,300})\](.*?)\[/url\]", re.I | re.S)
_TAG = re.compile(r"<[^>]{0,1000}>")
_BB = re.compile(r"\[/?(b|i|u|s|url|size|color|font|left|right|spoiler|email|sup|sub|table|tr|td|th|center|quote|list|heading|img|code)(=[^\]]*)?\]", re.I)
_CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


def plain_text(raw: Any, cap: int = DESCRIPTION_CAP) -> str:
    """BBCode / HTML / entities -> readable plain text, at most `cap` characters. Never raises."""
    if not isinstance(raw, str):
        return ""
    s = raw[:cap * 12]  # bound the work on hostile input before any regex runs
    s = _DROP_BLOCK.sub(" ", s)
    s = _URL_BB.sub(lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(2).strip() != m.group(1) else m.group(2), s)
    s = _ITEM.sub("\n- ", s)
    s = _BREAK.sub("\n", s)
    s = html.unescape(s)  # entities may hide tags ("&lt;script&gt;"): decode first, then strip
    s = _DROP_BLOCK.sub(" ", s)
    s = _BREAK.sub("\n", s)
    s = _TAG.sub("", s)
    s = _BB.sub("", s)
    s = _CTRL.sub("", s.replace("\r", "\n").replace("\t", " "))
    s = re.sub(r"[  ]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s if len(s) <= cap else s[:cap].rstrip() + "..."


# ---------------------------------------------------------------- parsing
def _int(v: Any) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def host_allowed(url: str) -> bool:
    """https, a host ending in .nexusmods.com, no credentials, default port."""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        port = p.port
    except ValueError:
        return False
    return (p.scheme == "https" and host.endswith(HOST_SUFFIX) and len(host) > len(HOST_SUFFIX) and not p.username and not p.password
            and port in (None, 443))


def safe_https_url(v: Any) -> str:
    return v if isinstance(v, str) and len(v) <= 500 and host_allowed(v) else ""


def parse_details(payload: Any) -> dict | None:
    """The fields the panel shows, from the v1 mod JSON. None when the reply is not a mod at all."""
    if not isinstance(payload, dict) or not (payload.get("name") or payload.get("mod_id")):
        return None
    uniq = _int(payload.get("mod_unique_downloads"))
    return {
        "name": plain_text(payload.get("name"), 200), "author": plain_text(payload.get("author") or payload.get("uploaded_by"), 100),
        "summary": plain_text(payload.get("summary"), 600), "description": plain_text(payload.get("description")),
        "version": plain_text(payload.get("version"), 40), "picture_url": safe_https_url(payload.get("picture_url")),
        "endorsements": _int(payload.get("endorsement_count")),
        "downloads": uniq if uniq is not None else _int(payload.get("mod_downloads")),
        "updated": _int(payload.get("updated_timestamp")),
    }


# ---------------------------------------------------------------- cache
def _dir() -> Path:
    return cache_dir() / "nexus"


def _path(mod_id: int, suffix: str = "") -> Path:
    return _dir() / f"mod-{int(mod_id)}{suffix}.json"


def _read(path: Path, now: float) -> tuple[dict | None, bool]:
    """(cached record, fresh). Corrupt, oversize or foreign files count as no cache."""
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None, False
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, False
    if (not isinstance(rec, dict) or not isinstance(rec.get("data"), (dict, type(None)))
            or not isinstance(rec.get("at"), (int, float)) or isinstance(rec.get("at"), bool)):
        return None, False
    return rec, 0 <= now - rec["at"] < TTL_S


def _write(path: Path, rec: dict) -> None:
    blob = json.dumps(rec, ensure_ascii=False).encode("utf-8")
    if len(blob) > MAX_JSON_BYTES:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, blob)
        files = sorted(path.parent.glob("mod-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[MAX_CACHE_FILES:]:
            p.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- quota guard
_QUOTA = re.compile(r"(\d+|\?)\s+daily\s*/\s*(\d+|\?)\s+hourly", re.I)


def remembered_hourly(registry: Any, now: float) -> int | None:
    """The hourly quota the last live call reported, if that was within the hour; else None (unknown)."""
    try:
        q = registry.data["updates"].get("quota") or {}
        m = _QUOTA.search(str(q.get("text") or ""))
        at = q.get("at")
        if m and m.group(2) != "?" and isinstance(at, (int, float)) and 0 <= now - at < QUOTA_FRESH_S:
            return int(m.group(2))
    except (AttributeError, KeyError, TypeError):
        pass
    return None


def record_quota(registry: Any, nx: NexusClient, now: float) -> None:
    try:
        if nx.requests > 0:
            registry.data["updates"]["quota"] = {"text": str(nx.quota), "at": now}
            registry.save()
    except (AttributeError, KeyError, OSError):
        pass


def blocked_by_quota(registry: Any, now: float) -> bool:
    h = remembered_hourly(registry, now)
    return h is not None and h < MIN_HOURLY


# ---------------------------------------------------------------- details
@dataclass
class Result:
    status: str
    data: dict | None = None
    message: str = ""
    stale: bool = False
    at: float = 0.0
    extra: dict = field(default_factory=dict)


MESSAGES = {
    NO_KEY: "Add your Nexus API key in Settings to see details from Nexus.",
    LOW_QUOTA: "The Nexus request budget is low right now; details will load later.",
    RATE_LIMITED: "Nexus is rate limiting this key. Try again later.",
    NOT_FOUND: "Nexus has no mod with this id (it may have been removed).",
    OFFLINE: "Could not reach Nexus.",
    UNLINKED: "Not linked to Nexus yet.",
    ERROR: "Nexus did not return usable details.",
}


def _cached_or(status: str, rec: dict | None) -> Result:
    """The old cache entry (marked stale) when there is one, else the plain status."""
    if rec and rec.get("data"):
        return Result(CACHED, rec["data"], MESSAGES.get(status, ""), stale=True, at=float(rec["at"]), extra={"why": status})
    return Result(status, None, MESSAGES.get(status, ""))


def _call(mod_id: int, registry: Any, t: float, key: str, factory: Callable[[str], NexusClient] | None, what: Callable[[NexusClient], Any]) -> Any:
    """One guarded Nexus call; the quota the reply reported is remembered even when the call fails."""
    with (factory or NexusClient)(key) as nx:
        if nx.exhausted:
            raise RateLimited("exhausted")
        try:
            return what(nx)
        finally:
            record_quota(registry, nx, t)


def _failure(e: Exception, mod_id: int, t: float, rec: dict | None) -> Result:
    if isinstance(e, NotFound):
        return Result(NOT_FOUND, None, MESSAGES[NOT_FOUND])
    if isinstance(e, RateLimited):
        return _cached_or(RATE_LIMITED, rec)
    _failed[mod_id] = t
    return _cached_or(OFFLINE if "Network" in str(e) else ERROR, rec)


def details_for(mod_id: int | None, registry: Any, *, api_key: str | None = None, now: Callable[[], float] = time.time,
                client_factory: Callable[[str], NexusClient] | None = None) -> Result:
    """Cached details when fresh; else exactly one mod() call, unless there is no key, the quota is low, or a recent call failed."""
    if not mod_id:
        return Result(UNLINKED, None, MESSAGES[UNLINKED])
    t = now()
    path = _path(mod_id)
    rec, fresh = _read(path, t)
    if rec is not None and fresh:
        return Result(NOT_FOUND, None, MESSAGES[NOT_FOUND]) if rec.get("data") is None else Result(OK, rec["data"], at=float(rec["at"]))
    key = api_key if api_key is not None else get_api_key()
    if not key:
        return _cached_or(NO_KEY, rec)
    if blocked_by_quota(registry, t):
        return _cached_or(LOW_QUOTA, rec)
    if t - _failed.get(mod_id, -1e12) < FAIL_BACKOFF_S:
        return _cached_or(OFFLINE, rec)
    try:
        payload = _call(mod_id, registry, t, key, client_factory, lambda nx: nx.mod(int(mod_id)))
    except NexusError as e:
        if isinstance(e, NotFound):
            _write(path, {"at": t, "data": None})
        return _failure(e, mod_id, t, rec)
    data = parse_details(payload)
    if data is None:
        _failed[mod_id] = t
        return _cached_or(ERROR, rec)
    _write(path, {"at": t, "data": data})
    return Result(OK, data, at=t)


def changes_for(mod_id: int | None, registry: Any, *, api_key: str | None = None, now: Callable[[], float] = time.time,
                client_factory: Callable[[str], NexusClient] | None = None) -> Result:
    """The changelog (newest first, plain text), same cache and guard rules; called only when the user opens "Versions"."""
    if not mod_id:
        return Result(UNLINKED, None, MESSAGES[UNLINKED])
    t = now()
    path = _path(mod_id, "-changes")
    rec, fresh = _read(path, t)
    if rec is not None and fresh and rec.get("data") is not None:
        return Result(OK, rec["data"], at=float(rec["at"]))
    key = api_key if api_key is not None else get_api_key()
    if not key:
        return _cached_or(NO_KEY, rec)
    if blocked_by_quota(registry, t):
        return _cached_or(LOW_QUOTA, rec)
    try:
        raw = _call(mod_id, registry, t, key, client_factory, lambda nx: nx.changelogs(int(mod_id)))
    except NexusError as e:
        return _failure(e, mod_id, t, rec)
    items = [{"version": plain_text(v, 40), "lines": [plain_text(x, 300) for x in lines[:30]]} for v, lines in list(raw.items())[-15:][::-1]]
    data = {"versions": items}
    _write(path, {"at": t, "data": data})
    return Result(OK, data, at=t)


# ---------------------------------------------------------------- images
def _image_path(url: str, ext: str) -> Path:
    return _dir() / "img" / f"{hashlib.sha256(url.encode()).hexdigest()[:32]}{ext}"


def sniff_image(head: bytes) -> tuple[str, str] | None:
    """(mime, extension) for jpeg, png and webp by magic bytes; None for anything else."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def _data_uri(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    kind = sniff_image(data[:16])
    if kind is None or len(data) > MAX_IMAGE_BYTES:
        return None
    return f"data:{kind[0]};base64,{base64.b64encode(data).decode('ascii')}"


def cached_image(url: str) -> str | None:
    """A data: URI for an image that is already on disk. Never touches the network."""
    if not host_allowed(url or ""):
        return None
    for ext in (".jpg", ".png", ".webp"):
        p = _image_path(url, ext)
        if p.is_file():
            return _data_uri(p)
    return None


def fetch_image(url: str, client: httpx.Client | None = None) -> str | None:
    """Download one picture and return it as a data: URI, or None. https on *.nexusmods.com only (every redirect hop included),
    at most 1.5 MB, and the bytes must really be jpeg, png or webp. The client carries the app's User-Agent and no API key."""
    have = cached_image(url)
    if have or not host_allowed(url or ""):
        return have
    own = client is None
    c = client or make_download_client(timeout=20.0)
    try:
        cur = url
        for _ in range(MAX_REDIRECTS + 1):
            buf = bytearray()
            with c.stream("GET", cur, follow_redirects=False) as r:
                if 300 <= r.status_code < 400:
                    nxt = urljoin(cur, r.headers.get("location", ""))
                    if not host_allowed(nxt):
                        return None
                    cur = nxt
                    continue
                if r.status_code != 200:
                    return None
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype and not (ctype.startswith("image/") or ctype == "application/octet-stream"):
                    return None
                if (_int(r.headers.get("content-length")) or 0) > MAX_IMAGE_BYTES:
                    return None
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_IMAGE_BYTES:
                        return None
            kind = sniff_image(bytes(buf[:16]))
            if kind is None:
                return None
            path = _image_path(url, kind[1])
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(path, bytes(buf))
                _trim_images(path.parent)
            except OSError:
                pass
            return _data_uri(path)
        return None  # too many redirects
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if own:
            c.close()


def _trim_images(folder: Path) -> None:
    try:
        files = sorted((p for p in folder.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for p in files:
            total += p.stat().st_size
            if total > MAX_IMAGE_DIR_BYTES:
                p.unlink(missing_ok=True)
    except OSError:
        pass
