"""Entry point of the web-style setup wizard: same flags, platform gate and error handling as the tkinter `cogitator_setup.app.main`.

The tkinter wizard stays the default. `--self-check` and a missing WebView2 / pywebview hand over to the tkinter `main` with the same
argv. Uninstall and maintenance open the web uninstall window; Maintenance > Repair relaunches this program with the web-only
`--repair` flag (pywebview cannot reliably start twice in one process), which opens the wizard in repair mode."""
from __future__ import annotations

import logging
import subprocess
import sys
from typing import Callable

from cogitator.config import log_path, redact
from cogitator_setup import platform_gate
from cogitator_setup.entry_support import TITLE, USAGE, choose_mode, parse_flags, setup_services, uninstall_services

from .app import BACKGROUND, UI_DIR, notify, webview2_available
from . import winicon
from .bridge import Bridge
from .setup_session import SetupApi, WizardSession
from .uninstall_session import TITLE as UNINSTALL_TITLE, UninstallApi, UninstallSession

SIZE = (720, 560)
_DETECT = object()


def _classic(argv: list[str]) -> int:
    from cogitator_setup.app import main as tk_main

    return tk_main(argv)


def _need_webview(what: str, webview2_check: Callable[[], bool]):
    """The `webview` module, or None after telling the user why the classic window opens instead."""
    if not webview2_check():
        notify(f"The Microsoft Edge WebView2 runtime is not installed, so the web-style {what} cannot open.\n\nOpening the classic {what} window instead.")
        return None
    try:
        import webview
    except ImportError:
        notify(f"The 'pywebview' package is not installed, so the web-style {what} cannot open.\n\nOpening the classic {what} window instead.")
        return None
    return webview


def run_web_setup(argv: list[str], flags, *, webview2_check: Callable[[], bool] = webview2_available,
                  fallback: Callable[[list[str]], int] = _classic) -> int:
    webview = _need_webview("setup", webview2_check)
    if webview is None:
        return fallback(argv)
    repair = bool(getattr(flags, "repair", False))
    bridge = Bridge()
    session = WizardSession(bridge, services=setup_services(flags.game_dir), dry_run=flags.dry_run,
                            repair=repair, first_run=flags.wizard and not repair)
    holder: dict = {"window": None}

    def on_ready() -> None:
        w = holder["window"]
        bridge.attach(w)
        w.show()
        winicon.set_window_icon(w)

    api = SetupApi(session, on_ready=on_ready)
    winicon.set_app_id()
    title = f"{TITLE} {session.model.version}" + (" (repair)" if repair else "") + (" - DRY RUN (nothing will be changed)" if flags.dry_run else "")
    window = webview.create_window(title, url=str(UI_DIR / "setup.html"), js_api=api, hidden=True, width=SIZE[0], height=SIZE[1],
                                   resizable=False, background_color=BACKGROUND, text_select=True)
    holder["window"] = window

    def quit_now() -> None:
        session.closing_ok = True
        window.destroy()

    session.on_quit = quit_now
    session.pick_folder = lambda: (lambda r: str(r[0]) if r else None)(window.create_file_dialog(webview.FileDialog.FOLDER))
    session.pick_zip = lambda what: (lambda r: str(r[0]) if r else None)(
        window.create_file_dialog(webview.FileDialog.OPEN, file_types=("Zip files (*.zip)",)))
    import webbrowser

    session.open_url = webbrowser.open
    window.events.closing += session.on_close  # False keeps the window while a run is stopping
    webview.start(gui="edgechromium", debug=False)
    session.release()
    return 0


def repair_command(flags, *, frozen: bool | None = None, executable: str | None = None) -> list[str]:
    """The command that relaunches this program straight into the wizard in repair mode."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe = executable or sys.executable
    tail = ["--repair"] + (["--dry-run"] if flags.dry_run else []) + (["--game-dir", flags.game_dir] if flags.game_dir else [])
    return [exe, *tail] if frozen else [exe, "-m", "cogitator_web.setup_app", *tail]


def _spawn_detached(cmd: list[str]) -> None:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(cmd, close_fds=True, creationflags=flags)


def after_uninstall_window(action: str | None, flags, *, relauncher: Callable[[list[str]], None] = _spawn_detached,
                           frozen: bool | None = None, executable: str | None = None) -> None:
    """Maintenance > Repair: the window is gone, so start a fresh process for the wizard."""
    if action == "repair":
        relauncher(repair_command(flags, frozen=frozen, executable=executable))


def run_web_uninstall(argv: list[str], flags, start: str, *, webview2_check: Callable[[], bool] = webview2_available,
                      fallback: Callable[[list[str]], int] = _classic, relauncher: Callable[[list[str]], None] = _spawn_detached) -> int:
    webview = _need_webview("uninstaller", webview2_check)
    if webview is None:
        return fallback(argv)
    bridge = Bridge()
    session = UninstallSession(bridge, services=uninstall_services(flags.game_dir), dry_run=flags.dry_run, start=start)
    holder: dict = {"window": None}

    def on_ready() -> None:
        w = holder["window"]
        bridge.attach(w)
        w.show()
        winicon.set_window_icon(w)

    api = UninstallApi(session, on_ready=on_ready)
    winicon.set_app_id()
    title = f"{UNINSTALL_TITLE} {session.model.version}" + (" - DRY RUN (nothing will be changed)" if flags.dry_run else "")
    window = webview.create_window(title, url=str(UI_DIR / "uninstall.html"), js_api=api, hidden=True, width=SIZE[0], height=SIZE[1],
                                   resizable=False, background_color=BACKGROUND, text_select=True)
    holder["window"] = window
    session.on_quit = lambda: window.destroy()
    window.events.closing += session.on_close  # False keeps the window while a run is stopping
    webview.start(gui="edgechromium", debug=False)
    session.release()
    after_uninstall_window(session.action, flags, relauncher=relauncher)
    return 0


def main(argv: list[str] | None = None, *, build=_DETECT, show_error: Callable[[str, str], None] | None = None,
         installed: bool | None = None, runner: Callable[..., int] = run_web_setup,
         uninstall_runner: Callable[..., int] = run_web_uninstall, silent_fn: Callable[..., int] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repair = "--repair" in argv  # web-only: not known to the tkinter parser
    argv = [a for a in argv if a != "--repair"]
    flags = parse_flags(argv)
    flags.repair = repair
    if flags.help:
        print(USAGE.replace("cogitator-setup", "cogitator-setup-web"))
        return 0
    if "--self-check" in argv:
        from cogitator.config import setup_logging

        from .selfcheck import parse_expect, run_setup

        setup_logging()
        return run_setup(parse_expect(argv))
    from cogitator_setup.entry_support import _default_show_error

    err = show_error or _default_show_error
    b = platform_gate.windows_build() if build is _DETECT else build
    if not platform_gate.is_supported(b):
        err(TITLE, platform_gate.MESSAGE)
        return 1  # nothing has been created, read or changed
    from cogitator.config import SchemaTooNew
    from cogitator.registry import check_data_versions

    try:
        check_data_versions()
    except SchemaTooNew as e:
        err(TITLE, str(e))
        return 2
    try:
        from cogitator_setup.entry_support import _repair_app_folder
        from cogitator_setup.uninstall_vm import detect

        _repair_app_folder()
        if repair:
            return runner(argv, flags)  # repair = the wizard in repair mode, whatever is detected
        if installed is None:
            installed = detect(uninstall_services(flags.game_dir)).any_installed
        mode = choose_mode(flags, installed)
        if mode == "setup":
            return runner(argv, flags)
        if mode == "silent-uninstall":
            from cogitator_setup.silent import run_silent

            return (silent_fn or run_silent)(uninstall_services(flags.game_dir))
        return uninstall_runner(argv, flags, mode)  # "uninstall" | "maintenance"
    except Exception as e:
        logging.getLogger(__name__).exception("unhandled error")
        err(TITLE, f"Setup stopped because of an unexpected error: {redact(f'{type(e).__name__}: {e}')}\nDetails are in {log_path()}.")
        return 3


def entry(argv: list[str] | None = None) -> int:
    """Program entry (Cogitator Setup.exe, cogitator-setup): `--classic` forces the tkinter setup; it is stripped before parsing."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--classic" in argv:
        return _classic([a for a in argv if a != "--classic"])
    return main(argv)


if __name__ == "__main__":
    sys.exit(entry())
