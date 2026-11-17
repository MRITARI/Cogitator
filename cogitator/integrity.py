"""Content baselines for installed mods, so edits made outside Cogitator are noticed. A baseline is taken when Cogitator
installs or updates a mod (or when the user says 'trust the current state'). It detects change, not intent."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .config import config_dir
from .fsutil import atomic_write_text
from .mods import INTERNAL_FOLDERS, bad_folder_name, mods_dir
from .snapshots import folder_digest

log = logging.getLogger(__name__)


def path() -> Path:
    return config_dir() / "integrity.json"


def load() -> dict:
    try:
        d = json.loads(path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _build_key(registry=None) -> str | None:
    try:
        if registry is None:
            from .registry import Registry

            registry = Registry.load()
        return (registry.data.get("patch") or {}).get("build_key")
    except Exception:
        return None


def record(game_dir: Path, folders, registry=None) -> None:
    """Baseline `folders` as they are now. Never raises: a failure only means no baseline."""
    try:
        data = load()
        bk = _build_key(registry)
        for f in folders:
            if f in INTERNAL_FOLDERS or bad_folder_name(f):
                continue
            n, d = folder_digest(mods_dir(game_dir) / f)
            if d:
                data[f] = {"digest": d, "files": n, "at": time.time(), "build_key": bk}
        atomic_write_text(path(), json.dumps(data, indent=1))
    except Exception as e:
        log.warning("could not record integrity baseline: %s", type(e).__name__)


def forget(folder: str) -> None:
    data = load()
    if data.pop(folder, None) is not None:
        atomic_write_text(path(), json.dumps(data, indent=1))


def check(game_dir: Path) -> dict[str, str]:
    """{folder: 'modified' | 'missing'} for every baselined mod that no longer matches."""
    out = {}
    for f, rec in load().items():
        if not isinstance(rec, dict):
            continue
        d = mods_dir(game_dir) / f
        if not d.is_dir():
            continue  # removed on purpose or by Cogitator: the load-order check reports a listed mod that is gone
        _, now = folder_digest(d)
        if now != rec.get("digest"):
            out[f] = "modified"
    return out


def stale_build(game_dir: Path, current_build: str | None) -> list[str]:
    """Baselined mods that were last installed under a different game build than `current_build`."""
    if not current_build:
        return []
    return sorted(f for f, rec in load().items()
                  if isinstance(rec, dict) and rec.get("build_key") and rec["build_key"] != current_build
                  and (mods_dir(game_dir) / f).is_dir())
