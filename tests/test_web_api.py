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


def test_drag_and_drop_move_to(web):
    win, s, api, _ = web
    assert api.move_to("alpha", "beta", "yes")["ok"] is False and api.move_to("nope", "beta", True)["ok"] is False
    n = len(win.of("state"))
    assert api.move_to("alpha", "beta", True)["ok"] is True
    st = wait(lambda: win.of("state")[n:])[-1]
    assert [r["folder"] for r in st["rows"]][:2] == ["beta", "alpha"]


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
    en = {i["id"]: i["enabled"] for i in items if i}
    assert en["update"] is False and "changelog" not in en  # not linked, nothing to update
    assert en["link"] is True
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
        assert q["kind"] == "choice" and q["options"][-1] == "Not now" and "GitHub" in q["options"][1]
        Api(s).answer(q["id"], 2)
        s.ctl.join(5)
        assert win.of("state") and win.of("header")[-1]["header"]["frameworks"]["dml"]["status"] != "installed"
        wait(lambda: any("not installed" in p["text"] for p in win.of("log")))
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


def test_missing_webview2_says_so_and_returns_nonzero(monkeypatch):
    calls = []
    monkeypatch.setattr(web_app, "notify", calls.append)
    assert web_app.run_web(["--x"], webview2_check=lambda: False) == 4
    assert "WebView2" in calls[0] and "cogitator --help" in calls[0]


def test_installed_pywebview_has_the_parameters_app_uses():
    import inspect

    webview = pytest.importorskip("webview")
    params = inspect.signature(webview.create_window).parameters
    for name in ("hidden", "js_api", "min_size", "background_color", "text_select", "width", "height", "x", "y"):
        assert name in params, name
    assert "gui" in inspect.signature(webview.start).parameters and hasattr(webview.FileDialog, "FOLDER")


# ---------------------------------------------------------------- link / update / framework / changelog (W2)
def _row(folder, status="up to date", nid=7, **kw):
    from cogitator.updates import ModRow

    return ModRow(folder, nid, status, kw.get("installed", "1.0"), kw.get("latest", "2.0"), "newer file", baseline="chosen")


def _set_rows(s, *rows):
    with s._mlock:
        s.model.set_rows(rows)


def _calls(monkeypatch, **fakes):
    from cogitator_web import session as sess

    seen: list[tuple] = []
    for name, ret in fakes.items():
        monkeypatch.setattr(sess.actions_nexus, name, lambda *a, _n=name, _r=ret, **k: (seen.append((_n, a[2:])), _r)[1])
    return seen


def test_row_menu_enables_by_state(web):
    from cogitator.updates import UPDATE

    _, s, api, _ = web
    items = {i["id"]: i for i in api.row_menu("alpha")["items"] if i}
    assert items["link"]["enabled"] and not items["update"]["enabled"] and "changelog" not in items
    _set_rows(s, _row("alpha", UPDATE))
    items = {i["id"]: i for i in api.row_menu("alpha")["items"] if i}
    assert items["update"]["enabled"] and items["changelog"]["enabled"] and items["open_page"]["enabled"]
    assert not any("coming soon" in i["label"] for i in items.values())
    json.dumps(items)


def test_new_api_methods_validate_and_serialise(web, monkeypatch):
    _, s, api, _ = web
    _calls(monkeypatch, link_all=None)
    for call in (api.update_all, api.link_all, api.framework_update):
        r = call()
        assert r["ok"] is True
        json.dumps(r)
    for bad in ("nope", None, 5, "../x"):
        assert api.row_action(bad, "link")["ok"] is False and api.row_action(bad, "update")["ok"] is False
        assert api.row_action(bad, "changelog")["ok"] is False
    s.ctl.join(5)


def test_link_parses_the_reference_and_links(web, monkeypatch):
    win, s, api, _ = web
    seen = _calls(monkeypatch, link_one_mod=None)
    assert api.row_action("alpha", "link") == {"ok": True}
    q = wait(lambda: asks(win))[0]
    assert q["kind"] == "text"
    api.answer(q["id"], "https://www.nexusmods.com/warhammer40kdarktide/mods/1234")
    wait(lambda: seen)
    assert seen[0] == ("link_one_mod", ("alpha", 1234))


def test_link_refuses_an_internal_folder(web, monkeypatch):
    win, s, api, _ = web
    seen = _calls(monkeypatch, link_one_mod=None)
    s.link("dmf")
    assert not seen and not asks(win)
    assert any("belongs to the loader" in p["text"] for p in wait(lambda: win.of("log")))


def test_update_with_free_account_never_calls_premium(web, monkeypatch):
    from cogitator.updates import UPDATE

    _, s, api, _ = web
    _set_rows(s, _row("alpha", UPDATE))
    seen = _calls(monkeypatch, is_premium=False, manual_update=None, premium_update=actions.Outcome(), free_update_all_help=None)
    api.row_action("alpha", "update")
    wait(lambda: any(n == "manual_update" for n, _ in seen))
    api.update_all()
    wait(lambda: any(n == "free_update_all_help" for n, _ in seen))
    assert not any(n == "premium_update" for n, _ in seen)


def test_update_with_nothing_to_do_just_logs(web, monkeypatch):
    win, s, api, _ = web
    seen = _calls(monkeypatch, premium_update=actions.Outcome())
    api.row_action("alpha", "update")
    api.update_all()
    assert not seen and not asks(win)
    assert any("nothing to update" in p["text"] for p in wait(lambda: win.of("log")))


def test_changelog_is_shown_as_a_notice(web, monkeypatch):
    from cogitator.actions_nexus import Changelog

    win, s, api, _ = web
    _set_rows(s, _row("alpha"))
    _calls(monkeypatch, changelog_for=Changelog(True, [("2.0  fixed things", ""), ("1.0  first", "")]))
    api.row_action("alpha", "changelog")
    q = wait(lambda: asks(win))[0]
    assert q["kind"] == "notice" and q["title"] == "alpha changelog" and q["body"] == "2.0  fixed things\n1.0  first"
    json.dumps(q)


# ---------------------------------------------------------------- profiles, snapshots, bulk (W3)
def test_profiles_and_snapshots_reject_bad_input_and_serialise(web):
    _, s, api, _ = web
    for bad in ("nope", None, 5, ["a"], "../x", ""):
        assert api.profile_switch(bad)["ok"] is False and api.profile_delete(bad)["ok"] is False
        assert api.snapshot_restore(bad)["ok"] is False
    assert api.profile_save("x" * 61)["ok"] is False and api.profile_save("../evil")["ok"] is False and api.profile_save(5)["ok"] is False
    assert api.profile_import_code("c" * 50001)["ok"] is False and api.profile_import_code(None)["ok"] is False and api.profile_import_code("  ")["ok"] is False
    for keep in (0, -1, 1001, "5", True, 2.5, None):
        assert api.snapshot_prune(keep)["ok"] is False
    assert api.snapshot_create("r" * 300)["ok"] is False and api.snapshot_create(7)["ok"] is False
    assert api.bulk("format")["ok"] is False and api.bulk(3)["ok"] is False
    for r in (api.profiles_list(), api.snapshots_list(), api.bulk_menu(), api.snapshot_prune(5)):
        assert r["ok"] is True
        json.dumps(r)


def test_profile_share_save_and_switch_asks_first(web, monkeypatch):
    win, s, api, _ = web
    r = api.profile_share()
    assert r["ok"] and r["code"].startswith("cog1:")
    assert api.profile_save("duo") == {"ok": True}
    wait(lambda: any("Saved profile" in p["text"] for p in win.of("log")))
    items = api.profiles_list()["items"]
    assert [p["name"] for p in items] == ["duo"]
    from cogitator_web import session as sess

    called = []
    monkeypatch.setattr(sess.actions_setup, "switch_profile", lambda *a, **k: (called.append(1), actions.Outcome())[1])
    assert api.profile_switch("duo") == {"ok": True}
    q = wait(lambda: asks(win))[0]
    assert q["kind"] == "confirm" and "duo" in q["title"] and not called
    api.answer(q["id"], True)
    wait(lambda: called)


def test_snapshot_create_then_listed(web):
    win, s, api, _ = web
    assert api.snapshot_create("test") == {"ok": True}
    items = wait(lambda: api.snapshots_list()["items"])
    assert items and set(items[0]) == {"name", "label"}


def test_bulk_touches_only_existing_marked_mods(web, monkeypatch):
    from cogitator_web import session as sess

    win, s, api, _ = web
    assert api.bulk("enable")["ok"] is True  # nothing marked: only a log line
    assert not [i for i in api.bulk_menu()["items"] if i["enabled"]]
    api.mark("alpha")
    api.mark("gamma")
    with s._mlock:
        s.model.marked.add("ghost")
    assert all(i["enabled"] for i in api.bulk_menu()["items"])
    seen = []
    monkeypatch.setattr(sess.actions_setup, "bulk_set_enabled", lambda ctx, folders, enable: (seen.append((folders, enable)), [])[1])
    api.bulk("disable")
    wait(lambda: seen)
    assert seen[0] == (["alpha", "gamma"], False)
    api.bulk("clear")
    assert not s.model.marked


# ---------------------------------------------------------------- W4: ticks and tools
def _link():
    from types import SimpleNamespace
    return SimpleNamespace(mod_id=1, file_id=2)


def test_nxm_tick_guards_one_link_and_clears_busy(web, monkeypatch):
    from cogitator import actions_nexus
    from cogitator_web import session as sess
    win, s, _, _ = web
    calls = []
    monkeypatch.setattr(sess.nxm, "pending", lambda on_expired=None: [("p1", _link()), ("p2", _link())])
    monkeypatch.setattr(actions_nexus, "nxm_install", lambda ctx, ui, path, link, after: calls.append(path))
    s.bridge._pending[99] = ("confirm", {}, {})  # a question is open
    s.nxm_tick()
    assert not calls and not s._nxm_busy
    s.bridge._pending.clear()
    s.job("slow", "install", lambda: time.sleep(0.3))
    s.nxm_tick()
    assert not calls
    wait(lambda: not s.ctl.busy("install"))
    s.nxm_tick()
    wait(lambda: calls and not s._nxm_busy)
    assert calls == ["p1"]


def test_nxm_tick_clears_busy_when_install_raises(web, monkeypatch):
    from cogitator import actions_nexus
    from cogitator_web import session as sess
    win, s, _, _ = web
    monkeypatch.setattr(sess.nxm, "pending", lambda on_expired=None: [("p", _link())])

    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(actions_nexus, "nxm_install", boom)
    s.nxm_tick()
    wait(lambda: any("nxm link failed" in x["text"] for x in win.of("log")))
    assert not s._nxm_busy


def test_watch_tick_acts_per_step_and_never_overlaps(web, monkeypatch):
    from cogitator import actions_setup
    from cogitator_web import session as sess
    win, s, _, _ = web
    repaired = []
    monkeypatch.setattr(actions_setup, "repair_patch", lambda ctx: repaired.append(1) or [])
    monkeypatch.setattr(sess, "patch_state", lambda g: "off")
    for act in ("notify", "patch"):
        s._last_patch_state = "on"
        monkeypatch.setattr(sess.repair, "watch_step", lambda *a, act=act: ("off", True, act))
        s.watch_tick()
        wait(lambda: s._last_patch_state == "off")
        wait(lambda: not s.ctl.busy("watch") and not s.ctl.busy("patch"))
        if act == "notify":
            assert not repaired
            wait(lambda: any(actions_setup.REPAIR_NOTICE in x["text"] and "Mods on/off" in x["text"] for x in win.of("log")))
    wait(lambda: repaired == [1])
    s.job("w", "watch", lambda: time.sleep(0.3))
    n = len(repaired)
    s.watch_tick()  # busy: must not start another
    assert len(repaired) == n and s.ctl.busy("watch")


def test_ticker_survives_errors_and_stops_on_release(game, monkeypatch):
    win, bridge = FakeWindow(), Bridge()
    s = Session(bridge, Settings.load(), game_dir=game, check_updates=False, show_notice=False)
    bridge.attach(win)
    hits = []
    monkeypatch.setattr(s, "nxm_tick", lambda: (hits.append(1), 1 / 0))
    s._start_ticker()
    wait(lambda: len(hits) >= 2, timeout=8)
    assert s._ticker.is_alive()
    s.release()
    wait(lambda: not s._ticker.is_alive())


def test_tools_api_validation_and_results(web, monkeypatch):
    win, s, api, _ = web
    assert not api.search_nexus("")["ok"] and not api.search_nexus("  ")["ok"]
    assert not api.search_nexus("x" * 101)["ok"] and not api.search_nexus(5)["ok"]
    from cogitator import actions_nexus
    got = []
    monkeypatch.setattr(actions_nexus, "search_nexus", lambda ctx, ui, t: got.append(t))
    r = api.search_nexus(" dmf ")
    assert r == {"ok": True}
    wait(lambda: got == ["dmf"])
    json.dumps([api.bisect(), api.crashlog()])
    wait(lambda: asks(win) or s.ctl.busy("net") is False)


def test_crashlog_notice_is_plain_text_and_no_game_is_refused(web, game):
    win, s, api, _ = web
    api.crashlog()
    a = wait(lambda: [x for x in asks(win) if x.get("title") == "Crash-log leads"])[0]
    assert a["kind"] == "notice" and isinstance(a["body"], str) and "<" not in a["body"].replace("<unknown>", "")
    s.game_dir = None
    assert api.bisect() == {"ok": True}  # refused with a log line, not an error
    wait(lambda: any("No game folder" in x["text"] for x in win.of("log")))


# ---------------------------------------------------------------- window background
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 64
WEBP = b"RIFF\x10\0\0\0WEBPVP8 " + b"\0" * 32


def test_background_choice_validation_and_round_trip(web, tmp_path):
    from cogitator.config import config_dir

    _, s, api, _ = web
    assert api.get_settings()["settings"]["gui_background"] == "default"
    assert api.set_setting("gui_background", "plain")["settings"]["gui_background"] == "plain"
    assert api.set_setting("gui_background", "neon")["ok"] is False
    assert api.set_setting("gui_background", "custom")["ok"] is False  # no image yet
    assert api.get_background()["data"] is None
    for name, data, why in (("a.png", b"not an image at all", "bad magic"), ("a.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "svg"),
                            ("a.png", PNG + b"\0" * (4 * 1024 * 1024), "oversize"), ("a.png.png", JPG[:2], "short"), ("a.gif", b"GIF89a" + b"\0" * 20, "gif")):
        f = tmp_path / name
        f.write_bytes(data)
        s.pick_image = lambda f=f: str(f)
        assert api.pick_background()["ok"] is False, why
    assert not list(config_dir().glob("background.*"))
    f = tmp_path / "pic.jpeg"  # the bytes decide the format, not the extension
    f.write_bytes(PNG)
    s.pick_image = lambda: str(f)
    r = api.pick_background()
    assert r["ok"] and r["settings"]["gui_background"] == "custom" and (config_dir() / "background.png").is_file()
    assert api.get_background()["data"].startswith("data:image/png;base64,")
    assert Settings.load().gui_background == "custom"
    f2 = tmp_path / "w.webp"
    f2.write_bytes(WEBP)
    s.pick_image = lambda: str(f2)
    assert api.pick_background()["ok"] and not (config_dir() / "background.png").exists()  # replaced, not accumulated
    assert api.get_background()["data"].startswith("data:image/webp;")
    s.pick_image = lambda: None
    assert api.pick_background() == {"ok": True, "settings": api.get_settings()["settings"]}  # cancelled: nothing changes
    r = api.reset_background()
    assert r["settings"]["gui_background"] == "default" and not list(config_dir().glob("background.*")) and api.get_background()["data"] is None


def test_a_custom_setting_without_its_file_falls_back_to_default(web):
    _, s, api, _ = web
    s.settings.gui_background = "custom"
    assert api.get_settings()["settings"]["gui_background"] == "default"


# ---------------------------------------------------------------- details panel
def test_details_unlinked_mod_answers_at_once_without_any_network(web, monkeypatch):
    from cogitator import nexus_info

    win, s, api, _ = web
    monkeypatch.setattr(nexus_info, "details_for", lambda *a, **k: pytest.fail("no Nexus lookup for an unlinked mod"))
    assert api.mod_details("alpha") == {"ok": True, "started": True}
    wait(lambda: win.of("details"))
    p = win.of("details")[0]
    assert (p["folder"], p["status"], p["data"], p["image"]) == ("alpha", "unlinked", None, None)
    assert api.mod_details("../x")["ok"] is False and api.mod_details("nosuchmod")["ok"] is False and api.mod_versions(5)["ok"] is False


def test_details_linked_mod_without_key_makes_no_call_and_shows_cache(web, monkeypatch, tmp_path):
    from cogitator import nexus_info

    win, s, api, _ = web
    s.registry.set_mod("alpha", nexus_id=77)
    monkeypatch.setattr(nexus_info, "get_api_key", lambda: None)
    monkeypatch.setattr(nexus_info, "NexusClient", lambda *a, **k: pytest.fail("no key: no client"))
    assert api.mod_details("alpha")["ok"]
    wait(lambda: win.of("details"))
    p = win.of("details")[-1]
    assert p["status"] == "no_key" and p["nexus_id"] == 77 and "Settings" in p["message"] and p["data"] is None


def test_details_push_carries_data_and_image_only_for_the_open_panel(web, monkeypatch):
    from cogitator import nexus_info

    win, s, api, _ = web
    s.registry.set_mod("alpha", nexus_id=77)
    data = {"name": "Alpha", "picture_url": "https://staticdelivery.nexusmods.com/a.png", "description": "d", "summary": "s", "author": "me",
            "version": "1", "endorsements": 1, "downloads": 2, "updated": 3}
    fetched = []
    monkeypatch.setattr(nexus_info, "details_for", lambda mod_id, reg, **k: nexus_info.Result("ok", data, at=1.0))
    monkeypatch.setattr(nexus_info, "cached_image", lambda url: None)
    monkeypatch.setattr(nexus_info, "fetch_image", lambda url, *a: fetched.append(url) or "data:image/png;base64,AAAA")
    api.mod_details("alpha")
    wait(lambda: win.of("details"))
    p = win.of("details")[-1]
    assert p["status"] == "ok" and p["data"]["name"] == "Alpha" and p["image"].startswith("data:image/png") and fetched == [data["picture_url"]]
    # a row refresh never fetches anything (thumbnails / scrolling must stay offline)
    s.refresh_all()
    assert fetched == [data["picture_url"]]


def test_details_burst_keeps_the_newest_request(web, monkeypatch):
    import threading

    from cogitator import nexus_info

    win, s, api, _ = web
    s.registry.set_mod("alpha", nexus_id=1)
    s.registry.set_mod("beta", nexus_id=2)
    gate, seen = threading.Event(), []

    def slow(mod_id, reg, **k):
        seen.append(mod_id)
        gate.wait(3)
        return nexus_info.Result("ok", {"name": str(mod_id), "picture_url": ""}, at=1.0)

    monkeypatch.setattr(nexus_info, "details_for", slow)
    api.mod_details("alpha")
    wait(lambda: seen)
    api.mod_details("beta")
    api.mod_details("alpha")
    api.mod_details("beta")  # the newest wins; older queued ones are dropped
    gate.set()
    wait(lambda: [p for p in win.of("details") if p["folder"] == "beta"])
    assert seen == [1, 2] and not [p for p in win.of("details") if p["status"] == "error"]


def test_versions_and_open_page(web, monkeypatch):
    from cogitator import nexus_info

    win, s, api, _ = web
    s.registry.set_mod("alpha", nexus_id=77)
    monkeypatch.setattr(nexus_info, "changes_for", lambda mod_id, reg, **k: nexus_info.Result("ok", {"versions": [{"version": "1", "lines": ["x"]}]}))
    api.mod_versions("alpha")
    wait(lambda: win.of("versions"))
    assert win.of("versions")[0]["versions"][0]["version"] == "1"
    opened = []
    s.open_url = opened.append
    api.open_nexus_page("alpha")
    assert opened == ["https://www.nexusmods.com/warhammer40kdarktide/mods/77"]
    api.open_nexus_page("beta")  # unlinked: nothing opens
    assert len(opened) == 1


def test_row_payload_tells_whether_a_rollback_exists(web):
    _, s, api, _ = web
    assert s.row_payload("alpha")["rollback"] is False
    s.registry.set_mod("alpha", previous={"version": "1"})
    assert s.row_payload("alpha")["rollback"] is True
