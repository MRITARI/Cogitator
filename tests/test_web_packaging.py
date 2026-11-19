"""W7: self-check of the windowed programs, entry routing (--classic)."""
from pathlib import Path

import pytest

from cogitator import __version__
from cogitator_web import app, selfcheck, setup_app


def _run(**kw):
    lines = []
    rc = selfcheck.run(say=lines.append, webview2_check=kw.pop("webview2_check", lambda: True), **kw)
    return rc, lines


def test_selfcheck_passes_on_the_real_ui():
    rc, lines = _run(expect_version=__version__)
    assert rc == 0, lines


def test_selfcheck_wrong_version_fails():
    rc, lines = _run(expect_version="0.0.0-nope")
    assert rc == 1 and any(l.startswith("FAIL  version") for l in lines)


def test_selfcheck_missing_ui_file_fails(tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    for f in app.UI_DIR.rglob("*"):
        if f.is_file():
            t = ui / f.relative_to(app.UI_DIR)
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(f.read_bytes())
    assert selfcheck.ui_problems(ui) == []
    (ui / "js" / "dom.js").unlink()  # only reachable through imports
    assert "js/dom.js" in selfcheck.ui_problems(ui)
    (ui / "setup.css").unlink()
    assert "setup.css" in selfcheck.ui_problems(ui)
    rc, lines = _run(ui_dir=ui)
    assert rc == 1 and any("web ui files" in l and l.startswith("FAIL") for l in lines)


def test_selfcheck_missing_webview2_is_only_logged(caplog):
    caplog.set_level("INFO", logger="cogitator.web.selfcheck")
    rc, lines = _run(webview2_check=lambda: False)
    assert rc == 0 and any("NOT found" in l for l in lines)
    assert any("NOT found" in r.getMessage() for r in caplog.records)  # reaches the log file


def test_selfcheck_works_without_stdout(monkeypatch):
    monkeypatch.setattr("sys.stdout", None)
    assert selfcheck.run(webview2_check=lambda: True) == 0


def test_web_main_self_check_routes(monkeypatch):
    got = []
    monkeypatch.setattr(selfcheck, "run", lambda expect=None, **kw: got.append(expect) or 0)
    monkeypatch.setattr("cogitator.config.setup_logging", lambda *a, **k: None)
    assert app.main(["--self-check", "--expect-version", "1.2"]) == 0 and got == ["1.2"]


def test_entry_classic_goes_to_tkinter_without_the_flag(monkeypatch):
    seen = []
    monkeypatch.setattr(setup_app, "_classic", lambda argv: seen.append(("tk", argv)) or 0)
    monkeypatch.setattr(setup_app, "main", lambda argv: seen.append(("web", argv)) or 0)
    assert setup_app.entry(["--classic", "--wizard"]) == 0 and seen[-1] == ("tk", ["--wizard"])
    assert setup_app.entry(["--wizard"]) == 0 and seen[-1] == ("web", ["--wizard"])
