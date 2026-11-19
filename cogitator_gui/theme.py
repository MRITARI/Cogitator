"""Look-and-feel helpers for the window: which theme to use, which fonts, the Windows title bar, the toolbar glyphs.

Nothing here needs a display. Functions that touch Windows (registry, DWM) are guarded and do nothing elsewhere or on failure."""
from __future__ import annotations

import sys
from typing import Callable, Iterable

CHOICES = ("system", "light", "dark")  # what the settings file may hold; "system" follows the Windows app theme
DEFAULT_CHOICE = "system"

_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def normalize_choice(value: object) -> str:
    """Whatever the settings file holds -> system | light | dark (system is the default)."""
    return value if isinstance(value, str) and value in CHOICES else DEFAULT_CHOICE


def system_uses_light(read: Callable[[], int] | None = None) -> bool:
    """True when Windows apps use the light theme. Any failure (not Windows, no key, bad value) -> light."""
    try:
        value = (read or _read_apps_use_light_theme)()
        return bool(value)
    except Exception:
        return True


def _read_apps_use_light_theme() -> int:
    if sys.platform != "win32":
        raise OSError("not Windows")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE) as key:
        return int(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])


def resolve_theme(choice: object, system_light: Callable[[], bool] | None = None) -> str:
    """system | light | dark (or junk) -> light | dark."""
    c = normalize_choice(choice)
    if c == "system":
        return "light" if (system_light or system_uses_light)() else "dark"
    return c


# ---------------------------------------------------------------- fonts
def first_available(wanted: Iterable[str], families: Iterable[str], default: str) -> str:
    have = {f.casefold(): f for f in families}
    for w in wanted:
        if w.casefold() in have:
            return have[w.casefold()]
    return default


def ui_family(families: Iterable[str]) -> str:
    return first_available(("Segoe UI Variable", "Segoe UI Variable Text"), families, "Segoe UI")


def heading_family(families: Iterable[str]) -> str | None:
    """Tk knows only normal/bold, so semibold is a separate family; None = use bold of the UI font."""
    fams = list(families)
    f = first_available(("Segoe UI Semibold",), fams, "")
    return f or None


def mono_family(families: Iterable[str]) -> str:
    return first_available(("Cascadia Mono", "Cascadia Code"), families, "Consolas")


def icon_family(families: Iterable[str]) -> str | None:
    """The symbol font for toolbar glyphs, or None (then the buttons show text only)."""
    f = first_available(("Segoe Fluent Icons", "Segoe MDL2 Assets"), families, "")
    return f or None


BASE_FONT_SIZE = 10
ROW_HEIGHT_PX = 32


def scaled(px: int, dpi: float) -> int:
    """Pixels designed for 96 dpi -> pixels on this display."""
    return max(1, round(px * dpi / 96.0))


# The glyph for each toolbar button (Segoe Fluent Icons / Segoe MDL2 Assets code points). Change them here.
TOOLBAR_ICONS = {
    "toggle": "",    # power
    "up": "",        # up arrow
    "down": "",      # down arrow
    "recheck": "",   # refresh
    "import": "",    # download
    "launch": "",    # play
}
ACCENT_BUTTONS = ("recheck", "launch")  # the primary actions use Accent.TButton


def button_text(key: str, label: str, has_icons: bool) -> str:
    glyph = TOOLBAR_ICONS.get(key)
    return f"{glyph}  {label}" if has_icons and glyph else label


# ---------------------------------------------------------------- Windows title bar
_current = {"dark": False}


def is_dark() -> bool:
    return _current["dark"]


def set_titlebar_dark(window, dark: bool) -> bool:
    """Ask Windows 10/11 to draw the title bar of this Tk window dark (attribute 20, older builds 19). True if it was accepted."""
    _current["dark"] = bool(dark)
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        window.update_idletasks()
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (19 before Windows 10 build 18985)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                return True
    except Exception:
        pass
    return False
