"""Named mod profiles: which mods are enabled and in what order, plus the Nexus link of each, so a setup can be switched
or shared. A profile never contains mod files, and importing one never runs anything: it is data only."""
from __future__ import annotations

import base64
import json
import re
import time
import zlib
from pathlib import Path

from .config import config_dir
from .fsutil import atomic_write_text
from .loadorder import LoadOrder
from .mods import INTERNAL_FOLDERS, ModError, bad_folder_name, load_order_path, scan_mods

SCHEMA = 1
CODE_PREFIX = "cog1:"
MAX_CODE_BYTES = 64 * 1024
MAX_MODS = 500
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")


def profiles_dir() -> Path:
    return config_dir() / "profiles"


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME.match(name):
        raise ModError("Profile names are 1-40 characters: letters, digits, space, '_', '.', '-' (starting with a letter or digit).")
    return name


def _path(name: str) -> Path:
    return profiles_dir() / (check_name(name).casefold() + ".json")


def capture(game_dir: Path, registry, name: str) -> dict:
    """The current setup as a profile."""
    entries = [[n, bool(e)] for n, e in LoadOrder.load(load_order_path(game_dir)).entries() if n not in INTERNAL_FOLDERS]
    mods = {}
    for f, _ in entries:
        r = (registry.mod(f) if registry is not None else None) or {}
        mods[f] = {"nexus_id": r.get("nexus_id"), "name": r.get("name"), "file_id": r.get("installed_file_id"),
                   "version": r.get("installed_version"), "pinned": r.get("pinned_version")}
    return {"schema": SCHEMA, "name": check_name(name), "created": time.time(), "entries": entries, "mods": mods}


def save(profile: dict) -> Path:
    p = _path(profile["name"])
    atomic_write_text(p, json.dumps(profile, indent=1))
    return p


def load(name: str) -> dict:
    p = _path(name)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ModError(f"No profile named '{name}'. Run 'profile list'.") from None
    except (OSError, ValueError) as e:
        raise ModError(f"Profile '{name}' is unreadable: {e}") from None
    return validate(data)


def list_profiles() -> list[dict]:
    out = []
    for p in sorted(profiles_dir().glob("*.json")) if profiles_dir().is_dir() else []:
        try:
            d = validate(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError, ModError):
            continue
        out.append({"name": d["name"], "mods": len(d["entries"]), "enabled": sum(1 for _, e in d["entries"] if e),
                    "created": d.get("created")})
    return out


def delete(name: str) -> None:
    p = _path(name)
    if not p.is_file():
        raise ModError(f"No profile named '{name}'.")
    p.unlink()


def validate(d) -> dict:
    """Strict check of untrusted profile data (a shared code or file). Raises ModError; returns a cleaned copy."""
    if not isinstance(d, dict) or d.get("schema") != SCHEMA:
        raise ModError("Not a Cogitator profile, or from a newer version.")
    name = check_name(str(d.get("name", "")))
    ents = d.get("entries")
    if not isinstance(ents, list) or len(ents) > MAX_MODS:
        raise ModError("Profile has no valid mod list.")
    clean, seen = [], set()
    for e in ents:
        if not (isinstance(e, (list, tuple)) and len(e) == 2 and isinstance(e[0], str) and isinstance(e[1], bool)):
            raise ModError("Profile has a malformed mod entry.")
        why = bad_folder_name(e[0])
        if why or e[0] in INTERNAL_FOLDERS:
            raise ModError(why or f"'{e[0]}' cannot be part of a profile.")
        if e[0].casefold() in seen:
            continue
        seen.add(e[0].casefold())
        clean.append([e[0], e[1]])
    mods = {}
    raw = d.get("mods") if isinstance(d.get("mods"), dict) else {}
    for f, _ in clean:
        r = raw.get(f) if isinstance(raw.get(f), dict) else {}
        mods[f] = {"nexus_id": r["nexus_id"] if isinstance(r.get("nexus_id"), int) and not isinstance(r.get("nexus_id"), bool) else None,
                   "name": str(r["name"])[:100] if isinstance(r.get("name"), str) else None,
                   "file_id": r["file_id"] if isinstance(r.get("file_id"), int) and not isinstance(r.get("file_id"), bool) else None,
                   "version": str(r["version"])[:40] if isinstance(r.get("version"), (str, int, float)) and r.get("version") is not None else None,
                   "pinned": str(r["pinned"])[:40] if isinstance(r.get("pinned"), str) else None}
    created = d.get("created") if isinstance(d.get("created"), (int, float)) else None
    return {"schema": SCHEMA, "name": name, "created": created, "entries": clean, "mods": mods}


# ---- sharing

def encode(profile: dict) -> str:
    raw = json.dumps(validate(profile), separators=(",", ":")).encode("utf-8")
    return CODE_PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")


def decode(code: str) -> dict:
    code = "".join((code or "").split())
    if not code.startswith(CODE_PREFIX) or len(code) > MAX_CODE_BYTES:
        raise ModError("That is not a Cogitator setup code (it should start with 'cog1:').")
    body = code[len(CODE_PREFIX):]
    try:
        comp = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        d = zlib.decompressobj()
        raw = d.decompress(comp, MAX_CODE_BYTES * 8)  # bounded: a tiny code must not inflate into gigabytes
        if d.unconsumed_tail:
            raise ValueError("too large")
        return validate(json.loads(raw.decode("utf-8")))
    except ModError:
        raise
    except (ValueError, zlib.error, UnicodeDecodeError) as e:
        raise ModError(f"The setup code is damaged or not valid ({type(e).__name__}).") from None


# ---- state for diffs

def as_state(profile: dict) -> dict:
    """A profile in the shape snapshots.diff_states compares (the version stands in for a content digest)."""
    return {"entries": profile["entries"],
            "mods": {f: {"files": 0, "digest": f"{m.get('version')}/{m.get('file_id')}"} for f, m in profile["mods"].items()}}


# ---- switching

def plan_switch(game_dir: Path, profile: dict) -> dict:
    present = {m.folder for m in scan_mods(game_dir) if m.present}
    pres = {f.casefold(): f for f in present}
    use = [(pres[n.casefold()], e) for n, e in profile["entries"] if n.casefold() in pres]
    names = {n.casefold() for n, _ in use}
    missing = [n for n, _ in profile["entries"] if n.casefold() not in pres]
    others = [m.folder for m in scan_mods(game_dir) if m.present and m.folder.casefold() not in names
              and m.state == "enabled"]
    return {"use": use, "missing": missing, "will_disable": others}


def switch(game_dir: Path, profile: dict, registry=None, *, dry_run: bool = False) -> dict:
    """Apply a profile: enable/order the listed mods that are installed, disable every other mod, and report the missing ones.
    Never deletes anything. A snapshot is taken first."""
    from . import snapshots

    plan = plan_switch(game_dir, profile)
    if dry_run:
        return plan
    snapshots.auto(game_dir, "profile-switch", registry=registry)
    path = load_order_path(game_dir)
    lo = LoadOrder.load(path)
    for n, en in plan["use"]:
        if lo.state(n) is None:
            lo.add(n, en)
        else:
            lo.set_enabled(n, en)
    for n in plan["will_disable"]:
        lo.set_enabled(n, False)
    lo.reorder([n for n, _ in plan["use"]], skip=INTERNAL_FOLDERS)
    lo.save(path)
    return plan
