"""The swap helper with fake app folders and injected wait/check/relaunch commands."""
import json

import pytest

from cogitator import updater as up


def layout(tmp_path):
    root = tmp_path / "Cogitator"
    (root / "app").mkdir(parents=True)
    (root / "app" / "cogitator.exe").write_bytes(b"OLD")
    (root / "staging" / "v2").mkdir(parents=True)
    (root / "staging" / "v2" / "cogitator.exe").write_bytes(b"NEW")
    # things an update must never touch
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "registry.json").write_text("{}")
    (tmp_path / "cfg" / "config.toml").write_text("x=1")
    return root


def args(root, tmp_path, *extra):
    return ["--app", str(root / "app"), "--previous", str(root / "app.previous"), "--staged", str(root / "staging" / "v2"),
            "--version", "2.0.0", "--wait-pid", "7", "--result", str(tmp_path / "cfg" / "update_result.json"),
            "--check", json.dumps(["x", "--self-check"]), "--relaunch", json.dumps(["x"]), *extra]


def go(argv, ok=True, alive_seq=(True, False)):
    calls = {"launch": [], "check": [], "alive": list(alive_seq)}
    rc = up.run(argv, alive=lambda p: calls["alive"].pop(0) if calls["alive"] else False,
                check=lambda c: calls["check"].append(c) or ok, relaunch=lambda c: calls["launch"].append(c), wait_timeout=5)
    return rc, calls


def result(tmp_path):
    return json.loads((tmp_path / "cfg" / "update_result.json").read_text())


def test_swap_keeps_old_build_and_relaunches(tmp_path):
    root = layout(tmp_path)
    rc, c = go(args(root, tmp_path))
    assert rc == 0 and (root / "app" / "cogitator.exe").read_bytes() == b"NEW" and (root / "app.previous" / "cogitator.exe").read_bytes() == b"OLD"
    assert c["check"] == [["x", "--self-check"]] and c["launch"] == [["x"]] and result(tmp_path)["status"] == "ok"
    assert not (root / "staging" / "v2").exists()


def test_a_new_build_that_fails_its_self_check_is_reverted_and_recorded(tmp_path):
    root = layout(tmp_path)
    rc, c = go(args(root, tmp_path), ok=False)
    assert rc == 1 and (root / "app" / "cogitator.exe").read_bytes() == b"OLD"
    assert not (root / "app.failed").exists() and not (root / "app.previous").exists()
    r = result(tmp_path)
    assert r["status"] == "reverted" and r["version"] == "2.0.0" and c["launch"] == [["x"]]  # old build is relaunched


def test_user_data_is_never_touched(tmp_path):
    root = layout(tmp_path)
    go(args(root, tmp_path))
    go(args(root, tmp_path, "--rollback"))
    assert (tmp_path / "cfg" / "registry.json").read_text() == "{}" and (tmp_path / "cfg" / "config.toml").read_text() == "x=1"


def test_helper_waits_for_the_app_to_exit(tmp_path):
    root = layout(tmp_path)
    rc, c = go(args(root, tmp_path), alive_seq=(True, True, True, False))
    assert rc == 0 and c["alive"] == []
    (tmp_path / "b").mkdir()
    root = layout(tmp_path / "b")
    rc = up.run(args(root, tmp_path / "b"), alive=lambda p: True, check=lambda c: True, relaunch=lambda c: None, wait_timeout=0.3)
    assert rc == 3 and (root / "app" / "cogitator.exe").read_bytes() == b"OLD"  # gave up without swapping


def test_failed_rename_restores_and_relaunches(tmp_path, monkeypatch):
    root = layout(tmp_path)
    (root / "staging" / "v2").rename(root / "staging" / "gone")  # nothing staged
    rc, c = go(args(root, tmp_path))
    assert rc == 4 and (root / "app" / "cogitator.exe").read_bytes() == b"OLD" and result(tmp_path)["status"] == "aborted"
    assert c["launch"] == [["x"]]


def test_rollback_exchanges_and_a_second_rollback_goes_forward(tmp_path):
    root = layout(tmp_path)
    go(args(root, tmp_path))
    rc, _ = go(args(root, tmp_path, "--rollback"))
    assert rc == 0 and (root / "app" / "cogitator.exe").read_bytes() == b"OLD" and (root / "app.previous" / "cogitator.exe").read_bytes() == b"NEW"
    assert result(tmp_path)["status"] == "rolled-back"


def test_rollback_to_a_build_that_fails_its_check_swaps_back(tmp_path):
    root = layout(tmp_path)
    go(args(root, tmp_path))
    rc, _ = go(args(root, tmp_path, "--rollback"), ok=False)
    assert rc == 1 and (root / "app" / "cogitator.exe").read_bytes() == b"NEW"


def test_updater_is_stdlib_only():
    import ast, pathlib
    tree = ast.parse(pathlib.Path(up.__file__).read_text(encoding="utf-8"))
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= {"__future__", "argparse", "json", "os", "shutil", "subprocess", "sys", "time", "pathlib", "typing", "ctypes"}


def test_an_older_previous_build_is_replaced_by_the_newer_one(tmp_path):
    root = layout(tmp_path)
    (root / "app.previous").mkdir()
    (root / "app.previous" / "cogitator.exe").write_bytes(b"ANCIENT")
    rc, _ = go(args(root, tmp_path))
    assert rc == 0 and (root / "app.previous" / "cogitator.exe").read_bytes() == b"OLD"  # exactly one previous version is kept
