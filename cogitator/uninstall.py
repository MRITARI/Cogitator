"""Remove DML/DMF (and optionally chosen user mods) without ever deleting anything while the game is still patched.

  * Never deletes the whole `mods/` folder: only what the installs own (the recorded manifest, or the known marker files).
  * Order: game closed -> un-patch with the guarded patcher if needed -> re-check the byte search -> only then remove
    DML-owned files. If that fails or the state is unknown, nothing is deleted.
  * Removed files go to a rollback directory first; any failure restores them (and re-patches if this run un-patched).
  * Files changed since install are kept unless the user approves."""
from __future__ import annotations

import copy
import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import cache_dir, clear_api_key, config_dir
from .frameworks import detect_framework
from .gamefinder import validate_game_dir
from .installer import Transaction
from .loadorder import LoadOrder
from .mods import INTERNAL_FOLDERS, load_order_path, mods_dir, scan_mods
from .patcher import (ON, OFF, UNKNOWN, PatcherError, bak_path, db_path, default_runner, patch, patch_state, unpatch)
from .procs import darktide_running
from .registry import Registry
from .setup_run import ProgressEvent, SetupCancelled, SetupError, explain

log = logging.getLogger(__name__)

# Fallback for installs without a manifest: DML owns exactly these files; DMF owns the mods/dmf folder.
DML_OWNED_FILES = ("binaries/mod_loader", "bundle/9ba626afa44a3aa3.patch_999", "toggle_darktide_mods.bat",
                   "tools/dtkit-patch.exe", "tools/README.md")
DML_OWNED_DIRS = ("mods/base",)
DMF_OWNED_DIRS = ("mods/dmf",)
PRUNE_IF_EMPTY = ("tools", "binaries")  # never `mods`, never `bundle`


@dataclass
class Item:
    rel: str
    kind: str  # file | dir
    sha256: str | None = None  # expected content hash (manifest installs only)


def owned_items(component: str, registry: Registry | None) -> tuple[list[Item], bool]:
    """(items, from_manifest). The manifest is exact; the fallback is the marker set."""
    m = registry.manifest(component) if registry else None
    if m:
        return [Item(rel, "file", sha) for rel, sha in sorted(m["files"].items())], True
    if component == "dml":
        return [Item(r, "file") for r in DML_OWNED_FILES] + [Item(r, "dir") for r in DML_OWNED_DIRS], False
    return [Item(r, "dir") for r in DMF_OWNED_DIRS], False


@dataclass
class UninstallPlan:
    game_dir: Path
    remove_dml: bool = True
    remove_dmf: bool = True
    user_mods: list[str] = field(default_factory=list)  # folder names under mods/ to delete (GUI double-confirms)
    remove_load_order: bool = False
    # called with the relative paths of files the user changed since install; returns the subset approved for deletion
    confirm_modified: Callable[[list[str]], set[str]] | None = None


@dataclass
class UninstallResult:
    ok: bool = False
    cancelled: bool = False
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # modified / unexpected files left in place
    warnings: list[str] = field(default_factory=list)
    unpatched: bool = False
    from_manifest: dict = field(default_factory=dict)
    rollback_dir: Path | None = None
    what: str = ""
    why: str = ""
    fix: str = ""
    failed_step: str | None = None
    rolled_back: bool = False
    rollback_errors: list[str] = field(default_factory=list)


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def backup_hint(game: Path, registry: Registry | None) -> str:
    """Where a verified copy of the original database is, if Cogitator saved one for this game build."""
    key = ((registry.data.get("patch") or {}).get("build_key") if registry else None)
    cand = cache_dir() / "db-backups" / f"{key}.data" if key else None
    if cand and cand.is_file() and _sha(cand).startswith(key):
        return f"Cogitator saved a verified copy of the original database at {cand} (its hash matches)."
    return "No verified saved copy of the original database was found."


def _fix_for_unpatch(game: Path, registry: Registry | None) -> str:
    return (f"{backup_hint(game, registry)} Restore it, or in Steam use Properties > Installed Files > "
            "\"Verify integrity of game files\", then run uninstall again. Nothing has been deleted.")


def load_order_removable(game: Path, registry: Registry | None, *, removing_all_user_mods: bool) -> bool:
    """Deletion of mod_load_order.txt is only offered when it holds no mod entries or all user mods go too."""
    p = load_order_path(game)
    if not p.is_file():
        return False
    entries = LoadOrder.load(p).entries()
    return not entries or removing_all_user_mods


def user_mod_folders(game: Path) -> list[str]:
    return [m.folder for m in scan_mods(game) if m.present]


def _safe_mod_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and not any(c in name for c in "/\\") and name not in INTERNAL_FOLDERS


def uninstall_framework(plan: UninstallPlan, progress: Callable[[ProgressEvent], None] | None = None,
                        cancel: threading.Event | None = None, *, registry: Registry | None = None,
                        runner: Callable = default_runner, is_running: Callable[[], bool] = darktide_running) -> UninstallResult:
    emit = progress or (lambda e: None)
    cancel = cancel or threading.Event()
    game = Path(plan.game_dir)
    reg = registry or Registry.load()
    res = UninstallResult()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tx = Transaction(cache_dir() / "rollback" / f"{stamp}-uninstall")
    res.rollback_dir = tx.backup_root
    steps = ["check", "mods_off", "remove", "finish"]
    state = {"unpatched": False, "current": "check"}
    reg_snap = copy.deepcopy(reg.data)

    def step(name: str, i: int, msg: str = "") -> None:
        state["current"] = name
        emit(ProgressEvent(name, "start", i, len(steps), msg))
        if cancel.is_set():
            raise SetupCancelled()

    try:
        step("check", 0)
        ok, why = validate_game_dir(game)
        if not ok:
            raise SetupError("That folder does not look like a Darktide install.", why, "Choose the folder that contains bundle and binaries.")
        if is_running():
            raise SetupError("Darktide is running.", "Changing game files while the game runs can corrupt them.", "Close Darktide, then press Retry.")
        for name in plan.user_mods:
            if not _safe_mod_name(name) or not (mods_dir(game) / name).is_dir():
                raise SetupError(f"\"{name}\" is not a mod folder that can be removed.", "Only real mod folders directly inside mods can be deleted.", "Remove it from the list.")
        wanted = [c for c, on in (("dml", plan.remove_dml), ("dmf", plan.remove_dmf)) if on]
        items: dict[str, list[Item]] = {}
        for c in wanted:
            items[c], res.from_manifest[c] = owned_items(c, reg)

        # ---- mods OFF first: nothing DML-owned may be deleted while the database is patched
        step("mods_off", 1)
        if plan.remove_dml:
            st = patch_state(game)
            if st == UNKNOWN:
                raise SetupError("Cogitator cannot tell whether mods are switched on.", "Removing the Mod Loader while the game is still set up to load it could stop the game from starting.",
                                 _fix_for_unpatch(game, reg))
            if st == ON:
                try:
                    unpatch(game, reg, runner=runner, is_running=is_running)
                except PatcherError as e:
                    raise SetupError(f"Mods could not be switched off: {e}", "Removing the Mod Loader while the game is still set up to load it could stop the game from starting.",
                                     _fix_for_unpatch(game, reg)) from e
                state["unpatched"] = True
                res.unpatched = True
                if patch_state(game) != OFF:  # trust but verify: the byte search is the source of truth
                    raise SetupError("The game still reports mods as switched on after switching them off.", "Removing the Mod Loader now could stop the game from starting.",
                                     _fix_for_unpatch(game, reg))
        if cancel.is_set():
            raise SetupCancelled()

        # ---- modified files need an explicit yes
        step("remove", 2)
        approved_mod: set[str] = set()
        modified: list[str] = []
        for c in wanted:
            for it in items[c]:
                p = game / it.rel
                if it.kind == "file" and it.sha256 and p.is_file() and _sha(p) != it.sha256:
                    modified.append(it.rel)
        if modified:
            approved_mod = set(plan.confirm_modified(list(modified))) if plan.confirm_modified else set()
            for rel in modified:
                if rel not in approved_mod:
                    res.kept.append(rel)
                    res.warnings.append(f"{rel} was changed after it was installed, so it was left in place.")

        skip = set(res.kept)
        for c in wanted:
            for it in items[c]:
                p = game / it.rel
                if it.rel in skip:
                    continue
                if it.kind == "file":
                    if tx.remove_file(p):
                        res.removed.append(it.rel)
                elif tx.remove_dir(p):
                    res.removed.append(it.rel + "/")
        # manifest installs removed individual files: prune the (now empty) folders they lived in, deepest first
        for c in wanted:
            if res.from_manifest.get(c):
                dirs = sorted({str(Path(it.rel).parent) for it in items[c]}, key=lambda d: -d.count("/") - d.count("\\"))
                for d in dirs:
                    cur = game / d
                    protected = {game / "mods", game / "bundle", game}  # only the TOP-level ones: DMF has its own scripts/mods/ inside
                    while cur not in protected and cur.is_relative_to(game):
                        if not tx.remove_empty_dir(cur):
                            break
                        cur = cur.parent
        for rel in PRUNE_IF_EMPTY:
            if plan.remove_dml:
                tx.remove_empty_dir(game / rel)
        for c, d in (("dml", "mods/base"), ("dmf", "mods/dmf")):
            if c in wanted and (game / d).is_dir():
                res.warnings.append(f"{d} still contains files that Cogitator did not install, so the folder was left in place.")

        # ---- user mods (only what the user explicitly listed)
        if plan.user_mods:
            lo_path = load_order_path(game)
            tx.snapshot_file(lo_path)
            lo = LoadOrder.load(lo_path)
            for name in plan.user_mods:
                if tx.remove_dir(mods_dir(game) / name):
                    res.removed.append(f"mods/{name}/")
                lo.remove(name)
            if lo_path.is_file():
                lo.save(lo_path, backup=False)
        if plan.remove_load_order:
            lo_path = load_order_path(game)
            remaining_user = [f for f in user_mod_folders(game)]
            if load_order_removable(game, reg, removing_all_user_mods=not remaining_user):
                tx.snapshot_file(lo_path)
                if tx.remove_file(lo_path):
                    res.removed.append("mods/mod_load_order.txt")
            else:
                res.warnings.append("mod_load_order.txt was kept because it still lists your mods.")

        step("finish", 3)
        for c in wanted:
            reg.data["frameworks"].pop(c, None)
            reg.drop_manifest(c)
        if plan.remove_dml:
            reg.data.pop("patch", None)
        for name in plan.user_mods:
            reg.data["mods"].pop(name, None)
        reg.save()
        res.ok = True
        emit(ProgressEvent("finish", "done", 3, len(steps)))
    except SetupCancelled:
        res.cancelled = True
        res.what, res.fix = "Uninstall was cancelled.", "Nothing was removed."
    except BaseException as e:  # noqa: BLE001 - everything must unwind
        res.failed_step = state["current"]
        res.what, res.why, res.fix = explain(e)
        log.error("uninstall step %s failed: %r", state["current"], e)
    if not res.ok:
        try:
            tx.rollback()
        except Exception as e:  # pragma: no cover - rollback is best effort and reported
            res.rollback_errors.append(str(e))
        if state["unpatched"]:  # put mods back exactly as they were
            try:
                patch(game, reg, runner=runner, is_running=is_running)
            except PatcherError as e:
                res.rollback_errors.append(f"Could not switch mods back on: {e}")
        reg.data.clear()
        reg.data.update(reg_snap)
        try:
            reg.save()
        except OSError:
            pass
        res.rolled_back = True
        res.removed.clear()
    return res


# ---------------------------------------------------------------- Cogitator's own data

def remove_app_data(*, key: bool = True, data: bool = False) -> list[str]:
    """Stored key (default) and, if asked, settings/registry/caches/rollback copies. Closes Cogitator's log file first."""
    import logging as _l
    import shutil

    done: list[str] = []
    if key:
        clear_api_key()
        done.append("Nexus key")
    if data:
        for h in list(_l.getLogger().handlers):
            if getattr(h, "_cogitator", False):
                _l.getLogger().removeHandler(h)
                h.close()
        for d in (cache_dir(), config_dir()):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                done.append(str(d))
    return done
