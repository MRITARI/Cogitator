"""Pilot tests. Fake game dir only: no real Steam, game, network (respx) or keyring (conftest fakes it)."""
import asyncio
import json
import logging
import threading
import time
import zipfile
from pathlib import Path

import pytest
import respx
from rich.text import Text
from textual.widgets import DataTable, Input, RichLog, Static

from cogitator import glyphs as glyph_mod
from cogitator.config import Settings, get_api_key, log_path, setup_logging
from cogitator.frameworks import DML_REQUIRED
from cogitator.linking import link_mod
from cogitator.loadorder import LoadOrder
from cogitator.nexus import GAME, parse_files
from cogitator.registry import Registry
from cogitator.tui import app as tui
from cogitator.tui.app import CogitatorApp, hint_for, mod_cell, nexus_text, status_text
from cogitator.tui.screens import ConfirmScreen, InputScreen, NoticeScreen
from cogitator.updates import ModRow

from test_patcher import FakeTool, ORIG  # the 0.1.7-faithful fake dtkit-patch

FX = Path(__file__).parent / "fixtures" / "nexus"
B = "https://api.nexusmods.com/v1"
HDR = {"x-rl-daily-remaining": "19990", "x-rl-hourly-remaining": "1990"}
SIZE = (140, 45)
REAL_NAME = "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z ndQ1md9gG.zip"


def fx(n):
    return json.loads((FX / n).read_text(encoding="utf-8"))


@pytest.fixture
def game(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "bundle" / "bundle_database.data").write_bytes(ORIG)
    for rel in DML_REQUIRED + ("tools/dtkit-patch.exe", "mods/dmf/dmf.mod"):
        p = g / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    (g / "mods" / "dmf" / "scripts" / "mods" / "dmf").mkdir(parents=True)
    (g / "mods" / "dmf" / "scripts" / "mods" / "dmf" / "dmf_loader.lua").write_text("x")
    for m, has_mod in (("alpha", True), ("beta", True), ("gamma", True), ("broken", False)):
        (g / "mods" / m).mkdir()
        if has_mod:
            (g / "mods" / m / f"{m}.mod").write_text("return {}")
    (g / "mods" / "mod_load_order.txt").write_bytes(b"-- header\r\nalpha\r\n--beta\r\nghost\r\n")
    return g


@pytest.fixture(autouse=True)
def accepted_notice():
    s = Settings.load()
    s.notice_accepted = True
    s.save()


def make_app(game, **kw):
    app = CogitatorApp(game, check_updates=kw.pop("check_updates", False), ascii_glyphs=kw.pop("ascii_glyphs", False), **kw)
    app.game_running = lambda: False
    return app


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def cell(app, folder, col):
    t = app.query_one("#mods", DataTable)
    v = t.get_cell(folder, col)
    return v.plain if isinstance(v, Text) else str(v)


def log_text(app):
    return "\n".join(line.text for line in app.query_one("#log", RichLog).lines)


def select(app, folder):
    t = app.query_one("#mods", DataTable)
    t.move_cursor(row=t.get_row_index(folder))


# ------------------------------------------------------------ rendering

async def test_boots_and_renders_table_header_and_badge(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        t = app.query_one("#mods", DataTable)
        assert [c.label.plain for c in t.columns.values()] == ["\u2714/\u2716", "Mod", "Installed", "Latest", "Status"]
        keys = [r.value for r in t.rows]
        assert keys == ["alpha", "beta", "ghost", "broken", "gamma"]  # load order first, then unlisted A-Z
        assert cell(app, "alpha", "en") == "\u2714" and cell(app, "beta", "en") == "\u2716" and cell(app, "gamma", "en") == "\u00b7"
        assert "(folder missing)" in cell(app, "ghost", "mod") and "(no .mod file!)" in cell(app, "broken", "mod")
        assert cell(app, "alpha", "status") == "not linked"
        hdr = str(app.query_one("#hdr", Static).render())
        assert str(game) in hdr and "DML installed" in hdr and "DMF installed" in hdr and "MODS: OFF" in hdr
        assert "no API key - press K" in hdr
        assert app.query_one("#banner").has_class("on")  # mods OFF -> prominent banner
        assert "press T to enable mods" in str(app.query_one("#banner", Static).render())
        assert "-" in str(app.query_one("#banner", Static).render()) and "\u2014" not in str(app.query_one("#banner", Static).render())


async def test_footer_lists_the_key_hints(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        shown = {a.binding.key_display or a.binding.key for a in app.active_bindings.values() if a.binding.show}
        assert {"T", "Space", "U", "Ctrl+U", "F", "D", "R", "L", "I", "K", "Q"} <= shown
        assert "Shift+U" not in shown


async def test_empty_state_message(tmp_path):
    g = tmp_path / "G"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    app = make_app(g)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert app.query_one("#mods", DataTable).row_count == 0
        assert "machine spirit" in str(app.query_one("#hint", Static).render())


def test_pure_view_helpers():
    from cogitator.mods import Mod
    g = glyph_mod.UNICODE
    unl = Mod("x", "unlisted")
    assert "Space" in hint_for(unl, None) and "add" in hint_for(unl, None)
    assert "badly" in hint_for(Mod("x", "enabled", True, False), None)
    assert "L to link" in hint_for(Mod("x", "enabled"), None)
    row = ModRow("x", 1, "update available", "1.0", "2.0", "1.0 -> 2.0")
    assert status_text(row, g).plain == "update available (1.0 \u2192 2.0)"
    assert status_text(row, glyph_mod.ASCII).plain == "update available (1.0 -> 2.0)"
    t = nexus_text(True, False, "10 daily / 5 hourly left (as of 14:22)", "checking")
    assert "key ok (free)" in t.plain and "as of 14:22" in t.plain and "checking" in t.plain
    assert nexus_text(True, None, None).plain.startswith("Nexus: key set (account unknown)")
    assert mod_cell(unl).style == "dim"


# ------------------------------------------------------------ glyph fallback

def test_glyph_fallback_selection(monkeypatch):
    assert not glyph_mod.glyphs_for("utf-8").ascii
    for enc in ("cp437", "cp1252", "ascii", "latin-1", None, "no-such-codec"):
        assert glyph_mod.glyphs_for(enc).ascii, enc
    assert glyph_mod.glyphs_for("utf-8", force_ascii=True).ascii
    monkeypatch.setenv("COGITATOR_ASCII", "1")
    assert glyph_mod.glyphs_for("utf-8").ascii
    for g in (glyph_mod.ASCII.on, glyph_mod.ASCII.off, glyph_mod.ASCII.arrow, *glyph_mod.ASCII.spinner):
        g.encode("ascii")


async def test_ascii_mode_renders_without_non_ascii_glyphs(game):
    app = make_app(game, ascii_glyphs=True)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert cell(app, "alpha", "en") == "[x]" and cell(app, "beta", "en") == "[ ]" and cell(app, "gamma", "en") == "."
        t = app.query_one("#mods", DataTable)
        assert list(t.columns.values())[0].label.plain == "[x]/[ ]"
        for key in ("alpha", "beta", "gamma"):
            for col in ("en", "installed", "latest", "status"):
                cell(app, key, col).encode("ascii")


# ------------------------------------------------------------ Space

async def test_space_toggles_and_rewrites_file_surgically(game):
    lo = game / "mods" / "mod_load_order.txt"
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        select(app, "alpha")
        await pilot.press("space")
        assert lo.read_bytes() == b"-- header\r\n--alpha\r\n--beta\r\nghost\r\n"
        assert cell(app, "alpha", "en") == "\u2716"
        await pilot.press("space")
        assert lo.read_bytes() == b"-- header\r\nalpha\r\n--beta\r\nghost\r\n"
        assert cell(app, "alpha", "en") == "\u2714"
        select(app, "beta")
        await pilot.press("space")
        assert LoadOrder.load(lo).state("beta") is True
        select(app, "gamma")  # unlisted -> added to the load order, enabled
        await pilot.pause()
        assert "add it to the load order" in str(app.query_one("#hint", Static).render())
        await pilot.press("space")
        assert lo.read_bytes().endswith(b"ghost\r\ngamma\r\n") and cell(app, "gamma", "en") == "\u2714"
        select(app, "ghost")  # folder missing -> error in the log pane, file untouched
        before = lo.read_bytes()
        await pilot.press("space")
        assert lo.read_bytes() == before and "No folder 'ghost'" in log_text(app)


# ------------------------------------------------------------ T

async def test_T_calls_the_patcher_adapter(game, monkeypatch):
    calls = []

    class R:
        changed, state, message = True, "ON", "mocked patch"

    monkeypatch.setattr(tui, "toggle_mods", lambda *a, **k: calls.append((a, k)) or R())
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("t")
        await settle(app, pilot)
        assert len(calls) == 1 and calls[0][0][0] == game
        assert calls[0][1]["is_running"]() is False and "mocked patch" in log_text(app)
        await pilot.press("T")  # the shifted letter works too
        await settle(app, pilot)
        assert len(calls) == 2


async def test_T_real_adapter_with_fake_tool_patches_then_confirms_before_unpatching(game):
    db = game / "bundle" / "bundle_database.data"
    tool = FakeTool()
    app = make_app(game)
    app.patch_runner = tool
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert "MODS: OFF" in str(app.query_one("#hdr", Static).render())
        await pilot.press("t")
        await settle(app, pilot)
        assert tool.calls == ["--patch"] and b"patch_999" in db.read_bytes()
        assert "MODS: ON" in str(app.query_one("#hdr", Static).render()) and not app.query_one("#banner").has_class("on")
        # un-patching asks first, and the default answer is No
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("enter")  # focus is on "No"
        await settle(app, pilot)
        assert tool.calls == ["--patch"] and b"patch_999" in db.read_bytes()
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("y")
        await settle(app, pilot)
        assert tool.calls == ["--patch", "--unpatch"] and db.read_bytes() == ORIG
        assert "MODS: OFF" in str(app.query_one("#hdr", Static).render())


async def test_T_failure_is_reported_not_raised(game):
    def boom(args, cwd):
        return 1, "", "disk on fire"

    app = make_app(game)
    app.patch_runner = boom
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("t")
        await settle(app, pilot)
        assert "Patch failed" in log_text(app) and "disk on fire" in log_text(app)
        assert (game / "bundle" / "bundle_database.data").read_bytes() == ORIG
        assert app.is_running  # app still alive


async def test_game_running_blocks_patch(game):
    app = make_app(game)
    app.game_running = lambda: True
    app.patch_runner = FakeTool()
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("t")
        await settle(app, pilot)
        assert "Darktide is running" in log_text(app) and app.patch_runner.calls == []


# ------------------------------------------------------------ modals & key guard

async def test_main_keys_are_inert_while_a_dialog_is_open_and_input_swallows_letters(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("i")
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        for ch in "qtu":
            await pilot.press(ch)
        assert isinstance(app.screen, InputScreen) and app.is_running  # 'q' typed into the field did not quit
        assert app.screen.query_one(Input).value == "qtu"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, InputScreen)


async def test_quit_releases_lock(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        lock = app._instance_lock.path
        assert lock.exists()
        await pilot.press("q")
    assert not lock.exists()


async def test_second_instance_is_refused(game):
    a = make_app(game)
    async with a.run_test(size=SIZE) as pilot:
        await settle(a, pilot)
        import os
        a._instance_lock.path.write_text(str(os.getppid()))  # pretend another live process owns it
        b = make_app(game)
        async with b.run_test(size=SIZE) as pb:
            await pb.pause()
            assert isinstance(b.screen, NoticeScreen) and "already" in "".join(str(w.render()) for w in b.screen.query(Static))
        a._instance_lock._held = False


async def test_first_run_notice_is_shown_once(game):
    s = Settings.load()
    s.notice_accepted = False
    s.save()
    app = make_app(game)
    app.show_notice = True
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, NoticeScreen)
        assert "NOT supported by Fatshark" in "".join(str(w.render()) for w in app.screen.query(Static))
        await pilot.press("enter")
        await settle(app, pilot)
        assert Settings.load().notice_accepted
    app2 = make_app(game)
    async with app2.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert not isinstance(app2.screen, NoticeScreen)


# ------------------------------------------------------------ K: masked key entry, nothing logged

SECRET = "SuperSecretNexusKey-123456789"


@respx.mock
async def test_key_modal_is_masked_validated_stored_and_never_logged(game, caplog):
    setup_logging(logging.DEBUG)
    respx.get(f"{B}/users/validate.json").respond(200, json=fx("validate.json"), headers=HDR)
    respx.get(f"{B}/games/{GAME}/mods/updated.json").respond(200, json=[])
    caplog.set_level(logging.DEBUG)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("k")
        await pilot.pause()
        inp = app.screen.query_one(Input)
        assert inp.password is True
        for ch in SECRET:
            await pilot.press(ch if ch != "-" else "minus")
        assert inp.value == SECRET
        assert SECRET not in str(app.screen.render()) and SECRET not in "".join(str(w.render()) for w in app.screen.query(Static))
        await pilot.press("enter")
        await settle(app, pilot)
        assert get_api_key() == SECRET  # stored (fake keyring in tests)
        assert "Key valid (free)" in log_text(app)
        assert "key ok (free)" in str(app.query_one("#hdr", Static).render())
        pane = log_text(app)
    for h in logging.getLogger().handlers:
        h.flush()
    assert SECRET not in pane
    assert SECRET not in caplog.text
    assert SECRET not in (log_path().read_text(encoding="utf-8") if log_path().exists() else "")
    assert SECRET not in json.dumps(Registry.load().data)


@respx.mock
async def test_bad_key_is_rejected_and_not_saved(game):
    respx.get(f"{B}/users/validate.json").respond(401)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("k")
        await pilot.pause()
        for ch in "badkeybadkey":
            await pilot.press(ch)
        await pilot.press("enter")
        await settle(app, pilot)
        assert get_api_key() is None and "Key not saved" in log_text(app) and "badkeybadkey" not in log_text(app)


# ------------------------------------------------------------ background checks, rows update in place

def seed_links(game, cached=False):
    reg = Registry.load()
    files = parse_files(fx("files_19.json"))
    if cached:  # latest known from a previous run
        link_mod(reg, "alpha", 19, "Alpha", files, via="manual", baseline=4069)  # old file -> update available
    else:       # baseline known, nothing cached yet: the row can only change once the live check answers
        link_mod(reg, "alpha", 19, "Alpha", None, via="manual", baseline="none")
        reg.set_mod("alpha", installed_file_id=4069, installed_version="25.12.02")
    link_mod(reg, "gamma", 8, "Gamma", None, via="manual", baseline="none")  # unknown baseline
    reg.set_framework("dml", version="26.06.24", source_kind="local", source="x", archive_name=REAL_NAME)


@respx.mock
async def test_start_check_runs_in_background_and_updates_rows_in_place(game, monkeypatch):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k" * 12)
    seed_links(game)
    gate = threading.Event()
    seen_during_wait = {}

    def slow_files_19(request):
        gate.wait(5)  # hold the network open while we prove the UI still responds
        import httpx
        return httpx.Response(200, json=fx("files_19.json"), headers=HDR)

    respx.get(f"{B}/users/validate.json").respond(200, json=fx("validate.json"), headers=HDR)
    respx.get(f"{B}/games/{GAME}/mods/19/files.json").mock(side_effect=slow_files_19)
    respx.get(f"{B}/games/{GAME}/mods/8/files.json").respond(200, json=fx("files_8.json"), headers=HDR)
    app = make_app(game, check_updates=True)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.checking
        # initial paint comes from cache (nothing cached for alpha yet); input keeps working while the network is stuck
        assert cell(app, "alpha", "status") == "unknown" and cell(app, "alpha", "latest") == "-"
        select(app, "beta")
        await pilot.press("space")  # a mod toggle while the check is blocked on the network
        assert cell(app, "beta", "en") == "\u2714"
        seen_during_wait["spinner"] = "checking for updates" in str(app.query_one("#hdr", Static).render())
        gate.set()
        await settle(app, pilot)
        assert seen_during_wait["spinner"]
        assert not app.checking
        assert cell(app, "alpha", "status") == "update available (25.12.02 \u2192 26.06.24)"
        assert cell(app, "alpha", "installed") == "25.12.02" and cell(app, "alpha", "latest") == "26.06.24"
        assert cell(app, "gamma", "status") == "unknown"  # linked, baseline unknown -> never "up to date"
        assert "k" * 12 not in str(app.query_one("#hdr", Static).render())
        hdr = str(app.query_one("#hdr", Static).render())
        assert "key ok (free)" in hdr and "19990 daily / 1990 hourly left (as of " in hdr
        assert "1 update(s) available" in log_text(app)
        # status line and hint follow the highlighted row
        select(app, "alpha")
        await pilot.pause()
        assert "update available" in str(app.query_one("#hint", Static).render())


@respx.mock
async def test_recheck_key_forces_live_run_after_ttl_cache(game, monkeypatch):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k" * 12)
    seed_links(game)
    v = respx.get(f"{B}/users/validate.json").respond(200, json=fx("validate.json"), headers=HDR)
    respx.get(f"{B}/games/{GAME}/mods/19/files.json").respond(200, json=fx("files_19.json"), headers=HDR)
    respx.get(f"{B}/games/{GAME}/mods/8/files.json").respond(200, json=fx("files_8.json"), headers=HDR)
    respx.get(f"{B}/games/{GAME}/mods/updated.json").respond(200, json=[], headers=HDR)
    app = make_app(game, check_updates=True)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        first = v.call_count
        assert first >= 1
        app.start_check()  # inside the TTL: no network
        await settle(app, pilot)
        assert v.call_count == first
        await pilot.press("r")  # R forces past the TTL
        await settle(app, pilot)
        assert v.call_count == first + 1


async def test_no_key_message_and_no_crash(game):
    seed_links(game)
    app = make_app(game, check_updates=True)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert "No Nexus API key" in log_text(app) and "no API key - press K" in str(app.query_one("#hdr", Static).render())


@respx.mock
async def test_worker_exception_goes_to_log_pane_not_a_crash(game, monkeypatch):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k" * 12)
    seed_links(game)

    def explode(*a, **k):
        raise RuntimeError("kaboom with key " + "k" * 12)

    monkeypatch.setattr(tui, "run_checks", explode)
    app = make_app(game, check_updates=True)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert "update check failed" in log_text(app) and "kaboom" in log_text(app)
        assert "k" * 12 not in log_text(app)  # redacted
        assert not app.checking
        await pilot.press("space")  # still responsive
        await pilot.pause()


# ------------------------------------------------------------ L: link

@respx.mock
async def test_link_flow_shows_name_for_confirmation(game, monkeypatch):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k" * 12)
    respx.get(f"{B}/games/{GAME}/mods/19.json").respond(200, json=fx("mod_19.json"), headers=HDR)
    respx.get(f"{B}/games/{GAME}/mods/19/files.json").respond(200, json=fx("files_19.json"), headers=HDR)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        select(app, "alpha")
        await pilot.press("l")
        await pilot.pause()
        for ch in "19":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        for _ in range(50):  # worker is blocked on the choice dialog (not the UI): wait for it to appear
            await pilot.pause(0.05)
            if app.screen.query("OptionList"):
                break
        text = "".join(str(w.render()) for w in app.screen.query(Static))
        assert "Darktide Mod Loader" in text and "latest 26.06.24" in text and "alpha" in text
        await pilot.press("enter")  # first option: link + mark current as installed
        await settle(app, pilot)
        rec = Registry.load().mod("alpha")
        assert rec["nexus_id"] == 19 and rec["installed_file_id"] == 5781
        assert cell(app, "alpha", "status") == "up to date"


async def test_link_rejects_bad_reference(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        select(app, "alpha")
        await pilot.press("l")
        await pilot.pause()
        for ch in "nope":
            await pilot.press(ch)
        await pilot.press("enter")
        await settle(app, pilot)
        assert "Not a Nexus mod URL" in log_text(app) and Registry.load().mod("alpha") is None


# ------------------------------------------------------------ I: import, U: update

def mod_zip(path, folder, payload="v1"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{folder}/{folder}.mod", "return {}")
        z.writestr(f"{folder}/{folder}.lua", payload)
    return path


async def test_import_flow_installs_and_adds_to_load_order(game, tmp_path):
    zp = mod_zip(tmp_path / "newmod.zip", "newmod")
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("i")
        await pilot.pause()
        app.screen.query_one(Input).value = str(zp)
        await pilot.press("enter")
        for _ in range(60):  # worker asks (1) which Nexus mod -> skip, (2) confirm install
            await pilot.pause(0.05)
            if isinstance(app.screen, InputScreen):
                break
        assert isinstance(app.screen, InputScreen)  # name carries no mod id -> it asks, it does not guess
        await pilot.press("enter")  # blank = install without linking
        for _ in range(60):
            await pilot.pause(0.05)
            if isinstance(app.screen, ConfirmScreen):
                break
        await pilot.press("y")
        for _ in range(60):  # import_archive itself may ask; "install without linking" is its only option
            await pilot.pause(0.05)
            if app.screen.query("OptionList"):
                await pilot.press("enter")
                break
        await settle(app, pilot)
        assert (game / "mods" / "newmod" / "newmod.mod").is_file()
        assert LoadOrder.load(game / "mods" / "mod_load_order.txt").state("newmod") is True
        assert cell(app, "newmod", "en") == "\u2714" and "Installed newmod" in log_text(app)


async def test_import_missing_file_is_a_log_line(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("i")
        await pilot.pause()
        app.screen.query_one(Input).value = r"C:\nope\missing.zip"
        await pilot.press("enter")
        await settle(app, pilot)
        assert "No such file" in log_text(app)


async def test_U_on_free_account_opens_page_and_offers_import(game, tmp_path, monkeypatch):
    seed_links(game, cached=True)
    reg = Registry.load()
    reg.data["updates"]["premium"] = False
    reg.save()
    opened = []
    dl = tmp_path / "Downloads"
    dl.mkdir()
    settings = Settings.load()
    settings.downloads_dir = str(dl)
    settings.save()

    def fake_open(mod_id):
        opened.append(mod_id)
        mod_zip(dl / REAL_NAME, "alpha", "v2")  # the "browser download" lands
        return f"https://www.nexusmods.com/{GAME}/mods/{mod_id}?tab=files"

    monkeypatch.setattr(tui, "open_files_page", fake_open)
    monkeypatch.setattr(tui, "wait_for_download", lambda *a, **k: dl / REAL_NAME)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.urows["alpha"] = ModRow("alpha", 19, "update available", "25.12.02", "26.06.24", "25.12.02 -> 26.06.24")
        select(app, "alpha")
        await pilot.press("u")
        for _ in range(60):
            await pilot.pause(0.05)
            if isinstance(app.screen, ConfirmScreen):
                break
        assert opened == [19] and isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")  # import the downloaded file
        for _ in range(60):
            await pilot.pause(0.05)
            if isinstance(app.screen, ConfirmScreen):
                break
        await pilot.press("y")  # install confirmation
        await settle(app, pilot)
        assert (game / "mods" / "alpha" / "alpha.lua").read_text() == "v2"


async def test_U_nothing_to_update_and_ctrl_U_free_lists_links(game):
    seed_links(game)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        select(app, "gamma")
        await pilot.press("u")
        assert "nothing to update" in log_text(app)
        app.urows["alpha"] = ModRow("alpha", 19, "update available", "1", "2", "1 -> 2")
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert "Free account" in log_text(app) and f"{GAME}/mods/19?tab=files" in log_text(app)
        assert not isinstance(app.screen, ConfirmScreen)


@respx.mock
async def test_ctrl_U_premium_updates_all_after_one_confirm(game, monkeypatch):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k" * 12)
    seed_links(game)
    reg = Registry.load()
    reg.data["updates"]["premium"] = True
    reg.save()
    done = []
    monkeypatch.setattr(tui, "apply_update_premium", lambda f, *a, **k: done.append(f) or type("R", (), {"version": "9", "file_id": 1})())
    monkeypatch.setattr(tui, "run_checks", lambda *a, **k: type("B", (), {"mods": type("M", (), {"rows": [], "rate_limited": False, "error": ""})(),
                                                                          "frameworks": [], "key_set": True, "live": False, "requests": 0})())
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.urows["alpha"] = ModRow("alpha", 19, "update available", "1", "2", "1 -> 2")
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await settle(app, pilot)
        assert done == ["alpha"]


# ------------------------------------------------------------ bootstrap offer

async def test_missing_framework_offers_install_and_declining_logs_nexus_pages(tmp_path):
    g = tmp_path / "G"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "bundle" / "bundle_database.data").write_bytes(ORIG)
    app = make_app(g)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        text = "".join(str(w.render()) for w in app.screen.query("Static"))
        assert app.screen.query("OptionList") and "Install Darktide Mod Loader and Mod Framework?" in text
        await pilot.press("down", "down", "enter")  # "Not now"
        await settle(app, pilot)
        assert "nexusmods.com/warhammer40kdarktide/mods/19" in log_text(app)
        assert not app.query_one("#banner").has_class("on")  # no DML -> no "press T" banner


# ------------------------------------------------------------ sorting

async def test_sort_cycles_columns(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("o")  # mod name
        keys = [r.key.value for r in app.query_one("#mods", DataTable).ordered_rows]
        assert [k for k in keys] == sorted(keys, key=lambda k: cell(app, k, "mod").casefold())
        await pilot.press("o")  # status
        assert "Sorted by status" in log_text(app)


# ------------------------------------------------------------ non-TUI plumbing

def test_headless_commands_do_not_import_textual():
    import subprocess, sys
    code = ("import sys; from cogitator import cli; cli.build_parser().parse_args(['status']); "
            "print('textual' in sys.modules)")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "False", out.stderr


# ------------------------------------------------------------ framework install through the TUI

from test_frameworks import DML_FILES, DMF_FILES, mkzip as fw_mkzip  # noqa: E402
from cogitator.loader import GITHUB_API  # noqa: E402


def bare_game(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "bundle" / "bundle_database.data").write_bytes(ORIG)
    return g


async def wait_for(app, pilot, pred, tries=80):
    for _ in range(tries):
        await pilot.pause(0.05)
        if pred():
            return True
    return False


def dialog_text(app):
    return "".join(str(w.render()) for w in app.screen.query(Static))


async def test_I_installs_a_framework_zip_after_showing_the_source(tmp_path):
    g = bare_game(tmp_path)
    dml = fw_mkzip(tmp_path / "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z abc.zip", DML_FILES)
    app = make_app(g)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("down", "down", "enter")  # bootstrap offer -> "Not now"
        await settle(app, pilot)
        assert "DML missing" in str(app.query_one("#hdr", Static).render()) or "DML" in str(app.query_one("#hdr", Static).render())
        await pilot.press("i")
        await pilot.pause()
        app.screen.query_one(Input).value = str(dml)
        await pilot.press("enter")
        assert await wait_for(app, pilot, lambda: isinstance(app.screen, ConfirmScreen))
        text = dialog_text(app)
        assert "Install DML" in text and "local file Darktide Mod Loader 19" in text and "SHA-256" in text
        assert not (g / "binaries" / "mod_loader").exists()  # nothing installed before the answer
        await pilot.press("y")
        await settle(app, pilot)
        assert (g / "binaries" / "mod_loader").is_file() and "DML installed" in log_text(app)
        hdr = str(app.query_one("#hdr", Static).render())
        assert "DML installed 26.06.24" in hdr and "MODS: OFF" in hdr
        assert app.query_one("#banner").has_class("on")  # DML now present, database unpatched -> prominent banner


@respx.mock
async def test_github_bootstrap_is_opt_in_and_shows_each_source_before_installing(tmp_path):
    import io
    g = bare_game(tmp_path)
    DML_R, DMF_R = "Darktide-Mod-Framework/Darktide-Mod-Loader", "Darktide-Mod-Framework/Darktide-Mod-Framework"
    sha = "0107475abcdef0123456789abcdef0123456789a"

    def zbytes(files, prefix):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            for n, d in files.items():
                z.writestr(prefix + n, d)
        return b.getvalue()

    api = lambda p: f"{GITHUB_API}/repos/{p}"
    gh = [respx.get(api(f"{DML_R}/releases/latest")).respond(200, json={"tag_name": "26.06.24", "assets": []}),
          respx.get(api(f"{DML_R}/zipball/26.06.24")).respond(200, content=zbytes(DML_FILES, "o-r-4bd075a/")),
          respx.get(api(f"{DMF_R}/releases/latest")).respond(404),
          respx.get(api(f"{DMF_R}/commits/HEAD")).respond(200, json={"sha": sha, "commit": {"committer": {"date": "2026-10-22T00:00:00Z"}}}),
          respx.get(api(f"{DMF_R}/zipball/{sha}")).respond(200, content=zbytes(DMF_FILES, "o-r-0107475/"))]
    app = make_app(g)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert all(r.call_count == 0 for r in gh)  # offering the install touches no network
        await pilot.press("down", "enter")  # "Fetch from GitHub (opt-in ...)"
        assert await wait_for(app, pilot, lambda: isinstance(app.screen, ConfirmScreen))
        t1 = dialog_text(app)
        assert "Install DML from GitHub?" in t1 and "tag 26.06.24" in t1 and "github.com".replace("github", "api.github") in t1
        assert not (g / "binaries" / "mod_loader").exists()
        await pilot.press("y")
        assert await wait_for(app, pilot, lambda: isinstance(app.screen, ConfirmScreen) and "DMF" in dialog_text(app))
        t2 = dialog_text(app)
        assert sha in t2 and "UNRELEASED SNAPSHOT" in t2
        await pilot.press("n")  # decline the unreleased snapshot
        await settle(app, pilot)
        assert (g / "binaries" / "mod_loader").is_file() and not (g / "mods" / "dmf").exists()
        assert "DMF install declined" in log_text(app)
        assert Registry.load().framework("dml")["source_kind"] == "github-tag-source" and Registry.load().framework("dmf") is None
