"""Install or update DML/DMF. Updating DML while mods are on runs un-patch, overwrite, re-patch; DMF is plain files under
mods/dmf. Each step uses the guarded patcher and the transactional installer."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .frameworks import InstallResult, Plan, install_plan
from .loader import LoaderError
from .patcher import ON, PatcherError, Runner, default_runner, patch, patch_state, unpatch
from .procs import darktide_running
from .registry import Registry

log = logging.getLogger(__name__)


@dataclass
class FrameworkInstall:
    result: InstallResult
    repatched: bool = False
    notes: list[str] = field(default_factory=list)


def needs_repatch(plan: Plan, game_dir: Path) -> bool:
    return plan.component == "dml" and patch_state(game_dir) == ON


def install_framework_plan(plan: Plan, game_dir: Path, registry: Registry | None = None, *,
                           runner: Runner = default_runner, is_running: Callable[[], bool] = darktide_running,
                           max_bytes: int = 500 * 1024 * 1024, max_files: int = 20000) -> FrameworkInstall:
    """Install `plan`; if it is DML and mods are ON, un-patch first and re-patch afterwards."""
    game_dir = Path(game_dir)
    was_on = needs_repatch(plan, game_dir)
    if was_on:
        unpatch(game_dir, registry, runner=runner, is_running=is_running)  # PatcherError aborts before any file is touched
    try:
        res = install_plan(plan, game_dir, registry, max_bytes=max_bytes, max_files=max_files, is_running=is_running)
    except BaseException:
        if was_on:  # put the previous state back; the install itself already rolled back its files
            try:
                patch(game_dir, registry, runner=runner, is_running=is_running)
            except PatcherError as e:
                log.error("could not restore the patch after a failed DML install: %s", e)
        raise
    out = FrameworkInstall(res)
    if was_on:
        try:
            patch(game_dir, registry, runner=runner, is_running=is_running)
            out.repatched = True
            out.notes.append("Mods re-enabled (database re-patched after the Mod Loader update).")
        except PatcherError as e:
            raise PatcherError(f"The Mod Loader was updated, but re-patching failed: {e} Press T to enable mods.") from e
    return out
