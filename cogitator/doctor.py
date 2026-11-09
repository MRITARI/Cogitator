"""`cogitator doctor`: read-only diagnosis. Never modifies the game, the registry or the config.

Exit codes: 0 all ok, 1 warnings, 2 failures. Every check returns a level (ok|warn|fail), a detail and a fix hint.
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

import httpx

from . import gamefinder
from .config import Settings, cache_dir, config_dir, get_api_key, log_path, redact
from .frameworks import DML_OPTIONAL, detect_framework
from .gamefinder import APPID, validate_game_dir
from .loadorder import LoadOrder
from .loader import GITHUB_API, make_client
from .mods import INTERNAL_FOLDERS, load_order_path, mods_dir
from .nexus import InvalidKey, NexusClient, NexusError, RateLimited
from .patcher import (BAK_NAME, MTIME_SLACK_S, OFF, ON, UNKNOWN, bak_path, bundle_dir, db_path, file_has_tag, find_tool,
                      patch_state, PatcherUnavailable)
from .procs import darktide_running
from .registry import Registry, SCHEMA

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "[OK]  ", WARN: "[WARN]", FAIL: "[FAIL]"}

# Heuristics for the console-log scan: a clean session has
# "[Lua] [Mod] Loading ./../mods/<name>/..." lines and no "scripts/mods ... error" lines).
_ERR = re.compile(r"(?i)\b(error|failed|exception|traceback|attempt to|stack)\b")
_DMF_LOADED = "Loading ./../mods/dmf/dmf.mod"
_MAX_LOG_BYTES = 40 * 1024 * 1024


@dataclass
class Check:
    id: str
    name: str
    level: str
    detail: str
    fix: str = ""


def exit_code(checks: list[Check]) -> int:
    if any(c.level == FAIL for c in checks):
        return 2
    return 1 if any(c.level == WARN for c in checks) else 0


def format_report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{_MARK[c.level]} {c.name}: {c.detail}")
        if c.fix and c.level != OK:
            lines.append(f"         fix: {c.fix}")
    n = {k: sum(1 for c in checks if c.level == k) for k in (OK, WARN, FAIL)}
    lines.append(f"\n{n[OK]} ok, {n[WARN]} warning(s), {n[FAIL]} failure(s).")
    return redact("\n".join(lines))


def to_json(checks: list[Check]) -> list[dict]:
    return [{**asdict(c), "detail": redact(c.detail)} for c in checks]


# ------------------------------------------------------------------ individual checks

def _steam(roots: list[Path]) -> Check:
    if roots:
        return Check("steam", "Steam", OK, f"found at {roots[0]}" + (f" (+{len(roots) - 1} more)" if len(roots) > 1 else ""))
    return Check("steam", "Steam", WARN, "no Steam install found (registry / default folders)",
                 "Fine for a non-Steam copy; otherwise install Steam or pass --game-dir.")


def _fw(game: Path, kind: str, reg: Registry) -> Check:
    st = detect_framework(game, kind)
    name = {"dml": "Darktide Mod Loader (DML)", "dmf": "Darktide Mod Framework (DMF)"}[kind]
    if st.status == "installed":
        ver = (reg.framework(kind) or {}).get("version")
        return Check(f"fw_{kind}", name, OK, f"installed, version {ver or 'unknown (installed outside Cogitator)'}")
    if st.status == "incomplete":
        return Check(f"fw_{kind}", name, FAIL, f"incomplete - missing {', '.join(st.missing)}",
                     "Reinstall it: cogitator install-framework --zip <Nexus zip>.")
    return Check(f"fw_{kind}", name, FAIL, "not installed", "cogitator install-framework --zip <Nexus zip> (or press I in the TUI).")


def _patch(game: Path, reg: Registry) -> list[Check]:
    st = patch_state(game)
    if detect_framework(game, "dml").status != "installed":
        if st != ON:
            return [Check("patch_state", "Mods patch", WARN, "cannot tell - the Mod Loader is not (fully) installed", "Install DML first.")]
        # a patched database with a broken/absent loader is the dangerous combination: still run the consistency check
        return [Check("patch_state", "Mods patch", WARN, "ON, but the Mod Loader is not (fully) installed", "Reinstall DML."),
                _patch_consistency(game, reg, st)]
    out: list[Check] = []
    if st == ON:
        out.append(Check("patch_state", "Mods patch", OK, "ON (bundle_database.data contains the patch_999 entry)"))
    elif st == OFF:
        out.append(Check("patch_state", "Mods patch", WARN, "OFF - a game update or file verification removed the patch, or it was never applied",
                         "Press T in the TUI or run: cogitator toggle"))
    else:
        out.append(Check("patch_state", "Mods patch", WARN, "UNKNOWN - bundle_database.data is missing or unreadable",
                         "Verify game files in Steam (Properties > Installed Files)."))
    try:
        find_tool(game)
        out.append(Check("patch_tool", "dtkit-patch", OK, "found in tools/"))
    except PatcherUnavailable as e:
        out.append(Check("patch_tool", "dtkit-patch", FAIL, "not usable on this system", str(e)))
    out.append(_patch_consistency(game, reg, st))
    out.append(_bak(game, reg, st))
    return out


def _patch_consistency(game: Path, reg: Registry, st: str) -> Check:
    """patch_999 consistency: the database entry, the bundle file it points at, and what Cogitator last did must agree."""
    bundle_file = bundle_dir(game) / "9ba626afa44a3aa3.patch_999"
    rec = reg.data.get("patch") or {}
    patched_at, unpatched_at = rec.get("last_patch_at"), rec.get("last_unpatch_at") or 0
    if st == ON and not bundle_file.is_file():
        return Check("patch_consistency", "patch_999 consistency", FAIL,
                     "the database is patched but bundle/9ba626afa44a3aa3.patch_999 is missing - the game may fail to start",
                     "Reinstall the Mod Loader (cogitator install-framework --zip ...) or un-patch (T) and verify game files.")
    if st == OFF and bundle_file.is_file() and isinstance(patched_at, (int, float)) and patched_at > unpatched_at:
        return Check("patch_consistency", "patch_999 consistency", WARN,
                     "Cogitator patched this game earlier, but the database is unpatched now (the game was updated or verified)",
                     "Press T to patch again; the DML/DMF versions may also need updating.")
    if st == ON and not isinstance(patched_at, (int, float)):
        return Check("patch_consistency", "patch_999 consistency", OK,
                     "database patched and patch_999 present (patched outside Cogitator: no timestamp recorded)")
    return Check("patch_consistency", "patch_999 consistency", OK, "database, patch_999 bundle and Cogitator's record agree")


def _bak(game: Path, reg: Registry, st: str) -> Check:
    """.bak freshness. dtkit-patch 0.1.7's --unpatch blindly restores this file, so a stale one is dangerous."""
    bak, db = bak_path(game), db_path(game)
    last = (reg.data.get("patch") or {}).get("last_patch_at")
    if st == ON:
        if not bak.is_file():
            return Check("bak", f"{BAK_NAME} freshness", WARN, "missing - the tool cannot restore the original; Cogitator will refuse to un-patch",
                         "Verify game files in Steam to restore a clean database, then press T to patch again.")
        if file_has_tag(bak):
            return Check("bak", f"{BAK_NAME} freshness", FAIL, "the backup is itself patched - un-patching would not remove the patch",
                         "Verify game files in Steam, then patch again.")
        if isinstance(last, (int, float)) and bak.stat().st_mtime < last - MTIME_SLACK_S:
            return Check("bak", f"{BAK_NAME} freshness", WARN,
                         "older than Cogitator's last patch - it predates this game build, so un-patching is refused",
                         "Verify game files in Steam; the next patch writes a fresh backup.")
        return Check("bak", f"{BAK_NAME} freshness", OK, "present, unpatched, and not older than the last patch")
    if bak.is_file():
        try:
            stale = bak.stat().st_mtime < db.stat().st_mtime
        except OSError:
            stale = False
        if stale:
            return Check("bak", f"{BAK_NAME} freshness", WARN,
                         "a leftover backup older than the current database exists (harmless; the next patch overwrites it)",
                         "Do not run dtkit-patch --unpatch by hand: v0.1.7 would overwrite the database with this stale file.")
    return Check("bak", f"{BAK_NAME} freshness", OK, "no stale backup")


def _load_order(game: Path) -> list[Check]:
    p = load_order_path(game)
    if not p.is_file():
        return [Check("load_order", "mod_load_order.txt", WARN, "file not found", "Reinstall the Mod Loader; it ships a default file.")]
    lo = LoadOrder.load(p)
    entries = lo.entries()
    folders = {d.name for d in mods_dir(game).iterdir() if d.is_dir()} if mods_dir(game).is_dir() else set()
    out: list[Check] = []
    wrong = sorted({n for n, _ in entries if n in INTERNAL_FOLDERS})
    missing = [n for n, _ in entries if n not in folders and n not in INTERNAL_FOLDERS]
    seen, dups = set(), set()
    for n, _ in entries:
        (dups if n in seen else seen).add(n)
    if wrong:
        out.append(Check("load_order_internal", "Load order: loader entries", WARN,
                         f"{', '.join(wrong)} listed, but the Mod Loader/Framework are loaded automatically and must not be listed",
                         "Remove those lines from mods/mod_load_order.txt."))
    if missing:
        out.append(Check("load_order_missing", "Load order: missing folders", WARN,
                         f"listed but not in mods/: {', '.join(sorted(set(missing)))}",
                         "Reinstall those mods or delete/comment their lines (a mod's folder name must match the line)."))
    if dups:
        out.append(Check("load_order_dups", "Load order: duplicates", WARN, f"listed more than once: {', '.join(sorted(dups))}",
                         "Keep one line per mod."))
    if not (wrong or missing or dups):
        unlisted = sorted(folders - {n for n, _ in entries} - set(INTERNAL_FOLDERS))
        extra = f"; {len(unlisted)} folder(s) not listed ({', '.join(unlisted[:5])}{'...' if len(unlisted) > 5 else ''})" if unlisted else ""
        out.append(Check("load_order", "mod_load_order.txt", OK, f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}, no problems{extra}"))
    return out


def _registry(game: Path, reg: Registry) -> list[Check]:
    out: list[Check] = []
    folders = {d.name for d in mods_dir(game).iterdir() if d.is_dir()} if mods_dir(game).is_dir() else set()
    corrupt = reg.path.with_name(reg.path.name + ".corrupt")
    problems: list[str] = []
    fixes: list[str] = []
    if corrupt.exists():
        problems.append(f"an unreadable registry was set aside at {corrupt.name} (links were lost)")
        fixes.append("Re-link your mods (L) or re-import their archives.")
    gone = sorted(n for n in reg.data["mods"] if n not in folders)
    if gone:
        problems.append(f"registry links for mods that are not in mods/: {', '.join(gone)}")
        fixes.append("cogitator unlink <folder> to drop a stale link, or reinstall the mod.")
    bad = sorted(n for n, r in reg.data["mods"].items() if r.get("nexus_id") is not None and not isinstance(r.get("nexus_id"), int))
    if bad:
        problems.append(f"malformed Nexus ids for: {', '.join(bad)}")
        fixes.append("cogitator link <folder> <url> to repair.")
    for kind in ("dml", "dmf"):
        present = detect_framework(game, kind).status == "installed"
        rec = reg.framework(kind)
        if rec and not present:
            problems.append(f"registry lists {kind.upper()} {rec.get('version') or ''} but its files are missing")
            fixes.append(f"Reinstall {kind.upper()}.")
    if problems:
        out.append(Check("registry", "registry.json vs mods/", WARN, "; ".join(problems), " ".join(fixes)))
    else:
        linked = [n for n in reg.data["mods"] if n in folders]
        unknown = sorted(n for n in linked if reg.data["mods"][n].get("installed_file_id") is None)
        note = f"; baseline unknown for {', '.join(unknown)} (status shows 'unknown' until set)" if unknown else ""
        out.append(Check("registry", "registry.json vs mods/", OK, f"{len(linked)} linked mod(s) match folders in mods/{note}"))
    for kind in ("dml", "dmf"):
        if detect_framework(game, kind).status == "installed" and not reg.framework(kind):
            out.append(Check(f"registry_{kind}", f"registry.json: {kind.upper()}", OK,
                             f"{kind.upper()} was installed outside Cogitator; its version is unknown (never guessed)"))
    return out


def _nexus(offline: bool, key: str | None, client: httpx.Client | None) -> Check:
    if offline:
        return Check("nexus", "Nexus API key", OK, "skipped (--offline)")
    if not key:
        return Check("nexus", "Nexus API key", WARN, "no key set - update checks are disabled", "cogitator set-key (or press K in the TUI).")
    try:
        with NexusClient(key, client=client) as nx:
            who = nx.validate()
            return Check("nexus", "Nexus API key", OK,
                         f"valid ({'premium' if who.get('is_premium') else 'free'} account); quota {nx.quota}")
    except InvalidKey:
        return Check("nexus", "Nexus API key", FAIL, "rejected by Nexus (HTTP 401)", "Create a new key in Nexus account settings > API, then cogitator set-key.")
    except RateLimited as e:
        return Check("nexus", "Nexus API key", WARN, f"rate limited: {e}", "Wait for the hourly window to reset.")
    except NexusError as e:
        return Check("nexus", "Nexus API key", WARN, f"could not verify: {e}", "Check your connection and retry.")


def _github(offline: bool, client: httpx.Client | None) -> Check:
    if offline:
        return Check("github", "GitHub reachability", OK, "skipped (--offline)")
    own = client is None
    c = client or make_client(timeout=10.0)
    try:
        r = c.get(f"{GITHUB_API}/rate_limit")  # does not count against the rate limit
        if r.status_code == 200:
            rem = (r.json().get("resources", {}).get("core", {}) or {}).get("remaining")
            return Check("github", "GitHub reachability", OK, f"api.github.com reachable; {rem if rem is not None else '?'} unauthenticated requests left this hour")
        return Check("github", "GitHub reachability", WARN, f"HTTP {r.status_code} from api.github.com",
                     "Only the opt-in GitHub install fallback needs it; use Nexus zips instead.")
    except (httpx.HTTPError, ValueError) as e:
        return Check("github", "GitHub reachability", WARN, f"unreachable ({type(e).__name__})",
                     "Only the opt-in GitHub install fallback needs it; use Nexus zips instead.")
    finally:
        if own:
            c.close()


def console_log_dirs(steam_roots: list[Path], appdata: str | None = None) -> list[Path]:
    dirs: list[Path] = []
    ad = appdata if appdata is not None else os.environ.get("APPDATA")
    if ad:
        dirs.append(Path(ad) / "Fatshark" / "Darktide" / "console_logs")
    for root in steam_roots:
        for lib in gamefinder.library_folders(root):
            dirs.append(lib / "steamapps" / "compatdata" / APPID / "pfx" / "drive_c" / "users" / "steamuser" /
                        "AppData" / "Roaming" / "Fatshark" / "Darktide" / "console_logs")
    return dirs


def scan_console_log(path: Path) -> tuple[list[str], bool]:
    """(mod error lines, dmf_loaded). A mod error = a line mentioning scripts/mods that looks like an error and is not
    merely a 'Loading ...' progress line."""
    errors: list[str] = []
    dmf = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(_MAX_LOG_BYTES)
    except OSError:
        return errors, dmf
    for line in data.splitlines():
        if _DMF_LOADED in line:
            dmf = True
        if "scripts/mods" in line and "[Mod] Loading" not in line and _ERR.search(line):
            errors.append(line.strip())
    return errors, dmf


def _console_log(dirs: list[Path], reg: Registry, st: str) -> Check:
    logs = []
    for d in dirs:
        try:
            logs += [p for p in d.glob("*.log") if p.is_file()]
        except OSError:
            continue
    if not logs:
        return Check("console_log", "Newest console log", OK, "no Darktide console logs found yet (start the game once)")
    newest = max(logs, key=lambda p: p.stat().st_mtime)
    errors, dmf = scan_console_log(newest)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(newest.stat().st_mtime))
    if errors:
        sample = " | ".join(e[:160] for e in errors[:3])
        return Check("console_log", "Newest console log", WARN,
                     f"{len(errors)} mod error line(s) in {newest.name} ({when}): {sample}",
                     "Open the log, find the mod named in the path (scripts/mods/<mod>/...) and disable it with Space; update or report it.")
    last = (reg.data.get("patch") or {}).get("last_patch_at")
    if st == ON and not dmf and isinstance(last, (int, float)) and newest.stat().st_mtime > last:
        return Check("console_log", "Newest console log", WARN,
                     f"{newest.name} ({when}) postdates the patch but never loads DMF - mods did not load in that session",
                     "Check that DML and DMF are installed and the game is patched (T); then restart the game.")
    return Check("console_log", "Newest console log", OK,
                 f"{newest.name} ({when}): no mod errors" + ("; DMF loaded" if dmf else ""))


def _paths() -> Check:
    """Where Cogitator keeps its own files. Microsoft Store Python silently redirects AppData writes."""
    base = str(getattr(sys, "_base_executable", "") or sys.executable) + " " + str(getattr(sys, "base_prefix", ""))
    store = os.name == "nt" and "WindowsApps" in base  # a venv's own sys.executable hides the Store base interpreter
    detail = f"config + registry in {config_dir()}; log {log_path()}; rollback copies in {cache_dir() / 'rollback'}"
    if store:
        detail += ("  NOTE: this Python is the Microsoft Store build, which redirects AppData writes to "
                   r"%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.*\LocalCache\Local\cogitator "
                   "(Explorer/PowerShell show that location, not the one above)")
    return Check("paths", "Cogitator data", OK, detail)


# ------------------------------------------------------------------ driver

def run_doctor(game_override: str | None = None, *, offline: bool = False, key: str | None = None,
               settings: Settings | None = None, registry: Registry | None = None,
               steam_roots_fn: Callable[[], list[Path]] | None = None, running: Callable[[], bool] = darktide_running,
               http: httpx.Client | None = None, log_dirs: list[Path] | None = None) -> list[Check]:
    settings = settings or Settings.load()
    reg = registry or Registry.load()
    key = key if key is not None else get_api_key()
    roots = (steam_roots_fn or gamefinder.steam_roots)()
    checks: list[Check] = [_steam(roots), _paths()]
    res = gamefinder.resolve_game_dir(game_override, settings.game_dir, roots)
    game = res.game_dir
    if game is None:
        why = {"ambiguous": "several installs found - pick one with --game-dir", "invalid": res.message}.get(res.source, res.message)
        checks.append(Check("game", "Darktide install", FAIL, why or "not found", "Pass --game-dir <folder> or set game_dir in config.toml."))
    else:
        ok, why = validate_game_dir(game)
        checks.append(Check("game", "Darktide install", OK if ok else FAIL, f"{game} ({res.source})" if ok else why,
                            "" if ok else "Point --game-dir at the folder that contains bundle/ and binaries/."))
        if ok:
            checks += [_fw(game, "dml", reg), _fw(game, "dmf", reg)]
            for fn in (lambda: _patch(game, reg), lambda: _load_order(game), lambda: _registry(game, reg)):
                try:
                    checks += fn()
                except Exception as e:  # one broken check must not hide the others
                    checks.append(Check("internal", "doctor", FAIL, f"internal error: {type(e).__name__}: {e}"))
    checks.append(_nexus(offline, key, http))
    checks.append(_github(offline, http))
    try:
        r = running()
    except Exception:
        r = False
    checks.append(Check("running", "Darktide running", WARN if r else OK,
                        "yes - Cogitator refuses to change game files until you quit the game" if r else "no",
                        "Quit Darktide before installing, updating or patching." if r else ""))
    try:
        dirs = log_dirs if log_dirs is not None else console_log_dirs(roots)
        st = patch_state(game) if game and validate_game_dir(game)[0] else UNKNOWN
        checks.append(_console_log(dirs, reg, st))
    except Exception as e:
        checks.append(Check("console_log", "Newest console log", WARN, f"could not scan: {type(e).__name__}: {e}"))
    return checks
