"""Patcher tests use a fake dtkit-patch that mimics the observed 0.1.7 behaviour (incl. its blind --unpatch)."""
import os
import time
from pathlib import Path

import pytest

from cogitator import patcher
from cogitator.frameworks import DML_REQUIRED
from cogitator.patcher import OFF, ON, PatcherError, banner, patch, patch_state, toggle, unpatch
from cogitator.registry import Registry

ORIG = b"HEADER" + b"\x00" * 50 + b"A33A4AA4AF26A69B-84-bytes-here" + b"tail" * 100
PATCHED_INSERT = b"[patch_999 entry 100 bytes....]"


class FakeTool:
    """0.1.7 semantics: --patch writes .bak then patches; --unpatch blindly renames .bak over the db."""

    def __init__(self, fail_patch=False, corrupt_patch=False, noop_patch=False, bad_bak=False):
        self.calls = []
        self.fail_patch, self.corrupt_patch, self.noop_patch, self.bad_bak = fail_patch, corrupt_patch, noop_patch, bad_bak

    def __call__(self, args, cwd):
        tool, flag, d = args
        self.calls.append(flag)
        d = Path(d)
        db, bak = d / "bundle_database.data", d / "bundle_database.data.bak"
        if flag == "--patch":
            data = db.read_bytes()
            if b"patch_999" in data:
                return 0, "", '"bundle_database.data" already patched'
            if self.noop_patch:
                return 0, "", "successfully patched"
            bak.write_bytes(b"WRONG" if self.bad_bak else data)
            if self.fail_patch:
                db.write_bytes(data[:10])  # half-written
                return 1, "", "failed to write patched"
            db.write_bytes(data + PATCHED_INSERT if not self.corrupt_patch else b"junk")
            return 0, "", "successfully patched"
        if flag == "--unpatch":
            if not bak.exists():
                return 1, "", 'backup "bundle_database.data.bak" not found'
            os.replace(bak, db)
            return 0, "", "successfully unpatched"
        raise AssertionError(flag)


@pytest.fixture
def game(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "bundle" / "bundle_database.data").write_bytes(ORIG)
    for rel in DML_REQUIRED + ("tools/dtkit-patch.exe",):
        p = g / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return g


def kw(tool, running=False):
    return dict(runner=tool, is_running=lambda: running)


def db(g):
    return (g / "bundle" / "bundle_database.data").read_bytes()


def test_state_byte_search(game):
    assert patch_state(game) == OFF
    (game / "bundle" / "bundle_database.data").write_bytes(ORIG + b"..patch_999..")
    assert patch_state(game) == ON
    (game / "bundle" / "bundle_database.data").write_bytes(b"")
    assert patch_state(game) == "UNKNOWN"
    (game / "bundle" / "bundle_database.data").unlink()
    assert patch_state(game) == "UNKNOWN"


def test_banner(game, tmp_path):
    assert "press T" in banner(game)
    patch(game, None, **kw(FakeTool()))
    assert banner(game) is None
    (game / "bundle" / "9ba626afa44a3aa3.patch_999").unlink()
    (game / "bundle" / "bundle_database.data").write_bytes(ORIG)
    assert banner(game) is None  # DML not installed -> nothing to enable


def test_patch_then_unpatch_roundtrip_bytes(game):
    reg, tool = Registry.load(), FakeTool()
    r = patch(game, reg, **kw(tool))
    assert r.changed and patch_state(game) == ON and db(game) != ORIG
    assert (game / "bundle" / "bundle_database.data.bak").read_bytes() == ORIG
    assert reg.data["patch"]["last_patch_at"] > 0 and Registry.load().data["patch"]["build_key"]
    again = patch(game, reg, **kw(tool))
    assert not again.changed and tool.calls == ["--patch"]  # tool not even invoked
    u = unpatch(game, reg, **kw(tool))
    assert u.changed and db(game) == ORIG and patch_state(game) == OFF
    assert tool.calls == ["--patch", "--unpatch"]
    assert "last_unpatch_at" in reg.data["patch"]


def test_never_uses_toggle_flag(game):
    t = FakeTool()
    patch(game, None, **kw(t))
    unpatch(game, None, **kw(t))
    assert "--toggle" not in t.calls


def test_own_backup_one_per_build(game):
    from cogitator.config import cache_dir
    patch(game, None, **kw(FakeTool()))
    unpatch(game, None, **kw(FakeTool()))
    patch(game, None, **kw(FakeTool()))
    files = list((cache_dir() / "db-backups").glob("*.data"))
    assert len(files) == 1 and files[0].read_bytes() == ORIG


@pytest.mark.parametrize("tool_kw", [dict(fail_patch=True), dict(corrupt_patch=True), dict(noop_patch=True), dict(bad_bak=True)])
def test_failed_patch_restores_original(game, tool_kw):
    with pytest.raises(PatcherError):
        patch(game, None, **kw(FakeTool(**tool_kw)))
    assert db(game) == ORIG and patch_state(game) == OFF


def test_patch_preflight_refusals(game, tmp_path):
    with pytest.raises(PatcherError, match="running"):
        patch(game, None, **kw(FakeTool(), running=True))
    (game / "binaries" / "mod_loader").unlink()
    with pytest.raises(PatcherError, match="Mod Loader"):
        patch(game, None, **kw(FakeTool()))
    with pytest.raises(PatcherError, match="Not a Darktide"):
        patch(tmp_path / "nope", None, **kw(FakeTool()))


def test_unpatch_refuses_missing_bak(game):
    t = FakeTool()
    patch(game, None, **kw(t))
    (game / "bundle" / "bundle_database.data.bak").unlink()
    before = db(game)
    with pytest.raises(PatcherError, match="missing"):
        unpatch(game, None, **kw(t))
    assert db(game) == before and t.calls == ["--patch"]


def test_unpatch_refuses_bak_older_than_last_patch(game):
    reg, t = Registry.load(), FakeTool()
    patch(game, reg, **kw(t))
    bak = game / "bundle" / "bundle_database.data.bak"
    stale = reg.data["patch"]["last_patch_at"] - 3600
    os.utime(bak, (stale, stale))
    before = db(game)
    with pytest.raises(PatcherError, match="older than Cogitator's last patch"):
        unpatch(game, reg, **kw(t))
    assert db(game) == before and t.calls == ["--patch"]  # the blind tool was never run


def test_unpatch_bak_within_slack_and_newer_ok(game):
    reg, t = Registry.load(), FakeTool()
    patch(game, reg, **kw(t))
    bak = game / "bundle" / "bundle_database.data.bak"
    now = reg.data["patch"]["last_patch_at"] - 1.0  # inside 2s granularity slack
    os.utime(bak, (now, now))
    assert unpatch(game, reg, **kw(t)).changed


def test_unpatch_refuses_patched_bak(game):
    t = FakeTool()
    patch(game, None, **kw(t))
    (game / "bundle" / "bundle_database.data.bak").write_bytes(ORIG + b"patch_999")
    with pytest.raises(PatcherError, match="itself patched"):
        unpatch(game, None, **kw(t))


def test_unpatch_externally_patched_without_record_is_allowed(game):
    t = FakeTool()
    patch(game, None, **kw(t))  # no registry -> no last_patch_at recorded
    assert unpatch(game, Registry.load(), **kw(t)).changed


def test_unpatch_unexpected_result_restored(game):
    class Evil(FakeTool):
        def __call__(self, args, cwd):
            rc = super().__call__(args, cwd)
            if args[1] == "--unpatch":
                (Path(args[2]) / "bundle_database.data").write_bytes(b"garbage")
            return rc

    t = Evil()
    patch(game, None, **kw(t))
    patched = db(game)
    with pytest.raises(PatcherError, match="expected original"):
        unpatch(game, None, **kw(t))
    assert db(game) == patched and (game / "bundle" / "bundle_database.data.bak").read_bytes() == ORIG


def test_unpatch_when_off_is_noop(game):
    t = FakeTool()
    r = unpatch(game, None, **kw(t))
    assert not r.changed and t.calls == []
    # the dangerous case the real 0.1.7 tool would have mishandled: stale .bak next to a healthy db
    (game / "bundle" / "bundle_database.data.bak").write_bytes(b"STALE")
    unpatch(game, None, **kw(t))
    assert db(game) == ORIG


def test_toggle_confirms_before_unpatch(game):
    t = FakeTool()
    assert toggle(game, None, confirm_unpatch=lambda: False, **kw(t)).changed  # OFF -> patch, no prompt
    asked = []
    r = toggle(game, None, confirm_unpatch=lambda: asked.append(1) or False, **kw(t))
    assert asked and not r.changed and patch_state(game) == ON
    assert toggle(game, None, confirm_unpatch=lambda: True, **kw(t)).state == OFF


def test_find_tool_missing(game):
    (game / "tools" / "dtkit-patch.exe").unlink()
    with pytest.raises(patcher.PatcherUnavailable):
        patcher.find_tool(game)
