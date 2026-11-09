"""Uninstall: user mods and mod_load_order.txt survive; un-patch is verified BEFORE any DML file is deleted; abort deletes nothing."""
import os
import shutil
import threading
from pathlib import Path

import pytest

from cogitator import cli, config as cfg
from cogitator import uninstall as un
from cogitator.config import get_api_key, set_api_key
from cogitator.frameworks import install_plan, plan_local
from cogitator.loadorder import LoadOrder
from cogitator.patcher import OFF, ON, bak_path, db_path, patch, patch_state
from cogitator.registry import Registry
from cogitator.uninstall import UninstallPlan, load_order_removable, owned_items, uninstall_framework

from test_frameworks import DMF_FILES, DML_FILES, mkzip
from test_patcher import FakeTool, ORIG

NR = dict(is_running=lambda: False)
USER_ORDER = b"-- my list\r\nusermod\r\n--othermod\r\n"


def tree(g: Path) -> dict:
    return {p.relative_to(g).as_posix(): p.read_bytes() for p in sorted(g.rglob("*")) if p.is_file()}


def dirs(g: Path) -> set:
    return {p.relative_to(g).as_posix() for p in g.rglob("*") if p.is_dir()}


@pytest.fixture
def installed(tmp_path):
    """A fake game with DML + DMF installed by the real installer (so a manifest exists), mods ON, and user data."""
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "binaries" / "Darktide.exe").write_bytes(b"GAME")
    (g / "bundle" / "bundle_database.data").write_bytes(ORIG)
    for m in ("usermod", "othermod"):
        (g / "mods" / m).mkdir(parents=True)
        (g / "mods" / m / f"{m}.mod").write_text("return {}")
        (g / "mods" / m / "data.lua").write_text("mine")
    (g / "mods" / "mod_load_order.txt").write_bytes(USER_ORDER)
    reg, tool = Registry.load(), FakeTool()
    z = tmp_path / "z"
    z.mkdir()
    install_plan(plan_local(mkzip(z / "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z a.zip", DML_FILES)), g, reg, **NR)
    install_plan(plan_local(mkzip(z / "Darktide Mod Framework 8 26.10.22 2026-10-23T02-11Z b.zip", DMF_FILES)), g, reg, **NR)
    patch(g, reg, runner=tool, **NR)
    return g, reg, tool


def run(g, reg, tool, **plan_kw):
    return uninstall_framework(UninstallPlan(g, **plan_kw), registry=reg, runner=tool, **NR)


DML_PATHS = ["binaries/mod_loader", "bundle/9ba626afa44a3aa3.patch_999", "toggle_darktide_mods.bat", "tools/dtkit-patch.exe",
             "tools/README.md", "mods/base/mod_manager.lua", "mods/base/function/class.lua"]


# ------------------------------------------------------------ default uninstall

def test_default_removes_dml_and_dmf_and_returns_the_game_to_vanilla(installed):
    g, reg, tool = installed
    r = run(g, reg, tool)
    assert r.ok and r.unpatched and not r.warnings and not r.kept
    assert patch_state(g) == OFF and db_path(g).read_bytes() == ORIG  # un-patched and verified
    for rel in DML_PATHS:
        assert not (g / rel).exists(), rel
    assert not (g / "mods" / "dmf").exists() and not (g / "mods" / "base").exists()
    # survivors: user mods, the user's load order, the game's own files, and the mods/ folder itself
    assert (g / "mods" / "mod_load_order.txt").read_bytes() == USER_ORDER
    assert (g / "mods" / "usermod" / "data.lua").read_text() == "mine" and (g / "mods" / "othermod" / "othermod.mod").is_file()
    assert (g / "binaries" / "Darktide.exe").read_bytes() == b"GAME" and (g / "bundle").is_dir() and (g / "mods").is_dir()
    assert not (g / "tools").exists()  # empty afterwards -> removed
    assert (g / "binaries").is_dir()  # not empty (the game's exe) -> kept
    assert reg.framework("dml") is None and reg.manifest("dml") is None and reg.framework("dmf") is None and "patch" not in reg.data
    assert Registry.load().data == reg.data  # persisted


def test_mods_dir_is_never_deleted_wholesale(installed):
    g, reg, tool = installed
    run(g, reg, tool)
    assert {"mods/usermod", "mods/othermod"} <= dirs(g) and "mods" in dirs(g)


def test_unpatch_happens_and_is_verified_before_any_dml_file_is_deleted(installed):
    g, reg, tool = installed
    seen = {}

    def spying_runner(args, cwd):
        if args[1] == "--unpatch":  # at this exact moment nothing DML-owned may have been touched yet
            seen["files_at_unpatch"] = all((g / rel).exists() for rel in DML_PATHS)
        rc = tool(args, cwd)
        if args[1] == "--unpatch":
            seen["state_after"] = patch_state(g)
        return rc

    r = uninstall_framework(UninstallPlan(g), registry=reg, runner=spying_runner, **NR)
    assert r.ok and seen == {"files_at_unpatch": True, "state_after": OFF}
    assert not (g / "binaries" / "mod_loader").exists()


# ------------------------------------------------------------ abort = nothing deleted

def test_unpatch_failure_aborts_with_nothing_removed(installed):
    g, reg, tool = installed

    def failing(args, cwd):
        return (1, "", "locked") if args[1] == "--unpatch" else tool(args, cwd)

    before = tree(g)
    r = uninstall_framework(UninstallPlan(g), registry=reg, runner=failing, **NR)
    assert not r.ok and r.failed_step == "mods_off" and tree(g) == before and patch_state(g) == ON
    assert "Verify integrity of game files" in r.fix and "Nothing has been deleted" in r.fix and "verified copy" in r.fix
    assert reg.framework("dml") and reg.manifest("dmf")  # registry untouched too


def test_stale_bak_means_refusal_and_nothing_removed(installed):
    g, reg, tool = installed
    stale = reg.data["patch"]["last_patch_at"] - 3600
    os.utime(bak_path(g), (stale, stale))
    before = tree(g)
    r = run(g, reg, tool)
    assert not r.ok and "older than Cogitator's last patch" in r.what and tree(g) == before and tool.calls == ["--patch"]


@pytest.mark.parametrize("db", ["missing", "empty"])
def test_unknown_state_aborts_before_deleting_anything(installed, db):
    g, reg, tool = installed
    p = db_path(g)
    p.unlink() if db == "missing" else p.write_bytes(b"")
    before = tree(g)
    r = run(g, reg, tool)
    assert not r.ok and "cannot tell whether mods are switched on" in r.what and tree(g) == before
    assert "Nothing has been deleted" in r.fix


def test_uninstall_re_checks_the_byte_search_itself_even_if_unpatch_claims_success(installed, monkeypatch):
    """Defence in depth: the patcher verifies internally, but deletion must not rely on that alone."""
    g, reg, tool = installed
    monkeypatch.setattr(un, "unpatch", lambda *a, **k: None)  # claims success, changes nothing
    before = tree(g)
    r = run(g, reg, tool)
    assert not r.ok and r.failed_step == "mods_off" and tree(g) == before and patch_state(g) == ON
    assert "still reports mods as switched on" in r.what and "Nothing has been deleted" in r.fix


def test_a_tool_that_lies_is_caught_by_the_patcher_before_the_uninstaller(installed):
    g, reg, tool = installed

    def liar(args, cwd):  # reports success but leaves the database patched
        return (0, "", "successfully unpatched") if args[1] == "--unpatch" else tool(args, cwd)

    before = tree(g)
    r = uninstall_framework(UninstallPlan(g), registry=reg, runner=liar, **NR)
    assert not r.ok and tree(g) == before and "expected original database" in r.what


def test_refuses_while_the_game_runs(installed):
    g, reg, tool = installed
    before = tree(g)
    r = uninstall_framework(UninstallPlan(g), registry=reg, runner=tool, is_running=lambda: True)
    assert not r.ok and "Darktide is running" in r.what and "Retry" in r.fix and tree(g) == before and tool.calls == ["--patch"]


# ------------------------------------------------------------ already vanilla, partial, idempotent

def test_already_unpatched_skips_unpatching(installed):
    g, reg, tool = installed
    from cogitator.patcher import unpatch
    unpatch(g, reg, runner=tool, **NR)
    tool.calls.clear()
    r = run(g, reg, tool)
    assert r.ok and not r.unpatched and tool.calls == [] and not (g / "binaries" / "mod_loader").exists()


def test_dmf_only_never_touches_the_patch(installed):
    g, reg, tool = installed
    r = run(g, reg, tool, remove_dml=False)
    assert r.ok and not r.unpatched and tool.calls == ["--patch"] and patch_state(g) == ON
    assert not (g / "mods" / "dmf").exists() and (g / "binaries" / "mod_loader").is_file()
    assert reg.framework("dml") and reg.framework("dmf") is None


def test_rerun_is_a_harmless_noop(installed):
    g, reg, tool = installed
    assert run(g, reg, tool).ok
    after = tree(g)
    r2 = run(g, reg, tool)
    assert r2.ok and r2.removed == [] and tree(g) == after


def test_recovers_after_a_partial_earlier_run(installed):
    g, reg, tool = installed
    (g / "mods" / "dmf" / "dmf.mod").unlink()  # some files already removed by hand
    (g / "toggle_darktide_mods.bat").unlink()
    r = run(g, reg, tool)
    assert r.ok and not (g / "mods" / "dmf").exists() and not (g / "binaries" / "mod_loader").exists()


def test_if_the_patch_tool_is_gone_while_mods_are_on_it_stops_and_deletes_nothing(installed):
    """A hand-deleted tools/ folder means the guarded patcher cannot un-patch; the safe answer is to stop."""
    g, reg, tool = installed
    shutil.rmtree(g / "tools")
    before = tree(g)
    r = run(g, reg, tool)
    assert not r.ok and r.failed_step == "mods_off" and tree(g) == before and patch_state(g) == ON
    assert "Verify integrity of game files" in r.fix


# ------------------------------------------------------------ manifest vs fallback, modified files

def test_manifest_install_keeps_files_it_did_not_place(installed):
    g, reg, tool = installed
    (g / "mods" / "base" / "my_notes.txt").write_text("not ours")
    r = run(g, reg, tool)
    assert r.ok and (g / "mods" / "base" / "my_notes.txt").read_text() == "not ours"
    assert not (g / "mods" / "base" / "mod_manager.lua").exists()
    assert any("mods/base still contains files" in w for w in r.warnings)


def test_fallback_without_a_manifest_uses_the_verified_marker_set(installed):
    g, reg, tool = installed
    reg.drop_manifest("dml")
    reg.drop_manifest("dmf")
    items, manifest = owned_items("dml", reg)
    assert not manifest and {i.rel for i in items} == set(un.DML_OWNED_FILES) | set(un.DML_OWNED_DIRS)
    assert owned_items("dmf", reg)[0][0].rel == "mods/dmf"
    r = run(g, reg, tool)
    assert r.ok and not r.from_manifest["dml"]
    for rel in DML_PATHS:
        assert not (g / rel).exists()
    assert (g / "mods" / "usermod" / "usermod.mod").is_file() and (g / "mods" / "mod_load_order.txt").read_bytes() == USER_ORDER


def test_fallback_never_removes_mods_or_bundle(installed):
    g, reg, tool = installed
    reg.drop_manifest("dml")
    run(g, reg, tool)
    assert (g / "mods").is_dir() and (g / "bundle" / "bundle_database.data").is_file()


def test_modified_files_are_kept_unless_approved(installed):
    g, reg, tool = installed
    (g / "binaries" / "mod_loader").write_bytes(b"USER PATCHED")
    r = run(g, reg, tool)
    assert r.ok and r.kept == ["binaries/mod_loader"] and (g / "binaries" / "mod_loader").read_bytes() == b"USER PATCHED"
    assert any("changed after it was installed" in w for w in r.warnings)


def test_modified_files_are_removed_when_the_user_approves(installed):
    g, reg, tool = installed
    (g / "binaries" / "mod_loader").write_bytes(b"USER PATCHED")
    asked = []
    r = run(g, reg, tool, confirm_modified=lambda rels: asked.append(rels) or set(rels))
    assert r.ok and asked == [["binaries/mod_loader"]] and not (g / "binaries" / "mod_loader").exists() and not r.kept


# ------------------------------------------------------------ rollback and cancel

def test_failure_while_deleting_restores_everything_and_the_patch(installed, monkeypatch):
    g, reg, tool = installed
    before, reg_before = tree(g), dict(reg.data)
    real = shutil.move
    n = {"c": 0}

    def flaky(src, dst, *a, **k):
        n["c"] += 1
        if n["c"] == 4:
            raise OSError("antivirus has the file open")
        return real(src, dst, *a, **k)

    monkeypatch.setattr("cogitator.installer.shutil.move", flaky)
    r = run(g, reg, tool)
    assert not r.ok and r.rolled_back and not r.rollback_errors and r.failed_step == "remove"
    assert patch_state(g) == ON  # mods switched back on, exactly as before
    assert {k: v for k, v in tree(g).items() if not k.endswith(".bak")} == {k: v for k, v in before.items() if not k.endswith(".bak")}
    assert reg.framework("dml") and reg.manifest("dml") and reg.framework("dmf")


def test_cancel_after_mods_off_switches_them_back_on_and_deletes_nothing(installed):
    g, reg, tool = installed
    cancel = threading.Event()

    def progress(ev):
        if ev.step == "mods_off" and ev.state == "start":
            pass

    def spy(args, cwd):
        rc = tool(args, cwd)
        if args[1] == "--unpatch":
            cancel.set()  # the user hits Cancel right after mods were switched off
        return rc

    before = tree(g)
    r = uninstall_framework(UninstallPlan(g), progress, cancel, registry=reg, runner=spy, **NR)
    assert r.cancelled and not r.ok and r.rolled_back and patch_state(g) == ON
    assert {k: v for k, v in tree(g).items() if not k.endswith(".bak")} == {k: v for k, v in before.items() if not k.endswith(".bak")}


def test_progress_events_cover_the_run(installed):
    g, reg, tool = installed
    ev = []
    uninstall_framework(UninstallPlan(g), ev.append, registry=reg, runner=tool, **NR)
    assert [e.step for e in ev if e.state == "start"] == ["check", "mods_off", "remove", "finish"]


# ------------------------------------------------------------ user mods and the load order

def test_removing_selected_user_mods_edits_the_load_order_surgically(installed):
    g, reg, tool = installed
    reg.set_mod("usermod", nexus_id=77)
    r = run(g, reg, tool, user_mods=["usermod"])
    assert r.ok and not (g / "mods" / "usermod").exists() and (g / "mods" / "othermod" / "data.lua").exists()
    assert (g / "mods" / "mod_load_order.txt").read_bytes() == b"-- my list\r\n--othermod\r\n"
    assert reg.mod("usermod") is None and "mods/usermod/" in r.removed


@pytest.mark.parametrize("bad", ["..", "../x", "mods\\x", "dmf", "base", "", "missing"])
def test_user_mod_names_are_validated(installed, bad):
    g, reg, tool = installed
    before = tree(g)
    r = run(g, reg, tool, user_mods=[bad])
    assert not r.ok and r.failed_step == "check" and tree(g) == before


def test_load_order_is_kept_by_default_and_deletion_is_conditional(installed):
    g, reg, tool = installed
    assert not load_order_removable(g, reg, removing_all_user_mods=False)  # lists mods
    assert load_order_removable(g, reg, removing_all_user_mods=True)
    r = run(g, reg, tool, remove_load_order=True)  # asked, but user mods still exist and are listed
    assert r.ok and (g / "mods" / "mod_load_order.txt").is_file() and any("kept because it still lists" in w for w in r.warnings)


def test_load_order_deleted_when_all_user_mods_go_too(installed):
    g, reg, tool = installed
    r = run(g, reg, tool, user_mods=["usermod", "othermod"], remove_load_order=True)
    assert r.ok and not (g / "mods" / "mod_load_order.txt").exists() and "mods/mod_load_order.txt" in r.removed


def test_default_header_only_load_order_can_be_removed(installed):
    g, reg, tool = installed
    (g / "mods" / "mod_load_order.txt").write_bytes(b"-- ####\r\n-- Enter user mod names below\r\n")
    assert load_order_removable(g, reg, removing_all_user_mods=False)


def test_rollback_restores_edited_load_order_and_removed_mods(installed, monkeypatch):
    g, reg, tool = installed
    before = {k: v for k, v in tree(g).items() if not k.endswith(".bak")}
    real = shutil.move

    def flaky(src, dst, *a, **k):  # fail only when moving the user mod out; rollback moves must still work
        if str(src).endswith("usermod"):
            raise OSError("boom")
        return real(src, dst, *a, **k)

    monkeypatch.setattr("cogitator.installer.shutil.move", flaky)
    r = run(g, reg, tool, user_mods=["usermod"])
    monkeypatch.undo()
    assert not r.ok and r.rolled_back and not r.rollback_errors
    assert patch_state(g) == ON
    assert {k: v for k, v in tree(g).items() if not k.endswith(".bak")} == before


def test_two_runs_in_the_same_second_do_not_collide(installed, monkeypatch):
    g, reg, tool = installed
    monkeypatch.setattr(un.time, "strftime", lambda *a, **k: "20260101-000000")
    assert run(g, reg, tool, remove_dmf=False).ok
    assert run(g, reg, tool, remove_dml=False).ok  # same rollback directory name, must pick fresh slots


# ------------------------------------------------------------ Cogitator's own data, CLI

def test_remove_app_data_clears_key_and_optionally_everything(tmp_path):
    cfg.config_dir().mkdir(parents=True, exist_ok=True)
    Settings = cfg.Settings
    Settings(game_dir="x").save()
    Registry.load().save()
    set_api_key("KeyToRemove-1234567890")
    cfg.cache_dir().mkdir(parents=True, exist_ok=True)
    (cfg.cache_dir() / "rollback").mkdir()
    done = un.remove_app_data(key=True, data=False)
    assert done == ["Nexus key"] and get_api_key() is None and cfg.Settings.path().exists()  # settings kept by default
    done = un.remove_app_data(key=False, data=True)
    assert not cfg.config_dir().exists() and not cfg.cache_dir().exists() and len(done) == 2


def test_cli_uninstall_framework(installed, monkeypatch, capsys):
    g, reg, tool = installed
    monkeypatch.setattr("cogitator.cli.uninstall_framework", lambda plan, *a, **k: uninstall_framework(plan, registry=reg, runner=tool, **NR))
    assert cli.main(["--game-dir", str(g), "uninstall-framework"]) == 2  # no terminal and no --yes -> asks, refuses
    assert (g / "binaries" / "mod_loader").is_file()
    assert cli.main(["--game-dir", str(g), "uninstall-framework", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Mods are switched off" in out and "Your mods and mod_load_order.txt were not touched" in out
    assert not (g / "binaries" / "mod_loader").exists() and (g / "mods" / "usermod").is_dir()
