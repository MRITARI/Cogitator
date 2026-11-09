"""DML update: un-patch -> overwrite -> re-patch (only when mods are on, and only for DML)."""
import os

import pytest

from cogitator import fwupdate
from cogitator.frameworks import DML_REQUIRED, DMF_REQUIRED, plan_local
from cogitator.fwupdate import install_framework_plan, needs_repatch
from cogitator.patcher import OFF, ON, PatcherError, patch, patch_state
from cogitator.registry import Registry

from test_frameworks import DMF_FILES, DML_FILES, mkzip
from test_patcher import ORIG, FakeTool

NR = dict(is_running=lambda: False)


@pytest.fixture
def game(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "bundle" / "bundle_database.data").write_bytes(ORIG)
    for rel in DML_REQUIRED + ("tools/dtkit-patch.exe",):
        p = g / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"old")
    return g


def db(g):
    return (g / "bundle" / "bundle_database.data").read_bytes()


def test_dml_update_while_on_unpatches_installs_then_repatches(game, tmp_path):
    reg, tool = Registry.load(), FakeTool()
    patch(game, reg, runner=tool, **NR)
    plan = plan_local(mkzip(tmp_path / "Darktide Mod Loader 19 26.07.01 2026-07-01T00-00Z x.zip", DML_FILES))
    assert needs_repatch(plan, game)
    out = install_framework_plan(plan, game, reg, runner=tool, **NR)
    assert tool.calls == ["--patch", "--unpatch", "--patch"] and out.repatched
    assert patch_state(game) == ON and (game / "binaries" / "mod_loader").read_bytes() == DML_FILES["binaries/mod_loader"]
    assert reg.framework("dml")["version"] == "26.07.01"


def test_dml_update_while_off_does_not_patch(game, tmp_path):
    tool = FakeTool()
    out = install_framework_plan(plan_local(mkzip(tmp_path / "dml.zip", DML_FILES)), game, Registry.load(), runner=tool, **NR)
    assert tool.calls == [] and not out.repatched and patch_state(game) == OFF


def test_dmf_never_triggers_the_dance(game, tmp_path):
    reg, tool = Registry.load(), FakeTool()
    patch(game, reg, runner=tool, **NR)
    plan = plan_local(mkzip(tmp_path / "dmf.zip", DMF_FILES))
    assert not needs_repatch(plan, game)
    install_framework_plan(plan, game, reg, runner=tool, **NR)
    assert tool.calls == ["--patch"] and patch_state(game) == ON


def test_unpatch_refusal_aborts_before_any_file_is_touched(game, tmp_path):
    reg, tool = Registry.load(), FakeTool()
    patch(game, reg, runner=tool, **NR)
    bak = game / "bundle" / "bundle_database.data.bak"
    stale = reg.data["patch"]["last_patch_at"] - 3600
    os.utime(bak, (stale, stale))  # stale backup: un-patching is refused
    before = {p.relative_to(game): p.read_bytes() for p in game.rglob("*") if p.is_file()}
    with pytest.raises(PatcherError, match="older than Cogitator's last patch"):
        install_framework_plan(plan_local(mkzip(tmp_path / "dml.zip", DML_FILES)), game, reg, runner=tool, **NR)
    assert {p.relative_to(game): p.read_bytes() for p in game.rglob("*") if p.is_file()} == before
    assert tool.calls == ["--patch"]


def test_failed_install_restores_the_patch(game, tmp_path, monkeypatch):
    reg, tool = Registry.load(), FakeTool()
    patch(game, reg, runner=tool, **NR)
    monkeypatch.setattr(fwupdate, "install_plan", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        install_framework_plan(plan_local(mkzip(tmp_path / "dml.zip", DML_FILES)), game, reg, runner=tool, **NR)
    assert tool.calls == ["--patch", "--unpatch", "--patch"] and patch_state(game) == ON


def test_repatch_failure_is_reported_with_the_remedy(game, tmp_path):
    reg, tool = Registry.load(), FakeTool()
    patch(game, reg, runner=tool, **NR)
    plan = plan_local(mkzip(tmp_path / "dml.zip", DML_FILES))
    calls = {"n": 0}

    def flaky(args, cwd):
        calls["n"] += 1
        if args[1] == "--patch" and calls["n"] == 2:  # call 1 = --unpatch, call 2 = the re-patch after the update
            return 1, "", "locked"
        return tool(args, cwd)

    with pytest.raises(PatcherError, match="re-patching failed.*Press T"):
        install_framework_plan(plan, game, reg, runner=flaky, **NR)
    assert (game / "binaries" / "mod_loader").read_bytes() == DML_FILES["binaries/mod_loader"]  # the update itself stayed
    assert db(game) == ORIG  # database left clean, not half-patched
