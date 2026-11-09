"""Nexus Mods API v1 client: rate-limit aware, tolerant parsing. Never logs or prints the key."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from . import __version__

log = logging.getLogger(__name__)

BASE = "https://api.nexusmods.com/v1"
GRAPHQL = "https://api.nexusmods.com/v2/graphql"
GAME = "warhammer40kdarktide"
# Category ids seen in files.json: 1=MAIN, 4=OLD_VERSION, 7=ARCHIVED; category 6 arrives with category_name null.
# Ids 2, 3, 5 and the name DELETED for 6 come from Nexus's documentation. `category_name` is preferred whenever present.
_CATEGORY_IDS = {1: "MAIN", 2: "UPDATE", 3: "OPTIONAL", 4: "OLD_VERSION", 5: "MISCELLANEOUS", 6: "DELETED", 7: "ARCHIVED"}
VERIFIED_CATEGORY_IDS = frozenset({1, 4, 6, 7})


class NexusError(Exception):
    pass


class NoApiKey(NexusError):
    pass


class InvalidKey(NexusError):
    pass


class NotFound(NexusError):
    pass


class RateLimited(NexusError):
    pass


@dataclass
class Quota:
    daily: int | None = None
    hourly: int | None = None

    def __str__(self) -> str:
        return f"{'?' if self.daily is None else self.daily} daily / {'?' if self.hourly is None else self.hourly} hourly left"


@dataclass
class NexusFile:
    file_id: int | None
    version: str | None
    category: str | None  # MAIN | UPDATE | OPTIONAL | OLD_VERSION | ...
    uploaded: int | None  # unix seconds
    name: str | None = None
    file_name: str | None = None
    is_primary: bool = False
    size_bytes: int | None = None


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def parse_files(payload: Any) -> list[NexusFile]:
    """Tolerant `files.json` parser: accepts {"files": [...]} or a bare list; skips junk entries."""
    items = payload.get("files") if isinstance(payload, dict) else payload
    out: list[NexusFile] = []
    if not isinstance(items, list):
        return out
    for d in items:
        if not isinstance(d, dict):
            continue
        fid = _int(d.get("file_id"))
        if fid is None:
            ident = d.get("id")
            fid = _int(ident[0]) if isinstance(ident, list) and ident else _int(ident)
        cat = d.get("category_name")
        if isinstance(cat, str) and cat.strip():
            cat = cat.strip().upper().replace(" ", "_")
        else:
            cat = _CATEGORY_IDS.get(_int(d.get("category_id")) or -1)
        up = _int(d.get("uploaded_timestamp"))
        ver = d.get("mod_version") or d.get("version")
        out.append(NexusFile(fid, str(ver).strip() if ver not in (None, "") else None, cat, up,
                             d.get("name") if isinstance(d.get("name"), str) else None,
                             d.get("file_name") if isinstance(d.get("file_name"), str) else None,
                             bool(d.get("is_primary")), _int(d.get("size_in_bytes"))))
    return out


def latest_main_file(files: list[NexusFile]) -> NexusFile | None:
    """The file to track. Among MAIN files the *primary* one wins (mod 19 has a MAIN "(Update Only)" variant
    uploaded seconds after the primary), then newest upload. Falls back to the primary file, else None."""
    pool = [f for f in files if f.category == "MAIN"] or [f for f in files if f.is_primary]
    if not pool:
        return None
    return max(pool, key=lambda f: (f.is_primary, f.uploaded or 0, f.file_id or 0))


def release_group(files: list[NexusFile], latest: NexusFile | None) -> set[int]:
    """File ids that count as the current release: the latest file plus MAIN variants with the same version."""
    if latest is None:
        return set()
    ids = {latest.file_id} if latest.file_id is not None else set()
    ids |= {f.file_id for f in files if f.category == "MAIN" and f.file_id is not None
            and latest.version is not None and f.version == latest.version}
    return ids


_REF = re.compile(r"nexusmods\.com/(?P<game>[^/\s?#]+)/mods/(?P<id>\d+)", re.I)


def parse_nexus_ref(text: str) -> tuple[int | None, str | None]:
    """(mod_id, problem). Accepts a bare id or a nexusmods.com/<game>/mods/<id> URL (query/fragment ignored)."""
    t = (text or "").strip()
    if t.isdigit():
        return int(t), None
    m = _REF.search(t)
    if not m:
        return None, "Not a Nexus mod URL or numeric id."
    if m["game"].lower() != GAME:
        return None, f"That mod belongs to '{m['game']}', not {GAME}."
    return int(m["id"]), None


class NexusClient:
    def __init__(self, api_key: str | None, client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        if not api_key:
            raise NoApiKey("No Nexus API key set (create one in Nexus account settings > API).")
        self._key = api_key
        self._own = client is None
        self._c = client or httpx.Client(timeout=30.0, verify=True, follow_redirects=False)
        self._sleep = sleep
        self.quota = Quota()
        self.exhausted = False
        self.requests = 0

    def close(self) -> None:
        if self._own:
            self._c.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _headers(self) -> dict[str, str]:
        return {"apikey": self._key, "Application-Name": "Cogitator", "Application-Version": __version__,
                "Accept": "application/json"}

    def _note_quota(self, r: httpx.Response) -> None:
        d, h = _int(r.headers.get("x-rl-daily-remaining")), _int(r.headers.get("x-rl-hourly-remaining"))
        if d is not None:
            self.quota.daily = d
        if h is not None:
            self.quota.hourly = h
        if d == 0 or h == 0:
            self.exhausted = True

    def get(self, path: str, params: dict | None = None) -> Any:
        if self.exhausted:
            raise RateLimited(f"Nexus API quota exhausted ({self.quota}); try again later.")
        last: Exception | None = None
        for attempt in range(3):
            try:
                self.requests += 1
                r = self._c.get(BASE + path, params=params, headers=self._headers())
            except httpx.HTTPError as e:
                last = NexusError(f"Network error talking to Nexus: {type(e).__name__}")
            else:
                self._note_quota(r)
                if 300 <= r.status_code < 400:
                    raise NexusError(f"Nexus sent a redirect (HTTP {r.status_code}) for {path}; refused so the API key is never sent elsewhere.")
                if r.status_code == 429:
                    self.exhausted = True
                    raise RateLimited("Nexus API rate limit reached (HTTP 429); try again later.")
                if r.status_code == 401:
                    raise InvalidKey("Nexus rejected the API key (HTTP 401).")
                if r.status_code == 404:
                    raise NotFound(f"Nexus returned 404 for {path}.")
                if r.status_code == 403:
                    raise NexusError(f"Nexus refused the request (HTTP 403) for {path}.")
                if r.status_code < 500:
                    if r.status_code != 200:
                        raise NexusError(f"Unexpected HTTP {r.status_code} for {path}.")
                    try:
                        return r.json()
                    except ValueError as e:
                        raise NexusError(f"Nexus returned invalid JSON for {path}.") from e
                last = NexusError(f"Nexus server error HTTP {r.status_code} for {path}.")
            if attempt < 2:
                self._sleep(2 ** attempt)
        raise last  # type: ignore[misc]

    # ---- endpoints
    def validate(self) -> dict:
        d = self.get("/users/validate.json")
        return d if isinstance(d, dict) else {}

    def mod(self, mod_id: int) -> dict:
        d = self.get(f"/games/{GAME}/mods/{mod_id}.json")
        return d if isinstance(d, dict) else {}

    def files(self, mod_id: int) -> list[NexusFile]:
        return parse_files(self.get(f"/games/{GAME}/mods/{mod_id}/files.json"))

    def search_mods(self, name: str, count: int = 10) -> list[dict]:
        """Name search through the v2 GraphQL endpoint (v1 has none). Returns [{"mod_id", "name"}]; [] on any odd reply."""
        if self.exhausted:
            raise RateLimited(f"Nexus API quota exhausted ({self.quota}); try again later.")
        q = "query($f: ModsFilter, $n: Int){ mods(filter:$f, count:$n){ nodes{ modId name } } }"
        v = {"f": {"gameDomainName": [{"value": GAME}], "name": [{"value": name, "op": "WILDCARD"}]}, "n": count}
        try:
            self.requests += 1
            r = self._c.post(GRAPHQL, json={"query": q, "variables": v}, headers=self._headers())
        except httpx.HTTPError as e:
            raise NexusError(f"Network error talking to Nexus: {type(e).__name__}") from None
        self._note_quota(r)
        if r.status_code == 401:
            raise InvalidKey("Nexus rejected the API key (HTTP 401).")
        if r.status_code == 429:
            self.exhausted = True
            raise RateLimited("Nexus API rate limit reached (HTTP 429); try again later.")
        if r.status_code != 200:
            raise NexusError(f"Nexus name search failed (HTTP {r.status_code}).")
        try:
            nodes = r.json()["data"]["mods"]["nodes"]
        except (ValueError, KeyError, TypeError):
            return []
        return [{"mod_id": n["modId"], "name": n["name"]} for n in nodes
                if isinstance(n, dict) and isinstance(n.get("modId"), int) and isinstance(n.get("name"), str)]

    def download_links(self, mod_id: int, file_id: int) -> list[dict]:
        """Premium only. Returns [{"URI": ..., "name": ..., "short_name": ...}, ...]."""
        d = self.get(f"/games/{GAME}/mods/{mod_id}/files/{file_id}/download_link.json")
        return [x for x in d if isinstance(x, dict) and x.get("URI")] if isinstance(d, list) else []

    def updated(self, period: str = "1m") -> list[dict]:
        d = self.get(f"/games/{GAME}/mods/updated.json", {"period": period})
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []
