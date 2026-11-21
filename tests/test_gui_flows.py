"""Nexus-facing flows shared by the GUI and the terminal UI (link, updates, frameworks, nxm, search, changelog), run with a
scripted Ui and a fake Nexus client. No display, no network."""
import contextlib

import pytest
from gui_fakes import FakeNexus, FakeUi, make_ctx, nfile

from cogitator import actions, actions_nexus
from cogitator.config import set_api_key
from cogitator.modinstall import ModImportError
from cogitator.updates import PINNED, PROBABLY_OUTDATED, UP_TO_DATE, UPDATE, FrameworkUpdate, ModRow

KEY = "FlowSecretKey-0123456789abcdef0123"


@pytest.fixture
def nx(monkeypatch):
    FakeNexus.reset()
    monkeypatch.setattr(actions_nexus, "NexusClient", FakeNexus)
    set_api_key(KEY)
    FakeNexus.mods = {7: ({"name": "Alpha Mod"}, [nfile(1, "1.0", 1_600_000_000), nfile(2, "2.0", 1_700_000_000)])}
    return FakeNexus


def row(folder, status, nexus_id=7, **kw):
    return ModRow(folder=folder, nexus_id=nexus_id, status=status, installed="1.0", latest="2.0", reason=f"why {status}", **kw)


# ---------------------------------------------------------------- link
def test_link_marks_current_and_refreshes(tmp_path, nx):
    ctx, ui = make_ctx(tmp_path), FakeUi(choices=[0])
    out = actions_nexus.link_one_mod(ctx, ui, "alpha", 7)
    assert out == actions.Outcome(refresh=True, folder="alpha")
    rec = ctx.registry.mod("alpha")
    assert rec["nexus_id"] == 7 and rec["installed_file_id"] == 2 and "Linked 'alpha' to Alpha Mod." in ui.text


def test_link_cancel_and_leave_unknown(tmp_path, nx):
    ctx = make_ctx(tmp_path)
    ui = FakeUi(choices=[2])
    assert actions_nexus.link_one_mod(ctx, ui, "alpha", 7) is None and "Link cancelled." in ui.text
    assert not ctx.registry.mod("alpha")
    assert actions_nexus.link_one_mod(ctx, FakeUi(choices=[None]), "alpha", 7) is None  # closing the dialog is a cancel too
    ui = FakeUi(choices=[1])
    actions_nexus.link_one_mod(ctx, ui, "alpha", 7)
    assert ctx.registry.mod("alpha")["installed_file_id"] is None and "Baseline unknown" in ui.text


def test_link_needs_key_and_reports_api_errors(tmp_path, nx, monkeypatch):
    ctx = make_ctx(tmp_path)
    nx.raise_on = {"mod"}
    ui = FakeUi()
    assert actions_nexus.link_one_mod(ctx, ui, "alpha", 7) is None and "Link failed" in ui.text and not ui.asked
    monkeypatch.setattr(actions_nexus, "get_api_key", lambda: None)
    ui = FakeUi()
    assert actions_nexus.link_one_mod(ctx, ui, "alpha", 7) is None and "key" in ui.text


def test_link_entry_rules():
    assert actions_nexus.link_needs_version_only(row("a", UPDATE, baseline="guessed"))
    assert actions_nexus.link_needs_version_only(row("a", UPDATE, baseline=None))
    assert not actions_nexus.link_needs_version_only(row("a", UPDATE, baseline="chosen"))
    assert not actions_nexus.link_needs_version_only(None)
    assert actions_nexus.link_refusal("dmf") and actions_nexus.link_refusal("alpha") is None


def test_version_picker_shows_dates_guess_and_i_do_not_know(tmp_path, nx):
    files = nx.mods[7][1]
    pool, opts, sel = actions_nexus.version_options(files, guess=1)
    assert [f.file_id for f in pool] == [2, 1]  # newest first
    assert opts[-1] == "I do not know" and "<- guess" in opts[1] and sel == 1 and "2023" in opts[0]
    ctx = make_ctx(tmp_path)
    actions_nexus.link_mod(ctx.registry, "alpha", 7, "Alpha Mod", files, via="manual", baseline="none")
    ui = FakeUi(choices=[1])  # the older file
    actions_nexus.pick_version(ctx, ui, "alpha", files)
    assert ctx.registry.mod("alpha")["installed_file_id"] == 1 and ui.asked[0][3] == 0
    actions_nexus.pick_version(ctx, FakeUi(choices=[2]), "alpha", files)  # "I do not know": the stored value stays
    assert ctx.registry.mod("alpha")["installed_file_id"] == 1


def test_set_version_for_an_already_linked_mod(tmp_path, nx):
    ctx, ui = make_ctx(tmp_path), FakeUi(choices=[0])
    out = actions_nexus.set_version(ctx, ui, "alpha", 7)
    assert out.refresh and out.folder == "alpha" and ctx.registry.mod("alpha")["installed_file_id"] == 2


def test_link_all_confirms_each_and_can_stop(tmp_path, nx):
    ctx = make_ctx(tmp_path)
    nx.searches = [{"mod_id": 7, "name": "Alpha Mod"}]
    nx.mods[8] = ({"name": "Beta Mod"}, [nfile(3, "1")])
    # alpha: "Yes" then version "I do not know"; beta: "No - paste" (index 1), type an id, then pick the version
    ui = FakeUi(choices=[0, 1, 1, 0], texts=["https://www.nexusmods.com/warhammer40kdarktide/mods/8"])
    out = actions_nexus.link_all(ctx, ui, ["alpha", "beta", "dmf"])
    assert out.refresh and ctx.registry.mod("alpha")["nexus_id"] == 7 and ctx.registry.mod("beta")["nexus_id"] == 8
    assert "2 linked" in ui.text and not ctx.registry.mod("dmf")
    assert actions_nexus.link_all(ctx, FakeUi(), ["alpha", "beta"]) is None  # everything is linked
    nx.searches = [{"mod_id": 7, "name": "Gamma Mod"}]
    ui = FakeUi(choices=[2])  # one candidate: Yes / No / Skip / Stop -> Skip
    actions_nexus.link_all(ctx, ui, ["gamma"])
    assert "0 linked" in ui.text and not ctx.registry.mod("gamma") and ui.asked[0][2][-2:] == ["Skip this mod", "Stop"]
    ui = FakeUi(choices=[3])  # Stop after the first question
    actions_nexus.link_all(ctx, ui, ["gamma", "delta"])
    assert len([a for a in ui.asked if a[0] == "choose"]) == 1, ui.logs


def test_link_all_without_key(tmp_path, nx, monkeypatch):
    monkeypatch.setattr(actions_nexus, "get_api_key", lambda: None)
    assert actions_nexus.link_all(make_ctx(tmp_path), FakeUi(), ["alpha"]) is None


# ---------------------------------------------------------------- updates
def test_update_candidates_skip_pinned_and_guesses_unless_asked():
    rows = {"a": row("a", UPDATE), "b": row("b", PINNED), "c": row("c", PROBABLY_OUTDATED), "d": row("d", UP_TO_DATE)}
    assert actions_nexus.update_candidates(rows) == ["a"]
    assert actions_nexus.update_candidates(rows, ["a", "b", "c"], include_outdated=True) == ["a", "c"]
    assert actions_nexus.update_candidates(rows, ["zzz"]) == []


def test_premium_update_reports_each_mod_and_continues_after_an_error(tmp_path, nx, monkeypatch):
    ctx, ui = make_ctx(tmp_path), FakeUi()
    ctx.registry.data["updates"]["premium"] = True
    done = []

    def fake(folder, *a, **k):
        if folder == "bad":
            raise ModImportError("disk full")
        done.append((folder, k["premium"]))
        return type("R", (), {"version": "9", "file_id": 1})()

    monkeypatch.setattr(actions_nexus, "apply_update_premium", fake)
    out = actions_nexus.premium_update(ctx, ui, ["alpha", "bad", "beta"])
    assert done == [("alpha", True), ("beta", True)] and out.refresh and out.recheck
    assert "alpha: updated to 9." in ui.text and "bad: disk full" in ui.text


def test_manual_update_opens_the_page_and_does_not_wait(tmp_path, nx, monkeypatch):
    opened = []
    ctx = make_ctx(tmp_path, open_url=opened.append)
    ui = FakeUi()
    actions_nexus.manual_update(ctx, ui, "alpha", lambda o: None)
    assert "not linked" in ui.text and not opened
    ctx.registry.set_mod("alpha", nexus_id=7, latest={"file_name": "m-2.zip", "file_id": 2, "version": "2.0"})
    monkeypatch.setattr(actions_nexus, "wait_for_download", lambda *a, **k: pytest.fail("must not wait for a download"))
    monkeypatch.setattr(actions_nexus.nxm, "read_handler", lambda: "ours")
    monkeypatch.setattr(actions_nexus.nxm, "is_ours", lambda c: True)
    ui = FakeUi()
    actions_nexus.manual_update(ctx, ui, "alpha", lambda o: None)
    assert opened and "mods/7" in opened[0] and "Mod Manager Download" in ui.text
    assert not [a for a in ui.asked if a[0] == "confirm"]


def test_free_update_all_opens_every_page(tmp_path, nx, monkeypatch):
    opened = []
    ctx = make_ctx(tmp_path, open_url=opened.append)
    monkeypatch.setattr(actions_nexus.time, "sleep", lambda s: None)
    monkeypatch.setattr(actions_nexus.nxm, "read_handler", lambda: "ours")
    monkeypatch.setattr(actions_nexus.nxm, "is_ours", lambda c: True)
    rows = {"a": type("R", (), {"nexus_id": 1})(), "b": type("R", (), {"nexus_id": 2})()}
    actions_nexus.free_update_all_help(ctx, FakeUi(), rows, ["a", "b"])
    assert len(opened) == 2 and "mods/1" in opened[0] and "mods/2" in opened[1]


# ---------------------------------------------------------------- nxm
class Link:
    mod_id, file_id = 7, 2


def test_nxm_installs_without_asking(tmp_path, nx, monkeypatch):
    cleaned, imported = [], []
    monkeypatch.setattr(actions_nexus.nxm, "done", cleaned.append)
    monkeypatch.setattr(actions_nexus.nxm, "download", lambda link, nxc, work: (tmp_path / "x.zip", None))
    monkeypatch.setattr(actions_nexus, "import_file", lambda c, u, raw, mid, after, **k: imported.append((raw, mid, k)))
    ui = FakeUi()
    actions_nexus.nxm_install(make_ctx(tmp_path), ui, "q1.json", Link(), lambda o: None)
    assert cleaned == ["q1.json"] and imported == [(str(tmp_path / "x.zip"), 7, {"quiet": True})]
    assert not [a for a in ui.asked if a[0] == "confirm"]


def test_nxm_download_import_and_error_paths(tmp_path, nx, monkeypatch):
    cleaned, imported = [], []
    monkeypatch.setattr(actions_nexus.nxm, "done", cleaned.append)
    monkeypatch.setattr(actions_nexus, "import_file", lambda c, u, raw, mid, after, **k: imported.append((raw, mid)))
    monkeypatch.setattr(actions_nexus.nxm, "download", lambda link, nxc, work: (tmp_path / "x.zip", None))
    actions_nexus.nxm_install(make_ctx(tmp_path), FakeUi(confirms=[True]), "q2", Link(), lambda o: None)
    assert imported == [(str(tmp_path / "x.zip"), 7)] and cleaned == ["q2"]

    def boom(*a):
        raise OSError("disk")

    monkeypatch.setattr(actions_nexus.nxm, "download", boom)
    ui = FakeUi(confirms=[True])
    actions_nexus.nxm_install(make_ctx(tmp_path), ui, "q3", Link(), lambda o: None)
    assert "Download failed: disk" in ui.text and cleaned == ["q2", "q3"] and len(imported) == 1
    nx.raise_on = {"mod"}
    ui = FakeUi()
    actions_nexus.nxm_install(make_ctx(tmp_path), ui, "q4", Link(), lambda o: None)
    assert "Nexus link:" in ui.text and cleaned[-1] == "q4"


def test_nxm_without_key_is_reported_and_removed(tmp_path, nx, monkeypatch):
    cleaned = []
    monkeypatch.setattr(actions_nexus.nxm, "done", cleaned.append)
    monkeypatch.setattr(actions_nexus, "get_api_key", lambda: None)
    ui = FakeUi()
    actions_nexus.nxm_install(make_ctx(tmp_path), ui, "q5", Link(), lambda o: None)
    assert "no API key" in ui.text and cleaned == ["q5"]


# ---------------------------------------------------------------- frameworks, search, changelog
def test_framework_update_text_and_pending_filter():
    ups = [FrameworkUpdate(kind="dml", status=UPDATE, installed="1.0", latest="1.1", latest_file_id=5, latest_file_name="dml.zip", latest_size=1, mod_id=1, reason=""),
           FrameworkUpdate(kind="dmf", status=UP_TO_DATE, installed="1", latest="1", latest_file_id=6, latest_file_name="f.zip", latest_size=1, mod_id=2, reason="")]
    pend = actions_nexus.pending_framework_updates(ups)
    assert [u.kind for u in pend] == ["dml"]
    free, prem = actions_nexus.framework_update_body(pend, False), actions_nexus.framework_update_body(pend, True)
    assert "DML: 1.0 -> 1.1" in free and "files page" in free and "Premium" in prem


def _fake_github(monkeypatch):
    monkeypatch.setattr(actions_nexus, "make_client", lambda github_only=False: contextlib.nullcontext("client"))
    monkeypatch.setattr(actions_nexus, "plan_github", lambda kind, client, wd: kind)
    monkeypatch.setattr(actions_nexus, "describe_plan", lambda plan: f"plan {plan}")


def test_github_bootstrap_declined_installs_nothing(tmp_path, monkeypatch):
    _fake_github(monkeypatch)
    monkeypatch.setattr(actions_nexus, "install_framework_plan", lambda *a, **k: pytest.fail("declined"))
    ui = FakeUi(confirms=[False, False])
    out = actions_nexus.install_from_github(make_ctx(tmp_path), ui, ["dmf", "dml"])
    assert out.refresh and [a[1] for a in ui.asked] == ["Install DML from GitHub?", "Install DMF from GitHub?"]  # loader first
    assert "DML install declined." in ui.text


def test_github_bootstrap_installs_with_the_extract_limits(tmp_path, monkeypatch):
    _fake_github(monkeypatch)
    got = {}
    monkeypatch.setattr(actions_nexus, "install_framework_plan",
                        lambda plan, gd, reg, **k: got.update(k) or type("F", (), {"result": type("R", (), {"written": 3})()})())
    ui, ctx = FakeUi(), make_ctx(tmp_path)
    actions_nexus.install_from_github(ctx, ui, ["dml"])
    assert (got["max_bytes"], got["max_files"]) == ctx.settings.extract_limits() and "DML installed (3 files)." in ui.text


def test_search_opens_the_chosen_page(tmp_path, nx):
    opened = []
    ctx = make_ctx(tmp_path, open_url=opened.append)
    nx.searches = [{"mod_id": 7, "name": "Alpha Mod"}, {"mod_id": 9, "name": "Other"}]
    actions_nexus.search_nexus(ctx, FakeUi(choices=[1]), "alp")
    assert opened == ["https://www.nexusmods.com/warhammer40kdarktide/mods/9"]
    actions_nexus.search_nexus(ctx, FakeUi(choices=[None]), "alp")
    assert len(opened) == 1
    nx.searches = []
    ui = FakeUi()
    actions_nexus.search_nexus(ctx, ui, "zzz")
    assert "Nothing found" in ui.text
    nx.raise_on = {"search"}
    ui = FakeUi()
    actions_nexus.search_nexus(ctx, ui, "zzz")
    assert "Search:" in ui.text


def test_changelog_lists_only_newer_versions(tmp_path, nx):
    ctx = make_ctx(tmp_path)
    ctx.registry.set_mod("alpha", nexus_id=7, installed_version="1.0")
    nx.changelog = {"1.0": ["old"], "1.1": ["fixed a", "fixed b"], "2.0": ["big"]}
    res = actions_nexus.changelog_for(ctx.registry, "alpha")
    texts = [t for t, _ in res.lines]
    assert res.ok and "alpha v1.1" in texts and "alpha v2.0" in texts and "alpha v1.0" not in texts and "   - fixed a" in texts
    nx.changelog = {}
    assert not actions_nexus.changelog_for(ctx.registry, "alpha").ok
    nx.raise_on = {"changelogs"}
    assert actions_nexus.changelog_for(ctx.registry, "alpha").lines[0][0].startswith("Changelog:")
    assert not actions_nexus.changelog_for(ctx.registry, "unlinked").ok


def test_free_update_offers_to_register_nxm_when_another_handler_owns_it(tmp_path, nx, monkeypatch):
    ctx = make_ctx(tmp_path, open_url=lambda u: None)
    ctx.registry.set_mod("alpha", nexus_id=7, latest={"file_name": "m.zip", "file_id": 2})
    registered = []
    monkeypatch.setattr(actions_nexus.nxm, "read_handler", lambda: "vortex")
    monkeypatch.setattr(actions_nexus.nxm, "is_ours", lambda c: False)
    monkeypatch.setattr(actions_nexus.nxm, "register", lambda: registered.append(1) or "Cogitator now handles nxm:// links.")
    ui = FakeUi(confirms=[False])
    actions_nexus.manual_update(ctx, ui, "alpha", lambda o: None)
    assert not registered and "use Import" in ui.text
    ui = FakeUi(confirms=[True])
    actions_nexus.manual_update(ctx, ui, "alpha", lambda o: None)
    assert registered == [1] and "Mod Manager Download there" in ui.text
