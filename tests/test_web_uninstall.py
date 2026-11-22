"""The web uninstall / maintenance session and API, the entry's routing, and the main web app's in-app self-update. Driven without a
window or a network: fake folders, fake services, fake updater. Fixtures mirror test_web_setup.py."""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cogitator import config as cfg
from cogitator.selfupdate import Layout, UpdateError
from cogitator.setup_run import ProgressEvent, StepResult
from cogitator.uninstall_run import FullUninstallResult, UninstallDeps
from cogitator_setup.controller import Controller
from cogitator_setup.dryrun import simulated_uninstall
from cogitator_setup.uninstall_vm import UPage, UServices, UninstallModel
from cogitator_web import setup_app
from cogitator_web.bridge import Bridge
from cogitator_web.uninstall_session import UninstallApi, UninstallSession

from test_uninstall import NR, installed, tree  # noqa: F401  (installed is a fixture)
from test_uninstall_run import FakeApp
from test_uninstall_vm import FakeUpdater
from test_web_api import FakeWindow, game, wait, web  # noqa: F401  (fixtures)


def ok_run(plan, progress, cancel, deps=None):
    progress(ProgressEvent("check", "start", 0.0, 1))
    progress(ProgressEvent("check", "done", 0.5, 1, "ok"))
    return FullUninstallResult(ok=True, steps=[StepResult("check", "done", "ok")])


def make(installed, run_fn=ok_run, *, running=None, app=None, updater=None, start="choose", dry_run=False, **kw):
    g, reg, tool = installed
    state = {"running": False} if running is None else running
    svc = UServices(game_dir=lambda: g, registry=lambda: reg, is_running=lambda: state["running"], app_remover=app,
                    data_dirs=lambda: [(cfg.config_dir(), 4096)], selfupdater=lambda: updater)
    model = UninstallModel(svc)
    ctl = Controller(model, UninstallDeps(runner=tool, is_running=lambda: state["running"], registry=reg), run_fn=run_fn)
    s = UninstallSession(Bridge(), model=model, controller=ctl, start=start, dry_run=dry_run, **kw)
    s.quit_delay = 0
    s.state_flags = state
    s.quits = []
    s.on_quit = lambda: s.quits.append(True)
    return s


def attach(s):
    win = FakeWindow()
    s.bridge.attach(win)
    return win, UninstallApi(s)


def settle(s, until=None, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        s.pump()
        if until is None:
            if not s.ctl.running:
                s.ctl.join(1)
                s.pump()
                return
        elif until():
            return
        time.sleep(0.01)
    raise AssertionError("did not settle")


def go_review(s):
    s.next()
    assert s.state()["page"] == "review"


# ---------------------------------------------------------------- state for each page
def test_state_payload_for_every_page(installed):
    u = FakeUpdater()
    m = make(installed, app=FakeApp(), updater=u, start="maintenance")
    st = m.state()
    json.dumps(st)
    assert st["page"] == "maintenance" and st["nav"]["visible"] is False and st["steps"] == []
    mt = st["maintenance"]
    assert mt["repair"] and mt["uninstall"] and mt["update"] and "Mod Loader: installed" in mt["summary"]
    assert "<b>Fixes</b> & more" in mt["notes"] and "v2.0.0" in mt["notes"] and "may be out of date" in mt["repair_note"]
    assert st["choose"] == {} and st["review"] == [] and st["finish"] == {}
    m.go_uninstall()
    st = m.state()
    assert st["page"] == "choose" and st["steps"] == ["choose", "review", "progress", "finish"] and st["maintenance"] == {}
    names = [o["name"] for o in st["choose"]["options"]]
    assert names == ["app", "dmf", "dml", "key", "data", "mods", "order"]
    assert st["choose"]["safe_note"] and "4.0 KB" in st["choose"]["data"][0]
    assert st["nav"]["next"]["label"] == "Next" and st["nav"]["next"]["enabled"] and st["nav"]["back"]["enabled"] is False
    go_review(m)
    st = m.state()
    assert st["nav"]["next"]["label"] == "Remove" and st["nav"]["back"]["enabled"] and st["review"] and st["choose"] == {}
    m.next()
    settle(m)
    st = m.state()
    json.dumps(st)
    assert st["page"] == "finish" and st["finish"]["ok"] and st["finish"]["lines"] and st["nav"]["cancel"]["label"] == "Close"


def test_options_follow_what_is_installed(installed):
    s = make(installed)  # no app remover
    opts = {o["name"]: o for o in s.state()["choose"]["options"]}
    assert opts["app"]["enabled"] is False and opts["app"]["note"] and opts["app"]["checked"] is False
    assert opts["dml"]["checked"] and opts["dmf"]["checked"] and opts["key"]["checked"] and not opts["data"]["checked"]
    assert opts["mods"]["enabled"] and opts["order"]["enabled"] is False  # the mod list still lists mods


# ---------------------------------------------------------------- plan from toggles, validation
def test_plan_is_built_from_the_toggles(installed):
    plans = []

    def capture(plan, progress, cancel, deps=None):
        plans.append(plan)
        return ok_run(plan, progress, cancel, deps)

    s = make(installed, capture, app=FakeApp())
    _, api = attach(s)
    for name, v in (("dml", False), ("key", False), ("data", True), ("mods", True)):
        assert api.toggle(name, v)["ok"]
    assert api.toggle_mod("othermod", False)["ok"]
    st = s.state()["choose"]
    assert [m for m in st["mods"] if m["checked"]] == [{"name": "usermod", "checked": True}]
    s.model.confirm_user_mods()  # the confirmation itself is covered below
    go_review(s)
    s.next()
    settle(s)
    p = plans[0]
    assert (p.remove_dml, p.remove_dmf, p.remove_key, p.remove_data, p.user_mods) == (False, True, False, True, ["usermod"])
    assert p.app_remover is not None


@pytest.mark.parametrize("bad", ["nope", "../usermod", "", None, 5, ["usermod"], "usermod\0", "mods/usermod"])
def test_toggle_mod_rejects_names_not_in_the_detected_list(installed, bad):
    s = make(installed)
    _, api = attach(s)
    api.toggle("mods", True)
    before = list(s.model.user_mods_selected)
    r = api.toggle_mod(bad, True)
    assert r["ok"] is False and s.model.user_mods_selected == before
    json.dumps(r)


def test_other_bad_arguments(installed):
    s = make(installed)
    _, api = attach(s)
    assert api.toggle("format_c", True)["ok"] is False and api.toggle(3, True)["ok"] is False
    assert api.toggle("key", "yes")["ok"] is False and api.toggle("key", 1)["ok"] is False
    assert api.toggle("app", True)["ok"] is False  # no app remover: not available
    assert api.toggle_mod("usermod", True)["ok"] is False  # "My installed mods" is not ticked
    assert api.answer(999, True)["ok"] is False and api.answer("1", True)["ok"] is False
    assert api.repair()["ok"] is False and api.update()["ok"] is False and api.go_uninstall()["ok"] is False  # not on the maintenance page
    assert api.next("confirmed")["ok"] is False  # the page has no way to pass a flag
    for r in (api.toggle("key", None), api.copy_details()):
        json.dumps(r)


# ---------------------------------------------------------------- second confirmation
def test_user_mod_confirmation_is_asked_and_cannot_be_bypassed(installed):
    ran = []
    s = make(installed, lambda *a, **k: ran.append(1) or ok_run(*a))
    win, api = attach(s)
    api.toggle("mods", True)
    st = s.state()
    assert st["nav"]["next"]["enabled"] and "confirm" in st["nav"]["reason"]
    assert api.next()["ok"]
    ask = wait(lambda: win.of("ask"))[-1]
    assert ask["kind"] == "confirm" and "mods/usermod/" in ask["body"] and "mods/othermod/" in ask["body"]
    assert s.state()["page"] == "choose" and s.model.needs_user_mod_confirmation
    assert api.next()["ok"] and len(win.of("ask")) == 1  # a second Next while asking does not ask again
    assert api.answer(ask["id"], False)["ok"]
    wait(lambda: not s.confirming)
    assert s.state()["page"] == "choose" and s.model.needs_user_mod_confirmation  # No keeps the user here
    # a page that jumps ahead still cannot start the run
    s.model.page = UPage.REVIEW
    s.next()
    assert not ran and not s.ctl.running
    s.model.page = UPage.CHOOSE
    assert api.next()["ok"]
    ask = wait(lambda: len(win.of("ask")) == 2 and win.of("ask")[-1])
    assert api.answer(ask["id"], True)["ok"]
    wait(lambda: s.state()["page"] == "review")
    assert not s.model.needs_user_mod_confirmation
    s.next()
    settle(s)
    assert ran and s.state()["page"] == "finish"


def test_changing_the_selection_while_asking_does_not_confirm(installed):
    s = make(installed)
    win, api = attach(s)
    api.toggle("mods", True)
    api.next()
    ask = wait(lambda: win.of("ask"))[-1]
    api.toggle_mod("othermod", False)  # the list changed under the open question
    api.answer(ask["id"], True)
    wait(lambda: not s.confirming)
    assert s.state()["page"] == "choose" and s.model.needs_user_mod_confirmation


# ---------------------------------------------------------------- game running
def test_running_game_blocks_next_and_retry_rechecks(installed):
    s = make(installed, running={"running": True})
    st = s.state()
    assert st["nav"]["next"]["enabled"] is False and "Darktide is running" in st["nav"]["reason"]
    assert st["choose"]["running"] and "Retry" in st["choose"]["running_text"]
    s.next()
    assert s.state()["page"] == "choose"
    s.retry()
    assert s.state()["nav"]["next"]["enabled"] is False  # still running
    s.state_flags["running"] = False
    s.retry()
    st = s.state()
    assert st["nav"]["next"]["enabled"] and not st["choose"]["running"]


def test_nothing_selected_explains_why(installed):
    s = make(installed)
    for n in ("dml", "dmf", "key"):
        s.toggle(n, False)
    st = s.state()
    assert st["nav"]["next"]["enabled"] is False and st["nav"]["reason"] == "Choose what to remove."


# ---------------------------------------------------------------- review, run, finish
def test_review_text_matches_the_model_and_keeps_note(installed):
    s = make(installed)
    go_review(s)
    rv = s.state()["review"]
    assert [(g["title"], g["lines"]) for g in rv] == [(t, ls) for t, ls in s.model.review_text()]
    assert rv[-1]["title"] == "Kept"
    s.back()
    assert s.state()["page"] == "choose" and s.state()["review"] == []


def test_run_shows_progress_and_finish_lines(installed):
    gate = threading.Event()

    def slow(plan, progress, cancel, deps=None):
        progress(ProgressEvent("check", "start", 0.0, 1))
        gate.wait(5)
        return ok_run(plan, progress, cancel, deps)

    s = make(installed, slow)
    win, api = attach(s)
    go_review(s)
    assert api.next()["ok"]
    st = s.state()
    assert st["page"] == "progress" and st["progress"]["running"] and st["nav"]["next"]["enabled"] is False
    names = [x["name"] for x in st["progress"]["steps"]]
    assert "check" in names and all(x["state"] in ("pending", "running") for x in st["progress"]["steps"])
    settle(s, lambda: s.state()["progress"]["steps"][0]["state"] == "running")
    gate.set()
    settle(s)
    st = s.state()
    assert st["page"] == "finish" and st["finish"]["title"] == "Done" and st["finish"]["ok"] and len(st["finish"]["lines"]) == 3
    assert st["progress"]["fraction"] > 0 and any("check" in line for line in st["progress"]["log"])
    assert "ok=True" in api.copy_details()["text"]
    assert wait(lambda: win.of("uninstall_state"))  # states are pushed
    assert api.close()["ok"] and s.quits == [True] and s.on_close() is True


def test_failed_run_shows_what_why_fix_and_a_notice_for_a_worker_error(installed):
    def bad(plan, progress, cancel, deps=None):
        return FullUninstallResult(ok=False, what="It stopped.", why="A file is busy.", fix="Close it.", failed_step="remove")

    s = make(installed, bad)
    go_review(s)
    s.next()
    settle(s)
    f = s.state()["finish"]
    assert f["title"] == "Uninstall could not finish" and f["lines"] == ["It stopped.", "A file is busy.", "Next step: Close it."]

    def boom(*a, **k):
        raise RuntimeError("explode")

    s2 = make(installed, boom)
    win, _ = attach(s2)
    go_review(s2)
    s2.next()
    s2.ctl.join(2)
    s2.pump()
    ask = wait(lambda: win.of("ask"))[-1]
    assert ask["kind"] == "notice" and "explode" in ask["body"]


def test_cancel_mid_run_and_close_while_running(installed):
    started = threading.Event()

    def blocking(plan, progress, cancel, deps=None):
        progress(ProgressEvent("check", "start", 0.0, 1))
        started.set()
        cancel.wait(5)
        return FullUninstallResult(ok=False, cancelled=True)

    s = make(installed, blocking)
    go_review(s)
    s.next()
    assert started.wait(2)
    assert s.on_close() is False and s.ctl.cancel_event.is_set()  # the window stays while the run stops
    st = s.state()
    assert st["nav"]["cancel"] == {"label": "Cancelling...", "enabled": False} and st["progress"]["cancelling"]
    assert s.quits == []
    settle(s)
    st = s.state()
    assert st["page"] == "finish" and st["finish"]["cancelled"] and st["finish"]["title"] == "Uninstall was cancelled"
    assert s.on_close() is True

    s2 = make(installed, blocking)
    started.clear()
    go_review(s2)
    s2.next()
    assert started.wait(2)
    s2.cancel()
    assert s2.ctl.cancel_event.is_set() and s2.quits == []
    settle(s2)


def test_escape_closes_only_on_choose_and_review(installed):
    s = make(installed, start="maintenance", updater=FakeUpdater())
    s.escape()
    assert s.quits == []  # maintenance
    s.go_uninstall()
    go_review(s)
    s.escape()
    assert s.quits == [True]
    s2 = make(installed)
    go_review(s2)
    s2.next()
    s2.escape()  # progress / finish
    assert s2.quits == []
    settle(s2)
    s2.escape()
    assert s2.quits == []


def test_dry_run_changes_nothing_and_says_so(installed):
    g, reg, tool = installed
    before = tree(g)
    s = make(installed, lambda p, pr, c, d=None: simulated_uninstall(p, pr, c, d, seconds=0), dry_run=True)
    assert s.state()["dry_run"] is True
    go_review(s)
    s.next()
    settle(s)
    f = s.state()["finish"]
    assert f["dry_run"] and f["ok"] and f["lines"] == ["Dry run finished: nothing was removed or changed."]
    assert tree(g) == before
    assert UninstallSession(Bridge(), services=s.model.svc, dry_run=True).ctl.run_fn is simulated_uninstall


# ---------------------------------------------------------------- maintenance
def test_repair_sets_the_action_and_quits(installed):
    s = make(installed, start="maintenance", updater=FakeUpdater())
    _, api = attach(s)
    assert api.repair()["ok"] and s.action == "repair" and s.quits == [True] and s.on_close() is True


class UpdaterWith(FakeUpdater):
    def __init__(self, fail=None, **kw):
        super().__init__(**kw)
        self.fail = fail

    def stage(self, info):
        if self.fail:
            raise UpdateError(self.fail)
        super().stage(info)


def test_maintenance_update_installs_and_quits_when_in_place(installed):
    u = UpdaterWith()
    s = make(installed, start="maintenance", updater=u)
    _, api = attach(s)
    assert api.update()["ok"]
    wait(lambda: s.quits)
    assert u.log == ["stage", "apply"] and s.action == "update" and "Installing v2.0.0" in s.state()["maintenance"]["message"]


def test_maintenance_update_error_is_shown_and_not_in_place_does_not_quit(installed):
    u = UpdaterWith(fail="The download failed its check.")
    s = make(installed, start="maintenance", updater=u)
    s.update()
    wait(lambda: not s.update_busy)
    mt = s.state()["maintenance"]
    assert mt["message"] == "The download failed its check." and mt["update"] is True and s.quits == [] and s.action is None
    u2 = UpdaterWith(in_place=False)
    s2 = make(installed, start="maintenance", updater=u2)
    s2.update()
    wait(lambda: not s2.update_busy)
    assert u2.log == [] and "pipx upgrade" in s2.state()["maintenance"]["message"] and s2.quits == []


def test_maintenance_remind_and_skip(installed):
    for call, entry in (("remind", "remind"), ("skip", ("skip", "2.0.0"))):
        u = FakeUpdater()
        s = make(installed, start="maintenance", updater=u)
        _, api = attach(s)
        assert api.__getattribute__(call)()["ok"] and u.log == [entry]
        mt = s.state()["maintenance"]
        assert mt["update"] is False and mt["notes"] == "" and mt["update_available"] is False
        assert api.__getattribute__(call)()["ok"] is False  # nothing left to postpone
    s = make(installed, start="maintenance", updater=FakeUpdater(shown=False))
    _, api = attach(s)
    assert s.state()["maintenance"]["update"] is False and api.update()["ok"] is False


# ---------------------------------------------------------------- the entry
def flags_of(argv):
    return setup_app.parse_flags(argv)


def test_repair_flag_forces_setup_in_repair_mode_and_is_stripped(installed):
    seen = []
    runner = lambda argv, flags: seen.append((argv, flags.repair, flags.dry_run, flags.game_dir)) or 0
    un = lambda *a: pytest.fail("the uninstall window must not open")
    assert setup_app.main(["--repair", "--dry-run", "--game-dir", "X"], build=22631, installed=True, runner=runner, uninstall_runner=un) == 0
    assert seen == [(["--dry-run", "--game-dir", "X"], True, True, "X")]
    assert setup_app.main([], build=22631, installed=False, runner=runner, uninstall_runner=un) == 0 and seen[-1][1] is False


def test_routing_of_uninstall_modes_and_silent_exit_codes(monkeypatch):
    calls = []
    runner = lambda argv, flags: pytest.fail("setup must not open")
    un = lambda argv, flags, mode: calls.append((mode, argv)) or 0
    assert setup_app.main(["--uninstall", "--dry-run"], build=22631, installed=True, runner=runner, uninstall_runner=un) == 0
    assert setup_app.main(["--maintenance"], build=22631, installed=False, runner=runner, uninstall_runner=un) == 0
    assert setup_app.main([], build=22631, installed=True, runner=runner, uninstall_runner=un) == 0
    assert [c[0] for c in calls] == ["uninstall", "maintenance", "maintenance"]
    for code in (0, 2):
        got = []
        silent = lambda svc, code=code: got.append(svc) or code
        assert setup_app.main(["--uninstall", "--silent"], build=22631, installed=True, runner=runner, uninstall_runner=un,
                              silent_fn=silent) == code and len(got) == 1
    assert len(calls) == 3  # silent never opens a window
    from cogitator_web import selfcheck

    monkeypatch.setattr(selfcheck, "run_setup", lambda expect=None, **kw: 7)
    assert setup_app.main(["--self-check"], build=22631, installed=True) == 7


def test_silent_uninstall_default_matches_the_tkinter_function(installed):
    from cogitator_setup.uninstall_app import run_silent

    g, reg, tool = installed
    svc = UServices(game_dir=lambda: g, registry=lambda: reg, is_running=lambda: True)
    out = []
    assert run_silent(svc, UninstallDeps(runner=tool, is_running=lambda: True, registry=reg), out.append) == 2
    assert setup_app.main(["--uninstall", "--silent"], build=22631, installed=True, silent_fn=lambda s: run_silent(
        svc, UninstallDeps(runner=tool, is_running=lambda: True, registry=reg), out.append)) == 2


def test_repair_command_and_relaunch_after_the_window_closes():
    f = flags_of(["--dry-run", "--game-dir", "C:\\Games\\DT"])
    assert setup_app.repair_command(f, frozen=True, executable="C:\\x\\setup.exe") == ["C:\\x\\setup.exe", "--repair", "--dry-run", "--game-dir", "C:\\Games\\DT"]
    assert setup_app.repair_command(flags_of([]), frozen=False, executable="py") == ["py", "-m", "cogitator_web.setup_app", "--repair"]
    started = []
    setup_app.after_uninstall_window("repair", f, relauncher=started.append, frozen=False, executable="py")
    assert started == [["py", "-m", "cogitator_web.setup_app", "--repair", "--dry-run", "--game-dir", "C:\\Games\\DT"]]
    for action in (None, "update"):
        setup_app.after_uninstall_window(action, f, relauncher=started.append)
    assert len(started) == 1


def test_missing_webview2_or_pywebview_falls_back_to_tkinter(monkeypatch):
    notes = []
    monkeypatch.setattr(setup_app, "notify", notes.append)
    got = []
    fb = lambda argv: got.append(argv) or 7
    f = flags_of(["--uninstall"])
    assert setup_app.run_web_uninstall(["--uninstall"], f, "uninstall", webview2_check=lambda: False, fallback=fb) == 7
    assert got == [["--uninstall"]] and "WebView2" in notes[-1]
    monkeypatch.setitem(__import__("sys").modules, "webview", None)  # import webview -> ImportError
    assert setup_app.run_web_uninstall(["--maintenance"], flags_of(["--maintenance"]), "maintenance", webview2_check=lambda: True, fallback=fb) == 7
    assert got[-1] == ["--maintenance"] and "pywebview" in notes[-1]


# ---------------------------------------------------------------- the main web app: in-app self-update
class UpdateFake(FakeUpdater):
    """FakeUpdater plus what Session and actions_setup read from a SelfUpdater."""

    def __init__(self, settings, **kw):
        super().__init__(**kw)
        self.settings = settings
        self.threads = []
        self.applied = False

    def latest(self):
        return self.rel

    def apply(self):
        super().apply()
        return SimpleNamespace(version="2.0.0")

    def skip_version(self, v):
        self.threads.append(threading.current_thread())
        super().skip_version(v)

    def remind_later(self):
        self.threads.append(threading.current_thread())
        super().remind_later()

    def absorb_result(self):
        return {"status": "ok", "version": "2.0.0"}

    def run_automatic(self):
        self.log.append("check")
        return SimpleNamespace(status="available", latest=self.rel)

    def should_apply_automatically(self):
        return self.settings.update_mode == "auto" and self._ready


def logs(win):
    return [p["text"] for p in win.of("log")]


def test_banner_points_at_the_tools_page(web):
    win, s, api, _ = web
    assert s.header_payload()["update_banner"] == "" and s.header_payload()["update_version"] == ""
    u = s.selfupdater = UpdateFake(s.settings)
    h = s.header_payload()
    assert h["update_banner"] == "Update available v2.0.0. Use the Update button on the Tools page to download and install it."
    assert h["update_version"] == "2.0.0" and "Help >" not in h["update_banner"]
    u._ready = True
    assert s.header_payload()["update_banner"] == "Update available v2.0.0 - downloaded and verified. Use the Update button on the Tools page to install it."
    u.layout = Layout("pipx")
    u._ready = False
    assert "pipx upgrade" in s.header_payload()["update_banner"]
    s.settings.update_mode = "off"
    assert s.header_payload()["update_banner"] == "" and s.header_payload()["update_version"] == ""


def test_update_with_nothing_waiting_says_so(web):
    win, s, api, _ = web
    s.selfupdater = UpdateFake(s.settings, shown=False)
    assert api.cogitator_update()["ok"]
    wait(lambda: "No Cogitator update is waiting." in logs(win))
    assert not win.of("ask")


def test_offer_download_ready_install_quits(web):
    win, s, api, _ = web
    quits = []
    s.on_quit = lambda: quits.append(True)
    u = s.selfupdater = UpdateFake(s.settings)
    api.cogitator_update()
    ask = wait(lambda: win.of("ask"))[-1]
    assert ask["kind"] == "choice" and "v2.0.0" in ask["question"] and ask["options"][0].startswith("Download it")
    assert u.log == []  # nothing is downloaded unasked
    api.answer(ask["id"], 0)
    ask = wait(lambda: len(win.of("ask")) == 2 and win.of("ask")[-1])
    assert u.log == ["stage"] and ask["options"][0].startswith("Install now") and "Fixes" in ask["question"]
    assert wait(lambda: [h for h in win.of("header") if h["header"]["update_banner"].startswith("Update available v2.0.0 - downloaded")])
    api.answer(ask["id"], 0)
    wait(lambda: quits)
    assert u.log == ["stage", "apply"]


def test_offer_cancel_downloads_nothing(web):
    win, s, api, _ = web
    u = s.selfupdater = UpdateFake(s.settings)
    api.cogitator_update()
    ask = wait(lambda: win.of("ask"))[-1]
    api.answer(ask["id"], 1)
    time.sleep(0.2)
    assert u.log == [] and len(win.of("ask")) == 1


@pytest.mark.parametrize("pick,entry", [(1, "remind"), (2, ("skip", "2.0.0"))])
def test_remind_and_skip_run_off_the_ui_thread(web, pick, entry):
    win, s, api, _ = web
    u = s.selfupdater = UpdateFake(s.settings)
    u._ready = True  # already staged: straight to install / remind / skip
    api.cogitator_update()
    ask = wait(lambda: win.of("ask"))[-1]
    api.answer(ask["id"], pick)
    wait(lambda: u.log == [entry])
    assert u.threads and u.threads[0] is not threading.main_thread() and u.threads[0].name.startswith("gui-")


def test_update_api_is_validated_and_takes_no_arguments(web):
    _, s, api, _ = web
    assert api.cogitator_update("x")["ok"] is False and api.cogitator_update(path="C:\\")["ok"] is False


def test_startup_check_reports_the_last_result_and_respects_the_settings(web):
    win, s, api, _ = web
    s.check_on_start = True
    s.settings.update_mode = "off"
    s.start_selfupdate_check()
    assert s.selfupdater is None
    s.settings.update_mode = "notify"
    s.settings.check_updates_on_start = False
    s.start_selfupdate_check()
    assert s.selfupdater is None
    s.settings.check_updates_on_start = True
    u = s.selfupdater = UpdateFake(s.settings)
    s.start_selfupdate_check()
    wait(lambda: "Cogitator v2.0.0 is available." in logs(win))
    assert u.log == ["check"] and "Updated to v2.0.0." in logs(win)
    assert wait(lambda: [h for h in win.of("header") if h["header"]["update_banner"]])


def test_automatic_apply_quits_only_in_auto_mode(web):
    win, s, api, _ = web
    quits = []
    s.on_quit = lambda: quits.append(True)
    s.check_on_start = True
    u = s.selfupdater = UpdateFake(s.settings)
    u._ready = True
    s.settings.update_mode = "notify"
    s.start_selfupdate_check()
    wait(lambda: u.log == ["check"])
    time.sleep(0.2)
    assert quits == [] and "apply" not in u.log
    wait(lambda: not s.ctl._jobs["selfupdate"].is_alive())
    s.settings.update_mode = "auto"
    s.start_selfupdate_check()
    wait(lambda: quits)
    assert u.log == ["check", "check", "apply"]
