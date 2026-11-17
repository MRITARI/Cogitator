"""Known mod conflicts: a small JSON list published as a signed release asset. It is only used when its Ed25519 signature
verifies against the same release key that protects self-updates; offline or unsigned means no list, never a guess.

Format: {"schema": 1, "conflicts": [{"mods": ["a", "b"], "kind": "conflict" | "order", "note": "..."}]}
        kind "order": the first mod must load before the second."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import config_dir
from .fsutil import atomic_write_bytes

log = logging.getLogger(__name__)
BASE_URL = "https://github.com/MRITARI/Cogitator/releases/latest/download/"
ASSET = "known-conflicts.json"
MAX_BYTES = 256 * 1024
MAX_ENTRIES = 2000


def cache_path() -> Path:
    return config_dir() / ASSET


def parse(data: bytes) -> list[dict]:
    d = json.loads(data.decode("utf-8"))
    if not isinstance(d, dict) or d.get("schema") != 1 or not isinstance(d.get("conflicts"), list):
        raise ValueError("unrecognised conflicts file")
    out = []
    for e in d["conflicts"][:MAX_ENTRIES]:
        if not isinstance(e, dict):
            continue
        mods, kind, note = e.get("mods"), e.get("kind"), e.get("note")
        if (isinstance(mods, list) and len(mods) == 2 and all(isinstance(m, str) and 0 < len(m) <= 80 for m in mods)
                and kind in ("conflict", "order") and isinstance(note, str)):
            out.append({"mods": [m.strip() for m in mods], "kind": kind, "note": note[:300]})
    return out


def load_cached(public_key: str | None = None) -> list[dict]:
    """The cached list, or [] when there is none or its signature does not verify."""
    from .selfupdate import RELEASE_PUBLIC_KEY_HEX, UpdateError, verify_signature

    key = RELEASE_PUBLIC_KEY_HEX if public_key is None else public_key
    try:
        data = cache_path().read_bytes()
        sig = cache_path().with_name(ASSET + ".sig").read_bytes()
        verify_signature(data, sig, key)
        return parse(data)
    except FileNotFoundError:
        return []
    except (OSError, UpdateError, ValueError) as e:
        log.warning("known-conflicts cache ignored: %s", type(e).__name__)
        return []


def refresh(client=None, public_key: str | None = None) -> str:
    """Download and verify the list. Returns a one-line result; never raises on network trouble."""
    import httpx

    from .loader import make_client
    from .selfupdate import RELEASE_PUBLIC_KEY_HEX, UpdateError, verify_signature

    key = RELEASE_PUBLIC_KEY_HEX if public_key is None else public_key
    own = client is None
    client = client or make_client()
    try:
        r = client.get(BASE_URL + ASSET)
        if r.status_code == 404:
            return "No conflicts list has been published yet."
        r.raise_for_status()
        s = client.get(BASE_URL + ASSET + ".sig")
        s.raise_for_status()
        if len(r.content) > MAX_BYTES:
            return "The conflicts list is too large; ignored."
        verify_signature(r.content, s.content, key)
        entries = parse(r.content)
    except (httpx.HTTPError, UpdateError, ValueError) as e:
        return f"Could not update the conflicts list ({type(e).__name__}); the previous one is kept."
    finally:
        if own:
            client.close()
    atomic_write_bytes(cache_path(), r.content)
    atomic_write_bytes(cache_path().with_name(ASSET + ".sig"), s.content)
    return f"Conflicts list updated ({len(entries)} entries)."
