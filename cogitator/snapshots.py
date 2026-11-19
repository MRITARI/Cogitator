"""Snapshots of the mod setup (load order, per-mod content digests, registry links, optionally mod folders), taken
before anything risky and restorable with `snapshot restore`. Stored under <config>/snapshots/<stamp>-<reason>/."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path

from .config import config_dir
from .fsutil import atomic_write_bytes, atomic_write_text, is_link
from .loadorder import LoadOrder
from .mods import INTERNAL_FOLDERS, ModError, load_order_path, mods_dir

log = logging.getLogger(__name__)
SCHEMA = 1
DEFAULT_KEEP = 20
_auto_done: set[tuple[str, str]] = set()  # (game_dir, reason) already snapshotted in this process (reorders are debounced)


def snapshots_dir() -> Path:
    return config_dir() / "snapshots"


def _safe_reason(reason: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in reason)[:30] or "manual"


def folder_digest(path: Path) -> tuple[int, str]:
    """(file count, sha256 over sorted relative paths + file hashes). Links are not followed."""
    h = hashlib.sha256()
    n = 0
    if not path.is_dir() or is_link(path):
        return 0, ""
    for p in sorted(path.rglob("*"), key=lambda q: q.relative_to(path).as_posix()):
        if is_link(p) or not p.is_file():
            continue
        h.update(p.relative_to(path).as_posix().encode("utf-8", "surrogateescape") + b"\0")
        try:
            h.update(hashlib.sha256(p.read_bytes()).digest())
        except OSError:
            h.update(b"unreadable")
        n += 1
    return n, h.hexdigest()


def current_state(game_dir: Path, registry=None) -> dict:
    """The same shape a snapshot manifest has, for the live setup."""
    md = mods_dir(game_dir)
    entries = [[n, e] for n, e in LoadOrder.load(load_order_path(game_dir)).entries() if n not in INTERNAL_FOLDERS]
    folders = sorted({p.name for p in md.iterdir() if p.is_dir()} - set(INTERNAL_FOLDERS), key=str.casefold) if md.is_dir() else []
    mods = {}
    for f in folders:
        n, d = folder_digest(md / f)
        mods[f] = {"files": n, "digest": d}
    reg = {}
    build_key = None
    if registry is not None:
        reg = {f: dict(registry.mod(f)) for f in folders if registry.mod(f)}
        build_key = (registry.data.get("patch") or {}).get("build_key")
    return {"entries": entries, "mods": mods, "registry": reg, "build_key": build_key}


def _keep_setting() -> int:
    try:
        from .config import Settings

        return max(1, Settings.load().snapshot_keep)
    except Exception:
        return DEFAULT_KEEP


def create(game_dir: Path, reason: str, *, folders=(), registry=None, keep: int | None = None) -> Path:
    """Write a snapshot. `folders` get their full contents copied (use it for the mods about to be overwritten or removed)."""
    game_dir = Path(game_dir)
    state = current_state(game_dir, registry)
    root = snapshots_dir()
    root.mkdir(parents=True, exist_ok=True)
    base = f"{time.strftime('%Y%m%d-%H%M%S')}-{_safe_reason(reason)}"
    dest, n = root / base, 1
    while dest.exists():
        n += 1
        dest = root / f"{base}-{n}"
    dest.mkdir()
    lo = load_order_path(game_dir)
    if lo.is_file():
        shutil.copy2(lo, dest / lo.name)
    copied = []
    for f in folders:
        src = mods_dir(game_dir) / f
        if src.is_dir() and not is_link(src) and f not in INTERNAL_FOLDERS:
            shutil.copytree(src, dest / "mods" / f)
            copied.append(f)
    manifest = {"schema": SCHEMA, "created": time.time(), "reason": reason, "game_dir": str(game_dir), "copied": copied,
                "has_load_order": lo.is_file(), **state}
    atomic_write_text(dest / "manifest.json", json.dumps(manifest, indent=1))
    prune(keep if keep is not None else _keep_setting())
    return dest


def auto(game_dir: Path, reason: str, *, folders=(), registry=None, once: bool = False) -> Path | None:
    """Safety-net snapshot that never blocks the action it protects (a failure is logged). `once` debounces per process."""
    key = (str(game_dir), reason)
    if once and key in _auto_done:
        return None
    try:
        p = create(game_dir, reason, folders=folders, registry=registry)
        _auto_done.add(key)
        return p
    except Exception as e:
        log.warning("snapshot (%s) failed: %s", reason, type(e).__name__)
        return None


def list_snapshots() -> list[dict]:
    out = []
    root = snapshots_dir()
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        try:
            m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"name": d.name, "reason": m.get("reason"), "created": m.get("created"), "mods": len(m.get("mods", {})),
                    "copied": m.get("copied", [])})
    return out


def _dir(name: str) -> Path:
    if not name or any(c in name for c in "/\\") or name in (".", ".."):
        raise ModError(f"Invalid snapshot name: {name!r}")
    d = snapshots_dir() / name
    if not (d / "manifest.json").is_file():
        raise ModError(f"No snapshot named '{name}'. Run `snapshot list`.")
    return d


def load_manifest(name: str) -> dict:
    return json.loads((_dir(name) / "manifest.json").read_text(encoding="utf-8"))


def _swap_in(src: Path, dest: Path) -> None:
    """Replace `dest` with a copy of `src`: copy to a sibling temp dir first, so a failure leaves `dest` as it was."""
    tmp = dest.with_name(f".{dest.name}.cog-restore-new")
    old = dest.with_name(f".{dest.name}.cog-restore-old")
    for p in (tmp, old):
        shutil.rmtree(p, ignore_errors=True)
    try:
        shutil.copytree(src, tmp)
        if dest.exists() and not is_link(dest):
            dest.rename(old)
            try:
                tmp.rename(dest)
            except OSError:
                old.rename(dest)
                raise
        else:
            tmp.rename(dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)


def restore(game_dir: Path, name: str, registry=None) -> list[str]:
    """Put the load order (and any copied mod folders) back. The current state is snapshotted first, so a restore can be undone."""
    d = _dir(name)
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    game_dir = Path(game_dir)
    # No pruning here: it could delete the very snapshot being restored (the oldest one). The next create() prunes.
    create(game_dir, "pre-restore", folders=list(m.get("copied", [])), registry=registry, keep=1 << 30)
    msgs = []
    lo = load_order_path(game_dir)
    if m.get("has_load_order") and (d / lo.name).is_file():
        atomic_write_bytes(lo, (d / lo.name).read_bytes())
        msgs.append("load order restored")
    for f in m.get("copied", []):
        _swap_in(d / "mods" / f, mods_dir(game_dir) / f)
        if registry is not None and f in m.get("registry", {}):
            registry.set_mod(f, **m["registry"][f])
        msgs.append(f"mod folder restored: {f}")
    return msgs


def diff_states(a: dict, b: dict) -> dict:
    """What changed going from state `a` to state `b` (snapshot manifests, profile states or current_state)."""
    ea, eb = {n: e for n, e in a["entries"]}, {n: e for n, e in b["entries"]}
    ma, mb = a.get("mods", {}), b.get("mods", {})
    common = [n for n, _ in a["entries"] if n in eb]
    ob = [n for n, _ in b["entries"] if n in ea]
    return {
        "added": sorted(set(mb) - set(ma)), "removed": sorted(set(ma) - set(mb)),
        "enabled": sorted(n for n in common if not ea[n] and eb[n]), "disabled": sorted(n for n in common if ea[n] and not eb[n]),
        "reordered": common != ob,
        "changed": sorted(f for f in set(ma) & set(mb) if ma[f].get("digest") != mb[f].get("digest")),
        "listed_added": sorted(set(eb) - set(ea)), "listed_removed": sorted(set(ea) - set(eb)),
    }


def diff_empty(d: dict) -> bool:
    return not any(d.values())


def format_diff(d: dict) -> list[str]:
    if diff_empty(d):
        return ["No differences."]
    labels = {"added": "folders added", "removed": "folders removed", "enabled": "now enabled", "disabled": "now disabled",
              "changed": "files changed", "listed_added": "added to load order", "listed_removed": "removed from load order"}
    out = [f"{labels[k]}: {', '.join(d[k])}" for k in labels if d.get(k)]
    if d.get("reordered"):
        out.append("load order sequence changed")
    return out


def prune(keep: int) -> int:
    snaps = list_snapshots()
    gone = 0
    for s in snaps[keep:]:
        shutil.rmtree(snapshots_dir() / s["name"], ignore_errors=True)
        gone += 1
    return gone


def prune_rollbacks(keep: int = 10) -> int:
    """Keep only the newest `keep` update-rollback folders in <cache>/rollback."""
    from .config import cache_dir

    root = cache_dir() / "rollback"
    dirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True) if root.is_dir() else []
    for p in dirs[keep:]:
        shutil.rmtree(p, ignore_errors=True)
    return max(0, len(dirs) - keep)
