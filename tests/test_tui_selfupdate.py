"""Cogitator's own update in the TUI: header line, the A key (only while an update is ready), install / remind / skip."""
import pytest
from textual.widgets import Static

from cogitator.registry import Registry
from cogitator.selfupdate import Layout, SelfUpdater

from test_selfupdate import FakeGitHub, PUB, root  # noqa: F401  (fixture)
from test_tui import accepted_notice, game, make_app, settle  # noqa: F401


def attach(app, gh, root, spawned):
    def factory():
        return SelfUpdater(app.registry, app.settings, current="1.0.0", layout=Layout("installer", root, helper=["H"]),
                           client_factory=gh.factory(), public_key=PUB, popen=lambda a, **k: spawned.append(a), pid=lambda: 1)
    app._selfupdater = lambda: app.selfupdater or setattr(app, "selfupdater", factory()) or app.selfupdater


def hdr(app):
    return str(app.query_one("#hdr", Static).render())


async def test_update_is_announced_downloaded_and_installed_with_a(game, root):
    spawned = []
    app = make_app(game, check_updates=True)
    attach(app, FakeGitHub(), root, spawned)
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        assert "Update available v1.1.0" in hdr(app) and "Press A to install" in hdr(app)
        assert app.check_action("apply_update", ()) is True
        await pilot.press("a")
        await pilot.pause()
        assert len(app.screen_stack) == 2  # the choice dialog, with plain-text notes
        assert app.check_action("apply_update", ()) is False
        app.screen.dismiss(0)
        await pilot.pause()
    assert spawned and spawned[0][0] == "H" and "--staged" in spawned[0]


async def test_a_key_is_inert_when_nothing_is_ready(game, root):
    app = make_app(game)  # startup check off: nothing downloaded
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        assert app.check_action("apply_update", ()) is False
        await pilot.press("a")
        assert len(app.screen_stack) == 1


async def test_remind_later_and_skip_hide_the_banner(game, root):
    for choice in (1, 2):
        app = make_app(game, check_updates=True)
        attach(app, FakeGitHub(), root, [])
        async with app.run_test(size=(140, 45)) as pilot:
            await settle(app, pilot)
            await pilot.press("a")
            await pilot.pause()
            app.screen.dismiss(choice)
            await pilot.pause()
            assert "Update available" not in hdr(app)
        Registry.load()  # still loads


async def test_a_failed_update_is_explained_at_the_next_start(game):
    from cogitator.tui.app import CogitatorApp

    app = CogitatorApp(game, check_updates=False)
    app.game_running = lambda: False
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app._report_update_result({"status": "aborted", "version": "0.1.3", "detail": "Could not replace the app folder: in use"})
        await pilot.pause()
        text = "\n".join(l.text for l in app.query_one("#log").lines)
        assert "Update to v0.1.3 failed" in text and "Could not replace the app folder" in text and "update.log" in text
        app._report_update_result({"status": "ok", "version": "0.1.3"})
        await pilot.pause()
        assert "Updated to v0.1.3" in "\n".join(l.text for l in app.query_one("#log").lines)
