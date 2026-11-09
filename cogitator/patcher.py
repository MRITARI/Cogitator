"""Adapter around dtkit-patch (--patch and --unpatch only; --toggle is interactive and is never used).

dtkit-patch 0.1.7 behaviour that this module guards against:
  * `--patch DIR` patches and writes DIR/bundle_database.data.bak (the pre-patch bytes); patching twice changes nothing.
  * `--unpatch DIR` renames the .bak over the database without checking either file, so a stale .bak overwrites a
    healthy database.
  * There is no status flag; "patched" means bundle_database.data contains b"patch_999".
The guards therefore live here, not in the tool."""
from __future__ import annotations

import hashlib
import logging
import mmap
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import cache_dir
from .frameworks import detect_framework
from .fsutil import atomic_write_bytes
from .gamefinder import validate_game_dir
from .procs import darktide_running
from .registry import Registry

log = logging.getLogger(__name__)

PATCH_TAG = b"patch_999"
DB_NAME = "bundle_database.data"
BAK_NAME = "bundle_database.data.bak"
TIMEOUT_S = 60
MTIME_SLACK_S = 2.0  # filesystem timestamp granularity

ON, OFF, UNKNOWN = "ON", "OFF", "UNKNOWN"


class PatcherError(Exception):
    pass


class PatcherUnavailable(PatcherError):
    pass


def bundle_dir(game_dir: Path) -> Path:
    return Path(game_dir) / "bundle"


def db_path(game_dir: Path) -> Path:
    return bundle_dir(game_dir) / DB_NAME


def bak_path(game_dir: Path) -> Path:
    return bundle_dir(game_dir) / BAK_NAME


def file_has_tag(path: Path) -> bool | None:
    """True/False if the file contains the patch tag; None if unreadable/empty."""
    try:
        with open(path, "rb") as f:
            try:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
                    return m.find(PATCH_TAG) != -1
            except ValueError:  # empty file
                return None
    except OSError:
        return None


def patch_state(game_dir: Path) -> str:
    r = file_has_tag(db_path(game_dir))
    return UNKNOWN if r is None else (ON if r else OFF)


def banner(game_dir: Path) -> str | None:
    """Startup banner text, or None when nothing to say."""
    if detect_framework(game_dir, "dml").status != "installed":
        return None
    st = patch_state(game_dir)
    if st == ON:
        return None
    return "Game updated or mods disabled - press T to enable mods." if st == OFF else \
        "Mod patch state unknown (bundle_database.data unreadable) - press T to try enabling mods."


def find_tool(game_dir: Path) -> Path:
    tools = Path(game_dir) / "tools"
    names = ["dtkit-patch.exe"] if os.name == "nt" else ["dtkit-patch"]
    for n in names:
        p = tools / n
        if p.is_file() and (os.name == "nt" or os.access(p, os.X_OK)):
            return p
    if os.name != "nt":
        raise PatcherUnavailable(
            "No native dtkit-patch found in <game>/tools (DML ships only dtkit-patch.exe). Options: download a Linux "
            "build from https://github.com/ManShanko/dtkit-patch/releases into <game>/tools/dtkit-patch (chmod +x), "
            "or use the community script talon-d/darktideML-4linux (handle_darktide_mods.sh --enable).")
    raise PatcherUnavailable("tools/dtkit-patch.exe is missing; reinstall the Darktide Mod Loader.")


Runner = Callable[[list, Path], "tuple[int, str, str]"]


def default_runner(args: list, cwd: Path) -> tuple[int, str, str]:
    r = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_S,
                       stdin=subprocess.DEVNULL, shell=False)
    return r.returncode, r.stdout or "", r.stderr or ""


@dataclass
class PatchResult:
    changed: bool
    state: str
    message: str


def _preflight(game_dir: Path, is_running: Callable[[], bool]) -> None:
    ok, why = validate_game_dir(game_dir)
    if not ok:
        raise PatcherError(f"Not a Darktide folder: {why}")
    if detect_framework(game_dir, "dml").status != "installed":
        raise PatcherError("Darktide Mod Loader is not (fully) installed; patching would load nothing.")
    if is_running():
        raise PatcherError("Darktide is running. Close the game before changing the mod patch.")
    if not db_path(game_dir).is_file():
        raise PatcherError(f"{db_path(game_dir)} not found.")


def _restore(game_dir: Path, pre: bytes) -> bool:
    """Put the database back to `pre` if it differs. True if it was rewritten."""
    p = db_path(game_dir)
    try:
        if p.read_bytes() == pre:
            return False
    except OSError:
        pass
    atomic_write_bytes(p, pre)
    return True


def _tail(s: str) -> str:
    return " ".join(s.split())[-300:]


def patch(game_dir: Path, registry: Registry | None = None, *, runner: Runner = default_runner,
          is_running: Callable[[], bool] = darktide_running) -> PatchResult:
    game_dir = Path(game_dir)
    _preflight(game_dir, is_running)
    if patch_state(game_dir) == ON:
        return PatchResult(False, ON, "Mods already enabled (database already patched).")
    tool = find_tool(game_dir)
    pre = db_path(game_dir).read_bytes()
    # one Cogitator backup per game build (keyed by the unpatched database's hash; a key, not a state check)
    key = hashlib.sha256(pre).hexdigest()[:16]
    mine = cache_dir() / "db-backups" / f"{key}.data"
    if not mine.exists():
        atomic_write_bytes(mine, pre)
    t0 = time.time()
    _mark_in_progress(registry, key)  # if the process dies from here on, doctor / restore-database can undo it
    try:
        rc, out, err = runner([tool, "--patch", bundle_dir(game_dir)], game_dir)
    except (OSError, subprocess.SubprocessError) as e:
        _restore(game_dir, pre)
        _mark_in_progress(registry, None)
        raise PatcherError(f"Could not run dtkit-patch: {e}") from e
    problem = None
    if rc != 0:
        problem = f"dtkit-patch exited {rc}: {_tail(err or out)}"
    elif patch_state(game_dir) != ON:
        problem = "dtkit-patch reported success but the database is not patched."
    else:
        try:
            if bak_path(game_dir).read_bytes() != pre:
                problem = "dtkit-patch did not leave a faithful .bak of the original database."
        except OSError:
            problem = "dtkit-patch did not leave a .bak of the original database."
    if problem:
        restored = _restore(game_dir, pre)
        _mark_in_progress(registry, None)
        log.error("patch failed: %s (restored=%s)", problem, restored)
        raise PatcherError(problem + (" The original database was restored." if restored else " The database was not modified."))
    if registry is not None:
        registry.data["patch"] = {"last_patch_at": t0, "build_key": key}  # also clears the in-progress marker
        registry.save()
    log.info("patched bundle_database.data (build %s)", key)
    return PatchResult(True, ON, "Mods enabled (database patched).")


def _mark_in_progress(registry: Registry | None, key: str | None) -> None:
    if registry is None:
        return
    rec = registry.data.setdefault("patch", {})
    if key:
        rec["in_progress"] = {"build_key": key, "at": time.time()}
    else:
        rec.pop("in_progress", None)
    registry.save()


def interrupted_patch(registry: Registry | None) -> dict | None:
    """The marker left by a patch run that never finished (the process was killed), else None."""
    rec = ((registry.data.get("patch") or {}).get("in_progress")) if registry else None
    return rec if isinstance(rec, dict) and rec.get("build_key") else None


def restore_database(game_dir: Path, registry: Registry | None, *, is_running: Callable[[], bool] = darktide_running) -> str:
    """Put back the original database saved before an interrupted patch. Returns a plain-language result."""
    rec = interrupted_patch(registry)
    if rec is None:
        raise PatcherError("No interrupted patch is recorded, so there is nothing to restore.")
    if is_running():
        raise PatcherError("Darktide is running. Close the game first.")
    steam_hint = "In Steam: Properties > Installed Files > Verify integrity of game files."
    mine = cache_dir() / "db-backups" / f"{rec['build_key']}.data"
    try:
        data = mine.read_bytes()
    except OSError as e:
        raise PatcherError("Cogitator's saved copy of the database is missing. " + steam_hint) from e
    if hashlib.sha256(data).hexdigest()[:16] != rec["build_key"] or file_has_tag(mine):
        raise PatcherError("Cogitator's saved copy of the database is damaged. " + steam_hint)
    try:
        already_original = hashlib.sha256(db_path(game_dir).read_bytes()).hexdigest()[:16] == rec["build_key"]
    except OSError:
        already_original = False
    if not already_original:
        atomic_write_bytes(db_path(game_dir), data)
    try:
        bak_path(game_dir).unlink()
    except OSError:
        pass
    _mark_in_progress(registry, None)
    log.warning("restored bundle_database.data from Cogitator's backup after an interrupted patch")
    return "The game database was put back to its original state. Mods are off; switch them on again when you like."


def unpatch_refusal(game_dir: Path, registry: Registry | None) -> str | None:
    """Why unpatching must NOT run (None = safe). Guards the tool's blind .bak rename."""
    bak = bak_path(game_dir)
    if not bak.is_file():
        return ("bundle_database.data.bak is missing, so the tool cannot restore the original. "
                "Use Steam > Properties > Installed Files > Verify integrity to get a clean database.")
    if file_has_tag(bak):
        return "bundle_database.data.bak is itself patched; refusing to restore it."
    last = ((registry.data.get("patch") or {}).get("last_patch_at")) if registry else None
    if isinstance(last, (int, float)) and bak.stat().st_mtime < last - MTIME_SLACK_S:
        return ("bundle_database.data.bak is older than Cogitator's last patch, so it predates the current game "
                "build and restoring it could corrupt the install. Verify game files in Steam instead.")
    return None


def unpatch(game_dir: Path, registry: Registry | None = None, *, runner: Runner = default_runner,
            is_running: Callable[[], bool] = darktide_running) -> PatchResult:
    game_dir = Path(game_dir)
    _preflight(game_dir, is_running)
    if patch_state(game_dir) != ON:
        return PatchResult(False, patch_state(game_dir), "Mods already disabled (database not patched).")
    why = unpatch_refusal(game_dir, registry)
    if why:
        raise PatcherError(why)
    tool = find_tool(game_dir)
    pre = db_path(game_dir).read_bytes()
    expected = bak_path(game_dir).read_bytes()
    try:
        rc, out, err = runner([tool, "--unpatch", bundle_dir(game_dir)], game_dir)
    except (OSError, subprocess.SubprocessError) as e:
        _restore(game_dir, pre)
        raise PatcherError(f"Could not run dtkit-patch: {e}") from e
    problem = None
    if rc != 0:
        problem = f"dtkit-patch exited {rc}: {_tail(err or out)}"
    elif db_path(game_dir).read_bytes() != expected or patch_state(game_dir) != OFF:
        problem = "Unpatch did not produce the expected original database."
    if problem:
        restored = _restore(game_dir, pre)
        if not bak_path(game_dir).exists():
            atomic_write_bytes(bak_path(game_dir), expected)  # the tool consumes the .bak; put it back
        log.error("unpatch failed: %s (restored=%s)", problem, restored)
        raise PatcherError(problem + (" The patched database was restored." if restored else ""))
    if registry is not None:
        registry.data.setdefault("patch", {})["last_unpatch_at"] = time.time()
        registry.save()
    log.info("unpatched bundle_database.data")
    return PatchResult(True, OFF, "Mods disabled (database restored).")


def toggle(game_dir: Path, registry: Registry | None = None, *, confirm_unpatch: Callable[[], bool],
           runner: Runner = default_runner, is_running: Callable[[], bool] = darktide_running) -> PatchResult:
    if patch_state(game_dir) == ON:
        if not confirm_unpatch():
            return PatchResult(False, ON, "Cancelled; mods remain enabled.")
        return unpatch(game_dir, registry, runner=runner, is_running=is_running)
    return patch(game_dir, registry, runner=runner, is_running=is_running)
