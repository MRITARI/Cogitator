"""The update helper. Standard library only: it runs from a temporary copy while the app folder is being replaced.

It waits for Cogitator to exit, swaps the app folder by renames (the old build stays as app.previous), runs the new
build's self-check, and relaunches. If anything fails it puts the old build back and records the failed version so the
app does not try it again. It never touches settings, saved state, keys, rollback folders or the game folder.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_for_exit(pid: int, timeout: float = 60.0, alive: Callable[[int], bool] = pid_alive) -> bool:
    end = time.monotonic() + timeout
    while alive(pid):
        if time.monotonic() > end:
            return False
        time.sleep(0.2)
    return True


def _check(argv: list[str]) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _launch(argv: list[str]) -> None:
    """Start Cogitator again. It is a terminal program, so it needs a console: Windows Terminal when present, else a new
    console window (a detached process has none and would exit at once). Its folder must not be the app folder."""
    flags = 0
    if os.name == "nt":
        wt = shutil.which("wt.exe")
        if wt:
            argv = [wt, "--title", "Cogitator", *argv]
        else:
            flags = 0x00000010  # CREATE_NEW_CONSOLE
    subprocess.Popen(argv, creationflags=flags, close_fds=True, shell=False, cwd=str(Path.home()))


def _rm(p: Path) -> None:
    shutil.rmtree(p, ignore_errors=True)


def _log(result: str | None, text: str) -> None:
    """Append a line to update.log beside the result file, so a failed update can be diagnosed afterwards."""
    if not result:
        return
    try:
        path = Path(result).with_name("update.log")
        if path.is_file() and path.stat().st_size > 200_000:
            path.replace(path.with_name("update.log.1"))  # keep the log from growing for ever
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


RENAME_TRIES = 40  # a folder can stay locked for a few seconds after its program exits (antivirus, indexer)
RENAME_DELAY = 0.5


def _mv(src: Path, dst: Path) -> None:
    """Rename, retrying while Windows reports the folder as in use."""
    for i in range(RENAME_TRIES):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if i == RENAME_TRIES - 1:
                raise
            time.sleep(RENAME_DELAY)


def _write_result(path: str | None, status: str, version: str, detail: str = "") -> None:
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"status": status, "version": version, "detail": detail, "at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def swap_in(app: Path, previous: Path, staged: Path) -> Path | None:
    """app -> app.previous, staged -> app. Any existing app.previous is set aside (not deleted) and returned; the caller
    drops it once the new build has passed its check. If a rename fails, app and app.previous are left as they were."""
    if not staged.is_dir():
        raise OSError("nothing staged")
    stash = previous.with_name(previous.name + ".old") if previous.exists() else None
    if stash is not None:
        _rm(stash)
        _mv(previous, stash)
    try:
        _mv(app, previous)
    except OSError:
        if stash is not None:
            _mv(stash, previous)
        raise
    try:
        _mv(staged, app)
    except OSError:
        _mv(previous, app)
        if stash is not None:
            _mv(stash, previous)
        raise
    return stash


def swap_back(app: Path, previous: Path) -> None:
    """Drop the failed build and restore app.previous as app."""
    failed = app.with_name(app.name + ".failed")
    _rm(failed)
    _mv(app, failed)
    try:
        _mv(previous, app)
    except OSError:
        _mv(failed, app)
        raise
    _rm(failed)


def exchange(app: Path, previous: Path) -> None:
    tmp = app.with_name(app.name + ".swap")
    _rm(tmp)
    _mv(app, tmp)
    try:
        _mv(previous, app)
    except OSError:
        _mv(tmp, app)
        raise
    _mv(tmp, previous)


def run(argv: list[str], *, alive: Callable[[int], bool] = pid_alive, check: Callable[[list[str]], bool] = _check,
        relaunch: Callable[[list[str]], None] = _launch, wait_timeout: float = 60.0) -> int:
    if argv[:1] == ["--self-check"]:  # extra flags (e.g. --expect-version) are accepted and ignored here
        print(f"ok    updater: frozen={getattr(sys, 'frozen', False)}")
        return 0
    p = argparse.ArgumentParser()
    p.add_argument("--app", required=True)
    p.add_argument("--previous", required=True)
    p.add_argument("--staged")
    p.add_argument("--rollback", action="store_true")
    p.add_argument("--version", default="")
    p.add_argument("--wait-pid", type=int, default=0)
    p.add_argument("--result")
    p.add_argument("--check", default="[]")
    p.add_argument("--relaunch", default="[]")
    a = p.parse_args(argv)
    if not a.rollback and not a.staged:
        p.error("--staged is required unless --rollback is given")
    app, previous = Path(a.app), Path(a.previous)
    check_cmd, relaunch_cmd = json.loads(a.check), json.loads(a.relaunch)

    def start() -> None:
        if relaunch_cmd:
            try:
                relaunch(relaunch_cmd)
            except OSError:
                pass

    _log(a.result, f"update helper started: {'rollback' if a.rollback else 'install ' + a.version}, waiting for pid {a.wait_pid}")
    if a.wait_pid and not wait_for_exit(a.wait_pid, wait_timeout, alive):
        _write_result(a.result, "aborted", a.version, "Cogitator did not close in time.")
        _log(a.result, "Cogitator did not close in time")
        return 3
    stash: Path | None = None
    try:
        if a.rollback:
            exchange(app, previous)
        else:
            stash = swap_in(app, previous, Path(a.staged))
    except OSError as e:
        _write_result(a.result, "aborted", a.version, f"Could not replace the app folder: {e}")
        _log(a.result, f"swap failed, nothing changed: {e}")
        start()
        return 4
    if check_cmd and not check(check_cmd):
        try:
            if a.rollback:
                exchange(app, previous)
            else:
                swap_back(app, previous)
                if stash is not None:
                    stash.rename(previous)  # the older previous build comes back too
        except OSError as e:
            _write_result(a.result, "broken", a.version, f"The new build failed its check and the old one could not be restored: {e}")
            return 5
        _log(a.result, "the new build failed its self-check; previous version restored")
        _write_result(a.result, "reverted", a.version, "The new build failed its self-check; the previous version was restored.")
        start()
        return 1
    if stash is not None:
        _rm(stash)  # only now, with the new build in place and checked, is the older previous build dropped
    _log(a.result, "done: " + ("rolled back" if a.rollback else "updated to " + a.version))
    _write_result(a.result, "rolled-back" if a.rollback else "ok", a.version)
    start()
    return 0


def main() -> int:
    try:
        os.chdir(tempfile.gettempdir())  # never keep the app folder (inherited from Cogitator) open: it cannot be renamed then
    except OSError:
        pass
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
