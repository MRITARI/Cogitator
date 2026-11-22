"""Window creation, the WebView2 check, and the entry point.

`webview` is imported here, inside functions only, so importing this module (and the package) needs nothing installed."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable

from cogitator import __version__
from cogitator.config import log_path, redact

from . import winicon, winstate
from .api import Api
from .bridge import Bridge
from .session import Session

log = logging.getLogger("cogitator.web")

TITLE = "Cogitator"
UI_DIR = Path(__file__).resolve().parent / "ui"
BACKGROUND = "#09090b"  # the dark page colour: no white flash before the page paints
USAGE = ("usage: cogitator-web [--game-dir DIR] [--instance NAME] [--no-update-check] [--version]\n"
         "Opens the Cogitator window (needs the Microsoft Edge WebView2 runtime, which Windows 11 includes).\n"
         "All other commands: run cogitator in a terminal (cogitator --help).")

_WEBVIEW2_KEYS = ("{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",  # runtime
                  "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}", "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}", "{65C35B14-6C1D-4122-AC46-7148CC9D6497}")


def webview2_available() -> bool:
    """True when an Edge WebView2 runtime (or a Beta/Dev/Canary build) is registered. Same keys pywebview itself looks at."""
    if sys.platform != "win32":
        return False
    import winreg

    for key in _WEBVIEW2_KEYS:
        for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
                          (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients")):
            try:
                with winreg.OpenKey(hive, rf"{sub}\{key}") as k:
                    pv, _ = winreg.QueryValueEx(k, "pv")
                if isinstance(pv, str) and pv.split(".")[0].isdigit() and int(pv.split(".")[0]) >= 86:
                    return True
            except OSError:
                continue
    return False


WEBVIEW2_MISSING = ("The Microsoft Edge WebView2 runtime is required for the Cogitator window. Windows 11 includes it; if it was "
                    "removed, reinstall it from Microsoft (search for 'WebView2 Runtime').\n"
                    "The command line still works: cogitator --help")


def notify(message: str) -> None:
    """One clear message: a message box, and always stderr."""
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x30)
    except Exception:
        pass


def _screens(webview) -> list[tuple[int, int, int, int]]:
    try:
        return [(s.x, s.y, s.width, s.height) for s in webview.screens]
    except Exception:
        return []


def _first(result) -> str | None:
    return str(result[0]) if result else None


def run_web(argv: list[str], game_override: str | None = None, check_updates: bool = True, *,
            webview2_check: Callable[[], bool] = webview2_available) -> int:
    from cogitator.config import SchemaTooNew
    from cogitator.registry import check_data_versions

    try:
        check_data_versions()
    except SchemaTooNew as e:
        notify(str(e))
        return 2
    if not webview2_check():
        notify(WEBVIEW2_MISSING)
        return 4
    import webview

    bridge = Bridge()
    session = Session(bridge, game_override=game_override, check_updates=check_updates)
    geo = winstate.load(_screens(webview))
    page: dict = {"window": None}

    def on_ready() -> None:
        w = page["window"]
        bridge.attach(w)
        w.show()  # the page has painted: no flash of an empty window
        winicon.set_window_icon(w)

    api = Api(session, on_ready=on_ready)
    winicon.set_app_id()
    window = webview.create_window(f"{TITLE} {__version__}", url=str(UI_DIR / "index.html"), js_api=api, hidden=True,
                                   min_size=winstate.MIN_SIZE, background_color=BACKGROUND, text_select=True, **geo)
    page["window"] = window
    session.pick_folder = lambda: _first(window.create_file_dialog(webview.FileDialog.FOLDER))
    session.pick_image = lambda: _first(window.create_file_dialog(webview.FileDialog.OPEN, file_types=("Images (*.png;*.jpg;*.jpeg;*.webp)",)))
    session.pick_files = lambda start: window.create_file_dialog(
        webview.FileDialog.OPEN, directory=str(start), allow_multiple=True, file_types=("Zip archives (*.zip)", "All files (*.*)"))
    session.on_quit = lambda: window.destroy()
    seen = dict(geo)
    window.events.resized += lambda width, height: seen.update(width=width, height=height)
    window.events.moved += lambda x, y: seen.update(x=x, y=y)

    def closed() -> None:
        winstate.save(seen)
        session.release()

    window.events.closed += closed
    webview.start(gui="edgechromium", debug=False)
    session.release()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(__version__)
        return 0
    if "-h" in argv or "--help" in argv:
        print(USAGE)
        return 0
    if "--self-check" in argv:
        from cogitator.config import setup_logging
        from .selfcheck import parse_expect, run as self_check

        setup_logging()
        return self_check(parse_expect(argv))
    game_dir = None
    for i, a in enumerate(argv):
        if a == "--game-dir" and i + 1 < len(argv):
            game_dir = argv[i + 1]
        elif a.startswith("--game-dir="):
            game_dir = a.split("=", 1)[1]
    from cogitator.cli import _early_instance
    from cogitator.config import setup_logging

    try:
        _early_instance(argv)
        setup_logging()
        return run_web(argv, game_override=game_dir, check_updates="--no-update-check" not in argv)
    except Exception as e:
        logging.getLogger(__name__).exception("unhandled error")
        notify(f"Cogitator stopped because of an unexpected error: {redact(f'{type(e).__name__}: {e}')}\n"
               f"Details are in {log_path()}. Please include that file when you report the problem.")
        return 3
