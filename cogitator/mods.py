"""Scan <game>/mods, join with mod_load_order.txt."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .loadorder import LOAD_ORDER_NAME, LoadOrder

# Folders owned by the Mod Loader (mods/base) and the Mod Framework (mods/dmf); never treated as user mods.
INTERNAL_FOLDERS: frozenset[str] = frozenset({"base", "dmf"})

ENABLED, DISABLED, UNLISTED = "enabled", "disabled", "unlisted"


class ModError(Exception):
    pass


@dataclass
class Mod:
    folder: str
    state: str  # enabled | disabled | unlisted
    present: bool = True  # False: listed in load order but folder missing
    has_mod_file: bool = True  # <folder>/<folder>.mod exists
    order: int | None = None  # index among listed mods


def mods_dir(game_dir: Path) -> Path:
    return Path(game_dir) / "mods"


def load_order_path(game_dir: Path) -> Path:
    return mods_dir(game_dir) / LOAD_ORDER_NAME


def scan_mods(game_dir: Path, internal: frozenset[str] | set[str] | None = None) -> list[Mod]:
    """Listed mods first (load order), then unlisted folders alphabetically."""
    internal = INTERNAL_FOLDERS if internal is None else internal
    md =mods_dir(game_dir)
    folders = {p.name for p in md.iterdir() if p.is_dir()} if md.is_dir() else set()
    folders -= set(internal)
    lo = LoadOrder.load(load_order_path(game_dir))
    out: list[Mod] = []
    seen: set[str] = set()
    for i, (name, enabled) in enumerate(lo.entries()):
        if name in internal or name in seen:
            continue
        seen.add(name)
        present = name in folders
        out.append(Mod(name, ENABLED if enabled else DISABLED, present,
                       present and (md / name / f"{name}.mod").is_file(), i))
    for name in sorted(folders - seen, key=str.casefold):
        out.append(Mod(name, UNLISTED, True, (md / name / f"{name}.mod").is_file()))
    return out


def set_mod_enabled(game_dir: Path, folder: str, enabled: bool,
                    internal: frozenset[str] | set[str] | None = None) -> str:
    """Enable/disable a mod. Returns a human message. Raises ModError."""
    internal = INTERNAL_FOLDERS if internal is None else internal
    if folder in internal:
        raise ModError(f"'{folder}' is part of the mod loader/framework and is not managed here.")
    if folder in ("", ".", "..") or any(c in folder for c in "/\\") or any(c.isspace() for c in folder):
        raise ModError(f"Invalid mod folder name: {folder!r}")
    if not (mods_dir(game_dir) / folder).is_dir():
        raise ModError(f"No folder '{folder}' in {mods_dir(game_dir)}.")
    path = load_order_path(game_dir)
    lo = LoadOrder.load(path)
    cur = lo.state(folder)
    if cur is None:
        if not enabled:
            return f"{folder}: not in load order (already not loaded)."
        lo.add(folder, True)
        msg = f"{folder}: added to load order (enabled)."
    elif cur == enabled:
        return f"{folder}: already {'enabled' if enabled else 'disabled'}."
    else:
        lo.set_enabled(folder, enabled)
        msg = f"{folder}: {'enabled' if enabled else 'disabled'}."
    lo.save(path)
    return msg


def toggle_mod(game_dir: Path, folder: str, internal=None) -> str:
    """Space-key semantics: enabled<->disabled; unlisted -> add (enabled)."""
    lo = LoadOrder.load(load_order_path(game_dir))
    return set_mod_enabled(game_dir, folder, lo.state(folder) is not True, internal)
