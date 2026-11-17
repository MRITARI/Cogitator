import base64
import json
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cogitator import cli, diag, doctor, integrity, modchecks
from cogitator.modfile import analyze_mod, strip_lua_comments
from cogitator.modinstall import import_archive
from cogitator.registry import Registry
from make_demo_game import build


@pytest.fixture
def game(tmp_path, monkeypatch):
    monkeypatch.setattr("cogitator.procs.darktide_running", lambda *a, **k: False)
    return build(tmp_path)


def run(game, *argv):
    return cli.main(["--game-dir", str(game), "--no-update-check", *argv])


def put(game, folder, lua="", extra=None):
    d = game / "mods" / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{folder}.mod").write_text(f'return {{ run = function() new_mod("{folder}", {{}}) end }}\n{lua}')
    for name, body in (extra or {}).items():
        (d / name).write_bytes(body if isinstance(body, bytes) else body.encode())


def by_id(checks):
    return {c.id: c for c in checks}


def set_order(game, text):
    (game / "mods" / "mod_load_order.txt").write_text(text)


# ---------------------------------------------------------------- static analysis

def test_comments_are_not_scanned():
    src = '-- mod:hook_origin("A", "b")\n--[[ get_mod("x") ]]\nlocal y = 1'
    assert "hook_origin" not in strip_lua_comments(src) and "get_mod" not in strip_lua_comments(src)


def test_analyze_finds_names_hooks_and_risks(tmp_path):
    put(tmp_path, "m", lua='local o = get_mod("other")\nmod:hook_origin("UIManager", "open", function() end)\nos.execute("x")\n'
                           'mod_data = { require = {"dep_a", "dep_b"} }',
        extra={"helper.dll": b"MZ"})
    i = analyze_mod(tmp_path / "mods" / "m")
    assert i.hook_origins == {"UIManager:open"}
    assert "os.execute" in i.risky and i.executables == ["helper.dll"]


# ---------------------------------------------------------------- doctor checks

def test_hook_origin_collision(game):
    put(game, "p", lua='mod:hook_origin("UIManager", "open", f)')
    put(game, "q", lua='mod:hook_origin("UIManager", "open", g)')
    set_order(game, "p\nq\n")
    c = by_id(modchecks.run(game))["mod_hooks"]
    assert c.level == "warn" and "p and q" in c.detail
    set_order(game, "p\n--q\n")
    assert by_id(modchecks.run(game))["mod_hooks"].level == "ok"


def test_plain_hooks_are_not_conflicts(game):
    put(game, "p", lua='mod:hook("UIManager", "open", f)')
    put(game, "q", lua='mod:hook("UIManager", "open", g)')
    set_order(game, "p\nq\n")
    assert by_id(modchecks.run(game))["mod_hooks"].level == "ok"


def test_risky_content_flagged(game):
    put(game, "evil", extra={"x.exe": b"MZ"})
    put(game, "shell", lua='io.popen("dir")')
    c = by_id(modchecks.run(game))["mod_risky"]
    assert c.level == "warn" and "x.exe" in c.detail and "io.popen" in c.detail


def test_doctor_runs_mod_checks(game):
    ids = {c.id for c in doctor.run_doctor(str(game), offline=True)}
    assert {"mod_hooks", "mod_risky", "mod_integrity"} <= ids


# ---------------------------------------------------------------- integrity baselines

def make_zip(path, folder="demo_hud", body="v2"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{folder}/{folder}.mod", "return {}\n")
        z.writestr(f"{folder}/x.lua", body)
    return path


def test_install_baselines_and_edit_is_detected_then_rebaselined(game, tmp_path):
    import_archive(make_zip(tmp_path / "demo_hud-5-1-0-1.zip"), game, Registry.load(), None, mod_id=5, is_running=lambda: False)
    assert integrity.check(game) == {}
    assert by_id(modchecks.run(game))["mod_integrity"].level == "ok"
    (game / "mods" / "demo_hud" / "x.lua").write_text("tampered")
    assert integrity.check(game) == {"demo_hud": "modified"}
    assert by_id(modchecks.run(game))["mod_integrity"].level == "warn"
    assert run(game, "mod", "rebaseline", "demo_hud") == 0
    assert integrity.check(game) == {}


def test_rebaseline_all_and_remove_forgets(game):
    assert run(game, "mod", "rebaseline", "--all") == 0
    assert set(integrity.load()) >= {"demo_hud", "demo_chat", "demo_tweaks"}
    assert run(game, "mod", "remove", "demo_hud", "--yes") == 0
    assert "demo_hud" not in integrity.load()
    assert run(game, "mod", "rebaseline") == 2


def test_stale_build_warns_after_game_patch(game):
    reg = Registry.load()
    reg.data["patch"] = {"build_key": "old"}
    integrity.record(game, ["demo_hud"], reg)
    assert modchecks.run(game, current_build="old") and "mod_build" not in by_id(modchecks.run(game, current_build="old"))
    assert by_id(modchecks.run(game, current_build="new"))["mod_build"].level == "warn"


# ---------------------------------------------------------------- history and diagnostics

def test_doctor_history_is_recorded_and_listed(game, capsys):
    run(game, "doctor", "--offline")
    capsys.readouterr()
    assert run(game, "doctor", "--history") == 0
    assert "exit" in capsys.readouterr().out
    assert len(doctor.read_history()) == 1


def test_history_is_capped():
    for _ in range(doctor.HISTORY_KEEP + 120):
        doctor.record_history([])
    assert len(doctor.history_path().read_text().splitlines()) <= doctor.HISTORY_KEEP + 100


def test_diag_bundle_has_no_secrets_or_home_paths(game, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "SECRET-KEY-VALUE-123456")
    home = str(tmp_path)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: type(tmp_path)(home)))
    (game / "mods" / "demo_hud" / "demo_hud.mod").write_text("PRIVATE MOD SOURCE")
    from cogitator import config

    config.log_path().parent.mkdir(parents=True, exist_ok=True)
    config.log_path().write_text(f"line apikey=SECRET-KEY-VALUE-123456 at {home}\\x\n")
    assert run(game, "diag") == 0
    out = capsys.readouterr().out
    zp = next(config.cache_dir().glob("cogitator-diag-*.zip"))
    assert str(zp) in out
    with zipfile.ZipFile(zp) as z:
        blob = "\n".join(z.read(n).decode() for n in z.namelist())
        assert {"versions.txt", "doctor.json", "log_tail.txt", "load_order.txt", "mods.txt"} <= set(z.namelist())
    assert "SECRET-KEY-VALUE" not in blob and "PRIVATE MOD SOURCE" not in blob
    assert home not in blob
    assert "demo_hud" in blob  # names only


def test_scrub_replaces_user_name(monkeypatch):
    monkeypatch.setenv("USERNAME", "alice_smith")
    assert "alice_smith" not in diag.scrub(r"C:\Users\alice_smith\file")
