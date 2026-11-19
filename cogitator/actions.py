"""Interface-independent actions: the flows both the terminal UI and the graphical UI run.

Nothing here knows about Textual or tkinter. A flow talks to the person through a `Ui` (log / confirm / ask_text / choose) and
changes the game folder and registry only through the engine modules. Flows block (they may wait for an answer), so they run on
worker threads; the caller owns the threading and the refresh afterwards (`Outcome` says what changed).

Log styles are rich-style strings ("green", "bold red", ...); the graphical UI maps them to text tags."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from . import integrity, snapshots
from .archives import parse_archive_name
from .config import Settings, cache_dir, get_api_key, redact, set_api_key
from .frameworks import classify_zip, describe_plan, plan_local
from .fwupdate import install_framework_plan, needs_repatch
from .loader import LoaderError
from .modinstall import ModImportError, dropped_paths, import_archive, inspect_mod_zip, newest_download, rollback_mod
from .mods import ModError, remove_mod
from .nexus import NexusClient, NexusError, parse_nexus_ref
from .patcher import PatcherError, default_runner
from .procs import darktide_running
from .registry import Registry
from .viewtext import where_text

OK, WARN, ERR, DIM, INFO = "green", "yellow", "bold red", "dim", "cyan"


class Ui(Protocol):
    def log(self, msg: str, style: str = "") -> None: ...
    def confirm(self, title: str, body: str = "", default_yes: bool = True) -> bool: ...
    def ask_text(self, title: str, hint: str = "", body: str = "", allow_blank: bool = False) -> str | None: ...
    def choose(self, question: str, options: list[str], selected: int = 0) -> int | None: ...


@dataclass
class Ctx:
    """What a flow works on. `lock` serialises multi-step registry work between workers."""
    game_dir: Path
    registry: Registry
    settings: Settings
    lock: threading.RLock = field(default_factory=threading.RLock)
    patch_runner: Callable = default_runner
    game_running: Callable[[], bool] = darktide_running

    def downloads_dir(self) -> Path:
        return Path(self.settings.downloads_dir) if self.settings.downloads_dir else Path.home() / "Downloads"


@dataclass
class Outcome:
    refresh: bool = False  # rescan mods and rebuild the table
    folder: str | None = None  # row to keep selected after the refresh
    header: bool = False  # redraw the status header only
    recheck: bool = False  # run an update check


# ---------------------------------------------------------------- import (mod zips and DML/DMF zips)

def import_file(ctx: Ctx, ui: Ui, raw: str, mod_id: int | None, after: Callable[[Outcome], None]) -> None:
    """Import one archive (or several, when `raw` holds more than one dropped path); `after` gets each file's Outcome.
    ModImportError / LoaderError / OSError propagate to the caller (a worker wrapper logs them)."""
    raw = raw.strip()
    if not raw:
        p = newest_download(ctx.downloads_dir())
        if p is None:
            ui.log(f"No .zip from the last hour in {ctx.downloads_dir()}.", ERR)
            return
        ui.log(f"Using the newest download: {p.name}", DIM)
    else:
        found = dropped_paths(raw)
        if len(found) > 1:
            for f in found:
                import_file(ctx, ui, str(f), None, after)
            return
        p = found[0] if found else Path(raw.strip('"'))
    if not p.is_file():
        ui.log(f"No such file: {p}", ERR)
        return
    limits = ctx.settings.extract_limits()
    try:
        kind = classify_zip(p)[0]
    except LoaderError:
        kind = None
    if kind:  # DML / DMF archive
        plan = plan_local(p)
        body = describe_plan(plan)
        if needs_repatch(plan, ctx.game_dir):
            body += "\n\nMods are currently ON: Cogitator will un-patch the game, install, then re-patch it."
        if not ui.confirm(f"Install {kind.upper()} into the game folder?", body):
            return
        try:
            with ctx.lock:
                fi = install_framework_plan(plan, ctx.game_dir, ctx.registry, runner=ctx.patch_runner, is_running=ctx.game_running,
                                            max_bytes=limits[0], max_files=limits[1])
        except PatcherError as e:
            ui.log(f"{kind.upper()}: {e}", ERR)
            after(Outcome(header=True))
            return
        ui.log(f"{kind.upper()} installed ({fi.result.written} files).", OK)
        for n in fi.notes:
            ui.log(n, OK)
        after(Outcome(refresh=True, recheck=True))
        return
    info = inspect_mod_zip(p)
    parsed = parse_archive_name(p.name)
    if mod_id is None and parsed.mod_id is None and len(info.folders) == 1 \
            and not (ctx.registry.mod(info.folders[0]) or {}).get("nexus_id"):
        ref = ui.ask_text(f"'{p.name}' does not say which Nexus mod it is", "Nexus URL or id (blank = install without linking)")
        if ref:
            mod_id, problem = parse_nexus_ref(ref)
            if mod_id is None:
                ui.log(problem or "Bad reference.", ERR)
                return
    if not ui.confirm(f"Install {', '.join(info.folders)}?", f"From {p.name}\nInto {ctx.game_dir / 'mods'}"):
        return
    key = get_api_key()
    nx = NexusClient(key) if key else None
    try:
        with ctx.lock:
            r = import_archive(p, ctx.game_dir, ctx.registry, nx, mod_id=mod_id, choose=lambda q, o: ui.choose(q, o),
                               is_running=ctx.game_running, max_bytes=limits[0], max_files=limits[1])
    finally:
        if nx:
            nx.close()
    ui.log(f"Installed {', '.join(r.folders)}" + (f"; added to load order: {', '.join(r.added_to_load_order)}" if r.added_to_load_order else ""), OK)
    for n in r.notes:
        ui.log(n, WARN)
    after(Outcome(refresh=True, folder=r.linked or (r.folders[0] if r.folders else None)))


# ---------------------------------------------------------------- remove / roll back

def remove_one(ctx: Ctx, ui: Ui, folder: str) -> Outcome | None:
    """None: nothing was attempted (game running)."""
    if ctx.game_running():
        ui.log("Close Darktide before removing a mod.", "bold yellow")
        return None
    try:
        with ctx.lock:
            snapshots.auto(ctx.game_dir, "remove", folders=[folder], registry=ctx.registry)
            dest = remove_mod(ctx.game_dir, folder, cache_dir() / "removed")
            ctx.registry.forget_mod(folder)
            integrity.forget(folder)
        ui.log(f"Removed {folder}. Its files are in {dest}", OK)
    except (ModError, OSError) as e:
        ui.log(f"Could not remove {folder}: {e}", ERR)
    return Outcome(refresh=True)


def roll_back(ctx: Ctx, ui: Ui, folder: str) -> Outcome:
    try:
        with ctx.lock:
            msg = rollback_mod(folder, ctx.game_dir, ctx.registry, is_running=ctx.game_running)
        ui.log(msg, OK)
    except (ModImportError, OSError) as e:
        ui.log(f"Could not roll back {folder}: {e}", ERR)
    return Outcome(refresh=True, folder=folder)


# ---------------------------------------------------------------- Nexus key

@dataclass
class KeyOutcome:
    ok: bool
    message: str
    premium: bool | None = None


def store_key(ctx: Ctx, key: str) -> KeyOutcome:
    """Validate the key with Nexus, store it and remember the account type. The message never contains the key."""
    try:
        with NexusClient(key) as nx:
            who = nx.validate()
    except NexusError as e:
        return KeyOutcome(False, redact(f"Key not saved: {e}", key))
    where = set_api_key(key)
    with ctx.lock:
        upd = ctx.registry.data["updates"]
        upd["premium"], upd["premium_checked"] = bool(who.get("is_premium")), time.time()
        ctx.registry.save()
    return KeyOutcome(True, f"Key valid ({'premium' if who.get('is_premium') else 'free'}); stored {where_text(where)}.",
                      bool(who.get("is_premium")))


# ---------------------------------------------------------------- per-mod metadata: filter, notes, tags, favourites, pin

def mod_meta(registry: Registry | None, folder: str) -> dict:
    return (registry.mod(folder) if registry else None) or {}


def mod_visible(folder: str, meta: dict, filter_text: str) -> bool:
    """Filter semantics: every word must occur in the name, tags, note, 'fav' or 'pinned'."""
    q = filter_text.strip().casefold()
    if not q:
        return True
    hay = " ".join([folder, " ".join(meta.get("tags") or []), meta.get("notes") or "", "fav" if meta.get("fav") else "",
                    "pinned" if meta.get("pinned_version") else ""]).casefold()
    return all(w in hay for w in q.split())


def meta_hint(meta: dict) -> str:
    bits = []
    if meta.get("pinned_version"):
        bits.append(f"pinned to {meta['pinned_version']}")
    if meta.get("tags"):
        bits.append("tags: " + ", ".join(meta["tags"]))
    if meta.get("notes"):
        bits.append("note: " + meta["notes"])
    return "  |  " + "  |  ".join(bits) if bits else ""


def parse_note(text: str) -> tuple[list[str], str]:
    """'#ui #hud free text' -> (['ui', 'hud'], 'free text')."""
    words = text.split()
    tags = [w[1:].lower()[:30] for w in words if w.startswith("#") and len(w) > 1]
    note = " ".join(w for w in words if not (w.startswith("#") and len(w) > 1))
    return tags, note[:500]


def set_note(registry: Registry, folder: str, text: str) -> str:
    tags, note = parse_note(text)
    registry.set_mod(folder, tags=tags, notes=note)
    return f"{folder}: " + (f"tags {', '.join(tags) or '(none)'}; note saved." if text else "tags and note cleared.")


def toggle_fav(registry: Registry, folder: str) -> str:
    fav = not mod_meta(registry, folder).get("fav")
    registry.set_mod(folder, fav=fav)
    return f"{folder}: {'favourite' if fav else 'not a favourite'}."


def toggle_pin(registry: Registry, folder: str) -> str:
    meta = mod_meta(registry, folder)
    if meta.get("pinned_version"):
        registry.set_mod(folder, pinned_version=None)
        return f"{folder}: unpinned."
    v = str(meta.get("installed_version") or "the installed version")
    registry.set_mod(folder, pinned_version=v)
    return f"{folder}: pinned to {v}; its updates are skipped until you unpin it."
