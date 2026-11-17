import json

import pytest

from cogitator import cli, config
from cogitator.registry import Registry
from make_demo_game import build


@pytest.fixture
def game(tmp_path, monkeypatch):
    monkeypatch.setattr("cogitator.procs.darktide_running", lambda *a, **k: False)
    return build(tmp_path)


def run(game, *argv):
    return cli.main(["--game-dir", str(game), "--no-update-check", "mod", *argv])


def test_list_json(game, capsys):
    assert run(game, "list", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["folder"] for r in rows} >= {"demo_hud", "demo_chat", "demo_tweaks"}


def test_up_down_changes_order(game, capsys):
    run(game, "list", "--json")
    first = json.loads(capsys.readouterr().out)[0]["folder"]
    assert run(game, "down", first) == 0
    capsys.readouterr()
    run(game, "list", "--json")
    assert json.loads(capsys.readouterr().out)[0]["folder"] != first


def test_remove_needs_confirmation_noninteractive(game):
    assert run(game, "remove", "demo_hud") == 2
    assert (game / "mods" / "demo_hud").is_dir()


def test_remove_dry_run_changes_nothing(game, capsys):
    assert run(game, "remove", "demo_hud", "--dry-run") == 0
    assert "demo_hud.mod" in capsys.readouterr().out
    assert (game / "mods" / "demo_hud").is_dir()


def test_remove_moves_to_trash_and_forgets_link(game):
    Registry.load().set_mod("demo_hud", nexus_id=1)
    assert run(game, "remove", "demo_hud", "--yes") == 0
    assert not (game / "mods" / "demo_hud").exists()
    assert any((config.cache_dir() / "removed").glob("demo_hud-*"))
    assert Registry.load().mod("demo_hud") is None
    assert "demo_hud" not in (game / "mods" / "mod_load_order.txt").read_text()


def test_remove_refused_while_game_running(game, monkeypatch):
    monkeypatch.setattr("cogitator.procs.darktide_running", lambda *a, **k: True)
    assert run(game, "remove", "demo_hud", "--yes") == 2
    assert (game / "mods" / "demo_hud").is_dir()


def test_remove_internal_folder_refused(game):
    assert run(game, "remove", "base", "--yes") == 2
