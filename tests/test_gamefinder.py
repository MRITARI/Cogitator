import json

from cogitator import cli
from cogitator.config import InstanceLock, LockError, RedactFilter, Settings, set_api_key, get_api_key
from cogitator.gamefinder import (
    find_candidates, library_folders, resolve_game_dir, validate_game_dir, game_in_library,
)
import logging
import pytest


def test_single_library(make_steam):
    root, games = make_steam()
    assert find_candidates([root]) == games
    assert game_in_library(root) == games[0]
    assert "," in games[0].name and " " in games[0].name  # comma + space path


def test_multiple_libraries_escaped_backslashes(make_steam):
    root, games = make_steam(libs=3, game_in=(2,))
    libs = library_folders(root)
    assert len(libs) == 3
    assert find_candidates([root]) == games


def test_old_format_and_steamapps_location(make_steam):
    root, games = make_steam(libs=2, game_in=(1,), old_format=True, loc="steamapps")
    assert find_candidates([root]) == games


def test_ambiguous(make_steam):
    root, games = make_steam(libs=2, game_in=(0, 1))
    res = resolve_game_dir(None, None, [root])
    assert res.source == "ambiguous" and len(res.candidates) == 2 and res.game_dir is None


def test_none_found(tmp_path):
    res = resolve_game_dir(None, None, [])
    assert res.source == "none"


def test_manifest_but_game_invalid(make_steam):
    root, games = make_steam()
    (games[0] / "bundle").rmdir()
    assert find_candidates([root]) == []


def test_validate(tmp_path):
    assert not validate_game_dir(tmp_path / "nope")[0]
    (tmp_path / "bundle").mkdir()
    ok, why = validate_game_dir(tmp_path)
    assert not ok and "binaries" in why
    (tmp_path / "binaries").mkdir()
    assert validate_game_dir(tmp_path) == (True, "ok")


def test_override_beats_saved(make_steam, tmp_path):
    _, games = make_steam()
    res = resolve_game_dir(str(games[0]), "garbage")
    assert res.source == "override"
    assert resolve_game_dir(str(tmp_path / "bad"), None).source == "invalid"
    assert resolve_game_dir(None, str(games[0])).source == "config"


def test_status_cli_persists(make_steam, capsys, monkeypatch):
    root, games = make_steam()
    import cogitator.gamefinder as gf
    monkeypatch.setattr(gf, "steam_roots", lambda: [root])
    assert cli.main(["status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["game_dir"] == str(games[0])
    assert Settings.load().game_dir == str(games[0])


def test_registry_key_is_logged(tmp_path, monkeypatch, caplog):
    import sys, types
    import cogitator.gamefinder as gf

    hklm, hkcu = tmp_path / "A", tmp_path / "B"
    hklm.mkdir(); hkcu.mkdir()
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE, fake.HKEY_CURRENT_USER = "HKLM", "HKCU"

    class K:
        def __init__(s, v): s.v = v
        def __enter__(s): return s.v
        def __exit__(s, *a): pass

    def open_key(hive, sub):
        if hive == "HKCU":
            raise OSError  # simulate missing key
        return K(str(hklm))

    fake.OpenKey = open_key
    fake.QueryValueEx = lambda k, name: (k, 1)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    with caplog.at_level("INFO", logger="cogitator.gamefinder"):
        found = gf.registry_steam_paths()
    assert found == [("HKLM\\SOFTWARE\\WOW6432Node\\Valve\\Steam\\InstallPath", hklm)]
    text = caplog.text
    assert "Steam path from registry key HKLM\\SOFTWARE\\WOW6432Node\\Valve\\Steam\\InstallPath" in text
    assert "not present: HKCU\\Software\\Valve\\Steam\\SteamPath" in text


def test_settings_roundtrip_windows_path():
    s = Settings(game_dir=r"C:\Games\Warhammer 40,000 DARKTIDE", check_updates_on_start=False)
    s.save()
    assert Settings.load() == s


def test_lock(tmp_path):
    a = InstanceLock(tmp_path)
    a.acquire()
    import os
    # same pid is treated as ours; simulate other live pid by parent pid
    a.path.write_text(str(os.getppid()))
    with pytest.raises(LockError):
        InstanceLock(tmp_path).acquire()
    a.path.write_text("999999999")  # dead pid -> stale, reclaimed
    b = InstanceLock(tmp_path).acquire()
    b.release()


def test_key_redaction():
    set_api_key("SECRETKEY12345678")
    assert get_api_key() == "SECRETKEY12345678"
    rec = logging.LogRecord("x", logging.INFO, "", 0, "hdr apikey: SECRETKEY12345678 and SECRETKEY12345678", None, None)
    RedactFilter().filter(rec)
    assert "SECRET" not in rec.getMessage()
