"""Link installed mods to Nexus mod ids and record which file is installed (the baseline).

Link sources, in priority order: Cogitator's own import, a metadata file in the mod folder, a manual `link`. Linking by hand
marks the latest MAIN file as installed unless told otherwise; an unknown baseline is never reported as up to date."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from .nexus import GAME, NexusFile, latest_main_file, release_group
from .registry import Registry

log = logging.getLogger(__name__)

_URL = re.compile(rf"nexusmods\.com/{GAME}/mods/(\d+)", re.I)
_ID_KEYS = re.compile(r"^(nexus[_\-]?(mod[_\-]?)?id|nexus[_\-]?id|mod[_\-]?id)$", re.I)
_URL_KEYS = re.compile(r"(nexus|url|link|homepage|page)", re.I)
_MAX_JSON = 256 * 1024


def read_metadata_link(mod_dir: Path) -> int | None:
    """Scan *.json in the mod folder root for a Nexus mod id or URL. Any parse problem means no link. Only keys like
        nexus_id, or a URL for this game, count; a generic `mod_id` is trusted only if the file also mentions Nexus."""
    try:
        files = sorted(p for p in Path(mod_dir).glob("*.json") if p.is_file() and p.stat().st_size <= _MAX_JSON)
    except OSError:
        return None
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        found = _scan(data, 0, "nexus" in p.read_text(encoding="utf-8-sig", errors="ignore").lower())
        if found:
            return found
    return None


def _scan(node, depth: int, nexus_context: bool) -> int | None:
    if depth > 4:
        return None
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (int, str)) and not isinstance(v, bool) and _ID_KEYS.match(str(k)):
                is_generic = str(k).lower().replace("_", "").replace("-", "") in ("modid",)
                if (not is_generic or nexus_context) and str(v).strip().isdigit():
                    return int(str(v).strip())
            if isinstance(v, str) and _URL_KEYS.search(str(k)):
                m = _URL.search(v)
                if m:
                    return int(m.group(1))
        for v in node.values():
            r = _scan(v, depth + 1, nexus_context)
            if r:
                return r
    elif isinstance(node, list):
        for v in node[:50]:
            r = _scan(v, depth + 1, nexus_context)
            if r:
                return r
    elif isinstance(node, str):
        m = _URL.search(node)
        if m:
            return int(m.group(1))
    return None


def _latest_record(files: list[NexusFile]) -> dict | None:
    latest = latest_main_file(files)
    if latest is None:
        return None
    return {"file_id": latest.file_id, "version": latest.version, "uploaded": latest.uploaded,
            "file_name": latest.file_name, "size_bytes": latest.size_bytes,
            "group": sorted(release_group(files, latest))}


def link_mod(registry: Registry, folder: str, nexus_id: int, name: str | None, files: list[NexusFile] | None, *,
             via: str, baseline: str | int = "current", now: float | None = None, save: bool = True) -> dict:
    """Create/replace the link for `folder`.

    baseline: "current" (mark latest MAIN as installed), "none" (unknown), or an int file id the user chose.
    """
    now = time.time() if now is None else now
    rec: dict = {"nexus_id": nexus_id, "name": name, "linked_via": via, "linked_at": now,
                 "installed_file_id": None, "installed_version": None, "installed_archive": None,
                 "baseline": None, "last_error": None, "latest": None, "checked_at": None}
    by_id = {f.file_id: f for f in (files or []) if f.file_id is not None}
    if isinstance(baseline, int) and not isinstance(baseline, bool):
        f = by_id.get(baseline)
        if f is None:
            raise ValueError(f"file {baseline} is not a file of Nexus mod {nexus_id}")
        rec.update(installed_file_id=f.file_id, installed_version=f.version, baseline="chosen")
    elif baseline == "current":
        lr = _latest_record(files or [])
        if lr and lr["file_id"] is not None:
            rec.update(installed_file_id=lr["file_id"], installed_version=lr["version"], baseline="marked-current")
    if files is not None:
        rec["latest"] = _latest_record(files)
        rec["checked_at"] = now
    return registry.set_mod(folder, save=save, **rec)
