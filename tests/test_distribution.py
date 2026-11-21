"""Authenticode signing hook, translations, and the interface's handling of nxm:// links."""
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cogitator import i18n, nxm
from cogitator.config import Settings
from cogitator.registry import Registry
from cogitator.tui.screens import ConfirmScreen
from test_tui import SIZE, fx, game, log_text, make_app, settle  # noqa: F401  (fixtures)
from test_tui_keys import dialog_text

spec = importlib.util.spec_from_file_location("release_sign", Path(__file__).parents[1] / "packaging" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

URL = "nxm://warhammer40kdarktide/mods/19/files/5?key=k1&expires=4102444800"


# ---------------------------------------------------------------- Authenticode hook

def test_signing_is_skipped_without_a_certificate(tmp_path, monkeypatch):
    monkeypatch.delenv("CODESIGN_PFX_BASE64", raising=False)
    f = tmp_path / "a.exe"
    f.write_bytes(b"x")
    ran = []
    assert release.sign_files([f], run=lambda *a, **k: ran.append(a)) is False and ran == []


def test_signing_runs_for_existing_files_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CODESIGN_PFX_BASE64", "abc")
    a = tmp_path / "a.exe"
    a.write_bytes(b"x")
    ran = []
    assert release.sign_files([a, tmp_path / "missing.exe"], run=lambda cmd, **k: ran.append(cmd)) is True
    assert str(a) in ran[0][-1] and "missing" not in ran[0][-1] and ran[0][ran[0].index("-File") + 1].endswith("sign.ps1")
    assert release.sign_files([tmp_path / "missing.exe"], run=lambda *a, **k: ran.append(1)) is False


def test_programs_are_signed_before_they_are_zipped(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(release, "sign_files", lambda paths, run=None: seen.append([p.name for p in paths]) or False)
    key = Ed25519PrivateKey.generate()
    kp = tmp_path / "k.pem"
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    app = tmp_path / "dist" / "cogitator"
    app.mkdir(parents=True)
    (app / "cogitator.exe").write_bytes(b"x")
    release.build("1.2.3", app, tmp_path / "out", kp, installer=False)
    assert seen and set(seen[0]) >= {"cogitator.exe", "cogitator-web.exe", "cogitator-updater.exe", "cogitator-nxm.exe", "Cogitator Setup.exe"}


def test_the_sign_script_is_a_noop_without_secrets():
    text = (Path(__file__).parents[1] / "packaging" / "sign.ps1").read_text(encoding="utf-8")
    assert "if (-not $env:CODESIGN_PFX_BASE64)" in text and "exit 0" in text
    assert "Remove-Item $pfx" in text  # the decoded certificate is never left on disk


# ---------------------------------------------------------------- translations

@pytest.fixture(autouse=True)
def english():
    st = Settings.load()
    st.notice_accepted = True  # the first-run notice would otherwise sit on top of every interface test
    st.save()
    yield
    i18n._extra_dirs.clear()
    i18n.set_language("en")


def write_catalog(tmp_path, code, data):
    d = tmp_path / "loc"
    d.mkdir(exist_ok=True)
    (d / f"{code}.json").write_text(json.dumps(data), encoding="utf-8")
    i18n._extra_dirs[:] = [d]


def test_tr_falls_back_to_english_and_uses_translations(tmp_path):
    assert i18n.tr("k", "Hello {name}", name="Bo") == "Hello Bo"
    write_catalog(tmp_path, "xx", {"k": "Hallo {name}", "plain": "Salut"})
    assert i18n.set_language("xx") and i18n.language() == "xx"
    assert i18n.tr("k", "Hello {name}", name="Bo") == "Hallo Bo"
    assert i18n.tr("plain", "Hi") == "Salut" and i18n.tr("other", "Other") == "Other"


def test_a_translation_that_changes_placeholders_is_ignored(tmp_path):
    write_catalog(tmp_path, "xx", {"k": "Hallo {nom}", "j": "no braces", "b": "bad {"})
    i18n.set_language("xx")
    assert i18n.tr("k", "Hello {name}", name="Bo") == "Hello Bo"
    assert i18n.tr("b", "ok {x}", x=1) == "ok 1"


def test_unknown_language_is_english(tmp_path):
    assert i18n.set_language("zz") is False
    assert i18n.tr("k", "English") == "English"
    assert i18n.set_language("en-GB") is True


def test_language_detection(monkeypatch):
    monkeypatch.delenv("COGITATOR_LANG", raising=False)
    assert i18n.detect("de") == "de"
    monkeypatch.setenv("COGITATOR_LANG", "FR")
    assert i18n.detect("de") == "fr"


def test_every_ui_key_is_collected_and_unique_in_meaning():
    keys = i18n.collect()
    assert {"notice.title", "notice.body", "help.body", "empty.state", "menu.help", "menu.quit"} <= set(keys)
    assert all(isinstance(v, str) and v for v in keys.values())


def test_check_reports_unknown_keys_and_placeholder_changes(tmp_path):
    keys = {"help.body": "log {log} cfg {cfg}", "menu.quit": "Quit   Q"}
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"help.body": "Protokoll {log} Konfig {cfg}", "menu.quit": "Beenden   Q"}))
    assert i18n.check(ok, keys) == []
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"help.body": "Protokoll {log}", "nope": "x", "menu.quit": 5}))
    problems = "\n".join(i18n.check(bad, keys))
    assert "placeholders differ" in problems and "unknown key: nope" in problems and "must be text" in problems
    assert i18n.check(tmp_path / "missing.json", keys)[0].startswith("cannot read")


def test_help_screen_is_translated_in_the_interface(game, tmp_path):
    s = Settings.load()
    s.notice_accepted = True
    s.language = "xx"
    s.save()
    keys = i18n.collect()
    body = keys["help.body"].replace("T  mods on/off (the game patch)", "XX-HELP-LINE")
    write_catalog(tmp_path, "xx", {"help.body": body})

    async def go():
        app = make_app(game)
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await pilot.press("question_mark")
            await pilot.pause()
            assert "XX-HELP-LINE" in dialog_text(app)

    import asyncio

    asyncio.run(go())


def test_shipped_catalogs_are_valid():
    for p in (Path(i18n.__file__).parent / "locales").glob("*.json"):
        assert i18n.check(p) == [], p.name


# ---------------------------------------------------------------- nxm links in the interface

def make_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("m/m.mod", "return {}")
    return buf.getvalue()


class FakeNexus:
    def __init__(self, key):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def mod(self, mod_id):
        return {"name": "Cool Mod"}

    def files(self, mod_id):
        from cogitator.nexus import NexusFile

        return [NexusFile(5, "1.0", "MAIN", 1, "Cool", "Cool-19-1-0-1.zip", True, 10)]


@pytest.fixture
def fake_nexus(monkeypatch):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k")
    monkeypatch.setattr("cogitator.actions_nexus.NexusClient", FakeNexus)


async def test_a_queued_link_downloads_and_imports_without_asking(game, fake_nexus, monkeypatch, tmp_path):
    got = {}

    def fake_download(link, nx, dest, client=None):
        dest.mkdir(parents=True, exist_ok=True)
        z = dest / "Cool.zip"
        z.write_bytes(make_zip())
        got["link"] = link
        return z, None

    monkeypatch.setattr(nxm, "download", fake_download)
    app = make_app(game)
    imported = []
    async with app.run_test(size=SIZE) as pilot:
        monkeypatch.setattr("cogitator.actions_nexus.import_file",
                            lambda ctx, ui, raw, mod_id, after, **k: imported.append((Path(raw).name, mod_id, k)))
        await settle(app, pilot)
        path = nxm.enqueue(URL)
        app._nxm_tick()
        for _ in range(60):
            await pilot.pause()
            if imported:
                break
        await settle(app, pilot)
        assert not isinstance(app.screen, ConfirmScreen)
        assert imported == [("Cool.zip", 19, {"quiet": True})] and got["link"].key == "k1"
        assert not path.exists() and app._nxm_busy is False  # the queue item is consumed, the work folder cleaned
        assert "k1" not in log_text(app)  # the key is never shown


async def test_a_link_without_an_api_key_explains(game, monkeypatch):
    monkeypatch.delenv("COGITATOR_NEXUS_KEY", raising=False)
    monkeypatch.setattr("cogitator.actions_nexus.get_api_key", lambda: None)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        nxm.enqueue(URL)
        app._nxm_tick()
        await settle(app, pilot)
        assert "no API key is set" in log_text(app) and nxm.pending() == []


async def test_links_wait_while_a_dialog_is_open(game):
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        nxm.enqueue(URL)
        await pilot.press("p")
        await pilot.pause()
        app._nxm_tick()
        assert app._nxm_busy is False and len(nxm.pending()) == 1


def test_i18n_detect_always_returns_a_two_letter_code(monkeypatch):
    from cogitator import i18n

    monkeypatch.delenv("COGITATOR_LANG", raising=False)
    for loc, want in ((None, "en"), ("English_United States", "en"), ("fi_FI", "fi"), ("C", "en")):
        monkeypatch.setattr(i18n.locale, "getlocale", lambda loc=loc: (loc, None))
        assert i18n.detect("auto") == want
