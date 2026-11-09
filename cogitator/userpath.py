"""`cogitator path add|remove|status`: the per-user PATH entry (HKCU\\Environment, no admin).

The value is read raw (no %VAR% expansion) and written back with its original registry type. Entries are compared
whole, case-insensitively, ignoring trailing backslashes, with %VAR% spellings expanded for the comparison only.
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import Callable

ENV_KEY = "Environment"
BAD_SUFFIX = "\\programs\\cogitatorpp"  # 0.1.6 wrote "...\\Cogitator" + BEL (a mangled "\a") + "pp"; add_entry() removes it


def _winreg():
    import winreg

    return winreg


def _norm(entry: str) -> str:
    s = os.path.expandvars(entry.strip().strip('"').strip())
    return s.replace("/", "\\").rstrip("\\ ").casefold()


def _items(value: str) -> list[str]:
    return [i.strip() for i in value.split(";") if i.strip()]


def read_path(key_path: str = ENV_KEY) -> tuple[str | None, int | None]:
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            value, kind = winreg.QueryValueEx(k, "Path")
    except FileNotFoundError:
        return None, None
    return (value if isinstance(value, str) else None), kind


def _write_path(value: str, kind: int, key_path: str) -> None:
    winreg = _winreg()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "Path", 0, kind, value)


def broadcast_environment_change() -> None:
    """Tell running programs (Explorer, new terminals) that the environment changed. Short timeout, never raises."""
    try:
        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p, ctypes.c_uint,
                         ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t)]
        send.restype = ctypes.c_ssize_t
        result = ctypes.c_size_t()
        # HWND_BROADCAST, WM_SETTINGCHANGE, wParam 0, "Environment", SMTO_ABORTIFHUNG, 1.5 s
        send(0xFFFF, 0x001A, 0, "Environment", 0x0002, 1500, ctypes.byref(result))
    except Exception:
        pass


def default_dir() -> str | None:
    return str(Path(sys.executable).parent) if getattr(sys, "frozen", False) else None


def has_entry(directory: str, key_path: str = ENV_KEY) -> bool:
    value, _ = read_path(key_path)
    want = _norm(directory)
    return any(_norm(i) == want for i in _items(value or ""))


def add_entry(directory: str, key_path: str = ENV_KEY,
              broadcast: Callable[[], None] | None = None) -> bool:
    """Append `directory` once and drop the known-bad entry. Returns True if the registry was changed."""
    winreg = _winreg()
    value, kind = read_path(key_path)
    items = _items(value or "")
    kept = [i for i in items if not _norm(i).replace("\x07", "").endswith(BAD_SUFFIX)]
    if not any(_norm(i) == _norm(directory) for i in kept):
        kept.append(directory.strip())
    if kept == items and value is not None:
        return False
    _write_path(";".join(kept), kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_EXPAND_SZ, key_path)
    (broadcast or broadcast_environment_change)()
    return True


def remove_entry(directory: str, key_path: str = ENV_KEY,
                 broadcast: Callable[[], None] | None = None) -> bool:
    """Delete every entry equal to `directory` and nothing else. Returns True if the registry was changed."""
    value, kind = read_path(key_path)
    if value is None:
        return False
    items = _items(value)
    kept = [i for i in items if _norm(i) != _norm(directory)]
    if len(kept) == len(items):
        return False
    _write_path(";".join(kept), kind, key_path)
    (broadcast or broadcast_environment_change)()
    return True
