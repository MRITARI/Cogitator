import subprocess
import sys

from cogitator import cli, selfcheck


def test_self_check_passes_in_process(capsys):
    assert selfcheck.run() == 0
    out = capsys.readouterr().out
    assert "ok    TUI" in out and "ok    TLS" in out and "ok    VDF parser" in out and "FAIL" not in out
    out.encode("ascii")  # must be printable on any console (the check exists to run on cp1252 / frozen builds)


def test_self_check_flag_and_failure_is_reported(capsys, monkeypatch):
    assert cli.main(["--self-check"]) == 0
    import cogitator.tui.app as app

    monkeypatch.setattr(app, "CogitatorApp", lambda **k: (_ for _ in ()).throw(RuntimeError("broken freeze")))
    assert selfcheck.run() == 1
    assert "FAIL  TUI: RuntimeError: broken freeze" in capsys.readouterr().out


def test_module_entry_point_matches_console_script():
    out = subprocess.run([sys.executable, "-I", "-m", "cogitator", "--version"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip() == "0.1.0"
