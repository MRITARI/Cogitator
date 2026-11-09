"""run_uninstall: order, what survives, failure isolation, and a preview that lists exactly what gets deleted."""
import os
import threading
from pathlib import Path

import pytest

from cogitator import config as cfg
from cogitator.config import get_api_key, set_api_key
from cogitator.patcher import ON, OFF, patch_state
from cogitator.registry import Registry
from cogitator.uninstall_run import (FullUninstallPlan, UninstallDeps, human, planned_steps, preview_uninstall, run_uninstall)

from test_uninstall import NR, USER_ORDER, installed, tree  # noqa: F401  (fixture re-used)


class FakeApp:
    def __init__(self, fail=False, log=None):
        self.fail, self.calls, self.log = fail, 0, log

    def describe(self):
        return ["Shortcuts: Cogitator (desktop), Cogitator (Start Menu)", r"Apps & features entry", r"Program folder: C:\Users\x\AppData\Local\Programs\Cogitator"]

    def remove(self):
        self.calls += 1
        if self.log is not None:
            self.log.append(("app", Registry.load().path.exists(), cfg.cache_dir().exists()))
        if self.fail:
            raise PermissionError("access denied")
        return ["shortcuts", "program folder"]


def deps(tool, reg, confirm=None):
    d = UninstallDeps(runner=tool, is_running=lambda: False, registry=reg)
    if confirm:
        d.confirm = confirm
    return d


def plan(g, **kw):
    return FullUninstallPlan(g, **kw)


def seed_data():
    """Something to find in Cogitator's own folders (registry.json already exists)."""
    cfg.cache_dir().mkdir(parents=True, exist_ok=True)
    (cfg.cache_dir() / "rollback" / "old").mkdir(parents=True, exist_ok=True)
    (cfg.cache_dir() / "rollback" / "old" / "blob").write_bytes(b"x" * 2048)


# ------------------------------------------------------------ order and survivors

def test_full_uninstall_order_and_survivors(installed):
    g, reg, tool = installed
    set_api_key("StoredKey-1234567890")
    seed_data()
    log = []
    app = FakeApp(log=log)
    events = []
    r = run_uninstall(plan(g, app_remover=app, remove_data=True), events.append, deps=deps(tool, reg))
    assert r.ok and not r.failures and r.mods_off is True
    assert [e.step for e in events if e.state == "start"] == ["check", "mods_off", "remove", "finish", "key", "app", "data"]
    assert planned_steps(plan(g, app_remover=app, remove_data=True)) == ["check", "mods_off", "remove", "finish", "key", "app", "data"]
    assert {e.total for e in events} == {7}
    # the framework is gone and un-patched; the user's data is untouched
    assert patch_state(g) == OFF and not (g / "binaries" / "mod_loader").exists() and not (g / "mods" / "dmf").exists()
    assert (g / "mods" / "mod_load_order.txt").read_bytes() == USER_ORDER and (g / "mods" / "usermod" / "data.lua").read_text() == "mine"
    # key gone, app removed AFTER the game was dealt with, Cogitator's own data removed LAST
    assert get_api_key() is None and app.calls == 1
    assert log == [("app", True, True)]  # when the app was removed, registry and cache still existed
    assert not cfg.config_dir().exists() and not cfg.cache_dir().exists()


def test_data_and_key_are_kept_unless_asked(installed):
    g, reg, tool = installed
    set_api_key("StoredKey-1234567890")
    r = run_uninstall(plan(g, remove_key=False), deps=deps(tool, reg))
    assert r.ok and get_api_key() == "StoredKey-1234567890" and cfg.config_dir().exists()


def test_without_a_game_folder_only_cogitators_own_things_are_removed(tmp_path):
    set_api_key("StoredKey-1234567890")
    app = FakeApp()
    r = run_uninstall(FullUninstallPlan(None, remove_dml=False, remove_dmf=False, app_remover=app), deps=UninstallDeps(is_running=lambda: False))
    assert r.ok and app.calls == 1 and r.framework is None and get_api_key() is None


# ------------------------------------------------------------ failure isolation

def test_framework_failure_stops_everything_after_it(installed):
    g, reg, tool = installed
    set_api_key("StoredKey-1234567890")
    seed_data()

    def failing(args, cwd):
        return (1, "", "locked") if args[1] == "--unpatch" else tool(args, cwd)

    before = tree(g)
    app = FakeApp()
    r = run_uninstall(plan(g, app_remover=app, remove_data=True), deps=deps(failing, reg))
    assert not r.ok and r.failed_step == "mods_off" and tree(g) == before and patch_state(g) == ON
    assert app.calls == 0 and get_api_key() == "StoredKey-1234567890" and cfg.config_dir().exists()  # nothing after it ran
    assert "Verify integrity" in r.fix


def test_one_failing_late_step_does_not_stop_the_others_and_is_reported(installed):
    g, reg, tool = installed
    set_api_key("StoredKey-1234567890")
    seed_data()
    app = FakeApp(fail=True)
    r = run_uninstall(plan(g, app_remover=app, remove_data=True), deps=deps(tool, reg))
    assert not r.ok and r.failed_step == "app" and "Cogitator" in r.what and r.fix
    assert len(r.failures) == 1 and get_api_key() is None and not cfg.cache_dir().exists()  # key and data still went
    assert not (g / "binaries" / "mod_loader").exists()  # the game part stays done


def test_cancel_after_the_game_part_skips_the_rest_and_says_what_stayed_removed(installed):
    g, reg, tool = installed
    set_api_key("StoredKey-1234567890")
    cancel = threading.Event()

    def progress(ev):
        if ev.step == "finish" and ev.state == "done":
            cancel.set()

    app = FakeApp()
    r = run_uninstall(plan(g, app_remover=app), progress, cancel, deps(tool, reg))
    assert r.cancelled and not r.ok and app.calls == 0 and get_api_key() == "StoredKey-1234567890"
    assert not (g / "binaries" / "mod_loader").exists()
    assert "already removed stays removed" in r.what


def test_cancel_during_the_game_part_puts_everything_back(installed):
    g, reg, tool = installed
    cancel = threading.Event()

    def spy(args, cwd):
        rc = tool(args, cwd)
        if args[1] == "--unpatch":
            cancel.set()
        return rc

    r = run_uninstall(plan(g), None, cancel, deps(spy, reg))
    assert r.cancelled and patch_state(g) == ON and (g / "binaries" / "mod_loader").is_file()


# ------------------------------------------------------------ files the user changed

def test_changed_files_ask_once_with_the_list(installed):
    g, reg, tool = installed
    (g / "binaries" / "mod_loader").write_bytes(b"USER EDIT")
    asked = []
    r = run_uninstall(plan(g), deps=deps(tool, reg, lambda t, b: asked.append((t, b)) or False))
    assert r.ok and (g / "binaries" / "mod_loader").read_bytes() == b"USER EDIT"  # declined -> kept
    assert len(asked) == 1 and "binaries/mod_loader" in asked[0][1] and "Delete them too" in asked[0][1]
    assert any("changed after it was installed" in w for w in r.warnings)


def test_changed_files_are_deleted_when_the_user_agrees(installed):
    g, reg, tool = installed
    (g / "binaries" / "mod_loader").write_bytes(b"USER EDIT")
    r = run_uninstall(plan(g), deps=deps(tool, reg, lambda t, b: True))
    assert r.ok and not (g / "binaries" / "mod_loader").exists()


# ------------------------------------------------------------ preview == reality

def framework_paths(items):
    return {i.path for i in items if i.group in ("framework", "user_mod", "load_order")}


@pytest.mark.parametrize("with_manifest", [True, False])
def test_preview_lists_exactly_what_is_then_deleted(installed, with_manifest):
    g, reg, tool = installed
    if not with_manifest:
        reg.drop_manifest("dml")
        reg.drop_manifest("dmf")
    p = plan(g, user_mods=["usermod"], remove_load_order=False)
    items = preview_uninstall(p, reg)
    r = run_uninstall(p, deps=deps(tool, reg))
    assert r.ok
    assert framework_paths(items) == set(r.framework.removed)  # nothing listed was spared, nothing unlisted was removed
    assert any(i.group == "mods_off" for i in items)


def test_preview_notes_changed_files_and_the_other_groups(installed):
    g, reg, tool = installed
    (g / "tools" / "README.md").write_text("edited")
    seed_data()
    app = FakeApp()
    items = preview_uninstall(plan(g, remove_data=True, app_remover=app), reg)
    by = {i.path: i for i in items if i.group == "framework"}
    assert "changed since it was installed" in by["tools/README.md"].note and by["binaries/mod_loader"].note == ""
    groups = {i.group for i in items}
    assert {"mods_off", "framework", "key", "app", "data"} <= groups and "user_mod" not in groups and "load_order" not in groups
    assert any("KB" in i.text for i in items if i.group == "data")  # sizes are shown


def test_preview_load_order_only_when_allowed(installed):
    g, reg, tool = installed
    keep = preview_uninstall(plan(g, remove_load_order=True), reg)
    assert not any(i.group == "load_order" for i in keep)  # still lists mods -> not offered
    both = preview_uninstall(plan(g, user_mods=["usermod", "othermod"], remove_load_order=True), reg)
    assert any(i.group == "load_order" for i in both)


def test_preview_skips_things_that_are_not_there(installed):
    g, reg, tool = installed
    assert run_uninstall(plan(g), deps=deps(tool, reg)).ok
    again = preview_uninstall(plan(g), reg)
    assert not any(i.group in ("framework", "mods_off") for i in again)


def test_human_sizes():
    assert [human(n) for n in (0, 1023, 2048, 5 * 1024 * 1024)] == ["0 B", "1023 B", "2.0 KB", "5.0 MB"]


def test_with_two_failures_the_first_is_reported_and_both_are_listed(installed, monkeypatch):
    from cogitator import uninstall_run as ur
    g, reg, tool = installed
    monkeypatch.setattr(ur, "clear_api_key", lambda: (_ for _ in ()).throw(OSError("keyring locked")))
    app = FakeApp(fail=True)
    r = run_uninstall(plan(g, app_remover=app), deps=deps(tool, reg))
    assert not r.ok and r.failed_step == "key" and r.what.startswith("Your Nexus key")
    assert len(r.failures) == 2 and any(f.startswith("Cogitator") for f in r.failures) and app.calls == 1  # the app step still ran
