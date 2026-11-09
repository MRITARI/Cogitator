"""TUI keys: update selected (U) versus update all (Ctrl+U), framework update (F), and the doctor (D)."""
import json
import zipfile
from pathlib import Path

import pytest
import respx
from textual.widgets import RichLog, Static

from cogitator.config import Settings, set_api_key
from cogitator.nexus import GAME
from cogitator.registry import Registry
from cogitator.tui import app as tui
from cogitator.tui.screens import ConfirmScreen, DoctorScreen
from cogitator.updates import UPDATE, FrameworkUpdate, ModRow

from test_frameworks import DML_FILES, mkzip
from test_patcher import FakeTool, ORIG
from test_tui import (HDR, B, REAL_NAME, SIZE, dialog_text, fx, game, log_text, make_app, seed_links, select, settle,  # noqa: F401
                      wait_for)

DML_UPDATE = FrameworkUpdate("dml", 19, UPDATE, "25.12.02", "26.06.24", 5781, "update", REAL_NAME, 156030)


@pytest.fixture(autouse=True)
def accepted(tmp_path):
    s = Settings.load()
    s.notice_accepted = True
    s.save()


# ------------------------------------------------------------ (1) case can never turn "update" into "update all"

@pytest.mark.parametrize("key", ["u", "U"])
async def test_u_and_capital_U_both_update_only_the_selected_mod(game, monkeypatch, key):
    seed_links(game, cached=True)
    reg = Registry.load()
    reg.data["updates"]["premium"] = False
    reg.save()
    opened = []
    monkeypatch.setattr(tui, "open_files_page", lambda mid: opened.append(mid) or "url")
    monkeypatch.setattr(tui, "wait_for_download", lambda *a, **k: None)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.urows["alpha"] = ModRow("alpha", 19, "update available", "1", "2", "1 -> 2")
        app.urows["gamma"] = ModRow("gamma", 8, "update available", "1", "2", "1 -> 2")
        select(app, "alpha")
        await pilot.press(key)
        await settle(app, pilot)
        assert opened == [19]  # exactly the selected mod; gamma (also updatable) was not touched
        assert "Free account: download each file" not in log_text(app)  # the update-all path did not run


async def test_ctrl_u_is_update_all_and_footer_says_so(game):
    seed_links(game)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.urows["alpha"] = ModRow("alpha", 19, "update available", "1", "2", "1 -> 2")
        app.urows["gamma"] = ModRow("gamma", 8, "update available", "1", "2", "1 -> 2")
        await pilot.press("ctrl+u")
        await pilot.pause()
        text = log_text(app)
        assert "alpha" in text and "gamma" in text  # both listed -> update-all ran
        labels = {a.binding.key_display or a.binding.key: a.binding.description for a in app.active_bindings.values() if a.binding.show}
        assert labels["Ctrl+U"] == "All" and labels["U"] == "Update"


# ------------------------------------------------------------ (2) F: framework update through the import flow

def dml_zip(tmp_path):
    return mkzip(tmp_path / REAL_NAME, DML_FILES)


async def test_F_with_nothing_to_update_says_so(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("f")
        await pilot.pause()
        assert "No framework update to apply" in log_text(app) and not isinstance(app.screen, ConfirmScreen)


async def test_F_free_account_confirms_opens_page_then_runs_the_import_flow(game, tmp_path, monkeypatch):
    zp = dml_zip(tmp_path)
    opened = []
    monkeypatch.setattr(tui, "open_files_page", lambda mid: opened.append(mid) or "url")
    monkeypatch.setattr(tui, "wait_for_download", lambda *a, **k: zp)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.fw_updates = [DML_UPDATE]
        await pilot.press("F")  # capital works too
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        text = dialog_text(app)
        assert "Update DML?" in text and "DML: 25.12.02 -> 26.06.24" in text and "opens the Nexus files page" in text
        assert opened == []  # nothing happens before the confirmation
        await pilot.press("y")
        assert await wait_for(app, pilot, lambda: isinstance(app.screen, ConfirmScreen) and "Install DML" in dialog_text(app))
        assert opened == [19]
        assert "SHA-256" in dialog_text(app) and "local file" in dialog_text(app)  # the existing flow's source confirmation
        await pilot.press("y")
        await settle(app, pilot)
        assert "DML installed" in log_text(app)
        assert Registry.load().framework("dml")["archive_name"] == REAL_NAME


async def test_F_declined_does_nothing(game, monkeypatch):
    opened = []
    monkeypatch.setattr(tui, "open_files_page", lambda mid: opened.append(mid) or "url")
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.fw_updates = [DML_UPDATE]
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("n")
        await settle(app, pilot)
        assert opened == [] and "DML installed" not in log_text(app)


async def test_F_dml_update_while_mods_on_unpatches_installs_and_repatches(game, tmp_path, monkeypatch):
    zp = dml_zip(tmp_path)
    tool = FakeTool()
    monkeypatch.setattr(tui, "open_files_page", lambda mid: "url")
    monkeypatch.setattr(tui, "wait_for_download", lambda *a, **k: zp)
    db = game / "bundle" / "bundle_database.data"
    app = make_app(game)
    app.patch_runner = tool
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("t")  # mods ON first
        await settle(app, pilot)
        assert tool.calls == ["--patch"] and b"patch_999" in db.read_bytes()
        app.fw_updates = [DML_UPDATE]
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("y")
        assert await wait_for(app, pilot, lambda: isinstance(app.screen, ConfirmScreen) and "Install DML" in dialog_text(app))
        assert "un-patch the game, install, then re-patch" in dialog_text(app)
        await pilot.press("y")
        await settle(app, pilot)
        assert tool.calls == ["--patch", "--unpatch", "--patch"]
        assert b"patch_999" in db.read_bytes() and "re-patched" in log_text(app)
        assert "MODS: ON" in str(app.query_one("#hdr", Static).render())


@respx.mock
async def test_F_premium_downloads_through_the_api_then_uses_the_same_import_flow(game, tmp_path, monkeypatch):
    set_api_key("k" * 12)
    reg = Registry.load()
    reg.data["updates"]["premium"] = True
    reg.save()
    data = dml_zip(tmp_path).read_bytes()
    respx.get(f"{B}/games/{GAME}/mods/19/files/5781/download_link.json").respond(200, json=[{"URI": "https://cdn.example/dml.zip"}], headers=HDR)
    respx.get("https://cdn.example/dml.zip").respond(200, content=data)
    monkeypatch.setattr(tui, "open_files_page", lambda mid: pytest.fail("premium must not open a browser"))
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.fw_updates = [FrameworkUpdate("dml", 19, UPDATE, "25.12.02", "26.06.24", 5781, "u", REAL_NAME, len(data))]
        await pilot.press("f")
        await pilot.pause()
        assert "Premium account" in dialog_text(app)
        await pilot.press("y")
        assert await wait_for(app, pilot, lambda: isinstance(app.screen, ConfirmScreen) and "Install DML" in dialog_text(app))
        await pilot.press("y")
        await settle(app, pilot)
        assert "DML installed" in log_text(app)


async def test_F_is_inert_while_a_dialog_is_open(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("i")
        await pilot.pause()
        n = len(app.screen_stack)
        await pilot.press("f")
        await pilot.pause()
        assert len(app.screen_stack) == n


# ------------------------------------------------------------ (3) D: doctor in the TUI

async def test_D_opens_the_doctor_report_and_closes_with_escape(game):
    app = make_app(game)
    app.doctor_offline = True
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("d")
        assert await wait_for(app, pilot, lambda: isinstance(app.screen, DoctorScreen))
        report = str(app.screen.query_one("#report", Static).render())
        assert "Darktide Mod Loader (DML)" in report and "[OK]" in report and "patch_999 consistency" in report
        assert "registry.json vs mods/" in report and "mod_load_order.txt" in report.replace("Load order", "mod_load_order.txt")
        await pilot.press("q")  # main keys are inert inside the report
        assert isinstance(app.screen, DoctorScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DoctorScreen)
        await pilot.press("D")  # capital works, and D closes it again
        assert await wait_for(app, pilot, lambda: isinstance(app.screen, DoctorScreen))
        await pilot.press("d")
        await pilot.pause()
        assert not isinstance(app.screen, DoctorScreen)


async def test_D_reports_problems_with_fixes(game):
    (game / "bundle" / "9ba626afa44a3aa3.patch_999").write_bytes(b"x")
    app = make_app(game)
    app.doctor_offline = True
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("d")
        assert await wait_for(app, pilot, lambda: isinstance(app.screen, DoctorScreen))
        report = str(app.screen.query_one("#report", Static).render())
        assert "[WARN]" in report and "fix:" in report and "Press T" in report  # mods OFF, with the remedy
        assert "ghost" in report  # listed in the load order but missing on disk
