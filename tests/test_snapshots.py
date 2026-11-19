import io
import json
import zipfile

import pytest

from cogitator import cli, config, snapshots
from cogitator.modinstall import ModImportError, import_archive, rollback_mod
from cogitator.registry import Registry
from make_demo_game import build


@pytest.fixture
def game(tmp_path, monkeypatch):
    monkeypatch.setattr("cogitator.procs.darktide_running", lambda *a, **k: False)
    return build(tmp_path)


def run(game, *argv):
    return cli.main(["--game-dir", str(game), "--no-update-check", *argv])


def lo(game):
    return (game / "mods" / "mod_load_order.txt").read_text()


def make_zip(path, folder="demo_hud", body="v2"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{folder}/{folder}.mod", "return {}\n")
        z.writestr(f"{folder}/scripts/x.lua", body)
    return path


def test_create_list_restore_load_order(game, capsys):
    before = lo(game)
    assert run(game, "snapshot", "create", "t1") == 0
    run(game, "mod", "down", "demo_hud")
    assert lo(game) != before
    snap = snapshots.list_snapshots()
    name = next(s["name"] for s in snap if s["reason"] == "t1")
    assert run(game, "snapshot", "restore", name, "--yes") == 0
    assert lo(game) == before
    assert any(s["reason"] == "pre-restore" for s in snapshots.list_snapshots())


def test_restore_is_refused_without_confirmation(game):
    run(game, "snapshot", "create", "t1")
    name = snapshots.list_snapshots()[0]["name"]
    run(game, "mod", "disable", "demo_hud")
    assert run(game, "snapshot", "restore", name) == 2
    assert "--demo_hud" in lo(game)  # still disabled: nothing was restored


def test_bad_snapshot_name_rejected(game):
    assert run(game, "snapshot", "restore", "../x", "--yes") == 2
    assert run(game, "snapshot", "diff", "nope") == 2


def test_diff_detects_changes(game, capsys):
    run(game, "snapshot", "create", "base")
    name = snapshots.list_snapshots()[0]["name"]
    capsys.readouterr()
    assert run(game, "snapshot", "diff", name) == 0
    run(game, "mod", "disable", "demo_hud")
    (game / "mods" / "demo_chat" / "extra.lua").write_text("x")
    capsys.readouterr()
    assert run(game, "snapshot", "diff", name, "--json") == 1
    d = json.loads(capsys.readouterr().out)
    assert "demo_hud" in d["disabled"] and "demo_chat" in d["changed"]


def test_prune_keeps_newest(game):
    for i in range(4):
        snapshots.create(game, f"r{i}", keep=100)
    assert run(game, "snapshot", "prune", "--keep", "2") == 0
    assert len(snapshots.list_snapshots()) == 2


def test_remove_snapshots_folder_and_restore_it(game):
    assert run(game, "mod", "remove", "demo_hud", "--yes") == 0
    snap = next(s for s in snapshots.list_snapshots() if s["reason"] == "remove")
    assert snap["copied"] == ["demo_hud"]
    assert run(game, "snapshot", "restore", snap["name"], "--yes") == 0
    assert (game / "mods" / "demo_hud" / "demo_hud.mod").is_file()


def test_reorder_snapshot_is_debounced(game):
    snapshots._auto_done.clear()
    run(game, "mod", "down", "demo_hud")
    run(game, "mod", "down", "demo_hud")
    assert sum(1 for s in snapshots.list_snapshots() if s["reason"] == "reorder") == 1


def test_overwrite_records_previous_and_rolls_back(game, tmp_path):
    reg = Registry.load()
    reg.set_mod("demo_hud", nexus_id=5, installed_version="1.0")
    old = (game / "mods" / "demo_hud" / "demo_hud.mod").read_text()
    zp = make_zip(tmp_path / "demo_hud-5-2-0-1.zip")
    r = import_archive(zp, game, reg, None, mod_id=5, is_running=lambda: False)
    assert r.linked == "demo_hud"
    assert (game / "mods" / "demo_hud" / "scripts" / "x.lua").is_file()
    assert reg.mod("demo_hud")["previous"]["version"] == "1.0"
    assert any(s["reason"] == "install" for s in snapshots.list_snapshots())
    msg = rollback_mod("demo_hud", game, reg, is_running=lambda: False)
    assert "1.0" in msg
    assert not (game / "mods" / "demo_hud" / "scripts").exists()
    assert (game / "mods" / "demo_hud" / "demo_hud.mod").read_text() == old
    assert reg.mod("demo_hud")["previous"] is None
    with pytest.raises(ModImportError):
        rollback_mod("demo_hud", game, reg, is_running=lambda: False)


def test_fresh_install_has_no_previous(game, tmp_path):
    reg = Registry.load()
    zp = make_zip(tmp_path / "brand_new-9-1-0-1.zip", folder="brand_new")
    import_archive(zp, game, reg, None, mod_id=9, is_running=lambda: False)
    assert reg.mod("brand_new")["previous"] is None
    with pytest.raises(ModImportError):
        rollback_mod("brand_new", game, reg, is_running=lambda: False)


def test_rollback_refused_while_game_running(game, tmp_path):
    reg = Registry.load()
    reg.set_mod("demo_hud", nexus_id=5, installed_version="1.0")
    import_archive(make_zip(tmp_path / "demo_hud-5-2-0-1.zip"), game, reg, None, mod_id=5, is_running=lambda: False)
    with pytest.raises(ModImportError, match="running"):
        rollback_mod("demo_hud", game, reg, is_running=lambda: True)


def test_import_dry_run_changes_nothing(game, tmp_path, capsys):
    zp = make_zip(tmp_path / "demo_hud-5-2-0-1.zip")
    assert run(game, "import", str(zp), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "OVERWRITE" in out and "Dry run" in out
    assert not (game / "mods" / "demo_hud" / "scripts").exists()


def test_snapshot_failure_never_blocks(game, monkeypatch):
    monkeypatch.setattr(snapshots, "create", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert snapshots.auto(game, "x") is None


def test_snapshot_keep_setting_default():
    assert config.Settings().snapshot_keep == 20


def _oldest_with_folder(game):
    """A snapshot (with a copied mod folder) that is renamed to be older than anything else, plus 19 newer ones (keep=20)."""
    p = snapshots.create(game, "old", folders=["demo_hud"])
    old = p.with_name("20000101-000000-old")
    p.rename(old)
    for i in range(19):
        snapshots.create(game, f"n{i}")
    assert len(snapshots.list_snapshots()) == 20
    assert snapshots.list_snapshots()[-1]["name"] == old.name
    return old.name


def test_restore_of_the_oldest_snapshot_survives_pruning(game):
    name = _oldest_with_folder(game)
    (game / "mods" / "demo_hud" / "scripts").mkdir(parents=True, exist_ok=True)
    (game / "mods" / "demo_hud" / "marker.txt").write_text("live")
    msgs = snapshots.restore(game, name)
    assert "mod folder restored: demo_hud" in msgs
    assert (snapshots.snapshots_dir() / name / "manifest.json").is_file()  # the restored snapshot was not pruned away
    assert not (game / "mods" / "demo_hud" / "marker.txt").exists()
    assert (game / "mods" / "demo_hud").is_dir()


def test_failed_restore_copy_leaves_the_live_folder_untouched(game, monkeypatch):
    p = snapshots.create(game, "t", folders=["demo_hud"])
    (game / "mods" / "demo_hud" / "marker.txt").write_text("live")
    real = snapshots.shutil.copytree

    def boom(src, dst, *a, **k):
        if "pre-restore" not in str(src) and str(src).startswith(str(p)):
            raise OSError("disk full")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(snapshots.shutil, "copytree", boom)
    with pytest.raises(OSError):
        snapshots.restore(game, p.name)
    assert (game / "mods" / "demo_hud" / "marker.txt").read_text() == "live"
    assert not [x for x in (game / "mods").iterdir() if "restore" in x.name]  # no temp leftovers


def test_prune_rejects_keep_below_one(game):
    assert run(game, "snapshot", "prune", "--keep", "0") == 2
    assert run(game, "snapshot", "prune", "--keep", "-3") == 2


def test_game_dir_override_is_saved_only_when_nothing_is_saved(game, tmp_path):
    assert run(game, "status") == 0
    assert config.Settings.load().game_dir == str(game)  # first-time flag is remembered
    other = build(tmp_path / "other")
    assert run(other, "status") == 0
    assert config.Settings.load().game_dir == str(game)  # a one-off flag never overwrites


def test_check_updates_takes_the_instance_lock_because_it_saves_the_registry():
    assert "check-updates" in cli._MUTATING
