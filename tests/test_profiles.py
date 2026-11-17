import base64
import json
import zlib

import pytest

from cogitator import cli, profiles, snapshots
from cogitator.loadorder import LoadOrder
from cogitator.mods import ModError
from cogitator.registry import Registry
from make_demo_game import build


@pytest.fixture
def game(tmp_path, monkeypatch):
    monkeypatch.setattr("cogitator.procs.darktide_running", lambda *a, **k: False)
    return build(tmp_path)


def run(game, *argv):
    return cli.main(["--game-dir", str(game), "--no-update-check", *argv])


def lo_bytes(game):
    return (game / "mods" / "mod_load_order.txt").read_bytes()


def entries(game):
    return LoadOrder.load(game / "mods" / "mod_load_order.txt").entries()


def test_save_then_switch_restores_state(game):
    assert run(game, "profile", "save", "main") == 0
    original = lo_bytes(game)
    run(game, "mod", "disable", "demo_hud")
    run(game, "mod", "enable", "demo_tweaks")
    run(game, "mod", "down", "demo_hud")
    assert lo_bytes(game) != original
    assert run(game, "profile", "switch", "main", "--yes") == 0
    assert lo_bytes(game) == original  # comment, CRLF endings and order all back


def test_switch_disables_mods_not_in_profile_and_reports_missing(game, capsys):
    prof = profiles.capture(game, Registry.load(), "small")
    prof["entries"] = [["demo_chat", True], ["ghost_mod", True]]
    prof["mods"] = {"demo_chat": {}, "ghost_mod": {"nexus_id": 77, "version": "1.2"}}
    profiles.save(profiles.validate(prof))
    assert run(game, "profile", "switch", "small", "--yes") == 0
    out = capsys.readouterr().out
    assert "ghost_mod" in out and "nexusmods.com" in out and "demo_hud" in out
    state = dict(entries(game))
    assert state["demo_chat"] is True and state["demo_hud"] is False and "ghost_mod" not in state
    assert entries(game)[0][0] == "demo_chat"
    assert (game / "mods" / "demo_hud").is_dir()  # disabled, never deleted
    assert any(s["reason"] == "profile-switch" for s in snapshots.list_snapshots())


def test_switch_dry_run_changes_nothing(game):
    run(game, "profile", "save", "a")
    run(game, "mod", "disable", "demo_hud")
    before = lo_bytes(game)
    assert run(game, "profile", "switch", "a", "--dry-run") == 0
    assert lo_bytes(game) == before


def test_switch_needs_confirmation(game):
    run(game, "profile", "save", "a")
    run(game, "mod", "disable", "demo_hud")
    before = lo_bytes(game)
    assert run(game, "profile", "switch", "a") == 2
    assert lo_bytes(game) == before


def test_code_roundtrip_and_import(game, capsys):
    run(game, "profile", "save", "share me")
    capsys.readouterr()
    run(game, "profile", "export", "share me")
    code = capsys.readouterr().out.strip()
    assert code.startswith("cog1:")
    assert run(game, "profile", "import", code, "--as", "copy") == 0
    assert profiles.load("copy")["entries"] == profiles.load("share me")["entries"]


def test_file_export_import(game, tmp_path):
    run(game, "profile", "save", "a")
    f = tmp_path / "a.json"
    assert run(game, "profile", "export", "a", "--file", str(f)) == 0
    profiles.delete("a")
    assert run(game, "profile", "import", str(f)) == 0
    assert profiles.load("a")["entries"]


def evil(entries, **extra):
    return {"schema": 1, "name": "x", "entries": entries, "mods": {}, **extra}


@pytest.mark.parametrize("folder", ["../escape", "a/b", "base", "dmf", "--hidden", "x -- y", " pad", ""])
def test_validate_rejects_hostile_folder_names(folder):
    with pytest.raises(ModError):
        profiles.validate(evil([[folder, True]]))


@pytest.mark.parametrize("bad", ["x", {"schema": 2, "name": "x", "entries": []}, evil("nope"), evil([["a"]]),
                                 evil([["a", "yes"]]), {"schema": 1, "name": "../x", "entries": []}])
def test_validate_rejects_malformed(bad):
    with pytest.raises(ModError):
        profiles.validate(bad)


def test_decode_rejects_junk_and_bombs():
    for code in ("", "cog1:", "cog1:!!!notbase64", "nope:abc", "cog1:" + "A" * 10):
        with pytest.raises(ModError):
            profiles.decode(code)
    bomb = base64.urlsafe_b64encode(zlib.compress(b"0" * (10 * 1024 * 1024))).decode().rstrip("=")
    with pytest.raises(ModError):
        profiles.decode("cog1:" + bomb)
    with pytest.raises(ModError):
        profiles.decode("cog1:" + "A" * (70 * 1024))


def test_validate_cleans_untrusted_fields():
    d = profiles.validate(evil([["a", True], ["A", False]], mods={"a": {"nexus_id": "7", "version": {"x": 1}, "name": "n" * 500}}))
    assert d["entries"] == [["a", True]]  # duplicate by case dropped
    assert d["mods"]["a"]["nexus_id"] is None and d["mods"]["a"]["version"] is None and len(d["mods"]["a"]["name"]) == 100


def test_diff_between_profile_and_current(game, capsys):
    run(game, "profile", "save", "a")
    capsys.readouterr()
    assert run(game, "profile", "diff", "a") == 0
    run(game, "mod", "disable", "demo_hud")
    capsys.readouterr()
    assert run(game, "profile", "diff", "a", "--json") == 1
    assert "demo_hud" in json.loads(capsys.readouterr().out)["disabled"]  # profile -> now


def test_delete_and_list(game, capsys):
    run(game, "profile", "save", "a")
    run(game, "profile", "save", "b")
    capsys.readouterr()
    run(game, "profile", "list", "--json")
    assert {p["name"] for p in json.loads(capsys.readouterr().out)} == {"a", "b"}
    assert run(game, "profile", "delete", "a", "--yes") == 0
    assert [p["name"] for p in profiles.list_profiles()] == ["b"]
    assert run(game, "profile", "show", "a") == 2


def test_bad_profile_names(game):
    for n in ("../x", "", ".hidden", "a" * 41):
        assert run(game, "profile", "save", n) == 2


def test_reorder_keeps_comments_and_internal_entries():
    lo = LoadOrder.parse("-- header\r\nbase\r\nb\r\n--a\r\n\r\n# note\r\nc\r\ndmf\r\n")
    lo.reorder(["c", "a"], skip=("base", "dmf"))
    assert lo.render().decode() == "-- header\r\nbase\r\nc\r\n--a\r\n\r\n# note\r\nb\r\ndmf\r\n"


def test_reorder_drops_duplicate_lines():
    lo = LoadOrder.parse("a\nb\na\n")
    lo.reorder(["b"])
    assert [n for n, _ in lo.entries()] == ["b", "a"]
