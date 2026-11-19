"""The web interface's Python side (api, bridge, session) against a fake game folder and a fake window. No window, no webview."""
import json
import time
import zipfile

import pytest

from cogitator import actions
from cogitator.config import Settings
from cogitator.registry import Registry
from cogitator_web import app as web_app
from cogitator_web import winstate
from cogitator_web.api import Api
from cogitator_web.bridge import ENTRY, Bridge
from cogitator_web.session import Session

KEY = "WebSecretKey-0123456789abcdef0123"


class FakeWindow:
    """Records what the pusher sends: each evaluate_js call carries `window.cogitatorPush([...])`."""

    def __init__(self):
        self.payloads: list[dict] = []
        self.scripts: list[str] = []

    def evaluate_js(self, script):
        assert script.startswith(ENTRY + "(") and script.endswith(")")
        self.scripts.append(script)
        self.payloads.extend(json.loads(script[len(ENTRY) + 1:-1]))

    def of(self, kind):
        return [p for p in self.payloads if p["type"] == kind]


def wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the condition")


@pytest.fixture
def game(tmp_path):
    g = tmp_path / "Darktide"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "mods").mkdir()
    (g / "mods" / "mod_load_order.txt").write_text("alpha\nbeta\n")
    for m in ("alpha", "beta", "gamma"):  # gamma exists but is not in the load order
        (g / "mods" / m).mkdir()
        (g / "mods" / m / f"{m}.mod").write_text("return {}")
    return g


@pytest.fixture
def web(game):
    win = FakeWindow()
    bridge = Bridge()
    s = Session(bridge, Settings.load(), game_dir=game, check_updates=False, show_notice=False)
    s.game_running = lambda: False
    s.game_dir, s.registry = game, Registry.load()
    api = Api(s)
    bridge.attach(win)
    s.refresh_all()
    wait(lambda: win.of("state"))
    yield win, s, api, bridge
    s.release()


def asks(win):
    return win.of("ask")


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("bad", ["nope", "../alpha", "a/b", "a\\b", "..", "", None, 5, ["alpha"], "alpha\0", "C:alpha"])
def test_unknown_or_hostile_folder_names_are_rejected(web, bad):
    _, _, api, _ = web
    for call in (lambda: api.toggle(bad), lambda: api.move(bad, 1), lambda: api.mark(bad), lambda: api.fav(bad), lambda: api.edit_note(bad),
                 lambda: api.row_menu(bad), lambda: api.row_action(bad, "toggle")):
        r = call()
        assert r["ok"] is False and "error" in r
        json.dumps(r)


def test_other_bad_arguments(web):
    _, _, api, _ = web
    assert api.move("alpha", 2)["ok"] is False and api.move("alpha", True)["ok"] is False and api.move("alpha", "1")["ok"] is False
    assert api.row_action("alpha", "format_c")["ok"] is False and api.row_action("alpha", 7)["ok"] is False
    assert api.set_sort("; drop")["ok"] is False and api.set_filter(123)["ok"] is False and api.set_filter("x" * 500)["ok"] is False
    assert api.set_setting("game_dir", "C:\\")["ok"] is False  # not on the page's list
    assert api.set_setting("check_updates_on_start", "yes")["ok"] is False
    assert api.set_setting("gui_theme", "neon")["ok"] is False
    assert api.set_setting("downloads_dir", "Z:\\no\\such\\folder")["ok"] is False
    assert api.set_setting(3, True)["ok"] is False
    for k in (None, 7, "short", "has space inside the key 12345", "k" * 300, "ключ-with-unicode-12345"):
        assert api.validate_key(k)["ok"] is False


def test_answer_rejects_unknown_ids_and_wrong_shapes(web):
    win, s, api, bridge = web
    assert api.answer(999, True)["ok"] is False and api.answer("1", True)["ok"] is False and api.answer(True, True)["ok"] is False
    s.job("ask", "t", lambda: s.ctl.ask("choice", question="q", options=["a", "b"], selected=0))
    qid = wait(lambda: asks(win))[0]["id"]
    for bad in (2, -1, "0", True, 1.5):
        assert api.answer(qid, bad)["ok"] is False
    assert api.answer(qid, 1)["ok"] is True
    assert api.answer(qid, 1)["ok"] is False  # answered once only


# ---------------------------------------------------------------- table and rows
def test_state_payload_and_row_updates(web):
    win, s, api, _ = web
    st = win.of("state")[-1]
    assert [r["folder"] for r in st["rows"]] == ["alpha", "beta", "gamma"]
    assert [r["en"] for r in st["rows"]] == ["on", "on", "unlisted"]
    r = api.toggle("alpha")
    assert r == {"ok": True}
    row = wait(lambda: [p for p in win.of("row") if p["row"]["folder"] == "alpha"])[-1]["row"]
    assert row["en"] == "off"
    assert "alpha" not in (s.game_dir / "mods" / "mod_load_order.txt").read_text().split() or "#" in (s.game_dir / "mods" / "mod_load_order.txt").read_text()
    api.toggle("gamma")  # unlisted -> added and enabled
    assert wait(lambda: [p for p in win.of("row") if p["row"]["folder"] == "gamma"])[-1]["row"]["en"] == "on"


def test_move_sort_filter_and_marks(web):
    win, s, api, _ = web
    n = len(win.of("state"))
    api.move("beta", -1)
    st = wait(lambda: win.of("state")[n:])[-1]
    assert [r["folder"] for r in st["rows"]][:2] == ["beta", "alpha"]
    api.set_filter("gam")
    st = wait(lambda: [p for p in win.of("state") if p["filter"] == "gam"])[-1]
    assert [r["folder"] for r in st["rows"]] == ["gamma"]
    api.set_filter("")
    api.set_sort("mod")
    wait(lambda: [p for p in win.of("state") if p["sort"]["col"] == "mod"])
    api.mark("alpha")
    assert wait(lambda: [p for p in win.of("row") if p["row"]["marked"]])[-1]["row"]["folder"] == "alpha"
    api.fav("beta")
    assert wait(lambda: [p for p in win.of("row") if p["row"]["fav"]])[-1]["row"]["folder"] == "beta"


def test_row_menu_has_the_tk_entries(web):
    _, _, api, _ = web
    items = api.row_menu("alpha")["items"]
    ids = [i["id"] for i in items if i]
    assert {"toggle", "up", "down", "mark", "fav", "note", "remove"} <= set(ids)
    assert all(not i["enabled"] for i in items if i and i["id"] in ("update", "changelog"))  # later phase: listed but disabled
    json.dumps(items)


def test_every_pushed_payload_is_json_serialisable(web):
    win, s, api, _ = web
    api.toggle("beta")
    api.set_sort("status")
    api.mark("gamma")
    s.say("hello", "green")
    wait(lambda: win.of("log"))
    for script in win.scripts:
        json.loads(script[len(ENTRY) + 1:-1])
    json.dumps(api.get_settings())
    json.dumps(api.boot())
    json.dumps(api.help())


# ---------------------------------------------------------------- confirm, cancel, workers
def test_confirm_round_trip_removes_the_mod(web):
    win, s, api, _ = web
    assert api.row_action("beta", "remove") == {"ok": True}
    q = wait(lambda: asks(win))[0]
    assert q["kind"] == "confirm" and "beta" in q["title"]
    assert (s.game_dir / "mods" / "beta").exists()  # nothing happens before the answer
    assert api.answer(q["id"], True) == {"ok": True}
    wait(lambda: not (s.game_dir / "mods" / "beta").exists())
    st = wait(lambda: [p for p in win.of("state") if "beta" not in [r["folder"] for r in p["rows"]]])[-1]
    assert [r["folder"] for r in st["rows"]] == ["alpha", "gamma"]


def test_cancel_keeps_the_mod(web):
    win, s, api, _ = web
    api.row_action("beta", "remove")
    q = wait(lambda: asks(win))[0]
    api.answer(q["id"], False)
    s.ctl.join(5)
    assert (s.game_dir / "mods" / "beta").exists()
    assert not any("Removed" in p["text"] for p in win.of("log"))


def test_note_dialog_round_trip(web):
    win, s, api, _ = web
    api.edit_note("alpha")
    q = wait(lambda: asks(win))[0]
    assert q["kind"] == "text" and q["allow_blank"] is True and q["password"] is False
    api.answer(q["id"], "#ui #hud keep this")
    row = wait(lambda: [p for p in win.of("row") if p["row"]["folder"] == "alpha" and p["row"]["tags"]])[-1]["row"]
    assert row["tags"] == ["ui", "hud"] and row["note"] is True


def test_worker_exception_is_reported_not_raised(web):
    win, s, _, _ = web
    s.job("boom", "t", lambda: 1 / 0)
    p = wait(lambda: [x for x in win.of("log") if "boom failed" in x["text"]])[0]
    assert "ZeroDivisionError" in p["text"] and p["style"] == "bold red"


def test_closing_releases_a_blocked_worker(game):
    win, bridge = FakeWindow(), Bridge()
    s = Session(bridge, Settings.load(), game_dir=game, check_updates=False)
    bridge.attach(win)
    done = []
    s.job("ask", "t", lambda: done.append(s.ctl.ask("confirm", title="t", body="b", default_yes=True)))
    wait(lambda: win.of("ask"))
    s.release()
    wait(lambda: done)
    assert done == [None]


def test_import_flow_asks_then_installs(web, tmp_path):
    win, s, api, _ = web
    z = tmp_path / "delta-9-1-0.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("delta/delta.mod", "return {}")
        f.writestr("delta/scripts/mods/delta/delta.lua", "return {}")
    s.pick_files = lambda start: [str(z)]
    assert api.import_zips() == {"ok": True}
    seen = []
    while not (s.game_dir / "mods" / "delta").exists():
        wait(lambda: len(asks(win)) > len(seen) or (s.game_dir / "mods" / "delta").exists())
        if len(asks(win)) <= len(seen):
            break
        q = asks(win)[len(seen)]
        seen.append(q)
        api.answer(q["id"], "" if q["kind"] == "text" else (True if q["kind"] == "confirm" else 0))
    wait(lambda: any("Installed delta" in p["text"] for p in win.of("log")))
    wait(lambda: "delta" in [r["folder"] for r in win.of("state")[-1]["rows"]])


# ---------------------------------------------------------------- start-up chain
def test_start_chain_asks_for_the_framework_bootstrap(game):
    win, bridge = FakeWindow(), Bridge()
    s = Session(bridge, Settings.load(), game_dir=game, check_updates=False, show_notice=True)
    s.game_running = lambda: False
    bridge.attach(win)
    try:
        assert s.start() is True and s.start() is False
        n1 = wait(lambda: asks(win))[0]
        assert n1["kind"] == "notice" and n1["dismissable"] is False and n1["button"] == "I understand"
        assert not Settings.load().notice_accepted
        assert Api(s).answer(n1["id"], True)["ok"]
        wait(lambda: Settings.load().notice_accepted)
        q = wait(lambda: asks(win)[1:])[0]
        assert q["kind"] == "choice" and q["options"][-1] == "Not now"
        Api(s).answer(q["id"], 1)
        s.ctl.join(5)
        assert win.of("state") and win.of("header")[-1]["header"]["frameworks"]["dml"]["status"] != "installed"
        assert any("not installed" in p["text"] for p in win.of("log"))
    finally:
        s.release()


def test_second_instance_is_told_and_quits(game):
    import os

    from cogitator.config import InstanceLock

    held = InstanceLock(game)
    held.path.parent.mkdir(parents=True, exist_ok=True)
    held.path.write_text(str(os.getppid()))  # another live process holds the lock
    win, bridge = FakeWindow(), Bridge()
    s = Session(bridge, Settings.load(), game_dir=game, check_updates=False)
    quit_calls = []
    s.on_quit = lambda: quit_calls.append(1)
    bridge.attach(win)
    try:
        s.start()
        q = wait(lambda: asks(win))[0]
        assert q["kind"] == "notice" and "already running" in q["title"]
        Api(s).answer(q["id"], True)
        wait(lambda: quit_calls)
    finally:
        s.release()
        held.path.unlink(missing_ok=True)


def test_not_found_uses_the_native_picker_and_validates(tmp_path, game):
    win, bridge = FakeWindow(), Bridge()
    s = Session(bridge, Settings.load(), check_updates=False, show_notice=False)
    s.game_running = lambda: False
    picks = iter([str(tmp_path / "not-a-game"), str(game)])
    s.pick_folder = lambda: next(picks)
    bridge.attach(win)
    try:
        s.start()
        wait(lambda: any("Not a Darktide folder" in p["text"] for p in win.of("log")))
        wait(lambda: win.of("state"))
        assert s.settings.game_dir == str(game)
    finally:
        s.release()


# ---------------------------------------------------------------- secrets
def test_key_never_appears_in_payloads_logs_or_results(web, monkeypatch, caplog):
    win, s, api, _ = web
    caplog.set_level("DEBUG")
    results = []

    def good(ctx, key):
        return actions.KeyOutcome(True, f"Key valid (free); stored. [{key}]", False)

    monkeypatch.setattr(actions, "store_key", good)
    results.append(api.validate_key(KEY))
    wait(lambda: win.of("key_result"))
    s.ctl.join(5)

    def bad(ctx, key):
        raise RuntimeError(f"network exploded for {key}")

    monkeypatch.setattr(actions, "store_key", bad)
    results.append(api.validate_key(KEY))
    wait(lambda: [p for p in win.of("log") if "validate key failed" in p["text"]])
    blob = json.dumps(win.payloads) + json.dumps(results) + caplog.text
    assert KEY not in blob
    assert results[0] == {"ok": True, "started": True}
    assert win.of("key_result")[0]["ok"] is True


# ---------------------------------------------------------------- settings file
def test_settings_save_to_the_shared_file_and_unknown_keys_survive_load(web, tmp_path):
    _, s, api, _ = web
    dl = tmp_path / "dl"
    dl.mkdir()
    assert api.set_setting("gui_theme", "dark")["ok"]
    assert api.set_setting("check_updates_on_start", False)["ok"]
    assert api.set_setting("auto_repair", "auto")["ok"]
    assert api.set_setting("downloads_dir", str(dl))["ok"]
    again = Settings.load()
    assert (again.gui_theme, again.check_updates_on_start, again.auto_repair, again.downloads_dir) == ("dark", False, "auto", str(dl))
    path = Settings.path()
    path.write_text(path.read_text() + 'web_future_key = "x"\n')  # an unknown key must not break the other interfaces
    assert Settings.load().gui_theme == "dark"
    s.pick_folder = lambda: str(tmp_path)
    assert api.pick_downloads()["settings"]["downloads_dir"] == str(tmp_path)
    s.pick_folder = lambda: None
    assert api.pick_downloads()["cancelled"] is True


# ---------------------------------------------------------------- window state and fallback
def test_window_geometry_is_sanitised(tmp_path):
    screens = [(0, 0, 1920, 1080)]
    assert winstate.sane_geometry({"width": 1000, "height": 700, "x": 10, "y": 20}, screens) == {"width": 1000, "height": 700, "x": 10, "y": 20}
    off = winstate.sane_geometry({"width": 1000, "height": 700, "x": 9000, "y": 20}, screens)
    assert "x" not in off  # that monitor is gone
    junk = winstate.sane_geometry({"width": "wide", "height": -4, "x": True}, screens)
    assert junk == {"width": winstate.DEFAULT_SIZE[0], "height": winstate.DEFAULT_SIZE[1]}
    assert winstate.sane_geometry("nonsense") == junk
    winstate.save({"x": 5, "y": 6, "width": 1000, "height": 800, "evil": "x"})
    assert json.loads(winstate.state_path().read_text()) == {"x": 5, "y": 6, "width": 1000, "height": 800}
    assert winstate.load(screens)["width"] == 1000


def test_missing_webview2_falls_back_to_the_tk_gui(monkeypatch):
    calls = []
    monkeypatch.setattr(web_app, "fallback_to_gui", lambda msg, argv: calls.append(msg) or 0)
    assert web_app.run_web(["--x"], webview2_check=lambda: False) == 0
    assert "WebView2" in calls[0]


def test_missing_pywebview_falls_back_to_the_tk_gui(monkeypatch):
    calls = []
    monkeypatch.setattr(web_app, "fallback_to_gui", lambda msg, argv: calls.append(msg) or 0)
    monkeypatch.setitem(__import__("sys").modules, "webview", None)  # import webview -> ImportError
    assert web_app.run_web([], webview2_check=lambda: True) == 0
    assert "pywebview" in calls[0]


def test_installed_pywebview_has_the_parameters_app_uses():
    import inspect

    webview = pytest.importorskip("webview")
    params = inspect.signature(webview.create_window).parameters
    for name in ("hidden", "js_api", "min_size", "background_color", "text_select", "width", "height", "x", "y"):
        assert name in params, name
    assert "gui" in inspect.signature(webview.start).parameters and hasattr(webview.FileDialog, "FOLDER")
