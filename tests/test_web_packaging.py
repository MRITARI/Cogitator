"""W7: self-check of the windowed programs, entry routing."""
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


def test_entry_goes_to_main(monkeypatch):
    seen = []
    monkeypatch.setattr(setup_app, "main", lambda argv: seen.append(argv) or 0)
    assert setup_app.entry(["--wizard"]) == 0 and seen == [["--wizard"]]


# ---------------------------------------------------------------- launch targets (setup "Start Cogitator")

def _installed(tmp_path, web=True):
    (tmp_path / "setup").mkdir()
    (tmp_path / "app").mkdir()
    if web:
        (tmp_path / "app" / "cogitator-web.exe").write_bytes(b"x")
    return str(tmp_path / "setup" / "Cogitator Setup.exe")


def test_launch_target_installed_source_and_missing(tmp_path):
    from cogitator_web import launch

    exe = _installed(tmp_path)
    assert launch.web_target(True, exe) == [str(tmp_path / "app" / "cogitator-web.exe")]
    assert launch.web_target(False, "py.exe") == ["py.exe", "-m", "cogitator_web"]
    assert launch.web_target(True, str(tmp_path / "nowhere" / "setup" / "S.exe")) is None


def test_launch_app_detached_or_says_it_is_missing(tmp_path, capsys):
    from cogitator_web import launch

    got = []
    argv = launch.launch_app(frozen=True, executable=_installed(tmp_path), popen=lambda a, **k: got.append((a, k)), args=["--no-update-check"])
    assert argv and argv[-1] == "--no-update-check" and got[0][0] == argv and "creationflags" in got[0][1]
    got.clear()
    assert launch.launch_app(frozen=True, executable=str(tmp_path / "x" / "setup" / "S.exe"), popen=lambda a, **k: got.append(a)) is None
    assert not got and "cogitator --help" in capsys.readouterr().err


def test_bare_cogitator_opens_the_window_and_forwards_flags(monkeypatch):
    from cogitator import cli
    from cogitator_web import launch

    seen = []
    monkeypatch.setattr(launch, "launch_app", lambda **k: seen.append(k) or ["x"])
    assert cli.main(["--game-dir", "G", "--no-update-check"]) == 0
    assert seen == [{"args": ["--game-dir", str(Path("G").resolve()), "--no-update-check"]}]
    monkeypatch.setattr(launch, "launch_app", lambda **k: None)
    assert cli.main([]) == 1


def test_web_target_finds_a_side_by_side_exe(tmp_path):
    from cogitator_web import launch

    (tmp_path / "cogitator-web.exe").write_bytes(b"")
    assert launch.web_target(True, str(tmp_path / "cogitator.exe")) == [str(tmp_path / "cogitator-web.exe")]


def test_setup_start_button_uses_the_launch_helper(monkeypatch):
    from cogitator_web import launch, setup_session

    seen = []
    monkeypatch.setattr(launch, "launch_app", lambda **k: seen.append(1))
    setup_session._launch_cogitator()
    assert seen == [1]


# ---------------------------------------------------------------- nxm click

def test_nxm_click_starts_the_web_exe_when_present(tmp_path, monkeypatch):
    from cogitator import nxm

    (tmp_path / "cogitator.exe").write_bytes(b"x")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(tmp_path / "cogitator-nxm.exe"))
    assert nxm._spawn_command() == [str(tmp_path / "cogitator.exe")]  # older install: unchanged
    (tmp_path / "cogitator-web.exe").write_bytes(b"x")
    assert nxm._spawn_command() == [str(tmp_path / "cogitator-web.exe")]
    monkeypatch.setattr(nxm, "enqueue", lambda url: None)
    calls = []
    nxm.handle_click("nxm://x", popen=lambda c, **k: calls.append((c, k)), running=lambda: False)
    assert calls[0][0][0].endswith("cogitator-web.exe") and calls[0][1]["creationflags"] == 0  # no console for a windowed program
    calls.clear()
    nxm.handle_click("nxm://x", popen=lambda c, **k: calls.append(c), running=lambda: True)
    assert calls == []  # a window is open: it picks the link up, no second instance


# ---------------------------------------------------------------- update restart target

def _updater_with(tmp_path, starter, web=True):
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from test_selfupdate import FakeGitHub, updater
    from cogitator.selfupdate import Layout

    root = tmp_path / "inst"
    (root / "app").mkdir(parents=True)
    (root / "app" / "cogitator.exe").write_bytes(b"x")
    if web:
        (root / "app" / "cogitator-web.exe").write_bytes(b"x")
    u = updater(FakeGitHub(), root, layout=Layout("installer", root, helper=["H"]))
    u.starter = starter
    return u, root / "app"


def test_update_restarts_the_program_that_started_it(tmp_path):
    import json

    from cogitator.selfupdate import REQUIRED_EXE, WEB_EXE

    u, app = _updater_with(tmp_path, WEB_EXE)
    a = u._common_args("1.2.3")
    assert json.loads(a[a.index("--relaunch") + 1]) == [str(app / WEB_EXE)]
    checks = json.loads(a[a.index("--check") + 1])
    assert [c[0] for c in checks] == [str(app / REQUIRED_EXE), str(app / WEB_EXE)] and all("--expect-version" in c for c in checks)
    u, app = _updater_with(tmp_path / "b", REQUIRED_EXE)
    a = u._common_args("1.2.3")
    assert json.loads(a[a.index("--relaunch") + 1]) == [str(app / REQUIRED_EXE)]
    assert json.loads(a[a.index("--check") + 1])[0] == str(app / REQUIRED_EXE)
    u, app = _updater_with(tmp_path / "c", WEB_EXE, web=False)  # web exe vanished: console as before
    assert json.loads((lambda x: x[x.index("--relaunch") + 1])(u._common_args("1.2.3"))) == [str(app / REQUIRED_EXE)]


def test_starter_exe_follows_the_running_program(monkeypatch):
    from cogitator import selfupdate

    assert selfupdate.starter_exe() == "cogitator.exe"  # source run
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", r"C:\x\app\Cogitator-Web.exe")
    assert selfupdate.starter_exe() == "cogitator-web.exe"
    monkeypatch.setattr("sys.executable", r"C:\x\setup\Cogitator Setup.exe")
    assert selfupdate.starter_exe() == "cogitator.exe"


def test_helper_launches_web_exe_without_a_terminal_and_runs_all_checks(monkeypatch):
    from cogitator import updater

    seen = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda argv, **k: seen.append((argv, k.get("creationflags"))))
    monkeypatch.setattr(updater.os, "name", "nt")
    monkeypatch.setattr(updater.shutil, "which", lambda n: r"C:\wt.exe")
    updater._launch([r"C:\a\cogitator-web.exe"])
    assert seen[-1][0] == [r"C:\a\cogitator-web.exe"] and seen[-1][1] == 0x208
    updater._launch([r"C:\a\cogitator.exe"])
    assert seen[-1][0][0] == r"C:\wt.exe"  # the console program still gets Windows Terminal
    runs = []
    monkeypatch.setattr(updater.subprocess, "run", lambda argv, **k: runs.append(argv) or type("R", (), {"returncode": 0 if "ok" in argv[0] else 1})())
    assert updater._check([["ok1", "--self-check"], ["ok2", "--self-check"]]) is True
    assert updater._check([["ok1"], ["bad"]]) is False


def test_rollback_to_a_build_without_the_web_exe_uses_the_console_exe(tmp_path):
    import json

    from cogitator.selfupdate import REQUIRED_EXE, WEB_EXE

    u, app = _updater_with(tmp_path, WEB_EXE)
    prev = app.parent / "app.previous"
    prev.mkdir()
    (prev / REQUIRED_EXE).write_bytes(b"x")  # an older build: no cogitator-web.exe
    a = u._common_args("previous")
    assert json.loads(a[a.index("--relaunch") + 1]) == [str(app / REQUIRED_EXE)]
    assert json.loads(a[a.index("--check") + 1]) == [str(app / REQUIRED_EXE), "--self-check"]
    (prev / WEB_EXE).write_bytes(b"x")  # a previous build that has it: web stays web
    a = u._common_args("previous")
    assert json.loads(a[a.index("--relaunch") + 1]) == [str(app / WEB_EXE)]
