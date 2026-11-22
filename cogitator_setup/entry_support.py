"""Setup/uninstall entry logic with no window toolkit: flags, mode choice, services, app-folder repair, error box."""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from .viewmodel import Services

TITLE = "Cogitator Setup"

USAGE = """Cogitator Setup

  cogitator-setup                     install (or, if Cogitator is already installed, repair / uninstall)
  cogitator-setup --wizard            always open the install wizard
  cogitator-setup --uninstall         open the uninstaller
  cogitator-setup --uninstall --silent   uninstall with the defaults only, no window
  cogitator-setup --dry-run           look through every screen; NOTHING is installed, removed or saved
  cogitator-setup --game-dir PATH     use this Darktide folder (for example a scratch copy)
  cogitator-setup --self-check        check this build can run
"""


@dataclass
class Flags:
    uninstall: bool = False
    silent: bool = False
    maintenance: bool = False
    wizard: bool = False
    dry_run: bool = False
    help: bool = False
    game_dir: str | None = None


def parse_flags(argv: list[str]) -> Flags:
    f = Flags(uninstall="--uninstall" in argv, silent="--silent" in argv, maintenance="--maintenance" in argv, wizard="--wizard" in argv,
              dry_run="--dry-run" in argv, help=("--help" in argv or "-h" in argv))
    if "--game-dir" in argv and argv.index("--game-dir") + 1 < len(argv):
        f.game_dir = argv[argv.index("--game-dir") + 1]
    return f


def choose_mode(f: Flags, installed: bool) -> str:
    """setup | maintenance | uninstall | silent-uninstall. Running Setup when Cogitator is already installed opens maintenance."""
    if f.uninstall:
        return "silent-uninstall" if f.silent else "uninstall"
    if f.wizard:
        return "setup"
    return "maintenance" if (f.maintenance or installed) else "setup"


def setup_services(game_dir: str | None) -> Services:
    from cogitator.gamefinder import Resolution

    if not game_dir:
        return Services()
    g = Path(game_dir)
    return Services(find_game=lambda: Resolution(g, "override", [g]))


def uninstall_services(game_dir: str | None):
    from .uninstall_vm import UServices

    return UServices(game_dir=lambda: Path(game_dir)) if game_dir else UServices()


def _default_show_error(title: str, body: str) -> None:
    print(f"{title}: {body}", file=sys.stderr)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, body, title, 0x10)
    except Exception:
        pass


def _repair_app_folder() -> None:
    """This program lives in <root>/setup. If an interrupted update left <root>/app missing, put the previous version back."""
    try:
        if getattr(sys, "frozen", False):
            root = Path(sys.executable).resolve().parent.parent
            if (root / "install.json").is_file():
                from cogitator.selfupdate import repair_install

                repair_install(root)
    except Exception:
        logging.getLogger(__name__).exception("app folder repair failed")
