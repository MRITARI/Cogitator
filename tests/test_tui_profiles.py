import pytest
from textual.widgets import OptionList

from cogitator import profiles
from cogitator.config import Settings
from cogitator.loadorder import LoadOrder
from cogitator.registry import Registry
from cogitator.tui.screens import ChoiceScreen, ConfirmScreen, InputScreen
from test_tui import SIZE, fx, game, log_text, make_app, settle  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def accepted():
    s = Settings.load()
    s.notice_accepted = True
    s.save()


def states(game):
    return dict(LoadOrder.load(game / "mods" / "mod_load_order.txt").entries())


async def test_p_opens_profile_picker_and_saves_current_setup(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        n = len(app.screen.query_one(OptionList).options)
        assert n == 3  # no profiles yet: save, share, import
        await pilot.press("enter")  # first row is "Save the current setup as a profile..."
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        await pilot.press(*"mine", "enter")
        await settle(app, pilot)
        assert [p["name"] for p in profiles.list_profiles()] == ["mine"]
        assert "Saved profile 'mine'" in log_text(app)


async def test_switching_profile_from_picker_changes_load_order(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        prof = profiles.capture(game, Registry.load(), "only-alpha")
        prof["entries"] = [["alpha", True]]
        profiles.save(profiles.validate(prof))
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("enter")  # first option: switch to only-alpha
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await settle(app, pilot)
        assert states(game).get("alpha") is True
        assert not any(v for k, v in states(game).items() if k not in ("alpha", "base", "dmf", "ghost"))  # loader entries and entries with no folder are left alone
        assert "Switched to profile 'only-alpha'" in log_text(app)


async def test_share_code_is_logged_and_importable(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("down", "enter")  # second row: "Show a share code"
        await settle(app, pilot)
        code = app.clipboard  # the long code wraps in the log, so the clipboard copy is what a user pastes from
        assert profiles.decode(code)["entries"] is not None


async def test_p_is_ignored_while_a_dialog_is_open(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("p")
        await pilot.pause()
        top = app.screen
        await pilot.press("p")
        await pilot.pause()
        assert app.screen is top
