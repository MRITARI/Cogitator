import pytest
from textual.widgets import DataTable, Static

from cogitator import bisect, launch
from cogitator.config import Settings
from cogitator.loadorder import LoadOrder
from cogitator.registry import Registry
from cogitator.tui.screens import ChoiceScreen, ConfirmScreen, InputScreen
from test_tui import SIZE, cell, fx, game, log_text, make_app, settle  # noqa: F401  (fixtures)
from test_tui_keys import select


@pytest.fixture(autouse=True)
def accepted():
    s = Settings.load()
    s.notice_accepted = True
    s.save()


def states(game):
    return dict(LoadOrder.load(game / "mods" / "mod_load_order.txt").entries())


def rows(app):
    return list(app.query_one("#mods", DataTable).rows.keys())


async def test_mark_shows_plus_and_bulk_disable(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        select(app, "alpha")
        await pilot.press("x")
        assert app.marked == {"alpha"} and cell(app, "alpha", "mod").startswith("+ ")
        await pilot.press("x")
        assert app.marked == set() and not cell(app, "alpha", "mod").startswith("+")
        select(app, "alpha")
        await pilot.press("x")
        select(app, "gamma")
        await pilot.press("x")
        await pilot.press("b")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        await pilot.press("down", "enter")  # "Disable 2 mod(s)"
        await settle(app, pilot)
        assert states(game)["alpha"] is False and "gamma" not in states(game)  # unlisted stays unlisted (already not loaded)
        assert states(game)["beta"] is False  # untouched


async def test_bulk_without_marks_explains(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("b")
        await pilot.pause()
        assert not isinstance(app.screen, ChoiceScreen) and "Mark mods with X first" in log_text(app)


async def test_bulk_tag_and_remove(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        for f in ("alpha", "beta"):
            select(app, f)
            await pilot.press("x")
        await pilot.press("b")
        await pilot.pause()
        await pilot.press("down", "down", "down", "enter")  # "Add a tag..."
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        await pilot.press(*"ui", "enter")
        await settle(app, pilot)
        assert Registry.load().mod("alpha")["tags"] == ["ui"] and Registry.load().mod("beta")["tags"] == ["ui"]
        await pilot.press("b")
        await pilot.pause()
        await pilot.press("down", "down", "down", "down", "enter")  # "Remove them..."
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await settle(app, pilot)
        assert not (game / "mods" / "alpha").exists() and not (game / "mods" / "beta").exists()
        assert (game / "mods" / "gamma").is_dir()


async def test_favourite_note_and_filter(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        select(app, "alpha")
        await pilot.press("asterisk")
        assert cell(app, "alpha", "mod").startswith("* ") and Registry.load().mod("alpha")["fav"] is True
        select(app, "beta")
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        await pilot.press(*"#hud needs restart", "enter")
        await settle(app, pilot)
        rec = Registry.load().mod("beta")
        assert rec["tags"] == ["hud"] and rec["notes"] == "needs restart"
        assert "needs restart" in str(app.query_one("#hint", Static).render())
        await pilot.press("slash")
        await pilot.pause()
        await pilot.press(*"hud", "enter")
        await settle(app, pilot)
        assert rows(app) == ["beta"]
        await pilot.press("slash")
        await pilot.pause()
        await pilot.press("enter")  # empty input clears the filter
        await settle(app, pilot)
        assert {"alpha", "beta", "gamma"} <= set(rows(app))


async def test_g_launches_through_launch_module(game, monkeypatch):
    seen = {}

    def fake(game_dir, reg, **kw):
        seen["called"] = True
        return launch.LaunchResult(True, ["a note"])

    monkeypatch.setattr("cogitator.launch.launch", fake)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("g")
        await settle(app, pilot)
        assert seen == {"called": True} and "Asked Steam to start Darktide" in log_text(app) and "a note" in log_text(app)


async def test_launch_failure_is_shown(game, monkeypatch):
    def boom(*a, **k):
        raise launch.LaunchError("Not launching: broken")

    monkeypatch.setattr("cogitator.launch.launch", boom)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("g")
        await settle(app, pilot)
        assert "Not launching: broken" in log_text(app)


def patched_once(game):
    reg = Registry.load()
    reg.data["patch"] = {"last_patch_at": 100.0}
    reg.save()


async def test_watcher_notifies_after_game_update(game):
    patched_once(game)  # Cogitator patched earlier; the database reads as unpatched now
    app = make_app(game)
    toasts = []
    async with app.run_test(size=SIZE) as pilot:
        app.notify = lambda msg, **k: toasts.append(msg)
        await settle(app, pilot)
        app._last_patch_state = None
        app._watch()
        assert any("Press T" in t for t in toasts)
        n = len(toasts)
        app._watch()
        assert len(toasts) == n  # only on a change


async def test_watcher_is_quiet_when_never_patched_or_off(game):
    app = make_app(game)
    toasts = []
    async with app.run_test(size=SIZE) as pilot:
        app.notify = lambda msg, **k: toasts.append(msg)
        await settle(app, pilot)
        app._last_patch_state = None
        app._watch()
        assert toasts == []
        patched_once(game)
        app.settings.auto_repair = "off"
        app._last_patch_state = None
        app._watch()
        assert toasts == []


async def test_watcher_auto_repair_patches(game, monkeypatch):
    patched_once(game)
    calls = []

    class R:
        changed, message = True, "Mods enabled."

    monkeypatch.setattr("cogitator.actions_setup.patch", lambda *a, **k: calls.append(1) or R())
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        app.settings.auto_repair = "auto"
        await settle(app, pilot)
        app._last_patch_state = None
        app._watch()
        await settle(app, pilot)
        assert calls == [1] and "Game was updated" in log_text(app)


async def test_bisect_flow_from_the_menu(game):
    for m in ("delta", "epsilon"):
        (game / "mods" / m).mkdir()
        (game / "mods" / m / f"{m}.mod").write_text("return {}")
    (game / "mods" / "mod_load_order.txt").write_text("alpha\nbeta\ngamma\ndelta\n")
    before = (game / "mods" / "mod_load_order.txt").read_bytes()
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("f10")
        await pilot.pause()
        app.screen.dismiss(None)
        await pilot.pause()
        app.action_bisect()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await settle(app, pilot)
        s = bisect.load()
        assert s and "Step 1" in log_text(app)
        app.action_bisect()
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        await pilot.press("down", "down", "enter")  # stop and restore
        await settle(app, pilot)
        assert bisect.load() is None and (game / "mods" / "mod_load_order.txt").read_bytes() == before


async def test_context_menu_has_new_entries_and_pin_toggles(game):
    reg = Registry.load()
    reg.set_mod("alpha", nexus_id=19, installed_version="1.0", installed_file_id=1)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app._mod_menu_action("alpha", "pin")
        assert Registry.load().mod("alpha")["pinned_version"] == "1.0"
        app._mod_menu_action("alpha", "pin")
        assert Registry.load().mod("alpha")["pinned_version"] is None
        app._mod_menu_action("alpha", "toggle_fav")
        assert Registry.load().mod("alpha")["fav"] is True
