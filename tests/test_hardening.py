"""Recovery and safety behaviours: interrupted patch, interrupted update swap, unreadable settings, load-order names,
signed checksum list, GitHub-only downloads."""
import base64
import types

import pytest

from cogitator import cli, loader
from cogitator.config import RECOVERED, Settings, config_dir
from cogitator.loadorder import LoadOrder
from cogitator.patcher import PatcherError, interrupted_patch, patch, restore_database
from cogitator.registry import Registry
from cogitator.selfupdate import repair_install

from test_patcher import ORIG, FakeTool, db, game, kw  # noqa: F401  (game is a fixture)


def test_a_killed_patch_leaves_a_marker_and_restore_database_undoes_it(game):
    reg = Registry.load()

    class Killed(FakeTool):
        def __call__(self, args, cwd):
            super().__call__(args, cwd)
            raise KeyboardInterrupt  # the process dies after the tool touched the file

    with pytest.raises(KeyboardInterrupt):
        patch(game, reg, **kw(Killed()))
    assert interrupted_patch(reg) is not None
    assert db(game) != ORIG
    msg = restore_database(game, reg, is_running=lambda: False)
    assert "original" in msg and db(game) == ORIG and interrupted_patch(reg) is None


def test_restore_database_refuses_when_nothing_was_interrupted(game):
    with pytest.raises(PatcherError):
        restore_database(game, Registry.load(), is_running=lambda: False)


def test_a_finished_patch_clears_the_marker(game):
    reg = Registry.load()
    patch(game, reg, **kw(FakeTool()))
    assert interrupted_patch(reg) is None


def _dirs(root, **present):
    for name in present:
        (root / name).mkdir()
        (root / name / "x.txt").write_text(name)
        (root / name / "cogitator.exe").write_bytes(b"MZ")


def test_repair_puts_the_previous_build_back_when_app_is_missing(tmp_path):
    _dirs(tmp_path, **{"app.previous": 1, "app.previous.old": 1})
    assert repair_install(tmp_path)
    assert (tmp_path / "app" / "x.txt").read_text() == "app.previous"
    assert (tmp_path / "app.previous" / "x.txt").read_text() == "app.previous.old"


def test_repair_finishes_a_rollback_exchange_and_never_touches_a_healthy_install(tmp_path):
    _dirs(tmp_path, **{"app.swap": 1, "app.previous": 1})
    repair_install(tmp_path)
    assert (tmp_path / "app").is_dir()
    healthy = tmp_path / "ok"
    healthy.mkdir()
    _dirs(healthy, app=1, **{"app.previous": 1})
    assert repair_install(healthy) == [] and (healthy / "app").is_dir() and (healthy / "app.previous").is_dir()


def test_unreadable_settings_are_kept_and_reported_not_overwritten():
    RECOVERED.clear()
    config_dir().mkdir(parents=True, exist_ok=True)
    Settings.path().write_text('game_dir = "C:\\broken\nupdate_mode = "off"\n', encoding="utf-8")
    s = Settings.load()
    assert s.update_mode == "notify" and RECOVERED
    assert RECOVERED[0].is_file() and not Settings.path().exists()


def test_update_modes_that_download_need_explicit_consent():
    config_dir().mkdir(parents=True, exist_ok=True)
    Settings.path().write_text('update_mode = "download"\n', encoding="utf-8")
    assert Settings.load().update_mode == "notify"
    Settings.path().write_text('update_mode = "download"\nupdate_consent = true\n', encoding="utf-8")
    assert Settings.load().update_mode == "download"


def test_load_order_names_ignore_case():
    lo = LoadOrder.parse("Alpha\r\n--Beta\r\n")
    assert lo.state("alpha") is True and lo.state("beta") is False
    assert lo.set_enabled("BETA", True) and lo.remove("alpha") == 1


def test_verify_download_accepts_a_signed_list_and_rejects_tampering(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    monkeypatch.setattr("cogitator.selfupdate.RELEASE_PUBLIC_KEY_HEX", pub)
    f = tmp_path / "Cogitator-1.0.0-win-x64-installer.exe"
    f.write_bytes(b"installer")
    import hashlib

    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{hashlib.sha256(b'installer').hexdigest()}  {f.name}\n", encoding="utf-8", newline="\n")
    (tmp_path / "SHA256SUMS.sig").write_bytes(base64.b64encode(key.sign(sums.read_bytes())))
    args = types.SimpleNamespace(sums=str(sums), file=str(f))
    assert cli.cmd_verify_download(args) == 0
    f.write_bytes(b"tampered")
    assert cli.cmd_verify_download(args) == 1
    sums.write_text("00  x\n", encoding="utf-8")
    assert cli.cmd_verify_download(types.SimpleNamespace(sums=str(sums), file=None)) == 1


def test_github_only_client_refuses_other_hosts():
    with loader.make_client(github_only=True) as c:
        with pytest.raises(loader.LoaderError):
            c.get("https://evil.example/x.zip")


def test_repair_never_restores_or_deletes_when_the_folders_are_not_complete(tmp_path):
    for n in ("app.previous", "app.previous.old", "app.failed"):
        (tmp_path / n).mkdir()  # no cogitator.exe in any of them
    assert repair_install(tmp_path) == [] and not (tmp_path / "app").exists()
    (tmp_path / "app").mkdir()  # an app folder without the program counts as unusable: leftovers stay
    assert repair_install(tmp_path, full=True) == [] and (tmp_path / "app.failed").is_dir() and (tmp_path / "app.previous.old").is_dir()


def test_restore_database_only_clears_the_marker_when_the_database_is_already_the_original(game):
    reg = Registry.load()

    class Killed(FakeTool):
        def __call__(self, args, cwd):
            raise KeyboardInterrupt  # killed before the tool changed anything

    with pytest.raises(KeyboardInterrupt):
        patch(game, reg, **kw(Killed()))
    assert restore_database(game, reg, is_running=lambda: False) and interrupted_patch(reg) is None and db(game) == ORIG


def test_restore_database_refuses_a_damaged_copy_and_a_running_game(game, tmp_path):
    from cogitator.config import cache_dir

    reg = Registry.load()

    class Killed(FakeTool):
        def __call__(self, args, cwd):
            super().__call__(args, cwd)
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        patch(game, reg, **kw(Killed()))
    patched = db(game)
    with pytest.raises(PatcherError, match="running"):
        restore_database(game, reg, is_running=lambda: True)
    (cache_dir() / "db-backups" / f"{reg.data['patch']['in_progress']['build_key']}.data").write_bytes(b"bad")
    with pytest.raises(PatcherError, match="damaged"):
        restore_database(game, reg, is_running=lambda: False)
    assert db(game) == patched and interrupted_patch(reg) is not None  # nothing overwritten, marker kept


@pytest.mark.parametrize("url,ok", [
    ("https://github.com/a/b/releases/download/v1/x.zip", True),
    ("https://api.github.com/repos/a/b/zipball/v1", True),
    ("https://codeload.github.com/a/b/zip/refs/tags/v1", True),
    ("https://release-assets.githubusercontent.com/x", True),
    ("https://objects.githubusercontent.com/x", True),
    ("https://github.com.evil.example/x", False),
    ("https://evilgithub.com/x", False),
    ("https://github.com@evil.example/x", False),
    ("https://evil.example@github.com/x", True),  # the user-info is ignored: the host is still github.com
    ("https://github.com:8443/x", False),
    ("http://github.com/x", False),
    ("https://github.com./x", False),
])
def test_github_only_host_check(url, ok):
    import httpx

    req = httpx.Request("GET", url)
    if ok:
        loader._only_github(req)
    else:
        with pytest.raises(loader.LoaderError):
            loader._only_github(req)


def test_settings_with_wrong_types_fall_back():
    config_dir().mkdir(parents=True, exist_ok=True)
    Settings.path().write_text("\n".join(['update_mode = 5', 'update_consent = "yes"', 'update_channel = []']), encoding="utf-8")
    s = Settings.load()
    assert (s.update_mode, s.update_consent, s.update_channel) == ("notify", False, "stable")


async def test_first_run_notice_keeps_its_ok_button_on_screen_at_80x24(game):
    from test_tui import make_app, settle

    app = make_app(game)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        r = app.screen.query_one("#ok").region
        assert r.height > 0 and r.bottom <= 24
