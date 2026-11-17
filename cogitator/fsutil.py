"""Small filesystem helpers: atomic writes."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, mode: int | None = None, newline: str = "\n") -> None:
    """Write UTF-8 text, normalising line endings to `newline`."""
    data = text.replace("\r\n", "\n").replace("\n", newline).encode("utf-8")
    atomic_write_bytes(path, data, mode)


def unlink_link(p) -> None:
    """Remove a symlink or junction itself. Never follows it and never deletes what it points at."""
    import os

    if os.path.islink(p):
        os.unlink(p)
    else:
        os.rmdir(p)  # a junction: rmdir removes the link only (it fails if given a real, non-empty folder)


def make_dir_link(link, target) -> None:
    """Directory junction on Windows (no admin rights needed), symlink elsewhere."""
    import os
    import subprocess

    if os.name == "nt":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout).strip() or "mklink failed")
    else:
        os.symlink(target, link, target_is_directory=True)


def is_link(p) -> bool:
    """True for a symlink or a Windows junction/reparse point (never followed when copying, hashing or deleting)."""
    import os
    import stat

    try:
        st = os.lstat(p)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)
