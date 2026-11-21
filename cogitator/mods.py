"""Scan <game>/mods, join with mod_load_order.txt."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .fsutil import is_link, make_dir_link, unlink_link
from .loadorder import LOAD_ORDER_NAME, LoadOrder, folder_index, resolve_folder

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


def bad_folder_name(folder: str) -> str | None:
    """Why `folder` cannot be a mod name in mod_load_order.txt, or None. Spaces inside a name are fine (the loader reads
    the whole line); a path separator, surrounding space or something that reads as a comment is not."""
    import re

    if folder in ("", ".", "..") or any(c in folder for c in "/\\") or folder != folder.strip():
        return f"Invalid mod folder name: {folder!r}"
    if folder.startswith(("--", "#")) or re.search(r"\s--", folder):
        return f"The mod folder name {folder!r} cannot be written in mod_load_order.txt (it would read as a comment)."
    return None


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
    idx = folder_index(folders)
    out: list[Mod] = []
    seen: set[str] = set()
    for i, (name, enabled) in enumerate(lo.entries()):
        disk = resolve_folder(name, folders, idx)
        shown = disk or name
        if name in internal or shown.casefold() in seen:
            continue
        seen.add(shown.casefold())
        out.append(Mod(shown, ENABLED if enabled else DISABLED, disk is not None,
                       disk is not None and (md / shown / f"{shown}.mod").is_file(), i))
    for name in sorted((f for f in folders if f.casefold() not in seen), key=str.casefold):
        out.append(Mod(name, UNLISTED, True, (md / name / f"{name}.mod").is_file()))
    return out


def set_mod_enabled(game_dir: Path, folder: str, enabled: bool,
                    internal: frozenset[str] | set[str] | None = None) -> str:
    """Enable/disable a mod. Returns a human message. Raises ModError."""
    internal = INTERNAL_FOLDERS if internal is None else internal
    if folder in internal:
        raise ModError(f"'{folder}' is part of the mod loader/framework and is not managed here.")
    why = bad_folder_name(folder)
    if why:
        raise ModError(why)
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


def move_mod(game_dir: Path, folder: str, delta: int, internal: frozenset[str] | set[str] | None = None) -> str:
    """Move a listed mod one place up (delta<0) or down (delta>0) in mod_load_order.txt by swapping it with its
    neighbouring mod entry. Comments, blank lines, line endings and the Mod Loader/Framework entries stay where they
    are. Raises ModError."""
    internal = INTERNAL_FOLDERS if internal is None else internal
    path = load_order_path(game_dir)
    lo = LoadOrder.load(path)
    mine = next((i for i, ln in enumerate(lo.lines) if ln.name == folder and folder not in internal), None)
    if mine is None:
        raise ModError(f"{folder} is not in the load order yet. Press Space to add it first.")
    # other movable entries (a repeated line of the same mod is not a neighbour)
    seq = [i for i, ln in enumerate(lo.lines) if ln.name and ln.name not in internal and (ln.name != folder or i == mine)]
    pos = seq.index(mine) + (1 if delta > 0 else -1)
    if not 0 <= pos < len(seq):
        return f"{folder}: already {'last' if delta > 0 else 'first'} in the load order."
    a, b = lo.lines[mine], lo.lines[seq[pos]]
    neighbour = b.name
    a.text, b.text = b.text, a.text  # swap the entries; each line keeps its own position and line ending
    lo.save(path)
    return f"{folder}: moved {'down, after' if delta > 0 else 'up, before'} {neighbour}."


def move_mod_to(game_dir: Path, folder: str, target: str, after: bool = False,
                internal: frozenset[str] | set[str] | None = None) -> str:
    """Drag and drop: put a listed mod right before (or, with after=True, right after) another listed mod. Only the mod
    entries change places; comments, blank lines and the Mod Loader/Framework entries stay where they are. Raises ModError."""
    internal = INTERNAL_FOLDERS if internal is None else internal
    path = load_order_path(game_dir)
    lo = LoadOrder.load(path)
    seq = [i for i, ln in enumerate(lo.lines) if ln.name and ln.name not in internal]
    mine = next((k for k, i in enumerate(seq) if lo.lines[i].name == folder), None)
    dest = next((k for k, i in enumerate(seq) if lo.lines[i].name == target), None)
    if mine is None or dest is None:
        raise ModError(f"{folder if mine is None else target} is not in the load order yet. Turn it on first.")
    if folder == target:
        return f"{folder}: stays where it is."
    texts = [lo.lines[i].text for i in seq]
    moved = texts.pop(mine)
    if mine < dest:
        dest -= 1  # the list shrank in front of the target
    texts.insert(dest + (1 if after else 0), moved)
    if texts == [lo.lines[i].text for i in seq]:
        return f"{folder}: already there."
    for i, t in zip(seq, texts):
        lo.lines[i].text = t
    lo.save(path)
    return f"{folder}: moved {'after' if after else 'before'} {target}."


def link_dev_mod(game_dir: Path, source: Path, name: str | None = None) -> str:
    """Make <game>/mods/<name> a link to a folder you develop in, and enable it. Returns the mod folder name."""
    source = Path(source).resolve()
    if not source.is_dir():
        raise ModError(f"Not a folder: {source}")
    name = name or source.name
    why = bad_folder_name(name)
    if why:
        raise ModError(why)
    if name in INTERNAL_FOLDERS:
        raise ModError(f"'{name}' is reserved for the mod loader/framework.")
    if not mods_dir(game_dir).is_dir():
        raise ModError("mods/ not found - install the Mod Loader first.")
    if not (source / f"{name}.mod").is_file():
        raise ModError(f"{source} has no {name}.mod, so the game would not load it. Name the link with --name if the .mod file is called differently.")
    dest = mods_dir(game_dir) / name
    if dest.exists() or is_link(dest):
        raise ModError(f"'{name}' already exists in {mods_dir(game_dir)}. Remove it first.")
    try:
        make_dir_link(dest, source)
    except OSError as e:
        raise ModError(f"Could not create the link: {e}") from None
    set_mod_enabled(game_dir, name, True)
    return name


def remove_mod(game_dir: Path, folder: str, trash_dir: Path, internal: frozenset[str] | set[str] | None = None) -> Path:
    """Take a mod out of the game: its folder is MOVED (not deleted) into `trash_dir` and its load-order lines are
    removed. Returns where the folder went. Raises ModError."""
    import shutil
    import time

    internal = INTERNAL_FOLDERS if internal is None else internal
    if folder in internal:
        raise ModError(f"'{folder}' is part of the mod loader/framework and is not managed here.")
    why = bad_folder_name(folder)
    if why:
        raise ModError(why)
    src = mods_dir(game_dir) / folder
    path = load_order_path(game_dir)
    lo = LoadOrder.load(path)
    dest = None
    if is_link(src):  # a dev link: drop the link, never move or copy what it points at
        unlink_link(src)
        if lo.remove(folder):
            lo.save(path)
        return trash_dir
    if src.is_dir():
        trash_dir.mkdir(parents=True, exist_ok=True)
        dest = trash_dir / f"{folder}-{time.strftime('%Y%m%d-%H%M%S')}"
        n = 1
        while dest.exists():
            n += 1
            dest = trash_dir / f"{folder}-{time.strftime('%Y%m%d-%H%M%S')}-{n}"
        shutil.move(str(src), str(dest))
    elif lo.state(folder) is None:
        raise ModError(f"No folder '{folder}' in {mods_dir(game_dir)}.")
    if lo.remove(folder):
        lo.save(path)
    return dest or trash_dir
