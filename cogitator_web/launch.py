"""Starting the web window for the "Start Cogitator" button of the setup: detached, no terminal."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

WEB_EXE = "cogitator-web.exe"
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def web_target(frozen: bool, executable: str) -> list[str] | None:
    """installed: app/cogitator-web.exe next to the setup folder (None when it is missing); source: python -m cogitator_web."""
    if not frozen:
        return [executable, "-m", "cogitator_web"]
    exe = Path(executable).resolve().parent.parent / "app" / WEB_EXE
    return [str(exe)] if exe.is_file() else None


MISSING = "cogitator-web.exe was not found next to this install, so the Cogitator window cannot open. The command line still works: cogitator --help"


def launch_app(*, frozen: bool | None = None, executable: str | None = None, popen: Callable[..., object] = subprocess.Popen,
               args: list[str] | None = None) -> list[str] | None:
    """Start the web window detached. Without cogitator-web.exe, say so on stderr and return None; else the argv used."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    target = web_target(frozen, executable or sys.executable)
    if target is None:
        print(MISSING, file=sys.stderr)
        return None
    target = target + list(args or [])
    popen(target, creationflags=_DETACHED if sys.platform == "win32" else 0, close_fds=True, cwd=str(Path.home()))
    return target
