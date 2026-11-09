"""Steam / VDF / ACF based Darktide detection, manual override, validation."""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import vdf

APPID = "1361210"
log = logging.getLogger(__name__)


def registry_steam_paths() -> list[tuple[str, Path]]:
    """(registry key label, path) for every registry key that yields a Steam path. Logged."""
    import winreg

    found: list[tuple[str, Path]] = []
    for hive, hive_name, sub, val in (
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_CURRENT_USER, "HKCU", r"Software\Valve\Steam", "SteamPath"),
    ):
        label = f"{hive_name}\\{sub}\\{val}"
        try:
            with winreg.OpenKey(hive, sub) as k:
                p = Path(winreg.QueryValueEx(k, val)[0])
        except OSError:
            log.info("Steam registry key not present: %s", label)
            continue
        log.info("Steam path from registry key %s: %s", label, p)
        found.append((label, p))
    return found


def steam_roots() -> list[Path]:
    """Candidate Steam install dirs (existing ones only, de-duplicated)."""
    cands: list[Path] = []
    if sys.platform == "win32":
        try:
            cands += [p for _, p in registry_steam_paths()]
        except ImportError:  # pragma: no cover
            pass
    else:
        home = Path.home()
        cands += [
            home / ".steam" / "steam",
            home / ".local" / "share" / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        ]
    seen, out = set(), []
    for c in cands:
        try:
            r = c.resolve()
        except OSError:
            continue
        if r not in seen and r.is_dir():
            seen.add(r)
            out.append(r)
    return out


def parse_vdf_file(path: Path) -> dict:
    # vdf handles escaped backslashes ("C:\\Games") when escaped=True (default)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return vdf.load(f)


def library_folders(steam_root: Path) -> list[Path]:
    """All Steam library roots (including the Steam dir itself)."""
    libs: list[Path] = [steam_root]
    for rel in ("config/libraryfolders.vdf", "steamapps/libraryfolders.vdf"):
        p = steam_root / rel
        if not p.is_file():
            continue
        try:
            data = parse_vdf_file(p)
        except Exception as e:
            log.warning("cannot parse %s: %s", p, e)
            continue
        root = next((v for k, v in data.items() if k.lower() == "libraryfolders"), {})
        for key, entry in root.items():
            if not key.isdigit():
                continue
            path = entry.get("path") if isinstance(entry, dict) else entry  # old format: plain string
            if isinstance(path, str) and path:
                libs.append(Path(path))
    seen, out = set(), []
    for l in libs:
        k = os.path.normcase(str(l))
        if k not in seen:
            seen.add(k)
            out.append(l)
    return out


def game_in_library(lib: Path) -> Path | None:
    acf = lib / "steamapps" / f"appmanifest_{APPID}.acf"
    if not acf.is_file():
        return None
    try:
        data = parse_vdf_file(acf)
    except Exception as e:
        log.warning("cannot parse %s: %s", acf, e)
        return None
    state = next((v for k, v in data.items() if k.lower() == "appstate"), {})
    installdir = state.get("installdir") if isinstance(state, dict) else None
    if not installdir:
        return None
    return lib / "steamapps" / "common" / installdir


def validate_game_dir(path: Path | str) -> tuple[bool, str]:
    p = Path(path)
    if not p.is_dir():
        return False, f"not a directory: {p}"
    missing = [d for d in ("bundle", "binaries") if not (p / d).is_dir()]
    if missing:
        return False, f"missing {', '.join(m + '/' for m in missing)} in {p}"
    return True, "ok"


def find_candidates(roots: list[Path] | None = None) -> list[Path]:
    """Valid Darktide installs found via Steam (may be >1 across libraries)."""
    found: list[Path] = []
    seen = set()
    for root in roots if roots is not None else steam_roots():
        for lib in library_folders(root):
            g = game_in_library(lib)
            if g and validate_game_dir(g)[0]:
                k = os.path.normcase(str(g))
                if k not in seen:
                    seen.add(k)
                    found.append(g)
    return found


@dataclass
class Resolution:
    game_dir: Path | None
    source: str  # 'override' | 'config' | 'steam' | 'none' | 'ambiguous' | 'invalid'
    candidates: list[Path] = field(default_factory=list)
    message: str = ""


def resolve_game_dir(override: str | None, saved: str | None, roots: list[Path] | None = None) -> Resolution:
    if override:
        ok, why = validate_game_dir(override)
        if ok:
            return Resolution(Path(override), "override")
        return Resolution(None, "invalid", message=f"--game-dir rejected: {why}")
    if saved:
        ok, why = validate_game_dir(saved)
        if ok:
            return Resolution(Path(saved), "config")
        log.warning("saved game_dir invalid (%s); falling back to detection", why)
    cands = find_candidates(roots)
    if len(cands) == 1:
        return Resolution(cands[0], "steam", cands)
    if len(cands) > 1:
        return Resolution(None, "ambiguous", cands, "Multiple Darktide installs found; choose one.")
    return Resolution(None, "none", message="Cogitator could not find Darktide in your Steam libraries (only the Steam version is supported). "
                                                    "Type or paste the folder that contains the 'bundle' and 'binaries' folders.")
