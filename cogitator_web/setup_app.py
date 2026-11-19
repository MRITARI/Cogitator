"""Entry point of the web-style setup wizard: same flags, platform gate and error handling as the tkinter `cogitator_setup.app.main`.

The tkinter wizard stays the default. Uninstall, maintenance, repair-by-maintenance and `--self-check` are not on the web screens
yet, so those modes (and a missing WebView2 / pywebview) hand over to the tkinter `main` with the same argv."""
from __future__ import annotations

import logging
import sys
from typing import Callable

from cogitator.config import log_path, redact
from cogitator_setup import platform_gate
from cogitator_setup.app import TITLE, USAGE, choose_mode, parse_flags, setup_services, uninstall_services

from .app import BACKGROUND, UI_DIR, notify, webview2_available
from .bridge import Bridge
from .setup_session import SetupApi, WizardSession

SIZE = (720, 560)
_DETECT = object()


def _classic(argv: list[str]) -> int:
    from cogitator_setup.app import main as tk_main

    return tk_main(argv)


def run_web_setup(argv: list[str], flags, *, webview2_check: Callable[[], bool] = webview2_available,
                  fallback: Callable[[list[str]], int] = _classic) -> int:
    if not webview2_check():
        notify("The Microsoft Edge WebView2 runtime is not installed, so the web-style setup cannot open.\n\nOpening the classic setup window instead.")
        return fallback(argv)
    try:
        import webview
    except ImportError:
        notify("The 'pywebview' package is not installed, so the web-style setup cannot open.\n\nOpening the classic setup window instead.")
        return fallback(argv)
    bridge = Bridge()
    session = WizardSession(bridge, services=setup_services(flags.game_dir), dry_run=flags.dry_run, first_run=flags.wizard)
    holder: dict = {"window": None}

    def on_ready() -> None:
        w = holder["window"]
        bridge.attach(w)
        w.show()

    api = SetupApi(session, on_ready=on_ready)
    title = f"{TITLE} {session.model.version}" + (" - DRY RUN (nothing will be changed)" if flags.dry_run else "")
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


def main(argv: list[str] | None = None, *, build=_DETECT, show_error: Callable[[str, str], None] | None = None,
         installed: bool | None = None, runner: Callable[..., int] = run_web_setup) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    flags = parse_flags(argv)
    if flags.help:
        print(USAGE.replace("cogitator-setup", "cogitator-setup-web"))
        return 0
    if "--self-check" in argv:
        return _classic(argv)
    from cogitator_setup.app import _default_show_error

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
        from cogitator_setup.app import _repair_app_folder
        from cogitator_setup.uninstall_vm import detect

        _repair_app_folder()
        if installed is None:
            installed = detect(uninstall_services(flags.game_dir)).any_installed
        if choose_mode(flags, installed) != "setup":
            return _classic(argv)  # uninstall / maintenance screens: next phase
        return runner(argv, flags)
    except Exception as e:
        logging.getLogger(__name__).exception("unhandled error")
        err(TITLE, f"Setup stopped because of an unexpected error: {redact(f'{type(e).__name__}: {e}')}\nDetails are in {log_path()}.")
        return 3


if __name__ == "__main__":
    sys.exit(main())
