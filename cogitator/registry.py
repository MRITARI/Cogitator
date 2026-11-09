"""registry.json: the state Cogitator keeps outside the game folder (framework records and install manifests, mod links,
the update-check cache, the patch record). Older files are upgraded by `migrate`."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .config import config_dir
from .fsutil import atomic_write_text

log = logging.getLogger(__name__)
SCHEMA = 3  # v1: frameworks/mods/patch.  v2: "updates" (check cache) + mod links.  v3: "manifests" (files each framework install placed).


def empty() -> dict:
    return {"schema": SCHEMA, "frameworks": {}, "mods": {}, "updates": {}, "manifests": {}}


def migrate(data: object) -> dict:
    """Bring any loaded JSON up to the current schema. Unknown shapes -> fresh registry."""
    if not isinstance(data, dict):
        return empty()
    out = empty()
    out.update({k: v for k, v in data.items() if k not in ("schema",)})
    for key in ("frameworks", "mods", "updates", "manifests"):
        if not isinstance(out.get(key), dict):
            out[key] = {}
    # drop mod records that are not dicts (hand-edited / corrupt) rather than crash later
    out["mods"] = {k: v for k, v in out["mods"].items() if isinstance(v, dict)}
    out["schema"] = SCHEMA
    return out


class Registry:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config_dir() / "registry.json"
        self.data = empty()

    @classmethod
    def load(cls, path: Path | None = None) -> "Registry":
        r = cls(path)
        try:
            raw = json.loads(r.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return r
        except (ValueError, OSError) as e:
            bad = r.path.with_name(r.path.name + ".corrupt")
            log.warning("registry unreadable (%s); moving aside to %s", e, bad)
            try:
                r.path.replace(bad)
            except OSError:
                pass
            return r
        r.data = migrate(raw)
        return r

    def save(self) -> None:
        atomic_write_text(self.path, json.dumps(self.data, indent=2, sort_keys=True) + "\n")

    # ---- frameworks
    def framework(self, kind: str) -> dict | None:
        return self.data["frameworks"].get(kind)

    def set_framework(self, kind: str, *, version: str | None, source_kind: str, source: str,
                      unreleased: bool = False, sha256: str | None = None, archive_name: str | None = None) -> None:
        self.data["frameworks"][kind] = {
            "version": version,  # None == unknown (never guessed)
            "source_kind": source_kind,
            "source": source,
            "unreleased": unreleased,
            "sha256": sha256,
            "archive_name": archive_name,  # lets the Nexus check resolve the exact file id later
            "file_id": None,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.save()

    # ---- install manifests (what a framework install placed; drives uninstall)
    def manifest(self, kind: str) -> dict | None:
        m = self.data["manifests"].get(kind)
        return m if isinstance(m, dict) and isinstance(m.get("files"), dict) else None

    def set_manifest(self, kind: str, files: dict[str, str], *, created_load_order: bool = False, version: str | None = None) -> None:
        """files: game-relative posix path -> SHA-256 of the content we wrote."""
        self.data["manifests"][kind] = {"files": dict(files), "created_load_order": created_load_order, "version": version,
                                        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.save()

    def drop_manifest(self, kind: str) -> None:
        self.data["manifests"].pop(kind, None)

    # ---- mods
    def mod(self, folder: str) -> dict | None:
        return self.data["mods"].get(folder)

    def set_mod(self, folder: str, save: bool = True, **fields) -> dict:
        """Merge `fields` into the record for `folder` (creating it)."""
        rec = self.data["mods"].setdefault(folder, {})
        rec.update(fields)
        if save:
            self.save()
        return rec

    def forget_mod(self, folder: str) -> None:
        if self.data["mods"].pop(folder, None) is not None:
            self.save()
