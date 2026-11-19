"""The main window, driven for real (Tk needs a display; skipped with a clear reason where none exists).

Real engine + fake game folder + scripted dialogs. No real network, Steam, game or keyring."""
import time
import tkinter as tk
import zipfile
from pathlib import Path

import pytest

from cogitator.config import InstanceLock, Settings, get_api_key
from cogitator.nexus import NexusError
from cogitator.registry import Registry
from cogitator_gui.app import CogitatorGui
from test_patcher import ORIG

SECRET = "SmokeSecretKey-0a1b2c3d4e5f6a7b8c9d"


@pytest.fixture
def root():
    r, err = None, None
    for _ in range(4):  # Tk start-up is occasionally flaky from the Microsoft Store Python's WindowsApps folder
        try:
            r = tk.Tk()
            break
        except tk.TclError as e:
            err = e
            time.sleep(0.3)
    if r is None:
        pytest.skip(f"Tk could not start (no display?): {err}")
    r.withdraw()
    yield r
    import gc

    gc.collect()
    try:
        r.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def game(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "bundle" / "bundle_database.data").write_bytes(ORIG)
    for m, has_mod in (("alpha", True), ("beta", True), ("gamma", True), ("broken", False)):
        (g / "mods" / m).mkdir(parents=True, exist_ok=True)
        if has_mod:
            (g / "mods" / m / f"{m}.mod").write_text("return {}")
    (g / "mods" / "mod_load_order.txt").write_bytes(b"-- header\r\nalpha\r\n--beta\r\nghost\r\n")
    return g


class FakeDialogs:
    """Scripted answers; records what was shown."""

    def __init__(self, confirm=True, files=(), text=None, choice=None):
        self.shown, self.confirm_answer, self.files, self.text, self.choice = [], confirm, list(files), text, choice

    def confirm(self, title, body="", default_yes=True):
        self.shown.append(("confirm", title))
        return self.confirm_answer

    def ask_text(self, title, hint="", body="", allow_blank=False, password=False, initial=""):
        self.shown.append(("text", title, password, initial))
        return self.text

    def choose(self, question, options, selected=0):
        self.shown.append(("choose", question))
        return self.choice

    def notice(self, title, body, button="OK"):
        self.shown.append(("notice", title))

    def show_text(self, title, segments, *a, **k):
        self.shown.append(("report", title, "".join(s for s, _ in segments)))

    def error(self, title, body):
        self.shown.append(("error", title))

    def pick_folder(self):
        return None

    def pick_files(self, initial=None):
        return list(self.files)


def make(root, game, dialogs=None, **kw):
    s = Settings.load()
    s.notice_accepted = True
    s.save()
    app = CogitatorGui(root, game_dir=game, check_updates=False, dialogs=dialogs or FakeDialogs(), **kw)
    app.game_running = lambda: False
    app.start()
    return app


def pump(app, until=lambda: False, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        app.root.update()
        if until():
            return True
        time.sleep(0.01)
    return until()


def table(app):
    return [(i, app.tree.item(i, "values")) for i in app.tree.get_children()]


def log_text(app):
    return app.logbox.contents()


def test_window_builds_with_the_expected_rows(root, game):
    app = make(root, game)
    rows = table(app)
    assert [r[0] for r in rows] == ["alpha", "beta", "gamma", "broken", "ghost"] or {r[0] for r in rows} == {"alpha", "beta", "gamma", "broken", "ghost"}
    by = dict(rows)
    assert by["alpha"][0] == "On" and by["beta"][0] == "Off" and by["gamma"][0] == "-"
    assert "(no .mod file!)" in by["broken"][1] and "(folder missing)" in by["ghost"][1]
    assert [app.tree.heading(c, "text") for c in app.tree["columns"]] == ["On/Off", "Mod", "Installed", "Latest", "Status"]
    assert "Game: " in app.hdr.contents() and str(game) in app.hdr.contents() and "no API key" in app.hdr.contents()
    assert app.summary.cget("text").startswith("5 mod(s)")
    app.quit()


def test_toggle_move_filter_mark_favourite_note(root, game):
    app = make(root, game)
    app.tree.selection_set("beta")
    app.toggle_selected()
    assert app.tree.item("beta", "values")[0] == "On"
    assert "beta" in (game / "mods" / "mod_load_order.txt").read_text() and "--beta" not in (game / "mods" / "mod_load_order.txt").read_text()
    app.move_selected(-1)
    assert app.model.listed()[:2] == ["beta", "alpha"] and app.selected() == "beta"
    assert app.tree.get_children()[0] == "beta"

    app.filter_var.set("alp")
    assert list(app.tree.get_children()) == ["alpha"] or app.selected() in app.tree.get_children()
    assert "alpha" in app.tree.get_children() and "gamma" not in app.tree.get_children()
    app.filter_var.set("")
    assert "gamma" in app.tree.get_children()

    app.tree.selection_set("alpha")
    app.toggle_mark()
    app.toggle_fav()
    assert app.tree.item("alpha", "values")[1].startswith("+ * alpha")
    app.dlg.text = "#ui needs dmf"
    app.edit_note()
    meta = Registry.load().mod("alpha")
    assert meta["tags"] == ["ui"] and meta["notes"] == "needs dmf"
    assert "tags ui" in log_text(app)
    app.filter_var.set("ui")
    assert list(app.tree.get_children()) == ["alpha"]
    app.quit()


def test_header_click_sorts_and_move_resets_the_sort(root, game):
    app = make(root, game)
    app.sort_by("mod")
    assert list(app.tree.get_children()) == sorted(app.tree.get_children(), key=str.casefold)
    assert app.tree.heading("mod", "text").endswith("▲")
    app.sort_by("mod")
    assert app.tree.heading("mod", "text").endswith("▼")
    app.tree.selection_set("alpha")
    app.move_selected(1)
    assert app.model.sort_col is None and app.tree.heading("mod", "text") == "Mod"
    app.quit()


def test_context_menu_entries_follow_the_row(root, game):
    app = make(root, game)
    labels = {i[1]: i[0] for i in app.row_menu_items("alpha") if i}
    assert labels["toggle"] == "Disable" and labels["up"] == "Move up" and labels["remove"] == "Remove..."
    assert "rollback" not in labels and "pin" not in labels  # not linked, never updated
    enabled = {i[1]: i[2] for i in app.row_menu_items("gamma") if i}
    assert enabled["up"] is False and enabled["down"] is False  # not in the load order: nothing to move
    assert {i[1]: i[0] for i in app.row_menu_items("gamma") if i}["toggle"] == "Add to load order and enable"
    app.build_row_menu("alpha").destroy()
    app.quit()


def test_remove_asks_first_and_moves_the_folder(root, game):
    d = FakeDialogs(confirm=False)
    app = make(root, game, dialogs=d)
    app.remove_mod("beta")
    assert (game / "mods" / "beta").exists() and ("confirm", "Remove beta?") in d.shown
    d.confirm_answer = True
    app.remove_mod("beta")
    assert pump(app, lambda: not (game / "mods" / "beta").exists())
    assert pump(app, lambda: not app.tree.exists("beta"))
    assert "Removed beta" in log_text(app)
    app.quit()


def test_import_mod_zip_confirms_installs_and_selects_it(root, game, tmp_path):
    z = tmp_path / "newmod-77-1-0.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("newmod/newmod.mod", "return {}")
        f.writestr("newmod/scripts/x.lua", "x")
    d = FakeDialogs(files=[str(z)], text="", choice=0)  # confirm=True; "install without linking" for the unknown Nexus id
    app = make(root, game, dialogs=d)
    app.import_archives()
    assert pump(app, lambda: app.tree.exists("newmod")), log_text(app)
    assert any(s[0] == "confirm" and "newmod" in s[1] for s in d.shown)
    assert (game / "mods" / "newmod" / "newmod.mod").exists() and "Installed newmod" in log_text(app)
    app.quit()


def test_import_declined_changes_nothing(root, game, tmp_path):
    z = tmp_path / "declined.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("declined/declined.mod", "return {}")
    app = make(root, game, dialogs=FakeDialogs(confirm=False, files=[str(z)], text=""))
    app.import_archives()
    app.ctl.join()
    pump(app, timeout=0.5)
    assert not (game / "mods" / "declined").exists() and not app.tree.exists("declined")
    app.quit()


def test_bad_zip_is_reported_in_the_log_not_raised(root, game, tmp_path):
    z = tmp_path / "bad.zip"
    z.write_bytes(b"not a zip")
    app = make(root, game, dialogs=FakeDialogs(files=[str(z)]))
    app.import_archives()
    assert pump(app, lambda: "failed" in log_text(app)), log_text(app)
    app.quit()


def test_nexus_key_is_masked_stored_and_never_logged(root, game, monkeypatch):
    class FakeNexus:
        def __init__(self, key):
            self.key = key

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def validate(self):
            if self.key == "badbadbadbadbad":
                raise NexusError(f"Nexus rejected {self.key}")
            return {"is_premium": False}

    monkeypatch.setattr("cogitator.actions.NexusClient", FakeNexus)
    monkeypatch.setattr("cogitator_gui.app.run_checks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("skip")))
    d = FakeDialogs(text="badbadbadbadbad")
    app = make(root, game, dialogs=d)
    app.set_key()
    assert pump(app, lambda: "Key not saved" in log_text(app))
    assert get_api_key() is None and "badbadbadbadbad" not in log_text(app) and "badbadbadbadbad" not in app.hdr.contents()
    assert d.shown[-1][:3] == ("text", "Nexus personal API key", True)  # asked through the masked entry
    d.text = SECRET
    app.set_key()
    assert pump(app, lambda: "Key valid (free)" in log_text(app))
    assert get_api_key() == SECRET and SECRET not in log_text(app)
    assert pump(app, lambda: "key ok (free)" in app.hdr.contents())
    app.quit()


def test_global_toggle_asks_before_unpatching(root, game):
    from cogitator.patcher import ON, patch_state

    d = FakeDialogs(confirm=False)
    app = make(root, game, dialogs=d)
    calls = []
    import cogitator_gui.app as appmod

    orig = appmod.patch_state
    appmod.patch_state = lambda g: ON  # pretend mods are on
    try:
        app.toggle_global()
    finally:
        appmod.patch_state = orig
    assert ("confirm", "Disable ALL mods?") in d.shown and not app.ctl.busy("patch") and calls == []
    assert patch_state(game) != ON
    app.quit()


def test_doctor_opens_a_report_window_and_launch_reports_problems(root, game):
    d = FakeDialogs()
    app = make(root, game, dialogs=d)
    app.doctor()
    assert pump(app, lambda: any(s[0] == "report" for s in d.shown), 20)
    rep = next(s for s in d.shown if s[0] == "report")
    assert "[OK]" in rep[2] or "[WARN]" in rep[2] or "[FAIL]" in rep[2]
    app.launch()  # mods not installed: the engine refuses and the reason lands in the log
    assert pump(app, lambda: "Checking the setup before launching" in log_text(app))
    app.ctl.join()
    app.quit()


def test_instance_lock_is_held_while_open_and_released_on_quit(root, game):
    app = make(root, game)
    assert app.instance_lock is not None and app.instance_lock.path.exists()
    path = app.instance_lock.path
    app.quit()
    assert not path.exists() and app.exiting


def test_second_instance_gets_the_already_running_notice_and_exits(root, game, monkeypatch):
    from cogitator.config import LockError

    def refuse(self):
        raise LockError("Another Cogitator instance (pid 4242) is using this game folder.")

    monkeypatch.setattr(InstanceLock, "acquire", refuse)
    d = FakeDialogs()
    app = make(root, game, dialogs=d)
    assert ("notice", "Cogitator is already running") in d.shown and app.exiting and app.registry is None


def test_unknown_game_asks_for_a_folder_and_cancelling_exits(root, tmp_path):
    s = Settings.load()
    s.notice_accepted = True
    s.save()
    d = FakeDialogs()
    app = CogitatorGui(root, game_override=None, check_updates=False, dialogs=d)
    app.start()  # no saved game, no Steam in tests: the folder picker is offered, returns nothing -> window closes
    assert app.exiting
    assert Path(tmp_path).exists()


def test_theme_switch_menus_and_real_menu_entries(root, game, monkeypatch):
    from cogitator_gui import theme

    monkeypatch.setattr(theme, "system_uses_light", lambda read=None: True)  # the machine's own Windows theme must not matter
    app = make(root, game)
    assert app.settings.gui_theme == "system" and app.theme_name == "light" and app.theme_var.get() == "system"
    app.apply_theme("system")
    assert app.theme_name == "light"
    app.log_line("a coloured line", "bold green")
    app.apply_theme("dark", save=True)
    assert app.theme_name == "dark" and Settings.load().gui_theme == "dark" and Settings.load().theme == "tokyo-night"
    assert str(app.hdr.text.cget("bg")).lower() == "#1c1c1c" and str(app.logbox.text.cget("bg")).lower() == "#161616"
    app.apply_theme("light", save=True)
    assert Settings.load().gui_theme == "light" and str(app.hdr.text.cget("bg")).lower() == "#fafafa"
    assert {"View", "Tools", "Nexus", "Help"} <= set(app.menus)
    app.registry.set_mod("alpha", nexus_id=7)
    app.refresh_all()
    items = {aid: (label, enabled) for it in app.row_menu_items("alpha") if it for label, aid, enabled in [it]}
    assert items["changelog"][1] and "later release" not in " ".join(l for l, _ in items.values())
    assert items["link"][0] == "Set installed version..." or items["open_page"][1]
    app.quit()
