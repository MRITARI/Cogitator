"""The window's workflow methods driven through a stub host (real Controller, scripted dialogs, no Tk)."""
import pytest
from gui_fakes import FakeNexus, make_ctx, nfile

from cogitator import actions, actions_nexus, nxm
from cogitator.config import set_api_key
from cogitator.updates import PINNED, PROBABLY_OUTDATED, UPDATE, ModRow
from cogitator_gui import workflows
from cogitator_gui.controller import Controller, GuiUi
from cogitator_gui.workflows import ModFlows, plan_update_all, plan_update_selected


class Model:
    def __init__(self, rows=()):
        self.urows = {r.folder: r for r in rows}
        self.mods = []
        self.marked = set()


class Dlg:
    def __init__(self, confirm=True, text=None):
        self.confirm_answer, self.text, self.shown = confirm, text, []

    def confirm(self, title, body="", default_yes=True):
        self.shown.append(("confirm", title, body))
        return self.confirm_answer

    def ask_text(self, title, hint="", body="", allow_blank=False, password=False, initial=""):
        self.shown.append(("text", title))
        return self.text

    def show_text(self, title, segments, *a, **k):
        self.shown.append(("report", title, "".join(s for s, _ in segments)))


class Host(ModFlows):
    def __init__(self, tmp_path, rows=(), premium=False, **dlg):
        self._ctx = make_ctx(tmp_path)
        self._ctx.registry.data["updates"]["premium"] = premium
        self.ctl, self.dlg, self.model = Controller(), Dlg(**dlg), Model(rows)
        self.ui, self.logs, self.sel = GuiUi(self.ctl), [], None
        self.game_dir, self.registry = self._ctx.game_dir, self._ctx.registry
        self.fw_updates, self.exiting, self._modal, self._pending_asks, self._nxm_busy = [], False, 0, [], False
        self.outcomes, self.checks, self.palette = [], [], {}

    def selected(self):
        return self.sel

    def need_game(self):
        return True

    def log_line(self, msg, style=""):
        self.logs.append(msg)

    def ctx(self):
        return self._ctx

    def run_job(self, name, group, fn):
        return self.ctl.run(name, fn, group)

    def _modal_call(self, fn, *a, **k):
        return fn(*a, **k)

    def _after_outcome_ui(self, out):
        self.outcomes.append(out)

    def start_check(self, force=False):
        self.checks.append(force)

    def messages(self):
        self.ctl.join()
        return self.ctl.drain()


def row(folder, status, **kw):
    return ModRow(folder=folder, nexus_id=7, status=status, installed="1", latest="2", reason="because", **kw)


@pytest.fixture
def nx(monkeypatch):
    FakeNexus.reset()
    monkeypatch.setattr(actions_nexus, "NexusClient", FakeNexus)
    set_api_key("MixinSecretKey-0123456789abcdef01")
    FakeNexus.mods = {7: ({"name": "Alpha"}, [nfile(1, "1"), nfile(2, "2")])}
    return FakeNexus


def test_update_plans():
    assert plan_update_selected(None, True)[0] == "nothing"
    assert plan_update_selected(row("a", PINNED), True)[0] == "nothing"  # a pinned mod is skipped
    assert plan_update_selected(row("a", UPDATE), True) == ("premium", "because")
    assert plan_update_selected(row("a", PROBABLY_OUTDATED), False)[0] == "manual"
    rows = {"a": row("a", UPDATE), "b": row("b", PROBABLY_OUTDATED)}
    assert plan_update_all(rows, True) == ("premium", ["a"])  # a guess is not updated in bulk
    assert plan_update_all(rows, False) == ("manual", ["a"]) and plan_update_all({}, True) == ("nothing", [])


def test_update_selected_premium_asks_then_runs_the_job_and_rechecks(tmp_path, nx, monkeypatch):
    h = Host(tmp_path, [row("alpha", UPDATE)], premium=True)
    h.sel = "alpha"
    done = []
    monkeypatch.setattr(actions_nexus, "apply_update_premium",
                        lambda f, *a, **k: done.append(f) or type("R", (), {"version": "2", "file_id": 2})())
    h.dlg.confirm_answer = False
    h.update_selected()
    assert done == [] and h.dlg.shown[0][1] == "Download and install the update for alpha?"
    h.dlg.confirm_answer = True
    h.update_selected()
    msgs = h.messages()
    assert done == ["alpha"] and h.outcomes and any(m[0] == "ui" for m in msgs)
    for m in msgs:
        if m[0] == "ui":
            m[1](*m[2], **m[3])
    assert h.checks == [True]  # a forced re-check after the update


def test_update_selected_free_account_goes_through_the_manual_flow(tmp_path, nx, monkeypatch):
    h = Host(tmp_path, [row("alpha", UPDATE)], premium=False)
    h.sel = "alpha"
    seen = []
    monkeypatch.setattr(actions_nexus, "manual_update", lambda ctx, ui, f, after: seen.append(f))
    h.update_selected()
    h.messages()
    assert seen == ["alpha"] and not h.dlg.shown  # no confirmation up front: the files page comes first


def test_update_selected_nothing_to_do_and_update_all_free_lists_pages(tmp_path, nx):
    h = Host(tmp_path, [row("alpha", PINNED), row("beta", UPDATE)])
    h.sel = "alpha"
    h.update_selected()
    assert "nothing to update" in h.logs[-1]
    h.update_all()
    h.messages()
    text = "\n".join(m[1] for m in h.ctl.drain() if m[0] == "log")
    assert not h.dlg.shown  # free account: no download, just the pages (logged via the controller)


def test_update_all_premium_confirms_the_list(tmp_path, nx, monkeypatch):
    h = Host(tmp_path, [row("alpha", UPDATE), row("beta", UPDATE)], premium=True, confirm=False)
    monkeypatch.setattr(actions_nexus, "premium_update", lambda *a: pytest.fail("declined"))
    h.update_all()
    assert h.dlg.shown[0][1] == "Download and install 2 update(s)?" and h.dlg.shown[0][2] == "alpha\nbeta" and not h.messages()


def test_update_marked_needs_premium_and_updates_only_marked_candidates(tmp_path, nx, monkeypatch):
    h = Host(tmp_path, [row("alpha", UPDATE), row("beta", UPDATE)], premium=False)
    h.model.marked = {"alpha"}
    h.update_marked()
    assert "Free account" in h.logs[-1]
    h = Host(tmp_path, [row("alpha", UPDATE), row("beta", UPDATE)], premium=True)
    h.model.marked = {"beta", "zzz"}
    got = []
    monkeypatch.setattr(actions_nexus, "premium_update", lambda ctx, ui, folders: got.append(folders) or actions.Outcome())
    h.update_marked()
    h.messages()
    assert got == [["beta"]]


def test_link_selected_rules(tmp_path, nx, monkeypatch):
    h = Host(tmp_path, [row("alpha", UPDATE, baseline="chosen")])
    h.link_selected("dmf")
    assert "belongs to the loader" in h.logs[-1]
    h.dlg.text = "not a nexus thing"
    h.link_selected("alpha")
    assert h.logs[-1] and h.dlg.shown[-1][0] == "text" and not h.messages()
    # an already linked mod with a guessed baseline only needs the version
    h = Host(tmp_path, [row("alpha", UPDATE, baseline="guessed")])
    seen = []
    monkeypatch.setattr(actions_nexus, "set_version", lambda ctx, ui, f, mid: seen.append((f, mid)))
    h.link_selected("alpha")
    h.messages()
    assert seen == [("alpha", 7)] and not h.dlg.shown


def test_link_selected_with_a_url_starts_the_link_job(tmp_path, nx, monkeypatch):
    h = Host(tmp_path, [])
    h.dlg.text = "https://www.nexusmods.com/warhammer40kdarktide/mods/7"
    seen = []
    monkeypatch.setattr(actions_nexus, "link_one_mod", lambda ctx, ui, f, mid: seen.append((f, mid)))
    h.link_selected("alpha")
    h.messages()
    assert seen == [("alpha", 7)]


def test_flow_failure_is_reported_not_raised(tmp_path, nx, monkeypatch):
    h = Host(tmp_path, [])
    h.dlg.text = "7"

    def boom(*a):
        raise RuntimeError(f"kaboom with set_api_key value")

    monkeypatch.setattr(actions_nexus, "link_one_mod", boom)
    h.link_selected("alpha")
    errs = [m for m in h.messages() if m[0] == "error"]
    assert errs and errs[0][1] == "link" and "kaboom" in errs[0][2]


def test_framework_update_asks_with_the_how_text(tmp_path, nx, monkeypatch):
    from cogitator.updates import FrameworkUpdate

    h = Host(tmp_path)
    h.fw_updates = [FrameworkUpdate(kind="dml", status=UPDATE, installed="1", latest="2", latest_file_id=5, latest_file_name="d.zip",
                                    latest_size=1, mod_id=1, reason="")]
    h.dlg.confirm_answer = False
    monkeypatch.setattr(actions_nexus, "framework_update", lambda *a: pytest.fail("declined"))
    h.framework_update()
    assert h.dlg.shown[0][1] == "Update DML?" and "files page" in h.dlg.shown[0][2] and not h.messages()
    h.dlg.confirm_answer = True
    seen = []
    monkeypatch.setattr(actions_nexus, "framework_update", lambda ctx, ui, ups, after: seen.append([u.kind for u in ups]))
    h.framework_update()
    h.messages()
    assert seen == [["dml"]]
    h.fw_updates = []
    h.framework_update()
    assert "No framework update" in h.logs[-1]


def test_nxm_tick_waits_for_dialogs_busy_jobs_and_one_link_at_a_time(tmp_path, nx, monkeypatch):
    h = Host(tmp_path)
    polled = []
    link = type("L", (), {"mod_id": 7, "file_id": 2})()
    monkeypatch.setattr(nxm, "pending", lambda on_expired=None: polled.append(1) or [("p1", link), ("p2", link)])
    ran = []
    monkeypatch.setattr(actions_nexus, "nxm_install", lambda ctx, ui, path, lk, after: ran.append(path))
    h._modal = 1
    h.nxm_tick()  # a dialog is open: the queue is not even read
    h._modal = 0
    h._pending_asks = [("x",)]
    h.nxm_tick()
    assert polled == [] and ran == []
    h._pending_asks = []
    h.nxm_tick()
    h.messages()
    assert polled == [1] and ran == ["p1"] and h._nxm_busy is False  # only the first; the flag is cleared when the job ends
    h._nxm_busy = True
    h.nxm_tick()
    assert polled == [1]


def test_nxm_job_clears_the_busy_flag_when_the_flow_raises(tmp_path, nx, monkeypatch):
    h = Host(tmp_path)
    link = type("L", (), {"mod_id": 7, "file_id": 2})()
    monkeypatch.setattr(nxm, "pending", lambda on_expired=None: [("p1", link)])

    def boom(*a):
        raise OSError("disk")

    monkeypatch.setattr(actions_nexus, "nxm_install", boom)
    h.nxm_tick()
    msgs = h.messages()
    assert h._nxm_busy is False and any(m[0] == "error" and "disk" in m[2] for m in msgs)


def test_show_changelog_posts_a_window_or_log_lines(tmp_path, nx):
    h = Host(tmp_path)
    h.registry.set_mod("alpha", nexus_id=7, installed_version="1")
    nx.changelog = {"2": ["new thing"]}
    h.show_changelog("alpha")
    for m in h.messages():
        if m[0] == "ui":
            m[1](*m[2], **m[3])
    assert h.dlg.shown[0][0] == "report" and "new thing" in h.dlg.shown[0][2]
    nx.changelog = {}
    h.show_changelog("alpha")
    logs = [m for m in h.messages() if m[0] == "log"]
    assert logs and "no changelog" in logs[0][1]


def test_search_asks_for_text_and_ignores_cancel(tmp_path, nx, monkeypatch):
    h = Host(tmp_path)
    seen = []
    monkeypatch.setattr(actions_nexus, "search_nexus", lambda ctx, ui, text: seen.append(text))
    h.search_nexus()
    assert seen == []
    h.dlg.text = "hud"
    h.search_nexus()
    h.messages()
    assert seen == ["hud"] and workflows
