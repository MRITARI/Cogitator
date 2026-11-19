"""The web setup wizard's session and page-facing API, driven without a window. Fakes mirror tests/test_setup_gui.py."""
import functools
import json
import threading
import time

import pytest

from cogitator.gamefinder import Resolution
from cogitator.setup_run import KeyCheck, ProgressEvent, SetupResult, StepResult
from cogitator.sources import local_option
from cogitator_setup.controller import Controller
from cogitator_setup.dryrun import simulated_setup
from cogitator_setup.viewmodel import Page, Services, SetupModel
from cogitator_web.bridge import Bridge
from cogitator_web.setup_session import SetupApi, WizardSession

from test_frameworks import DMF_FILES, DML_FILES, mkzip

KEY = "k" * 12


@pytest.fixture
def world(tmp_path):
    g = tmp_path / "Darktide"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    dml = mkzip(tmp_path / "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z a.zip", DML_FILES)
    dmf = mkzip(tmp_path / "Darktide Mod Framework 8 26.10.22 2026-10-23T02-11Z b.zip", DMF_FILES)
    return g, dml, dmf


def services(world, running=False):
    g, dml, dmf = world
    return Services(find_game=lambda: Resolution(g, "steam", [g]), is_running=lambda: running,
                    installed_state=lambda game, k: ("missing", None),
                    resolve_options=lambda kind, premium, key: ([local_option(kind, dml if kind == "dml" else dmf)], ""),
                    check_key=lambda k: KeyCheck(True, False, "20000 daily", "Key ok."))


def ok_run(plan, progress, cancel, deps=None):
    progress(ProgressEvent("app", "start", 0, 1))
    progress(ProgressEvent("app", "done", 0, 1, "ok"))
    return SetupResult(ok=True, steps=[StepResult("app", "done", "ok")], mods_on=True)


def make(world, run_fn=ok_run, **kw):
    model = SetupModel(services(world))
    ctl = Controller(model, run_fn=run_fn)
    return WizardSession(Bridge(), model=model, controller=ctl, **kw)


def settle(s, until=None, timeout=5.0):
    if until is None:
        s.ctl.join(timeout)
        s.pump()
        return
    end = time.time() + timeout
    while time.time() < end:
        s.pump()
        if until():
            return
        time.sleep(0.01)
    raise AssertionError("did not settle")


def js(s):
    return json.dumps(s.state())


def test_flow_and_gating_match_the_model(world):
    s = make(world)
    s.start()
    settle(s)
    st = s.state()
    assert st["page"] == "express" and st["express"]["ok"] and st["nav"]["visible"] is False
    s.to_wizard()
    for expected in ("welcome", "game", "key", "components", "review"):
        settle(s)
        st = s.state()
        ok, why = s.model.can_next()
        if s.model.page is Page.REVIEW:
            ok, why = s.model.install_ready()
        assert st["page"] == expected and st["nav"]["next"]["enabled"] == ok and st["nav"]["reason"] == ("" if ok else why)
        json.dumps(st)
        if expected == "welcome":
            s.next()
            assert st["nav"]["next"]["enabled"] is False and s.state()["page"] == "welcome"  # gated until accepted
            s.accept(True)
        elif expected == "key":
            s.set_key(KEY)
            assert s.state()["nav"]["next"]["enabled"] is False  # typed but not validated
            s.validate_key()
            settle(s, lambda: s.state()["key"]["ok"])
            assert KEY not in js(s) and s.state()["key"]["has_key"] is True
        elif expected == "components":
            s.toggle("dmf", False)
            s.toggle("dmf", True)
        if expected != "review":
            s.next()
    assert s.state()["nav"]["next"]["label"] == "Install"
    assert [r["label"] for r in s.state()["review"]["rows"]]
    s.back()
    assert s.state()["page"] == "components"


def test_key_never_in_any_payload_and_skip(world):
    s = make(world)
    s.start()
    s.to_wizard()
    s.accept(True)
    s.next()
    settle(s)
    s.next()
    assert s.state()["page"] == "key"
    s.set_key(KEY)
    assert KEY not in js(s)
    s.validate_key()
    settle(s, lambda: s.state()["key"]["ok"])
    s.next()
    s.back()
    while not s.bridge.ctl.q.empty():
        assert KEY not in json.dumps(s.bridge.ctl.q.get_nowait(), default=str)
    s2 = make(world)
    s2.model.page = Page.KEY
    s2.in_wizard = True
    s2.skip_key()
    assert s2.state()["page"] == "components" and s2.state()["key"]["skipped"]


def test_express_install_runs_to_finish(world):
    s = make(world)
    s.start()
    settle(s)
    s.install()
    assert s.state()["page"] == "progress" and [x["name"] for x in s.state()["progress"]["steps"]]
    settle(s, lambda: s.state()["page"] == "finish")
    f = s.state()["finish"]
    assert f["ok"] and f["title"] == "All done" and f["lines"] and s.copy_details()
    json.dumps(s.state())


def test_cancel_during_progress(world):
    started = threading.Event()

    def slow(plan, progress, cancel, deps=None):
        progress(ProgressEvent("app", "start", 0, 1))
        started.set()
        cancel.wait(5)
        return SetupResult(cancelled=True)

    s = make(world, run_fn=slow)
    s.start()
    settle(s)
    s.install()
    assert started.wait(5)
    s.cancel()
    assert s.state()["progress"]["cancelling"] and s.state()["nav"]["cancel"]["label"] == "Cancelling..."
    settle(s, lambda: s.state()["page"] == "finish")
    assert s.state()["finish"]["cancelled"] and s.state()["finish"]["title"] == "Setup was cancelled"
    quit_calls = []
    s.on_quit = lambda: quit_calls.append(1)
    s.cancel()  # not on Progress any more: closes the window
    assert quit_calls


def test_confirm_round_trip_reaches_the_worker(world):
    seen = {}

    def asks(plan, progress, cancel, deps=None):
        seen["answer"] = deps.confirm("Hashes differ", "Install anyway?")
        return SetupResult(ok=True)

    s = make(world, run_fn=asks)
    s.start()
    settle(s)
    s.install()
    payload = None
    end = time.time() + 5
    while payload is None and time.time() < end:
        s.pump()
        while not s.bridge.ctl.q.empty():
            p = s.bridge._translate(s.bridge.ctl.q.get_nowait())
            if p and p.get("type") == "ask":
                payload = p
        time.sleep(0.01)
    assert payload and payload["kind"] == "confirm" and payload["title"] == "Hashes differ"
    assert s.bridge.answer(payload["id"], True)
    settle(s, lambda: s.state()["page"] == "finish")
    assert seen["answer"] is True


def test_dry_run_reaches_a_dry_run_finish(world):
    model = SetupModel(services(world))
    ctl = Controller(model, run_fn=functools.partial(simulated_setup, seconds=0))
    s = WizardSession(Bridge(), model=model, controller=ctl, dry_run=True)
    s.start()
    settle(s)
    s.install()
    settle(s, lambda: s.state()["page"] == "finish")
    f = s.state()["finish"]
    assert f["dry_run"] and f["ok"] and s.state()["dry_run"] is True
    launched = []
    s.launch = lambda: launched.append(1)
    s.on_quit = lambda: launched.append("quit")
    s.finish()
    end = time.time() + 5
    while "quit" not in launched and time.time() < end:
        time.sleep(0.01)
    assert launched[-1] == "quit"


def test_game_browse_and_typed(world, tmp_path):
    s = make(world)
    s.model.page, s.in_wizard = Page.GAME, True
    bad = tmp_path / "nothing"
    bad.mkdir()
    s.pick_folder = lambda: str(bad)
    s.browse_game()
    assert s.state()["game"]["path"] == "" and "Darktide" in s.state()["game"]["message"] and s.state()["nav"]["next"]["enabled"] is False
    s.typed_game(f'"{world[0]}"')
    assert s.state()["game"]["path"] == str(world[0]) and s.state()["nav"]["next"]["enabled"] is True


def test_invalid_inputs_are_rejected(world):
    s = make(world)
    api = SetupApi(s)
    assert api.toggle("app", True)["ok"] is False
    assert api.toggle("dml", "yes")["ok"] is False
    assert api.set_option("nope", True)["ok"] is False
    assert api.set_option("launch", 1)["ok"] is False
    assert api.typed_game("x" * 501)["ok"] is False
    assert api.typed_game(5)["ok"] is False
    assert api.set_key("k" * 201)["ok"] is False
    assert api.accept("true")["ok"] is False
    assert api.select_source("dml", 0)["ok"] is False and api.select_source("zzz", 0)["ok"] is False
    assert api.open_source("nexus")["ok"] is False and api.choose_zip("app")["ok"] is False
    assert api.answer(999, True)["ok"] is False
    assert api.set_option("enable_mods", False)["ok"] and s.model.enable_mods is False
    assert api.typed_game("x" * 500)["ok"] is True
    assert api.copy_details() == {"ok": True, "text": ""}


def test_escape_rules_and_close(world):
    s = make(world)
    s.start()
    settle(s)
    quits = []
    s.on_quit = lambda: quits.append(1)
    s.escape()  # express screen: Esc does nothing
    assert not quits
    s.to_wizard()
    s.escape()  # Welcome: closes without asking
    assert quits
    assert s.on_close() is True


def test_entry_gate_flags_and_delegation(monkeypatch):
    from cogitator_web import setup_app

    errors = []
    assert setup_app.main(["--wizard"], build=1, show_error=lambda t, b: errors.append(b)) == 1 and errors
    calls = []
    runner = lambda argv, flags: calls.append(("web", flags.dry_run)) or 0
    assert setup_app.main(["--wizard", "--dry-run"], build=22631, installed=False, runner=runner) == 0 and calls == [("web", True)]
    monkeypatch.setattr(setup_app, "_classic", lambda argv: calls.append(("tk", argv)) or 0)
    un = lambda argv, flags, mode: calls.append(("un", mode)) or 0
    assert setup_app.main(["--uninstall"], build=22631, installed=True, runner=runner, uninstall_runner=un) == 0 and calls[-1] == ("un", "uninstall")
    assert setup_app.main([], build=22631, installed=True, runner=runner, uninstall_runner=un) == 0 and calls[-1] == ("un", "maintenance")
    assert setup_app.main(["--self-check"], build=22631, installed=True, runner=runner, uninstall_runner=un) == 0 and calls[-1] == ("tk", ["--self-check"])
    assert setup_app.main(["--help"]) == 0
