"""Window/taskbar icon for the pywebview windows on Windows (pywebview's own `icon=` is GTK/Qt only). Never raises."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("cogitator.web.winicon")
ICON_PATH = Path(__file__).parent / "assets" / "cogitator.ico"
APP_ID = "MRITARI.Cogitator"
WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
SM_CXICON, SM_CXSMICON = 11, 49
_keep: list[Any] = []  # the icon handles must outlive the windows


def _hwnd(window: Any, user32: Any) -> int:
    try:
        h = int(window.native.Handle.ToInt32())
        if h:
            return h
    except Exception:
        pass
    return int(user32.FindWindowW(None, getattr(window, "title", "") or "") or 0)


def set_window_icon(window: Any, ico_path: Path | str | None = None, *, user32: Any = None, platform: str | None = None) -> bool:
    try:
        if (platform or sys.platform) != "win32":
            return False
        path = Path(ico_path or ICON_PATH)
        if not path.is_file():
            log.debug("icon file missing: %s", path)
            return False
        if user32 is None:
            import ctypes

            user32 = ctypes.windll.user32
        hwnd = _hwnd(window, user32)
        if not hwnd:
            log.debug("no window handle for the icon")
            return False
        small = user32.GetSystemMetrics(SM_CXSMICON) or 16
        big = user32.GetSystemMetrics(SM_CXICON) or 32
        for kind, size in ((ICON_SMALL, small), (ICON_BIG, big)):
            handle = user32.LoadImageW(None, str(path), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if handle:
                _keep.append(handle)
                user32.SendMessageW(hwnd, WM_SETICON, kind, handle)
        return True
    except Exception as e:
        log.debug("could not set the window icon: %s", e)
        return False


def set_app_id() -> None:
    """Source runs only: give the process its own taskbar identity so the window icon shows, not Python's. Frozen builds keep the default
    so pinned shortcuts still group with the running window."""
    try:
        if sys.platform == "win32" and not getattr(sys, "frozen", False):
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception as e:
        log.debug("could not set the app id: %s", e)
