"""Simulated runs for `--dry-run`: the windows behave like the real thing (steps tick, Cancel works) but NOTHING is installed,
removed, patched or stored. Used so a person can look at and click through every screen safely.
"""
from __future__ import annotations

import threading
import time

from cogitator.setup_run import ProgressEvent, SetupResult, StepResult
from cogitator.setup_run import planned_steps as setup_steps
from cogitator.uninstall_run import FullUninstallResult
from cogitator.uninstall_run import planned_steps as uninstall_steps

STEP_SECONDS = 0.35


def _walk(names: list[str], progress, cancel: threading.Event, seconds: float) -> tuple[list[StepResult], bool]:
    done: list[StepResult] = []
    for i, name in enumerate(names):
        if cancel.is_set():
            return done, True
        progress(ProgressEvent(name, "start", i, len(names)))
        if cancel.wait(seconds):
            return done, True
        done.append(StepResult(name, "done", "(simulated)"))
        progress(ProgressEvent(name, "done", i, len(names), "(simulated)"))
    return done, False


def simulated_setup(plan, progress, cancel, deps=None, seconds: float = STEP_SECONDS) -> SetupResult:
    steps, cancelled = _walk(setup_steps(plan), progress, cancel, seconds)
    return SetupResult(ok=not cancelled, cancelled=cancelled, steps=steps, dry_run=True, rolled_back=cancelled,
                       mods_on=None, what="Dry run cancelled." if cancelled else "")


def simulated_uninstall(plan, progress, cancel, deps=None, seconds: float = STEP_SECONDS) -> FullUninstallResult:
    steps, cancelled = _walk(uninstall_steps(plan), progress, cancel, seconds)
    return FullUninstallResult(ok=not cancelled, cancelled=cancelled, steps=steps, dry_run=True,
                               what="Dry run cancelled." if cancelled else "")
