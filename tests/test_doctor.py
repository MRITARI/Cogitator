import json
import os
import time
from pathlib import Path

import httpx
import pytest
import respx

from cogitator import cli, doctor
from cogitator.config import Settings
from cogitator.doctor import FAIL, OK, WARN, exit_code, format_report, run_doctor, scan_console_log
from cogitator.frameworks import DML_REQUIRED, DMF_REQUIRED
from cogitator.patcher import patch
from cogitator.registry import Registry

from test_patcher import ORIG, FakeTool

B = "https://api.nexusmods.com/v1"
SECRET = "doctor-secret-key-123456"


@pytest.fixture
def steam(tmp_path):
    s = tmp_path / "Steam"
    s.mkdir()
    return s


@pytest.fixture
def game(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "bundle" / "bundle_database.data").write_bytes(ORIG)
    for rel in DML_REQUIRED + DMF_REQUIRED + ("tools/dtkit-patch.exe",):
        p = g / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    for m in ("alpha", "beta"):
        (g / "mods" / m).mkdir()
        (g / "mods" / m / f"{m}.mod").write_text("return {}")
    (g / "mods" / "mod_load_order.txt").write_bytes(b"-- header\r\nalpha\r\n--beta\r\n")
    return g


def doctor_run(game, steam, *, patched=True, offline=True, **kw):
    reg = Registry.load()
    reg.set_framework("dml", version="26.06.24", source_kind="local", source="x")
    reg.set_framework("dmf", version="26.10.22", source_kind="local", source="x")
    reg.save()
    if patched:
        patch(game, reg, runner=FakeTool(), is_running=lambda: False)
    kw.setdefault("log_dirs", [])
    return run_doctor(str(game), offline=offline, key=kw.pop("key", None), registry=Registry.load(),
                      steam_roots_fn=lambda: [steam], running=kw.pop("running", lambda: False), **kw)


def by_id(checks):
    return {c.id: c for c in checks}


def test_healthy_install_exits_zero(game, steam):
    checks = doctor_run(game, steam)
    assert exit_code(checks) == 0, format_report(checks)
    c = by_id(checks)
    assert c["patch_state"].level == OK and "ON" in c["patch_state"].detail
    assert "agree" in c["patch_consistency"].detail and "not older" in c["bak"].detail
    assert c["registry"].level == OK and c["running"].level == OK and "skipped" in c["nexus"].detail


def test_every_requested_check_is_present(game, steam):
    ids = {c.id for c in doctor_run(game, steam)}
    assert {"steam", "game", "fw_dml", "fw_dmf", "patch_state", "patch_consistency", "bak", "load_order", "registry",
            "nexus", "github", "running", "console_log"} <= ids


def test_nothing_found(tmp_path, monkeypatch):
    checks = run_doctor(None, offline=True, key=None, steam_roots_fn=lambda: [], running=lambda: False, log_dirs=[])
    c = by_id(checks)
    assert c["steam"].level == WARN and c["game"].level == FAIL and exit_code(checks) == 2
    assert "--game-dir" in c["game"].fix and "fw_dml" not in c


def test_missing_and_incomplete_frameworks(game, steam):
    (game / "mods" / "dmf" / "dmf.mod").unlink()
    (game / "binaries" / "mod_loader").unlink()
    c = by_id(doctor_run(game, steam, patched=False))
    assert c["fw_dmf"].level == FAIL and "incomplete" in c["fw_dmf"].detail and "dmf.mod" in c["fw_dmf"].detail
    assert c["fw_dml"].level == FAIL and "install-framework" in c["fw_dml"].fix


# ---- patch state, patch_999 consistency, .bak freshness

def test_patch_off_is_a_warning_with_the_remedy(game, steam):
    c = by_id(doctor_run(game, steam, patched=False))
    assert c["patch_state"].level == WARN and "Press T" in c["patch_state"].fix
    assert c["bak"].level == OK


def test_patched_db_with_missing_patch_999_bundle_is_a_failure(game, steam):
    checks = doctor_run(game, steam)
    (game / "bundle" / "9ba626afa44a3aa3.patch_999").unlink()
    again = by_id(run_doctor(str(game), offline=True, key=None, registry=Registry.load(), steam_roots_fn=lambda: [steam],
                             running=lambda: False, log_dirs=[]))
    assert again["patch_consistency"].level == FAIL and "may fail to start" in again["patch_consistency"].detail
    assert again["fw_dml"].level == FAIL and "incomplete" in again["fw_dml"].detail  # reported both ways
    assert again["patch_state"].level == WARN and "not (fully) installed" in again["patch_state"].detail
    assert exit_code(list(again.values())) == 2


def test_game_updated_since_last_patch(game, steam):
    doctor_run(game, steam)
    (game / "bundle" / "bundle_database.data").write_bytes(ORIG)  # a game update restores an unpatched database
    c = by_id(run_doctor(str(game), offline=True, key=None, registry=Registry.load(), steam_roots_fn=lambda: [steam],
                         running=lambda: False, log_dirs=[]))
    assert c["patch_state"].level == WARN
    assert c["patch_consistency"].level == WARN and "game was updated" in c["patch_consistency"].detail


def test_bak_missing_patched_and_stale(game, steam):
    doctor_run(game, steam)
    bak = game / "bundle" / "bundle_database.data.bak"

    def run():
        return by_id(run_doctor(str(game), offline=True, key=None, registry=Registry.load(), steam_roots_fn=lambda: [steam],
                                running=lambda: False, log_dirs=[]))["bak"]

    assert run().level == OK
    last = Registry.load().data["patch"]["last_patch_at"]
    os.utime(bak, (last - 3600, last - 3600))
    r = run()
    assert r.level == WARN and "older than Cogitator's last patch" in r.detail and "refused" in r.detail
    os.utime(bak, None)
    bak.write_bytes(ORIG + b"patch_999")
    assert run().level == FAIL and "itself patched" in run().detail
    bak.unlink()
    r = run()
    assert r.level == WARN and "missing" in r.detail and "Verify game files" in r.fix


def test_leftover_stale_bak_while_off_warns_about_blind_unpatch(game, steam):
    bak = game / "bundle" / "bundle_database.data.bak"
    bak.write_bytes(b"old build")
    os.utime(bak, (time.time() - 86400, time.time() - 86400))
    c = by_id(doctor_run(game, steam, patched=False))
    assert c["bak"].level == WARN and "0.1.7" in c["bak"].fix


# ---- load order and registry consistency

def test_load_order_problems(game, steam):
    (game / "mods" / "mod_load_order.txt").write_bytes(b"dmf\r\nalpha\r\nghost\r\nalpha\r\n")
    c = by_id(doctor_run(game, steam))
    assert c["load_order_internal"].level == WARN and "dmf" in c["load_order_internal"].detail
    assert c["load_order_missing"].level == WARN and "ghost" in c["load_order_missing"].detail
    assert c["load_order_dups"].level == WARN and "alpha" in c["load_order_dups"].detail
    (game / "mods" / "mod_load_order.txt").unlink()
    assert by_id(doctor_run(game, steam))["load_order"].level == WARN


def test_registry_vs_mods_consistency(game, steam):
    reg = Registry.load()
    reg.set_mod("alpha", nexus_id=19, installed_file_id=None)
    reg.set_mod("removed_mod", nexus_id=77)
    reg.set_mod("weird", nexus_id="not-a-number")
    reg.save()
    c = by_id(doctor_run(game, steam))["registry"]
    assert c.level == WARN
    assert "removed_mod" in c.detail and "weird" in c.detail and "cogitator unlink" in c.fix


def test_registry_consistent_notes_unknown_baseline(game, steam):
    reg = Registry.load()
    reg.set_mod("alpha", nexus_id=19, installed_file_id=None)
    reg.save()
    c = by_id(doctor_run(game, steam))["registry"]
    assert c.level == OK and "1 linked mod(s) match" in c.detail and "baseline unknown for alpha" in c.detail


def test_registry_framework_record_without_files_and_corrupt_sidecar(game, steam):
    doctor_run(game, steam)
    (game / "mods" / "dmf" / "dmf.mod").unlink()
    Registry.load().path.with_name("registry.json.corrupt").write_text("{")
    c = by_id(run_doctor(str(game), offline=True, key=None, registry=Registry.load(), steam_roots_fn=lambda: [steam],
                         running=lambda: False, log_dirs=[]))["registry"]
    assert c.level == WARN and "unreadable registry" in c.detail and "DMF" in c.detail and "files are missing" in c.detail


def test_framework_installed_outside_cogitator_is_noted_not_guessed(game, steam):
    c = by_id(run_doctor(str(game), offline=True, key=None, registry=Registry.load(), steam_roots_fn=lambda: [steam],
                         running=lambda: False, log_dirs=[]))
    assert "installed outside Cogitator" in c["registry_dml"].detail and c["registry_dml"].level == OK


# ---- network checks (respx only)

@respx.mock
def test_nexus_key_valid_invalid_missing_and_limited(game, steam):
    respx.get(f"{B}/users/validate.json").respond(200, json={"is_premium": False},
                                                  headers={"x-rl-daily-remaining": "19990", "x-rl-hourly-remaining": "1990"})
    respx.get("https://api.github.com/rate_limit").respond(200, json={"resources": {"core": {"remaining": 55}}})
    c = by_id(doctor_run(game, steam, offline=False, key=SECRET))
    assert c["nexus"].level == OK and "free" in c["nexus"].detail and "19990 daily" in c["nexus"].detail
    assert c["github"].level == OK and "55" in c["github"].detail
    assert by_id(doctor_run(game, steam, offline=False, key=None))["nexus"].level == WARN
    respx.get(f"{B}/users/validate.json").respond(401)
    assert by_id(doctor_run(game, steam, offline=False, key=SECRET))["nexus"].level == FAIL
    respx.get(f"{B}/users/validate.json").respond(429)
    assert by_id(doctor_run(game, steam, offline=False, key=SECRET))["nexus"].level == WARN


@respx.mock
def test_github_unreachable_is_a_warning_not_a_failure(game, steam):
    respx.get("https://api.github.com/rate_limit").mock(side_effect=httpx.ConnectError("no route"))
    respx.get(f"{B}/users/validate.json").respond(200, json={})
    c = by_id(doctor_run(game, steam, offline=False, key=SECRET))
    assert c["github"].level == WARN and "ConnectError" in c["github"].detail


def test_doctor_does_not_write_the_registry(game, steam):
    doctor_run(game, steam)
    before = Registry.load().path.read_bytes()
    run_doctor(str(game), offline=True, key=None, registry=Registry.load(), steam_roots_fn=lambda: [steam],
               running=lambda: False, log_dirs=[])
    assert Registry.load().path.read_bytes() == before


# ---- running + console log

def test_game_running_warns(game, steam):
    c = by_id(doctor_run(game, steam, running=lambda: True))["running"]
    assert c.level == WARN and "refuses" in c.detail


LOG_OK = """12:06:42.500 [Lua] INFO [Log Internal] Exceptions will be handled as warnings
12:06:48.430 [Lua] [Mod] Loading ./../mods/dmf/dmf.mod
12:06:48.437 [Lua] [Mod] Loading ./../mods/dmf/scripts/mods/dmf/dmf_loader.lua
12:06:59.155 warning: [DummyRegistryAllocator] Registry 'vertex_buffer' has still registered 0 b used
"""
LOG_BAD = LOG_OK + """12:07:01.000 [Lua] ERROR scripts/mods/alpha/alpha.lua:12: attempt to index a nil value (field 'x')
12:07:02.000 [Lua] [MOD][beta][ERROR] failed to load scripts/mods/beta/beta_data.lua
12:07:03.000 [Lua] ERROR unrelated engine error not about mods
"""


def test_scan_console_log(tmp_path):
    ok = tmp_path / "ok.log"
    ok.write_text(LOG_OK)
    assert scan_console_log(ok) == ([], True)
    bad = tmp_path / "bad.log"
    bad.write_text(LOG_BAD)
    errors, dmf = scan_console_log(bad)
    assert dmf and len(errors) == 2 and "alpha.lua:12" in errors[0] and all("unrelated" not in e for e in errors)
    assert scan_console_log(tmp_path / "missing.log") == ([], False)


def test_console_log_check_uses_the_newest_log(game, steam, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    old, new = logs / "console-old.log", logs / "console-new.log"
    old.write_text(LOG_BAD)
    new.write_text(LOG_OK)
    os.utime(old, (time.time() - 5000, time.time() - 5000))
    c = by_id(doctor_run(game, steam, log_dirs=[logs]))["console_log"]
    assert c.level == OK and "console-new.log" in c.detail and "DMF loaded" in c.detail
    new.write_text(LOG_BAD)
    c = by_id(doctor_run(game, steam, log_dirs=[logs]))["console_log"]
    assert c.level == WARN and "2 mod error line(s)" in c.detail and "alpha.lua:12" in c.detail and "Space" in c.fix


def test_console_log_warns_when_a_post_patch_session_never_loaded_dmf(game, steam, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    doctor_run(game, steam)  # patches the fake game; records last_patch_at
    log = logs / "console-new.log"
    log.write_text("12:00:00.000 [Application] STARTUP\n12:00:01.000 [Lua] INFO nothing about mods\n")
    later = time.time() + 10
    os.utime(log, (later, later))  # a session that happened after the patch
    run = lambda: by_id(run_doctor(str(game), offline=True, key=None, registry=Registry.load(), steam_roots_fn=lambda: [steam],
                                   running=lambda: False, log_dirs=[logs]))["console_log"]
    c = run()
    assert c.level == WARN and "never loads DMF" in c.detail
    earlier = Registry.load().data["patch"]["last_patch_at"] - 600  # a log that predates the patch proves nothing
    os.utime(log, (earlier, earlier))
    assert run().level == OK
    log.write_text(LOG_OK)
    os.utime(log, (later, later))
    assert run().level == OK and "DMF loaded" in run().detail


def test_no_logs_is_fine_and_proton_dirs_are_searched(steam, tmp_path):
    (steam / "steamapps").mkdir()
    dirs = doctor.console_log_dirs([steam], appdata=str(tmp_path / "AppData"))
    assert dirs[0] == tmp_path / "AppData" / "Fatshark" / "Darktide" / "console_logs"
    assert any("compatdata" in str(d) and "1361210" in str(d) and "steamuser" in str(d) for d in dirs)


# ---- robustness, report, CLI

def test_one_broken_check_does_not_hide_the_others(game, steam, monkeypatch):
    monkeypatch.setattr(doctor, "_load_order", lambda g: (_ for _ in ()).throw(RuntimeError("boom")))
    checks = doctor_run(game, steam)
    c = by_id(checks)
    assert c["internal"].level == FAIL and "boom" in c["internal"].detail
    assert "registry" in c and "console_log" in c and exit_code(checks) == 2


def test_report_shows_fixes_only_for_problems_and_redacts(game, steam):
    checks = doctor_run(game, steam, patched=False)
    checks.append(doctor.Check("x", "leak", WARN, f"token {SECRET} here apikey: {SECRET}", "do a thing"))
    text = format_report(checks)
    assert "[WARN] Mods patch" in text and "fix: Press T" in text and "[OK]" in text
    assert text.count("fix:") == sum(1 for c in checks if c.fix and c.level != OK)
    assert text.rstrip().splitlines()[-1].endswith("failure(s).")


def test_exit_code_ladder():
    mk = lambda *lv: [doctor.Check(str(i), "n", l, "d") for i, l in enumerate(lv)]
    assert exit_code(mk(OK, OK)) == 0 and exit_code(mk(OK, WARN)) == 1 and exit_code(mk(WARN, FAIL, OK)) == 2 and exit_code([]) == 0


def test_cli_doctor_json_and_exit_codes(game, steam, capsys, monkeypatch):
    import cogitator.gamefinder as gf
    monkeypatch.setattr(gf, "steam_roots", lambda: [steam])
    monkeypatch.setattr(doctor, "darktide_running", lambda: False)
    monkeypatch.setattr("cogitator.doctor.console_log_dirs", lambda roots: [])
    reg = Registry.load()
    reg.set_framework("dml", version="26.06.24", source_kind="local", source="x")
    reg.set_framework("dmf", version="26.10.22", source_kind="local", source="x")
    patch(game, reg, runner=FakeTool(), is_running=lambda: False)
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", SECRET)
    rc = cli.main(["--game-dir", str(game), "doctor", "--offline", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert rc == data["exit_code"] == 0 and {c["id"] for c in data["checks"]} >= {"patch_consistency", "bak", "registry"}
    assert SECRET not in out
    (game / "bundle" / "9ba626afa44a3aa3.patch_999").unlink()
    assert cli.main(["--game-dir", str(game), "doctor", "--offline"]) == 2
    text = capsys.readouterr().out
    assert "[FAIL]" in text and "fix:" in text


def test_cli_unlink(game, capsys):
    reg = Registry.load()
    reg.set_mod("alpha", nexus_id=19)
    assert cli.main(["unlink", "alpha"]) == 0 and Registry.load().mod("alpha") is None
    assert cli.main(["unlink", "alpha"]) == 2
    assert (game / "mods" / "alpha" / "alpha.mod").is_file()  # the mod itself is untouched


def test_paths_check_flags_microsoft_store_python_redirection(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(doctor.sys, "_base_executable", r"C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12_x64\python.exe", raising=False)
    c = doctor._paths()
    assert c.level == OK and "Microsoft Store build" in c.detail and "LocalCache" in c.detail
    monkeypatch.setattr(doctor.sys, "_base_executable", r"C:\Python312\python.exe", raising=False)
    monkeypatch.setattr(doctor.sys, "base_prefix", r"C:\Python312")
    assert "Microsoft Store" not in doctor._paths().detail
