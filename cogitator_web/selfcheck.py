"""`--self-check` for the windowed programs (cogitator-web.exe, Cogitator Setup.exe): no window opens, nothing is written but the log.

Windowed programs have no console (stdout may be None), so every line is also written to the log file."""
from __future__ import annotations

import io
import logging
import re
import sys
from pathlib import Path
from typing import Callable

from cogitator import __version__

log = logging.getLogger("cogitator.web.selfcheck")
PAGES = ("index.html", "setup.html", "uninstall.html")
ASSET_FILES = ("img/logo.png",)
STYLES = ("theme.css", "app.css", "setup.css", "uninstall.css")
_REF = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+\.(?:js|css))["']""")
_IMPORT = re.compile(r"""(?:^|\n)\s*(?:import|export)\b[^\n;]*?from\s*["'](\.[^"']+)["']|import\s*\(\s*["'](\.[^"']+)["']""")


def ui_problems(ui_dir: Path) -> list[str]:
    """Missing UI files: the pages, the stylesheets, and every script reachable from the pages (page tags and relative imports)."""
    missing: list[str] = []
    todo: list[Path] = []
    for name in (*PAGES, *STYLES, *ASSET_FILES):
        p = ui_dir / name
        if not p.is_file():
            missing.append(name)
        elif name in PAGES:
            todo += [ui_dir / r for r in _REF.findall(p.read_text(encoding="utf-8")) if r.endswith(".js")]
    seen: set[Path] = set()
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        if not p.is_file():
            missing.append(p.relative_to(ui_dir).as_posix() if ui_dir in p.parents else str(p))
            continue
        for a, b in _IMPORT.findall(p.read_text(encoding="utf-8")):
            todo.append(Path(__import__("os").path.normpath(p.parent / (a or b))))
    return missing


def run(expect_version: str | None = None, *, ui_dir: Path | None = None, webview2_check: Callable[[], bool] | None = None,
        say: Callable[[str], None] | None = None) -> int:
    from . import app

    ui_dir = ui_dir or app.UI_DIR
    problems: list[str] = []

    def emit(line: str) -> None:
        log.info("self-check %s", line)
        if say:
            say(line)
            return
        try:
            if sys.stdout is not None:
                print(line)
        except Exception:
            pass

    def step(label: str, fn: Callable[[], str | None]) -> None:
        try:
            detail = fn()
            emit(f"ok    {label}" + (f": {detail}" if detail else ""))
        except Exception as e:
            problems.append(label)
            emit(f"FAIL  {label}: {type(e).__name__}: {e}")

    def version():
        if expect_version and expect_version != __version__:
            raise RuntimeError(f"this build is {__version__}, expected {expect_version}")
        return __version__

    def ui():
        from . import winicon

        missing = ui_problems(ui_dir)
        if not winicon.ICON_PATH.is_file():
            missing.append("assets/cogitator.ico")
        if missing:
            raise FileNotFoundError("missing in " + str(ui_dir) + ": " + ", ".join(sorted(missing)))
        return f"{ui_dir}"

    def webview_mod():
        import webview

        if sys.platform == "win32":
            import webview.platforms.edgechromium  # noqa: F401  (loads pythonnet / clr_loader; opens nothing)
        return f"pywebview {getattr(webview, '__version__', '?')}"

    def sv():
        import sv_ttk  # noqa: F401

    def runtime():
        # Not a failure: without WebView2 (or pywebview) the tkinter windows open instead.
        ok = (webview2_check or app.webview2_available)()
        return "WebView2 runtime found" if ok else "WebView2 runtime NOT found (the classic tkinter windows will be used)"

    for label, fn in (("version", version), ("web ui files", ui), ("pywebview", webview_mod), ("sv_ttk", sv), ("webview2", runtime)):
        step(label, fn)
    emit(f"python {sys.version.split()[0]}, frozen={getattr(sys, 'frozen', False)}")
    return 1 if problems else 0


def run_setup(expect_version: str | None = None, **kw) -> int:
    """Setup program: the tkinter setup self-check (its output is captured into the log) plus the web checks."""
    from cogitator_setup.selfcheck import run as classic

    buf = io.StringIO()
    real, sys.stdout = sys.stdout, buf
    try:
        rc = classic()
    finally:
        sys.stdout = real
    for line in buf.getvalue().splitlines():
        log.info("self-check %s", line)
        try:
            if real is not None:
                print(line)
        except Exception:
            pass
    return 1 if (rc or run(expect_version, **kw)) else 0


def parse_expect(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a == "--expect-version" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--expect-version="):
            return a.split("=", 1)[1]
    return None
