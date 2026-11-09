"""The install orchestrator: happy path, failure injection at every step, cancel at every boundary, idempotence."""
import copy
import io
import json
import logging
import threading
import zipfile
from pathlib import Path

import pytest

from cogitator import config as cfg
from cogitator import setup_run
from cogitator.config import Settings, get_api_key
from cogitator.doctor import run_doctor
from cogitator.patcher import ON, OFF, patch_state
from cogitator.registry import Registry
from cogitator.setup_run import (ComponentChoice, SetupDeps, SetupPlan, SetupResult, planned_steps, run_setup)
from cogitator.sources import local_option

from test_frameworks import DMF_FILES, DML_FILES, mkzip
from test_patcher import FakeTool, ORIG

USER_ORDER = b"-- my list\r\nusermod\r\n--othermod\r\n"
KEY = "SetupSecretKey-9f8e7d6c5b4a"


@pytest.fixture
def world(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "bundle" / "bundle_database.data").write_bytes(ORIG)
    (g / "mods" / "usermod").mkdir(parents=True)
    (g / "mods" / "usermod" / "usermod.mod").write_text("return {}")
    (g / "mods" / "mod_load_order.txt").write_bytes(USER_ORDER)
    z = tmp_path / "zips"
    z.mkdir()
    dml = mkzip(z / "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z a.zip", DML_FILES)
    dmf = mkzip(z / "Darktide Mod Framework 8 26.10.22 2026-10-23T02-11Z b.zip", DMF_FILES)
    return g, dml, dmf


def tree(g: Path) -> dict:
    return {p.relative_to(g).as_posix(): p.read_bytes() for p in sorted(g.rglob("*")) if p.is_file()}


class FakeApp:
    def __init__(self, fail=False):
        self.fail, self.installed, self.rolled_back = fail, False, False

    def install(self):
        if self.fail:
            raise OSError("disk full")
        self.installed = True
        return r"C:\Users\x\AppData\Local\Programs\Cogitator"

    def rollback(self):
        self.installed, self.rolled_back = False, True


class FakeShortcuts:
    def __init__(self, tmp, fail=False):
        self.dir, self.fail, self.existing = tmp, fail, []

    def create(self, desktop, start_menu):
        if self.fail:
            raise OSError("cannot write shortcut")
        out = []
        if desktop:
            out.append(self.dir / "Cogitator.lnk")
        if start_menu:
            out.append(self.dir / "Start Cogitator.lnk")
        for p in out:
            p.write_text("lnk")
        self.existing = out
        return out

    def remove(self, paths):
        for p in paths:
            p.unlink(missing_ok=True)
        self.existing = []


def make(world, tmp_path, *, tool=None, key=None, confirmed=True, app=None, shortcuts=None, mods=True, **kw):
    g, dml, dmf = world
    comps = [ComponentChoice(local_option("dml", dml), confirmed), ComponentChoice(local_option("dmf", dmf), confirmed)]
    plan = SetupPlan(g, comps, enable_mods=mods, api_key=key, app_installer=app, shortcuts=shortcuts,
                     doctor_offline=True, **kw)
    tool = tool or FakeTool()
    deps = SetupDeps(patch_runner=tool, is_running=lambda: False,
                     doctor_fn=lambda game, **k: run_doctor(game, log_dirs=[], steam_roots_fn=lambda: [], **{**k}))
    return plan, deps, tool


# ------------------------------------------------------------ happy path

def test_happy_path(world, tmp_path):
    g, *_ = world
    app, sc = FakeApp(), FakeShortcuts(tmp_path)
    plan, deps, tool = make(world, tmp_path, key=KEY, app=app, shortcuts=sc)
    events = []
    r = run_setup(plan, events.append, deps=deps)
    assert r.ok and not r.cancelled and not r.rolled_back and r.mods_on is True
    assert [s.name for s in r.steps] == ["check_game", "check_running", "app", "dml", "dmf", "patch", "register", "shortcuts", "doctor"]
    assert all(s.state == "done" for s in r.steps)
    assert app.installed and [p.name for p in sc.existing] == ["Cogitator.lnk", "Start Cogitator.lnk"]
    assert patch_state(g) == ON and tool.calls == ["--patch"]
    assert (g / "binaries" / "mod_loader").is_file() and (g / "mods" / "dmf" / "dmf.mod").is_file()
    assert (g / "mods" / "mod_load_order.txt").read_bytes() == USER_ORDER  # the user's file is untouched
    assert (g / "mods" / "usermod" / "usermod.mod").is_file()
    assert get_api_key() == KEY and Settings.load().game_dir == str(g) and Settings.load().notice_accepted
    reg = Registry.load()
    assert reg.manifest("dml") and reg.manifest("dmf") and reg.framework("dml")["version"] == "26.06.24"
    assert r.doctor is not None and r.installed.keys() == {"dml", "dmf"}
    # progress: every step reported, fractions monotone and ending at 1.0
    fr = [e.fraction for e in events]
    assert fr == sorted(fr) and fr[-1] == 1.0 and {e.step for e in events} >= {"dml", "dmf", "patch"}


def test_planned_steps_follow_the_plan(world, tmp_path):
    plan, *_ = make(world, tmp_path, mods=False)
    assert planned_steps(plan) == ["check_game", "check_running", "dml", "dmf", "register", "doctor"]


def test_dmf_listed_first_is_still_installed_after_dml(world, tmp_path):
    plan, deps, _ = make(world, tmp_path)
    plan.components.reverse()
    assert planned_steps(plan)[2:4] == ["dml", "dmf"]
    assert run_setup(plan, deps=deps).ok


def test_without_enabling_mods_the_game_database_is_not_touched(world, tmp_path):
    g, *_ = world
    plan, deps, tool = make(world, tmp_path, mods=False)
    r = run_setup(plan, deps=deps)
    assert r.ok and tool.calls == [] and (g / "bundle" / "bundle_database.data").read_bytes() == ORIG and r.mods_on is None


# ------------------------------------------------------------ refusals leave no trace

def test_unconfirmed_source_is_refused_before_anything_happens(world, tmp_path):
    g, *_ = world
    before = tree(g)
    plan, deps, tool = make(world, tmp_path, confirmed=False, app=FakeApp())
    r = run_setup(plan, deps=deps)
    assert not r.ok and r.failed_step == "check_game" and "not confirmed" in r.what
    assert tree(g) == before and tool.calls == [] and not plan.app_installer.installed


def test_game_running_blocks_with_a_retry_message(world, tmp_path):
    g, *_ = world
    before = tree(g)
    plan, deps, _ = make(world, tmp_path)
    deps.is_running = lambda: True
    r = run_setup(plan, deps=deps)
    assert r.failed_step == "check_running" and "Darktide is running" in r.what and "Retry" in r.fix and tree(g) == before


def test_not_a_darktide_folder(world, tmp_path):
    plan, deps, _ = make(world, tmp_path)
    plan.game_dir = tmp_path / "nope"
    r = run_setup(plan, deps=deps)
    assert r.failed_step == "check_game" and "does not look like a Darktide install" in r.what


# ------------------------------------------------------------ failure injection: every step unwinds completely

def inject_download(component):
    def patcher_(monkeypatch):
        real = setup_run.fetch_source

        def fetch(opt, *a, **k):
            if opt.component == component:
                raise setup_run.LoaderError("Download failed: HTTP 502 for file.zip")
            return real(opt, *a, **k)

        monkeypatch.setattr(setup_run, "fetch_source", fetch)
    return patcher_


def inject_extract_dmf(monkeypatch):  # a hostile DMF archive (zip-slip) discovered while installing it
    pass


CASES = {
    "app": ("app", lambda w, t, m: dict(app=FakeApp(fail=True))),
    "download_dml": ("dml", lambda w, t, m: (inject_download("dml")(m), {})[1]),
    "download_dmf": ("dmf", lambda w, t, m: (inject_download("dmf")(m), {})[1]),
    "patch": ("patch", lambda w, t, m: dict(tool=FakeTool(fail_patch=True))),
    "register": ("register", lambda w, t, m: (m.setattr(setup_run, "set_api_key", lambda k: (_ for _ in ()).throw(OSError("keyring locked"))), dict(key=KEY))[1]),
    "shortcuts": ("shortcuts", lambda w, t, m: dict(shortcuts=FakeShortcuts(t, fail=True))),
}


@pytest.mark.parametrize("case", list(CASES))
def test_failure_at_each_step_restores_everything(world, tmp_path, monkeypatch, case):
    g, *_ = world
    failed, build = CASES[case]
    before, reg_before = tree(g), copy.deepcopy(Registry.load().data)
    kw = build(world, tmp_path, monkeypatch)
    kw.setdefault("app", FakeApp())
    kw.setdefault("shortcuts", FakeShortcuts(tmp_path))
    kw.setdefault("key", KEY)
    plan, deps, tool = make(world, tmp_path, **kw)
    sc = plan.shortcuts
    r = run_setup(plan, deps=deps)
    assert not r.ok and r.failed_step == failed and r.rolled_back and not r.rollback_errors, r.diagnostics()
    assert r.what and r.fix  # plain-language: what happened + the next step
    assert tree(g) == before  # every byte in the game folder, including mod_load_order.txt and user mods
    assert (g / "mods" / "mod_load_order.txt").read_bytes() == USER_ORDER
    assert patch_state(g) == OFF and not (g / "bundle" / "bundle_database.data.bak").exists()
    assert not plan.app_installer.installed and sc.existing == []
    assert get_api_key() is None  # the key was not left behind
    assert Settings.load().game_dir == "" and not Settings.path().exists()
    assert Registry.load().data == reg_before
    assert KEY not in r.diagnostics()


def test_zip_slip_in_dmf_unwinds_dml_too(world, tmp_path):
    g, dml, dmf = world
    evil = mkzip(tmp_path / "zips" / "Darktide Mod Framework 8 26.10.22 2026-10-23T02-11Z evil.zip", {**DMF_FILES, "../../escape.txt": b"x"})
    before = tree(g)
    plan, deps, _ = make((g, dml, evil), tmp_path, app=FakeApp())
    r = run_setup(plan, deps=deps)
    assert r.failed_step == "dmf" and r.rolled_back and tree(g) == before and not (tmp_path / "escape.txt").exists()
    assert "outside the game folder" in r.what


def test_patch_failure_message_says_mods_stayed_off(world, tmp_path):
    plan, deps, _ = make(world, tmp_path, tool=FakeTool(corrupt_patch=True))
    r = run_setup(plan, deps=deps)
    assert r.failed_step == "patch" and "left switched off" in r.fix


# ------------------------------------------------------------ cancel at every boundary

@pytest.mark.parametrize("after", ["check_game", "check_running", "app", "dml", "dmf", "patch", "register", "shortcuts"])
def test_cancel_after_each_step_unwinds_everything(world, tmp_path, after):
    g, *_ = world
    before, reg_before = tree(g), copy.deepcopy(Registry.load().data)
    plan, deps, _ = make(world, tmp_path, key=KEY, app=FakeApp(), shortcuts=FakeShortcuts(tmp_path))
    cancel = threading.Event()

    def progress(ev):
        if ev.step == after and ev.state == "done":
            cancel.set()

    r = run_setup(plan, progress, cancel, deps)
    assert r.cancelled and not r.ok and r.rolled_back and not r.rollback_errors, r.diagnostics()
    assert tree(g) == before and patch_state(g) == OFF
    assert not plan.app_installer.installed and plan.shortcuts.existing == []
    assert get_api_key() is None and Registry.load().data == reg_before and not Settings.path().exists()


def test_cancel_before_start(world, tmp_path):
    g, *_ = world
    before = tree(g)
    plan, deps, _ = make(world, tmp_path)
    c = threading.Event()
    c.set()
    r = run_setup(plan, None, c, deps)
    assert r.cancelled and tree(g) == before and r.steps == []


# ------------------------------------------------------------ idempotence, repair, update

def test_running_setup_again_repairs_without_touching_user_data(world, tmp_path):
    g, dml, dmf = world
    plan, deps, tool = make(world, tmp_path, key=KEY)
    assert run_setup(plan, deps=deps).ok
    (g / "mods" / "mod_load_order.txt").write_bytes(USER_ORDER + b"added_later\r\n")  # user edits after the first run
    (g / "mods" / "newmod").mkdir()
    (g / "mods" / "newmod" / "newmod.mod").write_text("x")
    (g / "binaries" / "mod_loader").write_bytes(b"BROKEN")  # damage something Setup owns
    plan2, deps2, _ = make(world, tmp_path, key=KEY, tool=tool)
    r = run_setup(plan2, deps=deps2)
    assert r.ok
    assert (g / "binaries" / "mod_loader").read_bytes() == DML_FILES["binaries/mod_loader"]  # repaired
    assert (g / "mods" / "mod_load_order.txt").read_bytes() == USER_ORDER + b"added_later\r\n"
    assert (g / "mods" / "newmod" / "newmod.mod").is_file() and (g / "mods" / "usermod" / "usermod.mod").is_file()
    assert {s.name: s.state for s in r.steps}["patch"] == "skipped"  # already ON: nothing to patch again
    assert patch_state(g) == ON


def test_updating_dml_while_mods_are_on_unpatches_and_repatches(world, tmp_path):
    g, dml, dmf = world
    plan, deps, tool = make(world, tmp_path)
    assert run_setup(plan, deps=deps).ok
    newer = mkzip(tmp_path / "zips" / "Darktide Mod Loader 19 26.07.01 2026-07-01T00-00Z c.zip",
                  {**DML_FILES, "binaries/mod_loader": b"NEWER-LOADER"})
    plan2, deps2, _ = make((g, newer, dmf), tmp_path, tool=tool)
    plan2.components = plan2.components[:1]
    r = run_setup(plan2, deps=deps2)
    assert r.ok and tool.calls == ["--patch", "--unpatch", "--patch"] and patch_state(g) == ON
    assert (g / "binaries" / "mod_loader").read_bytes() == b"NEWER-LOADER"
    assert Registry.load().framework("dml")["version"] == "26.07.01"


def test_failure_during_an_update_restores_the_previous_install(world, tmp_path, monkeypatch):
    g, dml, dmf = world
    plan, deps, tool = make(world, tmp_path)
    assert run_setup(plan, deps=deps).ok
    before = tree(g)
    newer = mkzip(tmp_path / "zips" / "Darktide Mod Loader 19 26.07.01 2026-07-01T00-00Z c.zip", {**DML_FILES, "binaries/mod_loader": b"NEWER"})
    plan2, deps2, _ = make((g, newer, dmf), tmp_path, tool=tool, app=FakeApp(), shortcuts=FakeShortcuts(tmp_path, fail=True))
    plan2.components = plan2.components[:1]
    r = run_setup(plan2, deps=deps2)
    assert r.failed_step == "shortcuts" and r.rolled_back
    assert tree(g) == before or {k: v for k, v in tree(g).items() if not k.endswith(".bak")} == {k: v for k, v in before.items() if not k.endswith(".bak")}
    assert (g / "binaries" / "mod_loader").read_bytes() == DML_FILES["binaries/mod_loader"] and patch_state(g) == ON


# ------------------------------------------------------------ the key never leaks

def test_key_appears_nowhere_after_setup(world, tmp_path, caplog):
    g, *_ = world
    caplog.set_level(logging.DEBUG)
    cfg.setup_logging(logging.DEBUG)
    plan, deps, _ = make(world, tmp_path, key=KEY, app=FakeApp(), shortcuts=FakeShortcuts(tmp_path))
    r = run_setup(plan, deps=deps)
    assert r.ok and get_api_key() == KEY  # stored, but only in the key store
    for h in logging.getLogger().handlers:
        h.flush()
    haystacks = [r.diagnostics(), caplog.text, json.dumps(Registry.load().data)]
    for root in (tmp_path / "cfg", tmp_path / "cache", g, tmp_path / "zips"):
        for p in Path(root).rglob("*"):
            if p.is_file() and p.stat().st_size < 30_000_000:
                haystacks.append(p.read_bytes().decode("utf-8", "replace"))
    assert not any(KEY in h for h in haystacks)
    assert not (tmp_path / "cfg" / "nexus_key").exists()  # went to the (fake) keyring, not a file


# ------------------------------------------------------------ pinned DMF: verified vs mismatch (Express default for free accounts)

def _pinned_world(world, tmp_path, *, drift):
    import respx
    from cogitator.frameworks import payload_hash
    from cogitator.sources import PINNED_DMF, Pin, resolve_pinned_dmf
    g, dml, dmf = world
    pin = Pin(PINNED_DMF.repo, "a" * 40, "26.10.22", payload_hash(dmf)[0], payload_hash(dmf)[1])
    files = {**DMF_FILES, **({"dmf/scripts/mods/dmf/dmf_loader.lua": b"-- drifted"} if drift else {})}
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for n, d in files.items():
            z.writestr("o-r-aaaaaaa/" + n, d)
    comps = [ComponentChoice(local_option("dml", dml), True), ComponentChoice(resolve_pinned_dmf(pin), True)]
    plan = SetupPlan(g, comps, doctor_offline=True, run_doctor=False)
    return plan, pin, data.getvalue()


def _deps(tool, confirm=None):
    d = SetupDeps(patch_runner=tool, is_running=lambda: False)
    if confirm:
        d.confirm = confirm
    return d


def test_verified_pinned_dmf_installs_without_asking(world, tmp_path):
    import respx
    g, *_ = world
    plan, pin, data = _pinned_world(world, tmp_path, drift=False)
    asked = []
    with respx.mock:
        respx.get(f"https://api.github.com/repos/{pin.repo}/zipball/{pin.sha}").respond(200, content=data)
        r = run_setup(plan, deps=_deps(FakeTool(), lambda t, b: asked.append(t) or False))
    assert r.ok and asked == []
    assert r.installed["dmf"] == "matches Nexus release 26.10.22 (verified)"
    rec = Registry.load().framework("dmf")
    assert (rec["version"], rec["source_kind"], rec["unreleased"]) == ("26.10.22", "github-pinned", False)
    assert (g / "mods" / "dmf" / "dmf.mod").is_file()


def test_mismatching_pinned_dmf_is_refused_unless_the_user_confirms(world, tmp_path):
    import respx
    g, *_ = world
    before = tree(g)
    plan, pin, data = _pinned_world(world, tmp_path, drift=True)
    asked = []
    with respx.mock:
        respx.get(f"https://api.github.com/repos/{pin.repo}/zipball/{pin.sha}").respond(200, content=data)
        r = run_setup(plan, deps=_deps(FakeTool(), lambda t, b: asked.append((t, b)) or False))
    assert not r.ok and r.failed_step == "dmf" and r.rolled_back and tree(g) == before  # DML was rolled back too
    assert len(asked) == 1 and "does not match" in asked[0][0] and "UNRELEASED SNAPSHOT" in asked[0][1]
    assert "chose not to install" in r.what and "Nexus" in r.fix


def test_mismatching_pinned_dmf_installs_as_unreleased_when_confirmed(world, tmp_path):
    import respx
    g, *_ = world
    plan, pin, data = _pinned_world(world, tmp_path, drift=True)
    with respx.mock:
        respx.get(f"https://api.github.com/repos/{pin.repo}/zipball/{pin.sha}").respond(200, content=data)
        r = run_setup(plan, deps=_deps(FakeTool(), lambda t, b: True))
    assert r.ok and "UNRELEASED SNAPSHOT" in r.installed["dmf"]
    rec = Registry.load().framework("dmf")
    assert rec["unreleased"] is True and rec["version"] == pin.sha  # recorded by commit, never passed off as a release
    assert (g / "mods" / "dmf" / "scripts" / "mods" / "dmf" / "dmf_loader.lua").read_bytes() == b"-- drifted"
