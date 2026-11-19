import json
import math
import os
import sys

import httpx
import pytest
import respx

from cogitator import bisect, cli, config, crashlog, launch, nexus, repair
from cogitator.loadorder import LoadOrder
from cogitator.mods import ModError
from cogitator.registry import Registry
from cogitator.updates import PINNED, UPDATE, decide_mod
from make_demo_game import build

B, G = nexus.BASE, nexus.GAME


@pytest.fixture
def game(tmp_path, monkeypatch):
    monkeypatch.setattr("cogitator.procs.darktide_running", lambda *a, **k: False)
    return build(tmp_path)


def run(game, *argv):
    return cli.main(["--game-dir", str(game), "--no-update-check", *argv])


def put(game, folder, lua=""):
    d = game / "mods" / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{folder}.mod").write_text(f"return {{}}\n{lua}")


def order(game):
    return LoadOrder.load(game / "mods" / "mod_load_order.txt").entries()


# ---------------------------------------------------------------- pinning / notes / tags

REC = {"nexus_id": 1, "installed_file_id": 1, "installed_version": "1.0", "baseline": "import",
       "latest": {"file_id": 2, "version": "2.0"}}


def test_pinned_mod_shows_pinned_not_update():
    assert decide_mod(REC)[0] == UPDATE
    assert decide_mod({**REC, "pinned_version": "1.0"})[0] == PINNED
    up_to_date = {**REC, "installed_file_id": 2, "pinned_version": "1.0"}
    assert decide_mod(up_to_date)[0] == "up to date"  # nothing to skip: the pin does not change the status


def test_pin_unpin_cli(game, capsys):
    Registry.load().set_mod("demo_hud", nexus_id=1, installed_version="1.0")
    assert run(game, "mod", "pin", "demo_hud") == 0
    assert Registry.load().mod("demo_hud")["pinned_version"] == "1.0"
    assert run(game, "mod", "pin", "demo_hud", "0.9") == 0
    assert Registry.load().mod("demo_hud")["pinned_version"] == "0.9"
    assert run(game, "mod", "unpin", "demo_hud") == 0
    assert Registry.load().mod("demo_hud")["pinned_version"] is None
    assert run(game, "mod", "pin", "no_such_mod") == 2


def test_notes_tags_fav_cli(game, capsys):
    assert run(game, "mod", "note", "demo_hud", "needs", "a", "restart") == 0
    assert run(game, "mod", "tag", "demo_hud", "UI", "hud", "ui") == 0
    assert run(game, "mod", "fav", "demo_hud") == 0
    capsys.readouterr()
    run(game, "mod", "list", "--json")
    row = next(r for r in json.loads(capsys.readouterr().out) if r["folder"] == "demo_hud")
    assert row["notes"] == "needs a restart" and row["tags"] == ["ui", "hud"] and row["fav"] is True
    run(game, "mod", "untag", "demo_hud", "ui")
    run(game, "mod", "unfav", "demo_hud")
    run(game, "mod", "note", "demo_hud")
    rec = Registry.load().mod("demo_hud")
    assert rec["tags"] == ["hud"] and rec["fav"] is False and rec["notes"] == ""


# ---------------------------------------------------------------- changelogs

@respx.mock
def test_changelogs_parse_defensively():
    respx.get(f"{B}/games/{G}/mods/19/changelogs.json").respond(200, json={"1.0": ["a"], "2.0": ["b", "c", {"x": 1}], "3.0": "no", 4: ["x"]})
    with nexus.NexusClient("k") as nx:
        assert nx.changelogs(19) == {"1.0": ["a"], "2.0": ["b", "c"], "4": ["x"]}  # JSON keys are text; bad shapes dropped


@respx.mock
def test_changelogs_empty_reply():
    respx.get(f"{B}/games/{G}/mods/19/changelogs.json").respond(200, json=[])
    with nexus.NexusClient("k") as nx:
        assert nx.changelogs(19) == {}


def test_newer_versions_helper():
    assert cli._newer(["1.0", "1.10", "1.2", "0.9"], "1.2") == ["1.10"]
    assert cli._newer(["1.0", "2.0"], None) == ["2.0", "1.0"]
    assert cli._newer(["1.0"], "5.0") == ["1.0"]  # nothing newer listed: still show the latest entry


@respx.mock
def test_changelog_command(game, monkeypatch, capsys):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k")
    Registry.load().set_mod("demo_hud", nexus_id=19, installed_version="1.0", name="HUD")
    respx.get(f"{B}/games/{G}/mods/19/changelogs.json").respond(200, json={"1.0": ["old"], "2.0": ["new thing"]})
    assert run(game, "changelog", "demo_hud") == 0
    out = capsys.readouterr().out
    assert "new thing" in out and "old" not in out
    assert run(game, "changelog", "demo_chat") == 2  # not linked


# ---------------------------------------------------------------- dev links

@pytest.mark.skipif(sys.platform != "win32", reason="junction test")
def test_dev_link_enable_and_safe_remove(game, tmp_path, capsys):
    src = tmp_path / "workspace" / "my_dev_mod"
    src.mkdir(parents=True)
    (src / "my_dev_mod.mod").write_text("return {}")
    (src / "keep.lua").write_text("precious")
    assert run(game, "mod", "link-dev", str(src)) == 0
    link = game / "mods" / "my_dev_mod"
    assert link.is_dir() and dict(order(game))["my_dev_mod"] is True
    assert Registry.load().mod("my_dev_mod")["dev"] is True
    assert run(game, "snapshot", "create", "x") == 0  # snapshots never copy through the link
    assert run(game, "mod", "rebaseline", "--all") == 0
    from cogitator import integrity

    assert "my_dev_mod" not in integrity.load()
    capsys.readouterr()
    assert run(game, "mod", "remove", "my_dev_mod", "--yes") == 0
    assert "dev link" in capsys.readouterr().out
    assert not link.exists() and (src / "keep.lua").read_text() == "precious"  # the source is untouched
    assert "my_dev_mod" not in dict(order(game))


@pytest.mark.skipif(sys.platform != "win32", reason="junction test")
def test_dev_link_validation(game, tmp_path):
    src = tmp_path / "nomodfile"
    src.mkdir()
    assert run(game, "mod", "link-dev", str(src)) == 2  # no <name>.mod
    assert run(game, "mod", "link-dev", str(tmp_path / "missing")) == 2
    good = tmp_path / "demo_hud"
    good.mkdir()
    (good / "demo_hud.mod").write_text("x")
    assert run(game, "mod", "link-dev", str(good)) == 2  # name already exists in mods/
    assert (game / "mods" / "demo_hud" / "demo_hud.mod").read_text() != "x"


# ---------------------------------------------------------------- bisect

def make_mods(game, names, requires=None):
    (game / "mods" / "mod_load_order.txt").write_text("".join(f"{n}\n" for n in names))
    for n in names:
        req = (requires or {}).get(n)
        put(game, n, lua=f'mod_data = {{ require = {{"{req}"}} }}' if req else "")


def enabled(game):
    return {n for n, e in order(game) if e}


@pytest.mark.parametrize("culprit", list("abcdefg"))
def test_bisect_finds_each_culprit_in_log_steps(game, culprit):
    names = list("abcdefg")
    make_mods(game, names)
    before = (game / "mods" / "mod_load_order.txt").read_bytes()
    s = bisect.start(game)
    steps = 1
    while not s.get("culprit"):
        assert enabled(game) == set(s["test"])  # exactly the test set is on
        s = bisect.answer(game, still_broken=culprit in enabled(game))
        steps += 1
        assert steps < 20
    assert s["culprit"] == culprit
    assert steps <= math.ceil(math.log2(len(names))) + 2
    bisect.reset(game)
    assert (game / "mods" / "mod_load_order.txt").read_bytes() == before
    assert bisect.load() is None


def test_bisect_rules(game):
    with pytest.raises(ModError):
        bisect.answer(game, True)  # no session
    make_mods(game, ["only"])
    with pytest.raises(ModError):
        bisect.start(game)  # needs two mods
    make_mods(game, ["a", "b"])
    bisect.start(game)
    with pytest.raises(ModError):
        bisect.start(game)  # already running


def test_bisect_cli_flow(game, capsys):
    make_mods(game, list("abcd"))
    assert run(game, "bisect", "start") == 0
    assert run(game, "bisect", "status") == 0
    assert run(game, "bisect", "bad") == 0
    assert run(game, "bisect", "bad") == 0
    assert "Found it" in capsys.readouterr().out
    assert run(game, "bisect", "reset") == 0
    assert run(game, "bisect", "status") == 0 and "No bisect session" in capsys.readouterr().out


# ---------------------------------------------------------------- crash log

LOG = """[Lua] [Mod] Loading ./../mods/dmf/dmf.mod
[Lua] [Mod] Loading ./../mods/alpha/alpha.mod
[Lua] [Mod] Loading ./../mods/beta/beta.mod
Error: scripts/mods/beta/beta.lua:12: attempt to index a nil value
stack: scripts/mods/beta/helper.lua:3
Error: scripts/mods/alpha/alpha.lua:1: attempt to call nil
Error: scripts/mods/beta/beta.lua:40: attempt to call a nil value
Error: scripts/mods/dmf/core.lua:1: something in the framework
Error: scripts/mods/ghost/x.lua:1: attempt to call nil
"""


def test_crashlog_ranks_mods_and_ignores_framework(tmp_path):
    p = tmp_path / "console.log"
    p.write_text(LOG)
    rep = crashlog.analyze(p)
    assert rep.suspects[0] == ("beta", 3) and ("alpha", 1) in rep.suspects
    assert "dmf" not in dict(rep.suspects)
    assert rep.last_loaded == "beta"
    only = crashlog.analyze(p, installed={"alpha", "beta"})  # a name that is not installed is not a suspect
    assert "ghost" not in dict(only.suspects)
    text = "\n".join(crashlog.format_report(rep, [("alpha", True), ("beta", True)]))
    assert "beta: named in 3" in text and "load position 2" in text


def test_crashlog_picks_newest_and_handles_none(tmp_path):
    assert crashlog.newest_log([tmp_path]) is None
    assert "No Darktide console logs" in crashlog.format_report(crashlog.CrashReport())[0]
    old, new = tmp_path / "a.log", tmp_path / "b.log"
    old.write_text("x")
    new.write_text("y")
    os.utime(old, (1, 1))
    assert crashlog.newest_log([tmp_path]) == new


def test_crashlog_cli(game, tmp_path, monkeypatch, capsys):
    d = tmp_path / "logs"
    d.mkdir()
    (d / "c.log").write_text(LOG)
    put(game, "alpha")
    put(game, "beta")  # only installed mods can be suspects
    monkeypatch.setattr("cogitator.doctor.console_log_dirs", lambda roots: [d])
    assert run(game, "crashlog", "--json") == 0
    assert json.loads(capsys.readouterr().out)["suspects"] == [["beta", 3], ["alpha", 1]]
    assert run(game, "crashlog") == 0


# ---------------------------------------------------------------- repair

def test_needs_repair_only_after_a_cogitator_patch(game):
    reg = Registry.load()
    assert not repair.needs_repair(game, reg)  # never patched by Cogitator
    reg.data["patch"] = {"last_patch_at": 100.0}
    assert repair.needs_repair(game, reg)  # database is original again (state OFF) after a patch
    reg.data["patch"] = {"last_patch_at": 100.0, "last_unpatch_at": 200.0}
    assert not repair.needs_repair(game, reg)  # the user turned mods off on purpose


@pytest.mark.parametrize("mode,running,expected", [("off", False, "none"), ("ask", False, "notify"), ("auto", False, "patch"),
                                                   ("auto", True, "notify"), ("ask", True, "notify")])
def test_repair_decision(mode, running, expected):
    assert repair.decide(mode, running) == expected


def test_auto_repair_setting_validated(tmp_path):
    assert config.Settings().auto_repair == "ask"
    s = config.Settings()
    s.auto_repair = "bogus"
    s.save()
    assert config.Settings.load().auto_repair == "ask"
    s.auto_repair = "auto"
    s.save()
    assert config.Settings.load().auto_repair == "auto"


# ---------------------------------------------------------------- launch

class OK:
    level, id, name, detail = "ok", "x", "x", ""


class Bad:
    level, id, name, detail = "fail", "game", "Darktide install", "broken"


def fake_launch(game, reg, **kw):
    opened = []
    kw.setdefault("doctor", lambda *a, **k: [OK()])
    kw.setdefault("is_running", lambda: False)
    r = launch.launch(game, reg, opener=opened.append, **kw)
    return r, opened


def test_launch_starts_through_steam(game):
    r, opened = fake_launch(game, Registry.load())
    assert r.started and opened == [launch.STEAM_URL] and launch.STEAM_URL.endswith("/1361210")


def test_launch_refuses_when_running_or_failing(game):
    with pytest.raises(launch.LaunchError, match="already running"):
        fake_launch(game, Registry.load(), is_running=lambda: True)
    with pytest.raises(launch.LaunchError, match="--force"):
        fake_launch(game, Registry.load(), doctor=lambda *a, **k: [Bad()])
    r, opened = fake_launch(game, Registry.load(), doctor=lambda *a, **k: [Bad()], force=True)
    assert r.started and opened


def test_launch_profile_and_vanilla_are_exclusive(game):
    with pytest.raises(launch.LaunchError):
        fake_launch(game, Registry.load(), profile="p", vanilla=True)


def test_launch_vanilla_unpatches_and_normal_repatches(game, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "patch_state", lambda g: "ON")
    monkeypatch.setattr(launch, "unpatch", lambda *a, **k: calls.append("unpatch"))
    r, _ = fake_launch(game, Registry.load(), vanilla=True)
    assert calls == ["unpatch"] and "OFF" in r.notes[0]
    calls.clear()
    monkeypatch.setattr(launch, "patch_state", lambda g: "OFF")
    monkeypatch.setattr(launch, "patch", lambda *a, **k: calls.append("patch"))
    reg = Registry.load()
    reg.data["patch"] = {"last_patch_at": 1.0}
    r, _ = fake_launch(game, reg)
    assert calls == ["patch"] and "patched again" in r.notes[0]
    calls.clear()
    fake_launch(game, Registry.load())  # never patched by Cogitator: leave it alone
    assert calls == []


def test_launch_applies_profile(game):
    from cogitator import profiles

    reg = Registry.load()
    prof = profiles.capture(game, reg, "just-chat")
    prof["entries"] = [["demo_chat", True]]
    profiles.save(profiles.validate(prof))
    r, _ = fake_launch(game, reg, profile="just-chat")
    assert dict(order(game))["demo_hud"] is False and "just-chat" in r.notes[0]


def test_launch_cli(game, monkeypatch, capsys):
    monkeypatch.setattr("cogitator.launch.launch", lambda *a, **k: launch.LaunchResult(True, ["a note"]))
    assert run(game, "launch") == 0
    assert "a note" in capsys.readouterr().out

    def boom(*a, **k):
        raise launch.LaunchError("nope")

    monkeypatch.setattr("cogitator.launch.launch", boom)
    assert run(game, "launch") == 2


def test_launch_does_not_repatch_after_a_deliberate_unpatch(game, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "patch_state", lambda g: "OFF")
    monkeypatch.setattr(launch, "patch", lambda *a, **k: calls.append("patch"))
    reg = Registry.load()
    reg.data["patch"] = {"last_patch_at": 1.0, "last_unpatch_at": 2.0}  # the user turned mods off after patching
    r, _ = fake_launch(game, reg)
    assert calls == [] and r.started
    reg.data["patch"] = {"last_patch_at": 3.0, "last_unpatch_at": 2.0}  # patched later, now OFF again: a game update
    fake_launch(game, reg)
    assert calls == ["patch"]


def test_crashlog_handles_mod_folders_with_spaces_and_dots(tmp_path):
    p = tmp_path / "console.log"
    p.write_text("[Lua] [Mod] Loading ./../mods/My Cool Mod/My Cool Mod.mod\n"
                 "Error: scripts/mods/My Cool Mod/x.lua:3: attempt to call nil\n"
                 "Error: scripts/mods/my.mod.v2/y.lua:3: attempt to call nil\n"
                 "Error: 'scripts/mods/quoted/z.lua' [scripts/mods/bracket/z.lua] failed\n"
                 "Error: see mods/ and /path/x.lua failed\n")
    rep = crashlog.analyze(p)
    assert dict(rep.suspects) == {"My Cool Mod": 1, "my.mod.v2": 1, "quoted": 1, "bracket": 1}
    assert rep.loaded == ["My Cool Mod"]
