"""Start Darktide through Steam, after a doctor check, optionally with a profile or with mods off ('vanilla')."""
from __future__ import annotations

import os
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import profiles
from .doctor import FAIL, run_doctor
from .gamefinder import APPID
from .patcher import OFF, ON, PatcherError, patch, patch_state, unpatch
from .procs import darktide_running

STEAM_URL = f"steam://rungameid/{APPID}"


class LaunchError(Exception):
    pass


@dataclass
class LaunchResult:
    started: bool = False
    notes: list[str] = field(default_factory=list)


def open_steam(url: str = STEAM_URL) -> None:
    if os.name == "nt":
        os.startfile(url)  # type: ignore[attr-defined]
    else:
        webbrowser.open(url)


def launch(game_dir: Path, registry, *, profile: str | None = None, vanilla: bool = False, force: bool = False,
           opener: Callable[[str], None] = open_steam, is_running: Callable[[], bool] = darktide_running,
           runner=None, doctor=run_doctor) -> LaunchResult:
    """Raises LaunchError when it should not start. Everything it changed is listed in `notes`."""
    res = LaunchResult()
    if is_running():
        raise LaunchError("Darktide is already running.")
    if profile and vanilla:
        raise LaunchError("Choose either a profile or vanilla, not both.")
    checks = doctor(str(game_dir), offline=True, registry=registry)
    failed = [c for c in checks if c.level == FAIL and c.id not in ("running",)]
    if failed and not force:
        raise LaunchError("Not launching: " + "; ".join(f"{c.name}: {c.detail}" for c in failed[:3]) + "  (use --force to launch anyway)")
    kw = {"runner": runner} if runner else {}
    try:
        if profile:
            prof = profiles.load(profile)
            plan = profiles.switch(game_dir, prof, registry)
            res.notes.append(f"Profile '{profile}' applied" + (f"; not installed: {', '.join(plan['missing'])}" if plan["missing"] else "."))
        if vanilla:
            if patch_state(game_dir) == ON:
                unpatch(game_dir, registry, is_running=is_running, **kw)
                res.notes.append("Mods are OFF for this launch. Run 'cogitator toggle' (or press T) to turn them on again.")
        elif patch_state(game_dir) == OFF and (registry.data.get("patch") or {}).get("last_patch_at"):
            patch(game_dir, registry, is_running=is_running, **kw)
            res.notes.append("Mods were off (game updated?); patched again before launching.")
    except PatcherError as e:
        raise LaunchError(str(e)) from None
    opener(STEAM_URL)
    res.started = True
    return res
