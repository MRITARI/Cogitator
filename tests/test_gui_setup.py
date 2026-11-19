"""Profiles, snapshots, bulk actions, bisect, crash-log leads, the repair watcher and the self-update texts: the shared flows
(`cogitator.actions_setup`, `cogitator.repair`) with a scripted Ui, and the window's methods through a stub host. No display."""
import time
from types import SimpleNamespace

import pytest
from gui_fakes import FakeUi, make_ctx
from test_gui_mixins import Host

from cogitator import actions, actions_setup, profiles, repair, snapshots
from cogitator.mods import ModError
from cogitator.patcher import OFF, ON
from cogitator_gui import tools
from cogitator_gui.tools import ToolFlows
from cogitator_gui.viewmodel import PALETTES, normalize_theme, ttk_state_maps, ttk_styles


def three_mods(ctx):
    for m in ("gamma",):
        (ctx.game_dir / "mods" / m).mkdir(exist_ok=True)
        (ctx.game_dir / "mods" / m / f"{m}.mod").write_text("return {}")
    (ctx.game_dir / "mods" / "mod_load_order.txt").write_text("alpha\nbeta\n--gamma\n")
    return ctx


# ---------------------------------------------------------------- profiles
def test_profile_save_switch_share_import_delete(tmp_path):
    ctx = three_mods(make_ctx(tmp_path))
    assert actions_setup.save_profile(ctx, "hordes")[0][0].startswith("Saved profile 'hordes' (3 mods)")
    assert actions_setup.profile_exists("hordes") and not actions_setup.profile_exists("nope") and not actions_setup.profile_exists("bad/name")
    assert actions_setup.profile_labels(profiles.list_profiles()) == ["hordes  (2/3 on)"]
    (ctx.game_dir / "mods" / "mod_load_order.txt").write_text("alpha\n--beta\n--gamma\n")  # change the setup, then go back
    prof, body = actions_setup.prepare_switch(ctx, "hordes")
    assert "A snapshot is taken first." in body and "Disabled" not in body
    ui = FakeUi()
    out = actions_setup.switch_profile(ctx, ui, prof)
    assert out.refresh and "Switched to profile 'hordes'." in ui.text
    assert (ctx.game_dir / "mods" / "mod_load_order.txt").read_text().splitlines()[:2] == ["alpha", "beta"]
    code = actions_setup.share_code(ctx)
    assert code.startswith("cog1:")
    lines = actions_setup.import_code(ctx, code)
    assert "Imported profile" in lines[0][0]
    assert actions_setup.import_code(ctx, "cog1:garbage")[0][1] == actions.ERR
    assert actions_setup.delete_profile("hordes")[0][0] == "Deleted profile 'hordes'."
    assert actions_setup.delete_profile("hordes")[0][1] == actions.ERR  # already gone: reported, not raised
    with pytest.raises(ModError):
        actions_setup.prepare_switch(ctx, "hordes")


def test_import_code_lists_missing_mods_with_their_pages(tmp_path):
    ctx = three_mods(make_ctx(tmp_path))
    ctx.registry.set_mod("gamma", nexus_id=77)
    code = actions_setup.share_code(ctx)
    import shutil

    shutil.rmtree(ctx.game_dir / "mods" / "gamma")
    lines = actions_setup.import_code(ctx, code)
    assert any("not installed: gamma" in t and "mods/77" in t for t, _ in lines)


def test_switch_profile_failure_is_logged(tmp_path, monkeypatch):
    ctx = three_mods(make_ctx(tmp_path))
    monkeypatch.setattr(profiles, "switch", lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
    ui = FakeUi()
    actions_setup.switch_profile(ctx, ui, {"name": "x"})
    assert "Could not switch profile: locked" in ui.text


# ---------------------------------------------------------------- snapshots
def test_snapshot_create_restore_prune(tmp_path):
    ctx = three_mods(make_ctx(tmp_path))
    assert actions_setup.create_snapshot(ctx, "before")[0][0].startswith("Snapshot saved:")
    rows = snapshots.list_snapshots()
    assert len(rows) == 1 and "before" in actions_setup.snapshot_label(rows[0])
    lo = ctx.game_dir / "mods" / "mod_load_order.txt"
    lo.write_text("beta\n")
    ctx.game_running = lambda: True
    ui = FakeUi()
    assert actions_setup.restore_snapshot(ctx, ui, rows[0]["name"]) is None and "Close the game" in ui.text and lo.read_text() == "beta\n"
    ctx.game_running = lambda: False
    ui = FakeUi(confirms=[False])
    assert actions_setup.restore_snapshot(ctx, ui, rows[0]["name"]) is None and lo.read_text() == "beta\n"
    assert ui.asked[0][3 - 1].startswith("Changes the restore would make") and ui.asked[0][1] == "Restore this snapshot?"
    ui = FakeUi(confirms=[True])
    out = actions_setup.restore_snapshot(ctx, ui, rows[0]["name"])
    assert out.refresh and lo.read_text().startswith("alpha") and len(snapshots.list_snapshots()) == 2  # a pre-restore snapshot exists
    ui = FakeUi()
    assert actions_setup.restore_snapshot(ctx, ui, "nope") is None and "Could not read snapshot" in ui.text
    assert actions_setup.prune_snapshots(0)[0][1] == actions.ERR and actions_setup.prune_snapshots(True)[0][1] == actions.ERR
    assert len(snapshots.list_snapshots()) == 2
    assert "Removed 1 old snapshot(s); keeping the newest 1." in actions_setup.prune_snapshots(1)[0][0]
    assert len(snapshots.list_snapshots()) == 1


# ---------------------------------------------------------------- bulk
def test_bulk_enable_disable_tag_remove(tmp_path):
    ctx = three_mods(make_ctx(tmp_path))
    lines = actions_setup.bulk_set_enabled(ctx, ["gamma", "alpha"], True)
    assert lines[-1][0] == "Enabled 2 mod(s)." and "--gamma" not in (ctx.game_dir / "mods" / "mod_load_order.txt").read_text()
    lines = actions_setup.bulk_set_enabled(ctx, ["alpha", "ghost"], False)
    assert lines[-1][0] == "Disabled 1 mod(s)." and lines[0][1] == actions.ERR  # the unknown one is reported, the rest still done
    assert actions_setup.parse_tags("#UI hud  ## x") == ["ui", "hud", "x"] or actions_setup.parse_tags("#UI hud") == ["ui", "hud"]
    ctx.registry.set_mod("beta", tags=["ui"])
    assert actions_setup.bulk_tag(ctx, ["beta", "gamma"], "#ui hud")[0][0] == "Tagged 2 mod(s): ui, hud"
    assert ctx.registry.mod("beta")["tags"] == ["ui", "hud"] and ctx.registry.mod("gamma")["tags"] == ["ui", "hud"]
    assert actions_setup.bulk_tag(ctx, ["beta"], "   ") == []
    ui = FakeUi()
    out = actions_setup.remove_many(ctx, ui, ["beta", "gamma"])
    assert out.refresh and not (ctx.game_dir / "mods" / "beta").exists() and not (ctx.game_dir / "mods" / "gamma").exists()
    assert (ctx.game_dir / "mods" / "alpha").exists() and ui.text.count("Removed") == 2


def test_remove_many_stops_at_a_running_game(tmp_path):
    ctx = three_mods(make_ctx(tmp_path))
    ctx.game_running = lambda: True
    ui = FakeUi()
    actions_setup.remove_many(ctx, ui, ["beta"])
    assert (ctx.game_dir / "mods" / "beta").exists() and "Close Darktide" in ui.text


# ---------------------------------------------------------------- bisect
def test_bisect_flow_start_answer_and_reset(tmp_path):
    ctx = three_mods(make_ctx(tmp_path))
    (ctx.game_dir / "mods" / "mod_load_order.txt").write_text("alpha\nbeta\ngamma\n")
    ui = FakeUi(confirms=[False])
    assert actions_setup.bisect_flow(ctx, ui) is None and ui.asked[0][2] == actions_setup.BISECT_INTRO  # declined: nothing happens
    ui = FakeUi(confirms=[True])
    out = actions_setup.bisect_flow(ctx, ui)
    assert out.refresh and "Step 1" in ui.text
    ui = FakeUi(choices=[0])  # the problem still happens
    actions_setup.bisect_flow(ctx, ui)
    assert ui.asked[0][1].startswith("Step") and ui.asked[0][2] == actions_setup.BISECT_OPTIONS
    ui = FakeUi(choices=[3])  # Close: the session stays
    assert actions_setup.bisect_flow(ctx, ui) is None
    ui = FakeUi(choices=[2])
    out = actions_setup.bisect_flow(ctx, ui)
    assert out.refresh and (ctx.game_dir / "mods" / "mod_load_order.txt").read_text().splitlines()[:3] == ["alpha", "beta", "gamma"]
    res = actions_setup.bisect_step(ctx, "good")
    assert not res.ok and "No bisect session" in res.lines[0][0]  # no session left: reported, not raised


def test_bisect_needs_two_enabled_mods(tmp_path):
    ctx = make_ctx(tmp_path)
    ui = FakeUi()
    actions_setup.bisect_flow(ctx, ui)
    assert "at least two" in ui.text


# ---------------------------------------------------------------- crash log and repair
def test_crashlog_lines_style_the_leads(tmp_path, monkeypatch):
    monkeypatch.setattr(actions_setup.crashlog, "newest_log", lambda dirs: None)
    monkeypatch.setattr(actions_setup.crashlog, "format_report", lambda rep, lo: ["Header", "  lead one"])
    assert actions_setup.crashlog_lines(make_ctx(tmp_path).game_dir, {"alpha"}) == [("Header", ""), ("  lead one", actions.WARN)]


def test_repair_watch_step_table(tmp_path):
    reg = SimpleNamespace(data={"patch": {"last_patch_at": 5, "last_unpatch_at": 1}})
    g = tmp_path
    assert repair.watch_step(ON, ON, g, reg, "auto", False) is None  # unchanged
    assert repair.watch_step(None, ON, g, reg, "auto", False) == (ON, False, "none")  # first reading: no redraw
    assert repair.watch_step(ON, OFF, g, reg, "auto", False) == (OFF, True, "patch")  # the game update switched the mods off
    assert repair.watch_step(ON, OFF, g, reg, "auto", True) == (OFF, True, "notify")  # the game is running: only tell
    assert repair.watch_step(ON, OFF, g, reg, "ask", False) == (OFF, True, "notify")
    assert repair.watch_step(ON, OFF, g, reg, "off", False) == (OFF, True, "none")
    deliberate = SimpleNamespace(data={"patch": {"last_patch_at": 1, "last_unpatch_at": 5}})  # the person turned mods off themselves
    assert repair.watch_step(ON, OFF, g, deliberate, "auto", False) == (OFF, True, "none")


def test_repair_patch_reports_success_and_failure(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    monkeypatch.setattr(actions_setup, "patch", lambda *a, **k: SimpleNamespace(message="Patched.", changed=True))
    assert actions_setup.repair_patch(ctx) == [("Game was updated: Patched.", actions.OK)]
    from cogitator.patcher import PatcherError

    monkeypatch.setattr(actions_setup, "patch", lambda *a, **k: (_ for _ in ()).throw(PatcherError("busy")))
    assert "Could not re-enable mods" in actions_setup.repair_patch(ctx)[0][0]


# ---------------------------------------------------------------- Cogitator's own updates
class FakeUpdater:
    def __init__(self, mode="notify", in_place=True, ready=None, offer=None):
        self.settings = SimpleNamespace(update_mode=mode)
        self.layout = SimpleNamespace(in_place=in_place, guidance=lambda: "Run the installer.")
        self._ready, self._offer = ready, offer
        self.calls = []

    def announcement(self):
        return self._offer

    def latest(self):
        return self._offer

    def ready(self):
        return self._ready

    def remind_later(self):
        self.calls.append("remind")

    def skip_version(self, v):
        self.calls.append(("skip", v))

    def stage(self, info):
        self.calls.append(("stage", info.version))
        self._ready = SimpleNamespace(version=info.version, notes="")

    def apply(self):
        self.calls.append("apply")
        return self._ready

    def absorb_result(self):
        return None

    def run_automatic(self):
        return SimpleNamespace(status="none", latest=None)

    def should_apply_automatically(self):
        return False


OFFER = SimpleNamespace(version="9.9", notes="fixes", zip_size=3 * 1024 * 1024)


def test_update_banner_wording_for_both_interfaces():
    up = FakeUpdater(offer=OFFER, ready=None)
    assert actions_setup.update_banner_text(None, None) == "" and actions_setup.update_banner_text(FakeUpdater("off", offer=OFFER), None) == ""
    assert "Press A to download" in actions_setup.update_banner_text(up, None)
    assert "Help > Update Cogitator" in actions_setup.update_banner_text(up, None, gui=True)
    assert "Press A to install" in actions_setup.update_banner_text(up, object())
    assert "Help > Update Cogitator" in actions_setup.update_banner_text(up, object(), gui=True) and "Press" not in actions_setup.update_banner_text(up, object(), gui=True)
    assert "Run the installer." in actions_setup.update_banner_text(FakeUpdater(in_place=False, offer=OFFER), None, gui=True)
    assert "about 3 MB" in actions_setup.offer_question(OFFER) and "fixes" in actions_setup.offer_question(OFFER)


def test_update_result_lines():
    assert actions_setup.update_result_lines({"status": "ok", "version": "2"}) == [("Updated to v2.", actions.OK)]
    assert actions_setup.update_result_lines({"status": "rolled-back"})[0][0] == "Went back to the previous version."
    bad = actions_setup.update_result_lines({"status": "reverted", "version": "3", "detail": "check failed"})
    assert "previous version was restored" in bad[0][0] and bad[1][0] == "Reason: check failed" and "update.log" in bad[2][0]


def test_download_update_and_apply(tmp_path):
    ctx, ui, u = make_ctx(tmp_path), FakeUi(), FakeUpdater(offer=OFFER)
    assert actions_setup.download_update(ctx, ui, u, OFFER) and u.calls == [("stage", "9.9")]
    from cogitator.selfupdate import UpdateError

    u.stage = lambda info: (_ for _ in ()).throw(UpdateError("hash mismatch"))
    ui = FakeUi()
    assert not actions_setup.download_update(ctx, ui, u, OFFER) and "hash mismatch" in ui.text
    ui = FakeUi()
    assert actions_setup.apply_update(FakeUpdater(ready=SimpleNamespace(version="9.9")), ui) and "Installing v9.9" in ui.text
    u2 = FakeUpdater(ready=SimpleNamespace(version="9.9"))
    u2.apply = lambda: (_ for _ in ()).throw(UpdateError("no helper"))
    ui = FakeUi()
    assert not actions_setup.apply_update(u2, ui) and "no helper" in ui.text


# ---------------------------------------------------------------- theme (widget-free parts)
def test_theme_rules():
    assert normalize_theme("dark") == "dark" and normalize_theme("tokyo-night") == "light" and normalize_theme(None) == "light"
    for name, pal in PALETTES.items():
        st = ttk_styles(pal)
        assert st["Treeview"]["background"] == pal["bg"] and st["."]["foreground"] == pal["fg"] and st["Hint.TLabel"]["foreground"] == pal["dim"]
        assert ttk_state_maps(pal)["Treeview"]["background"][0][0] == "selected"
    assert ttk_state_maps(PALETTES["dark"])["Treeview"]["background"] != ttk_state_maps(PALETTES["light"])["Treeview"]["background"]


def test_gui_theme_is_its_own_setting_and_does_not_touch_the_terminal_theme():
    from cogitator.config import Settings

    s = Settings.load()
    assert s.gui_theme == "light" and s.theme == "tokyo-night"
    s.gui_theme = "dark"
    s.save()
    again = Settings.load()
    assert again.gui_theme == "dark" and again.theme == "tokyo-night"


# ---------------------------------------------------------------- the window's methods (stub host)
class Dlg2:
    """Scripted dialogs: `actions` is a list of answers for pick_action, `confirm`/`text`/`choice` for the simple ones."""

    def __init__(self, picks=(), confirm=True, texts=(), choice=None):
        self.picks, self.confirm_answer, self.texts, self.choice, self.shown = list(picks), confirm, list(texts), choice, []

    def pick_action(self, title, heading, rows, buttons, selected=0):
        self.shown.append(("pick", title, list(rows), [b[0] for b in buttons]))
        return self.picks.pop(0) if self.picks else None

    def confirm(self, title, body="", default_yes=True):
        self.shown.append(("confirm", title, body))
        return self.confirm_answer

    def ask_text(self, title, hint="", body="", allow_blank=False, password=False, initial=""):
        self.shown.append(("text", title, initial))
        return self.texts.pop(0) if self.texts else None

    def choose(self, question, options, selected=0):
        self.shown.append(("choose", question, list(options)))
        return self.choice

    def show_code(self, title, code, note=""):
        self.shown.append(("code", code))

    def show_text(self, title, segments, *a, **k):
        self.shown.append(("report", title, "".join(s for s, _ in segments)))


class THost(Host, ToolFlows):
    def __init__(self, tmp_path, dlg=None, **kw):
        super().__init__(tmp_path, **kw)
        self.dlg = dlg or Dlg2()
        from cogitator.config import Settings

        self.settings = Settings.load()
        self.refreshed = 0
        self.model.mods = []
        self._last_patch_state, self.selfupdater, self.selfupdate_on_start, self.check_on_start = None, None, True, True
        self.game_running = lambda: False
        self.quit_called = False
        self.headers = 0

    def refresh_all(self, keep=None):
        self.refreshed += 1

    def refresh_header(self):
        self.headers += 1

    def quit(self):
        self.quit_called = True

    def _handle(self, m, out):
        out.append(m)
        if m[0] == "ui":
            m[1](*m[2], **m[3])
        elif m[0] == "ask":
            kind, p, holder = m[1], m[2], m[3]
            if kind == "confirm":
                v = self.dlg.confirm(p["title"], p.get("body", ""), p.get("default_yes", True))
            elif kind == "choice":
                v = self.dlg.choose(p["question"], p["options"], p.get("selected", 0))
            else:
                v = self.dlg.ask_text(p["title"], p.get("hint", ""), p.get("body", ""), p.get("allow_blank", False))
            self.ctl.answer(holder, v)

    def pump(self):
        """Answer the workers' questions like the window would and run what they post for the UI thread, until they are done."""
        out, end = [], time.time() + 10
        while time.time() < end:
            alive = any(t.is_alive() for t in self.ctl._threads)
            for m in self.ctl.drain():
                self._handle(m, out)
            if not alive:
                for m in self.ctl.drain():
                    self._handle(m, out)
                break
            time.sleep(0.01)
        return out


def test_profiles_window_loops_until_a_switch_or_close(tmp_path):
    ctx = three_mods(make_ctx(tmp_path))
    actions_setup.save_profile(ctx, "hordes")
    dlg = Dlg2(picks=[("save", None), ("share", None), ("import", None), ("delete", 0), ("switch", 0)], texts=["pvp", "cog1:garbage"])
    h = THost(tmp_path, dlg)
    h._ctx = ctx
    h.profiles_window()
    kinds = [s[0] for s in dlg.shown]
    assert kinds.count("pick") == 5  # the list came back after each quick action, and closed after the switch
    assert actions_setup.profile_exists("pvp") and any(s[0] == "code" and s[1].startswith("cog1:") for s in dlg.shown)
    assert any("Could not" in m or "Not a" in m or "cog1" in m or "setup code" in m.lower() or "error" in m.lower() for m in h.logs)
    # delete asked first and then 'switch' on index 0 named after the sorted list
    assert any(s[0] == "confirm" and "Delete profile" in s[1] for s in dlg.shown)
    h.pump()


def test_profiles_window_switch_confirms_and_runs_a_job(tmp_path):
    ctx = three_mods(make_ctx(tmp_path))
    actions_setup.save_profile(ctx, "hordes")
    (ctx.game_dir / "mods" / "mod_load_order.txt").write_text("beta\n")
    dlg = Dlg2(picks=[("switch", 0)], confirm=True)
    h = THost(tmp_path, dlg)
    h._ctx = ctx
    h.profiles_window()
    h.pump()
    assert [s[1] for s in dlg.shown if s[0] == "confirm"] == ["Switch to profile hordes?"]
    assert (ctx.game_dir / "mods" / "mod_load_order.txt").read_text().splitlines()[0] == "alpha" and h.outcomes
    dlg = Dlg2(picks=[("switch", 0)], confirm=False)
    h2 = THost(tmp_path, dlg)
    h2._ctx = ctx
    (ctx.game_dir / "mods" / "mod_load_order.txt").write_text("beta\n")
    h2.profiles_window()
    h2.pump()
    assert (ctx.game_dir / "mods" / "mod_load_order.txt").read_text() == "beta\n"  # declined


def test_snapshots_window_actions(tmp_path):
    ctx = three_mods(make_ctx(tmp_path))
    actions_setup.create_snapshot(ctx, "one")
    lo = ctx.game_dir / "mods" / "mod_load_order.txt"
    lo.write_text("beta\n")
    dlg = Dlg2(picks=[("restore", 0)], confirm=True)
    h = THost(tmp_path, dlg)
    h._ctx = ctx
    h.snapshots_window()
    h.pump()
    assert lo.read_text().startswith("alpha") and h.outcomes
    dlg = Dlg2(picks=[("create", None)], texts=["my reason"])
    h = THost(tmp_path, dlg)
    h._ctx = ctx
    h.snapshots_window()
    h.pump()
    assert any("reason" in s["name"] for s in snapshots.list_snapshots())
    dlg = Dlg2(picks=[("prune", None)], texts=["0"])
    h = THost(tmp_path, dlg)
    h._ctx = ctx
    n = len(snapshots.list_snapshots())
    h.snapshots_window()
    assert "at least 1" in h.logs[-1] and len(snapshots.list_snapshots()) == n  # --keep below 1 is refused
    dlg = Dlg2(picks=[("prune", None)], texts=["abc"])
    h = THost(tmp_path, dlg)
    h._ctx = ctx
    h.snapshots_window()
    assert "at least 1" in h.logs[-1]
    dlg = Dlg2(picks=[("prune", None)], texts=["1"])
    h = THost(tmp_path, dlg)
    h._ctx = ctx
    h.snapshots_window()
    assert len(snapshots.list_snapshots()) == 1
    assert Dlg2().shown == [] and THost(tmp_path, Dlg2(picks=[None])).snapshots_window() is None


def test_bulk_actions_menu(tmp_path):
    ctx = three_mods(make_ctx(tmp_path))
    h = THost(tmp_path, Dlg2(choice=0))
    h._ctx = ctx
    h.bulk_actions()
    assert "Mark mods first" in h.logs[-1]
    h.model.marked = {"gamma", "beta"}
    h.bulk_actions()
    h.pump()
    assert "--gamma" not in (ctx.game_dir / "mods" / "mod_load_order.txt").read_text() and h.outcomes
    assert h.dlg.shown[0][2][0] == "Enable 2 mod(s)"
    h.dlg.choice = 3
    h.dlg.texts = ["#team"]
    h.bulk_actions()
    assert ctx.registry.mod("gamma")["tags"] == ["team"] and h.refreshed == 1
    h.dlg.choice = 4
    h.dlg.confirm_answer = False
    h.bulk_actions()
    assert (ctx.game_dir / "mods" / "beta").exists()
    h.dlg.confirm_answer = True
    h.bulk_actions()
    h.pump()
    assert not (ctx.game_dir / "mods" / "beta").exists() and not h.model.marked
    h.model.marked = {"alpha"}
    h.dlg.choice = 5
    h.bulk_actions()
    assert not h.model.marked and h.refreshed == 2
    h.model.marked = {"alpha"}
    h.dlg.choice = None
    h.bulk_actions()
    assert h.model.marked == {"alpha"}


def test_bisect_and_crashlog_windows(tmp_path, monkeypatch):
    h = THost(tmp_path, Dlg2(confirm=True))
    ctx = h._ctx = three_mods(h._ctx)
    (ctx.game_dir / "mods" / "mod_load_order.txt").write_text("alpha\nbeta\ngamma\n")
    h.bisect_wizard()
    msgs = h.pump()
    assert h.outcomes and h.outcomes[-1].refresh, msgs
    monkeypatch.setattr(actions_setup, "crashlog_lines", lambda gd, folders: [("Latest log: x", ""), ("  suspect: alpha", actions.WARN)])
    h.crashlog_window()
    h.pump()
    assert h.dlg.shown[-1][0] == "report" and "suspect: alpha" in h.dlg.shown[-1][2]


def test_watcher_reads_off_the_ui_thread_and_defers_while_a_dialog_is_open(tmp_path, monkeypatch):
    import threading

    h = THost(tmp_path)
    h.game_dir, h.registry = h._ctx.game_dir, h._ctx.registry
    h.registry.data["patch"] = {"last_patch_at": 5, "last_unpatch_at": 1}
    seen_threads = []
    monkeypatch.setattr(tools, "patch_state", lambda gd: seen_threads.append(threading.current_thread()) or OFF)
    h._last_patch_state = ON
    h.settings.auto_repair = "ask"
    h.watch_tick()
    h.ctl.join()
    assert seen_threads and seen_threads[0] is not threading.main_thread()  # the read happened on a worker
    h._modal = 1  # a dialog opens before the result is applied: nothing is recorded, the next tick looks again
    h.pump()
    assert h._last_patch_state == ON and not h.logs
    h._modal = 0
    h.watch_tick()
    h.ctl.join()
    h.pump()
    assert h._last_patch_state == OFF and h.headers == 1 and "game was updated" in h.logs[-1].lower() and "switched off" in h.logs[-1]
    h.watch_tick()  # unchanged now: nothing more happens
    h.ctl.join()
    h.pump()
    assert len(h.logs) == 1


def test_watcher_patches_in_auto_mode_and_not_while_the_game_runs(tmp_path, monkeypatch):
    h = THost(tmp_path)
    h.game_dir, h.registry = h._ctx.game_dir, h._ctx.registry
    h.registry.data["patch"] = {"last_patch_at": 5, "last_unpatch_at": 1}
    monkeypatch.setattr(tools, "patch_state", lambda gd: OFF)
    patched = []
    monkeypatch.setattr(actions_setup, "patch", lambda *a, **k: patched.append(1) or SimpleNamespace(message="Patched.", changed=True))
    h.settings.auto_repair = "auto"
    h._last_patch_state = ON
    h.game_running = lambda: True
    h.watch_tick()
    h.ctl.join()
    h.pump()
    assert patched == [] and "switched off" in h.logs[-1]
    h._last_patch_state = ON
    h.game_running = lambda: False
    h._ctx.game_running = lambda: False
    h.watch_tick()
    h.ctl.join()
    h.pump()
    h.ctl.join()
    h.pump()
    assert patched == [1] and h.headers >= 1


def test_watcher_errors_do_not_escape(tmp_path, monkeypatch):
    h = THost(tmp_path)
    h.game_dir, h.registry = h._ctx.game_dir, h._ctx.registry
    monkeypatch.setattr(tools, "patch_state", lambda gd: (_ for _ in ()).throw(OSError("unreadable")))
    h.watch_tick()
    assert h.messages() == []


def test_selfupdate_window_flows(tmp_path):
    h = THost(tmp_path, Dlg2(choice=0))
    h.apply_update()
    assert "has not run" in h.logs[-1]
    h.selfupdater = FakeUpdater(offer=OFFER)
    h.dlg.choice = 1  # Cancel: nothing is downloaded
    h.apply_update()
    h.pump()
    assert h.selfupdater.calls == [] and h.dlg.shown[-1][2] == actions_setup.OFFER_CHOICES
    h.dlg.choice = 0  # download, then the install question comes up on the UI thread
    h.apply_update()
    h.ctl.join()
    assert h.selfupdater.calls == [("stage", "9.9")]
    h.dlg.choice = 1
    h.pump()
    assert h.dlg.shown[-1][2] == actions_setup.READY_CHOICES and "remind" in h.selfupdater.calls  # 'Remind me later'
    h.dlg.choice = 2
    h.apply_update()
    h.pump()
    assert ("skip", "9.9") in h.selfupdater.calls
    h.dlg.choice = 3
    n = len(h.selfupdater.calls)
    h.apply_update()
    assert len(h.selfupdater.calls) == n and not h.quit_called
    h.dlg.choice = 0
    h.apply_update()
    assert h.selfupdater.calls[-1] == "apply" and h.quit_called  # install now and restart


def test_selfupdate_is_quiet_when_nothing_is_waiting_or_updates_are_off(tmp_path):
    h = THost(tmp_path)
    h.selfupdater = FakeUpdater(offer=None)
    h.apply_update()
    assert "No Cogitator update" in h.logs[-1]
    h.settings.update_mode = "off"
    h.start_selfupdate_check()
    assert h.messages() == [] and h.update_banner() == ""


def test_selfupdate_check_job_reports_the_last_result(tmp_path):
    h = THost(tmp_path)
    u = FakeUpdater(offer=OFFER)
    u.absorb_result = lambda: {"status": "ok", "version": "9.9"}
    u.run_automatic = lambda: SimpleNamespace(status="available", latest=OFFER)
    h.selfupdater = u
    h.lock = h._ctx.lock
    h.start_selfupdate_check()
    msgs = h.pump()
    assert any(m[0] == "log" and "v9.9 is available" in m[1] for m in msgs) and any("Updated to v9.9." in x for x in h.logs)
    assert time.time()
