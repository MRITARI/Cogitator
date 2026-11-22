"""Setup/uninstall entry logic without a window: silent uninstall, mode choice, flags, the Windows 11 gate."""
import pytest

from cogitator import config as cfg
from cogitator.config import get_api_key, set_api_key
from cogitator.patcher import OFF, patch_state
from cogitator.uninstall_run import UninstallDeps
from cogitator_setup import platform_gate
from cogitator_setup.entry_support import choose_mode, parse_flags
from cogitator_setup.silent import run_silent
from cogitator_setup.uninstall_vm import UServices

from test_uninstall import USER_ORDER, installed, tree  # noqa: F401  (installed is a fixture)
from test_uninstall_run import FakeApp


def test_silent_uninstall_applies_only_the_defaults(installed):
    g, reg, tool = installed
    set_api_key("StoredKey-1234567890")
    out = []
    svc = UServices(game_dir=lambda: g, registry=lambda: reg, is_running=lambda: False, app_remover=FakeApp())
    rc = run_silent(svc, UninstallDeps(runner=tool, is_running=lambda: False, registry=reg), out.append)
    assert rc == 0 and out and any("mods are switched off" in l for l in out)
    assert patch_state(g) == OFF and not (g / "binaries" / "mod_loader").exists() and get_api_key() is None
    assert (g / "mods" / "usermod" / "data.lua").exists() and (g / "mods" / "mod_load_order.txt").read_bytes() == USER_ORDER  # never user data
    assert cfg.config_dir().exists()  # never settings/caches in silent mode


def test_silent_uninstall_refuses_while_the_game_runs(installed):
    g, reg, tool = installed
    before = tree(g)
    out = []
    svc = UServices(game_dir=lambda: g, registry=lambda: reg, is_running=lambda: True)
    assert run_silent(svc, UninstallDeps(runner=tool, is_running=lambda: True, registry=reg), out.append) == 2
    assert tree(g) == before and "Close it" in out[0] and "Nothing was changed" in out[0]


@pytest.mark.parametrize("argv,installed_,mode", [
    ([], False, "setup"), ([], True, "maintenance"), (["--wizard"], True, "setup"), (["--maintenance"], False, "maintenance"),
    (["--uninstall"], False, "uninstall"), (["--uninstall", "--silent"], True, "silent-uninstall"), (["--silent"], True, "maintenance"),
])
def test_choose_mode(argv, installed_, mode):
    assert choose_mode(parse_flags(argv), installed_) == mode


def test_flag_parsing():
    f = parse_flags(["--dry-run", "--game-dir", r"C:\scratch\Darktide", "--uninstall"])
    assert f.dry_run and f.uninstall and f.game_dir == r"C:\scratch\Darktide" and not f.silent
    assert parse_flags(["--game-dir"]).game_dir is None


def test_gate_build_numbers():
    assert not platform_gate.is_supported(None)
    assert not platform_gate.is_supported(19045)  # Windows 10 22H2
    assert not platform_gate.is_supported(21999)
    assert platform_gate.is_supported(22000)  # first Windows 11 build
    assert platform_gate.is_supported(26300)
    assert platform_gate.MESSAGE == "Cogitator Setup supports Windows 11 only"
