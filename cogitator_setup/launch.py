"""Starting Cogitator's terminal UI for a user who has never opened a terminal: Windows Terminal if present, else a console."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

CREATE_NEW_CONSOLE = 0x00000010
TITLE = "Cogitator"


def terminal_command(target: list[str], which: Callable[[str], str | None] = shutil.which) -> tuple[list[str], int]:
    """(argv, creationflags). Windows Terminal ("wt.exe") when installed, otherwise a new console window."""
    wt = which("wt.exe") or which("wt")
    if wt:
        return [wt, "--title", TITLE, *target], 0
    return list(target), CREATE_NEW_CONSOLE


def launch(target: list[str], which: Callable[[str], str | None] = shutil.which,
           popen: Callable[..., object] = subprocess.Popen) -> list[str]:
    """Start `target` in a terminal; returns the argv used. No shell is involved."""
    argv, flags = terminal_command(target, which)
    popen(argv, creationflags=flags, close_fds=True)
    return argv


def cogitator_target(frozen: bool, executable: str) -> list[str] | None:
    """What "Start Cogitator" runs: app/cogitator.exe next to the setup folder when installed, or the module from source."""
    if not frozen:
        return [executable, "-m", "cogitator"]
    exe = Path(executable).resolve().parent.parent / "app" / "cogitator.exe"
    return [str(exe)] if exe.is_file() else None


def launch_cogitator() -> None:
    target = cogitator_target(bool(getattr(sys, "frozen", False)), sys.executable)
    if target:
        launch(target)
