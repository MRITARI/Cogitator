"""The uninstaller / maintenance window, routing flags, dry-run and silent mode. Fake folders only; real engine; no real game."""
import threading
import time
import tkinter as tk
from pathlib import Path

import pytest

from cogitator import config as cfg
from cogitator.config import get_api_key, set_api_key
from cogitator.patcher import OFF, ON, patch_state
from cogitator.registry import Registry
from cogitator.uninstall_run import FullUninstallResult, UninstallDeps
from cogitator_setup import app as setup_app_mod
from cogitator_setup.app import Flags, choose_mode, main, parse_flags
from cogitator_setup.uninstall_app import UninstallApp, run_silent
from cogitator_setup.uninstall_vm import UPage, UServices, UninstallModel

from test_setup_gui import FakeDialogs, pump, root, widget_texts  # noqa: F401  (root is a fixture)
from test_uninstall import NR, USER_ORDER, installed, tree  # noqa: F401  (installed is a fixture)
from test_uninstall_run import FakeApp
from test_patcher import FakeTool


def make(root, installed, *, app=None, dialogs=None, running=False, tool=None, **kw):
    g, reg, t = installed
    svc = UServices(game_dir=lambda: g, registry=lambda: reg, is_running=lambda: running, app_remover=app,
                    data_dirs=lambda: [(cfg.config_dir(), 4096)])
    deps = UninstallDeps(runner=tool or t, is_running=lambda: running, registry=reg)
    return UninstallApp(services=svc, deps=deps, dialogs=dialogs or FakeDialogs(), root=root, **kw)


def advance(win, to):
    for _ in range(3):
        if win.model.page is to:
            return
        win.btn_next.invoke()


# ------------------------------------------------------------ choose page: defaults

def test_defaults_are_the_ones_in_the_brief_and_user_data_is_not_ticked(root, installed):
    win = make(root, installed, app=FakeApp())
    v = {k: win.v[k].get() for k in win.v}
    assert v == {"app": True, "dmf": True, "dml": True, "key": True, "data": False, "mods": False, "order": False}
    assert "disabled" in win.cb_order.state()  # not offered while the mod list still lists mods
    assert "disabled" not in win.cb_dml.state() and "4.0 KB" in win.data_info.cget("text")


def test_what_is_not_installed_is_greyed_out_with_a_reason(root, installed, tmp_path):
    g, reg, tool = installed
    from cogitator.uninstall import uninstall_framework, UninstallPlan
    assert uninstall_framework(UninstallPlan(g, remove_dmf=True, remove_dml=False), registry=reg, runner=tool, **NR).ok
    win = make(root, installed)
    assert "disabled" in win.cb_dmf.state() and "(not installed)" in win.cb_dmf.cget("text")
    assert "disabled" in win.cb_app.state() and "removed by the installed version" in win.cb_app.cget("text")


def test_nothing_selected_and_running_game_messages(root, installed):
    win = make(root, installed, running=True)
    assert str(win.btn_next.cget("state")) == "disabled" and "Darktide is running" in win.nav_msg.cget("text")
    assert "Retry" in win.running_msg.cget("text")
    for k in ("dmf", "dml", "key"):
        win.v[k].set(False)
        win._tick(k)
    assert "Choose what to remove" in win.nav_msg.cget("text")


# ------------------------------------------------------------ the whole flow, real engine

def test_default_uninstall_end_to_end_returns_the_game_to_vanilla_and_keeps_user_data(root, installed):
    g, reg, tool = installed
    set_api_key("StoredKey-1234567890")
    app = FakeApp()
    d = FakeDialogs()
    win = make(root, installed, app=app, dialogs=d)
    win.btn_next.invoke()  # -> review
    assert win.model.page is UPage.REVIEW
    review = win.review.get("1.0", "end")
    assert "binaries/mod_loader" in review and "Switch mods off first" in review and "Your stored Nexus API key" in review
    assert "Program folder" in review and "Kept" in review and "mod_load_order.txt" in review  # the kept note
    assert "mods/usermod/" not in review.replace("Your installed mods and your mod list", "")  # user mods are not on the delete list
    assert str(win.btn_next.cget("text")) == "Remove"
    win.btn_next.invoke()
    assert pump(win, lambda: win.model.page is UPage.FINISH)
    assert win.result.ok and patch_state(g) == OFF and not (g / "binaries" / "mod_loader").exists() and not (g / "mods" / "dmf").exists()
    assert (g / "mods" / "mod_load_order.txt").read_bytes() == USER_ORDER and (g / "mods" / "usermod" / "usermod.mod").is_file()
    assert get_api_key() is None and app.calls == 1
    assert cfg.config_dir().exists()  # data stays unless asked
    texts = " ".join(widget_texts(win.root))
    assert "mods are switched off" in texts and "were not touched" in texts and "Verify integrity" in texts
    assert all(win.steps.item(i, "values")[0] == "[x]" for i in win.steps.get_children()) and win.bar["value"] == 100


def test_deleting_user_mods_needs_the_second_confirmation_listing_every_folder(root, installed):
    g, reg, tool = installed
    d = FakeDialogs(answers=[False, True])
    win = make(root, installed, dialogs=d)
    win.v["mods"].set(True)
    win._tick("mods")
    assert set(win.mod_vars) == {"usermod", "othermod"} and all(v.get() for v in win.mod_vars.values())
    assert "disabled" not in win.cb_order.state()  # all user mods selected -> the mod list can go too
    win.mod_vars["othermod"].set(False)
    win.model.toggle_user_mod("othermod", False)
    win._refresh_choose()
    assert "disabled" in win.cb_order.state()  # one mod stays -> the mod list stays
    win.btn_next.invoke()  # first confirmation dialog: user says No
    assert win.model.page is UPage.CHOOSE and d.log[0][0] == "ask" and "mods/usermod/" in d.log[0][2] and "mods/othermod/" not in d.log[0][2]
    win.btn_next.invoke()  # says Yes
    assert win.model.page is UPage.REVIEW
    assert "mods/usermod/" in win.review.get("1.0", "end")
    win.btn_next.invoke()
    assert pump(win, lambda: win.model.page is UPage.FINISH)
    assert win.result.ok and not (g / "mods" / "usermod").exists() and (g / "mods" / "othermod" / "data.lua").exists()
    assert (g / "mods" / "mod_load_order.txt").read_bytes() == b"-- my list\r\n--othermod\r\n"


def test_deleting_everything_you_own_can_include_the_mod_list(root, installed):
    g, reg, tool = installed
    win = make(root, installed, dialogs=FakeDialogs(answers=[True]))
    win.v["mods"].set(True)
    win._tick("mods")
    win.v["order"].set(True)
    win._tick("order")
    win.btn_next.invoke()
    win.btn_next.invoke()
    win.btn_next.invoke()
    assert pump(win, lambda: win.model.page is UPage.FINISH)
    assert win.result.ok and not (g / "mods" / "mod_load_order.txt").exists() and not (g / "mods" / "usermod").exists()
    assert (g / "bundle" / "bundle_database.data").is_file() and (g / "mods").is_dir()


def test_files_you_changed_are_asked_about_and_kept_on_no(root, installed):
    g, reg, tool = installed
    (g / "binaries" / "mod_loader").write_bytes(b"MY EDIT")
    d = FakeDialogs(answers=[False])
    win = make(root, installed, dialogs=d)
    win.btn_next.invoke()
    assert "changed since it was installed" in win.review.get("1.0", "end")
    win.btn_next.invoke()
    assert pump(win, lambda: win.model.page is UPage.FINISH)
    assert (g / "binaries" / "mod_loader").read_bytes() == b"MY EDIT"
    asks = [e for e in d.log if e[0] == "ask"]
    assert len(asks) == 1 and "binaries/mod_loader" in asks[0][2] and "Delete them too" in asks[0][2]
    assert win.result.ok and any("changed after it was installed" in w for w in win.result.warnings)


def test_a_failed_unpatch_deletes_nothing_and_says_what_to_do(root, installed):
    g, reg, tool = installed

    def failing(args, cwd):
        return (1, "", "locked") if args[1] == "--unpatch" else tool(args, cwd)

    before = tree(g)
    win = make(root, installed, tool=failing)
    win.btn_next.invoke()
    win.btn_next.invoke()
    assert pump(win, lambda: win.model.page is UPage.FINISH)
    assert not win.result.ok and tree(g) == before and patch_state(g) == ON
    texts = " ".join(widget_texts(win.root))
    assert "Next step:" in texts and "Verify integrity" in texts and "Uninstall could not finish" in texts
    win.btn_copy.invoke()
    assert "failed_step=mods_off" in win.root.clipboard_get()


class Slow:
    def __init__(self):
        self.started = False

    def __call__(self, plan, progress, cancel, deps):
        self.started = True
        for _ in range(500):
            if cancel.is_set():
                return FullUninstallResult(cancelled=True, what="Uninstall was cancelled.")
            time.sleep(0.01)
        return FullUninstallResult(ok=True)


def test_cancel_and_window_close_during_a_run_ask_the_engine_to_stop(root, installed):
    run = Slow()
    win = make(root, installed, run_fn=run)
    win.btn_next.invoke()
    win.btn_next.invoke()
    assert pump(win, lambda: run.started and win.model.page is UPage.PROGRESS)
    win.on_close()  # closing the window must not kill a run
    assert win.root.winfo_exists() and win.ctl.cancel_event.is_set() and str(win.btn_cancel.cget("state")) == "disabled"
    assert pump(win, lambda: win.model.page is UPage.FINISH)
    assert "cancelled" in " ".join(widget_texts(win.root)).lower()


def test_back_from_review_and_escape(root, installed):
    win = make(root, installed)
    win.btn_next.invoke()
    win.btn_back.invoke()
    assert win.model.page is UPage.CHOOSE
    win._on_return()  # keyboard: Enter advances
    assert win.model.page is UPage.REVIEW


# ------------------------------------------------------------ maintenance mode

def test_maintenance_page_offers_repair_update_uninstall(root, installed):
    win = make(root, installed, app=FakeApp(), start="maintenance")
    assert win.pages["maintenance"].winfo_exists()
    txt = win.maint_summary.cget("text")
    assert "already installed" in txt and "Mod Loader: installed" in txt and "mods are on" in txt
    assert "disabled" in win.btn_update.state()  # self-update arrives later
    assert "disabled" not in win.btn_repair.state() and "disabled" not in win.btn_uninstall.state()
    win.btn_uninstall.invoke()
    assert win.model.page is UPage.CHOOSE


def test_repair_closes_the_window_and_tells_the_caller(root, installed):
    win = make(root, installed, start="maintenance")
    win.btn_repair.invoke()
    assert win.action == "repair"


# ------------------------------------------------------------ dry-run: look at everything, change nothing

def test_dry_run_uninstall_changes_nothing(root, installed):
    g, reg, tool = installed
    set_api_key("StoredKey-1234567890")
    before, reg_before = tree(g), dict(reg.data)
    cfg_files = {p: p.read_bytes() for p in cfg.config_dir().rglob("*") if p.is_file()}
    win = make(root, installed, app=FakeApp(), dry_run=True)
    assert "DRY RUN" in win.root.title()
    win.btn_next.invoke()
    win.btn_next.invoke()
    assert pump(win, lambda: win.model.page is UPage.FINISH, 15)
    assert win.result.dry_run and "nothing was removed" in " ".join(widget_texts(win.root)).lower()
    assert tree(g) == before and patch_state(g) == ON and get_api_key() == "StoredKey-1234567890" and tool.calls == ["--patch"]
    assert dict(reg.data) == reg_before and {p: p.read_bytes() for p in cfg.config_dir().rglob("*") if p.is_file()} == cfg_files
    assert win.ctl.deps.runner is tool  # and the real runner was never even called again


def test_dry_run_can_be_cancelled(root, installed):
    g, reg, tool = installed
    before = tree(g)
    win = make(root, installed, dry_run=True)
    win.btn_next.invoke()
    win.btn_next.invoke()
    assert pump(win, lambda: win.model.page is UPage.PROGRESS)
    win.btn_cancel.invoke()
    assert pump(win, lambda: win.model.page is UPage.FINISH, 15) and tree(g) == before


def test_dry_run_setup_changes_nothing(root, tmp_path):
    from cogitator.gamefinder import Resolution
    from cogitator.sources import local_option
    from cogitator_setup.app import SetupApp
    from cogitator_setup.viewmodel import Services
    from test_frameworks import DMF_FILES, DML_FILES, mkzip
    from test_patcher import ORIG

    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "bundle" / "bundle_database.data").write_bytes(ORIG)
    z = tmp_path / "z"
    z.mkdir()
    dml = mkzip(z / "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z a.zip", DML_FILES)
    dmf = mkzip(z / "Darktide Mod Framework 8 26.10.22 2026-10-23T02-11Z b.zip", DMF_FILES)
    svc = Services(find_game=lambda: Resolution(g, "override", [g]), is_running=lambda: False,
                   installed_state=lambda game, k: ("missing", None),
                   resolve_options=lambda kind, prem, key: ([local_option(kind, dml if kind == "dml" else dmf)], ""))
    before = tree(g)
    app = SetupApp(services=svc, root=root, dialogs=FakeDialogs(), dry_run=True)
    assert "DRY RUN" in app.root.title()
    assert pump(app, lambda: str(app.ex_install.cget("state")) == "normal")
    app.ex_install.invoke()
    assert pump(app, lambda: app.model.page.value == "finish", 15)
    assert app.result.dry_run and "nothing was installed" in " ".join(widget_texts(app.root)).lower()
    assert tree(g) == before and not cfg.Settings.path().exists() and not Registry.load().path.exists()
    assert get_api_key() is None


# ------------------------------------------------------------ silent uninstall: defaults only

def test_silent_uninstall_applies_only_the_defaults(installed):
    g, reg, tool = installed
    set_api_key("StoredKey-1234567890")
    out = []
    svc = UServices(game_dir=lambda: g, registry=lambda: reg, is_running=lambda: False, app_remover=FakeApp())
    rc = run_silent(svc, UninstallDeps(runner=tool, is_running=lambda: False, registry=reg), out.append)
    assert rc == 0 and out and any("mods are switched off" in l for l in out)
    assert patch_state(g) == OFF and not (g / "binaries" / "mod_loader").exists() and get_api_key() is None
    assert (g / "mods" / "usermod" / "data.lua").exists() and (g / "mods" / "mod_load_order.txt").read_bytes() == USER_ORDER  # never user data
    assert cfg.config_dir().exists()  # never settings/caches in silent mode


def test_silent_uninstall_refuses_while_the_game_runs(installed):
    g, reg, tool = installed
    before = tree(g)
    out = []
    svc = UServices(game_dir=lambda: g, registry=lambda: reg, is_running=lambda: True)
    assert run_silent(svc, UninstallDeps(runner=tool, is_running=lambda: True, registry=reg), out.append) == 2
    assert tree(g) == before and "Close it" in out[0] and "Nothing was changed" in out[0]


# ------------------------------------------------------------ which window opens

@pytest.mark.parametrize("argv,installed_,mode", [
    ([], False, "setup"), ([], True, "maintenance"), (["--wizard"], True, "setup"), (["--maintenance"], False, "maintenance"),
    (["--uninstall"], False, "uninstall"), (["--uninstall", "--silent"], True, "silent-uninstall"), (["--silent"], True, "maintenance"),
])
def test_choose_mode(argv, installed_, mode):
    assert choose_mode(parse_flags(argv), installed_) == mode


def test_flag_parsing():
    f = parse_flags(["--dry-run", "--game-dir", r"C:\scratch\Darktide", "--uninstall"])
    assert f.dry_run and f.uninstall and f.game_dir == r"C:\scratch\Darktide" and not f.silent
    assert parse_flags(["--game-dir"]).game_dir is None


class Rec:
    made = []

    def __init__(self, **kw):
        self.kw = kw
        Rec.made.append(self)

    def mainloop(self):
        pass


def test_main_routes_to_the_right_window(monkeypatch):
    Rec.made = []
    sf = lambda **kw: Rec(kind="setup", **kw)
    uf = lambda **kw: Rec(kind="uninstall", **kw)
    assert main([], build=22000, app_factory=sf, uninstall_factory=uf, installed=False) == 0
    assert [(r.kw["kind"], r.kw.get("start")) for r in Rec.made] == [("setup", None)]
    Rec.made = []
    main(["--uninstall"], build=22000, app_factory=sf, uninstall_factory=uf, installed=False)
    assert [(r.kw["kind"], r.kw["start"]) for r in Rec.made] == [("uninstall", "choose")]
    Rec.made = []
    main([], build=22000, app_factory=sf, uninstall_factory=uf, installed=True)  # already installed -> maintenance
    assert [(r.kw["kind"], r.kw["start"]) for r in Rec.made] == [("uninstall", "maintenance")]


def test_repair_in_maintenance_opens_the_wizard_in_repair_mode(monkeypatch):
    Rec.made = []

    class Maint(Rec):
        action = "repair"

    uf = lambda **kw: Maint(kind="uninstall", **kw)
    sf = lambda **kw: Rec(kind="setup", **kw)
    main([], build=22000, app_factory=lambda **kw: sf(**kw), uninstall_factory=uf, installed=True)
    kinds = [r.kw["kind"] for r in Rec.made]
    assert kinds == ["uninstall", "setup"]


def test_silent_flag_runs_without_any_window():
    called = []
    rc = main(["--uninstall", "--silent"], build=22000, installed=True, silent_fn=lambda svc: called.append(svc) or 0,
              app_factory=lambda **kw: pytest.fail("no window"), uninstall_factory=lambda **kw: pytest.fail("no window"))
    assert rc == 0 and len(called) == 1


def test_dry_run_and_game_dir_reach_the_windows(monkeypatch, tmp_path):
    seen = {}

    class S(Rec):
        def __init__(self, **kw):
            seen["setup"] = kw
            super().__init__(**kw)

    class U(Rec):
        def __init__(self, **kw):
            seen["uninstall"] = kw
            super().__init__(**kw)

    monkeypatch.setattr(setup_app_mod, "SetupApp", S)
    import cogitator_setup.uninstall_app as ua
    monkeypatch.setattr(ua, "UninstallApp", U)
    main(["--dry-run", "--wizard", "--game-dir", str(tmp_path)], build=22000, installed=False)
    main(["--dry-run", "--uninstall", "--game-dir", str(tmp_path)], build=22000, installed=False)
    assert seen["setup"]["dry_run"] is True and seen["uninstall"]["dry_run"] is True
    assert seen["setup"]["services"].find_game().game_dir == tmp_path and seen["uninstall"]["services"].game_dir() == tmp_path


def test_the_platform_gate_still_comes_first():
    shown = []
    assert main(["--uninstall", "--silent"], build=19045, show_error=lambda t, b: shown.append(b), silent_fn=lambda s: pytest.fail("ran")) == 1
    assert shown == ["Cogitator Setup supports Windows 11 only"]
