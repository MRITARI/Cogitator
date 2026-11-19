"""Pure view helpers, constants and themes for the interface (no app state)."""
from __future__ import annotations

import logging

from pathlib import Path

from rich.text import Text
from textual.theme import Theme

from ..doctor import Check
from ..glyphs import Glyphs
from ..mods import Mod, ENABLED, DISABLED, UNLISTED
from ..registry import Registry
from ..updates import (FrameworkUpdate, ModRow)

log = logging.getLogger("cogitator.tui")

from ..viewtext import (report_segments, SORT_COLUMNS, NOTICE_TITLE, NOTICE_BODY, EMPTY_STATE, BASELINE_UNKNOWN_LABEL, hint_for, STATUS_STYLE as _STATUS_STYLE,  # noqa: F401, E402
                        fw_segments, installed_label, mod_suffix, nexus_segments, status_label)

HELP_BODY = (
    "T  mods on/off (the game patch)\n"
    "Space  enable/disable the selected mod\n"
    "P  profiles: switch, save, share a setup code\n"
    "X  mark a mod, B  act on the marked mods (enable, disable, update, tag, remove)\n"
    "N  tags and note, *  favourite, /  filter the list\n"
    "G  launch Darktide, C  crash-log leads (menu: find a crashing mod)\n"
    "Shift+Up/Down or [ ]  move in the load order\n"
    "O  sort    U  update selected    Ctrl+U  update all\n"
    "F  update Mod Loader/Framework    R  re-check\n"
    "L  link to Nexus    M  link all    I  import a zip\n"
    "K  Nexus key    A  update Cogitator\n"
    "D  doctor (diagnose problems)    Q  quit\n"
    "\n"
    "Mouse: click Menu (top left) for every action; right-click a mod for its actions;\n"
    "drag a mod up or down to change its load order; ? opens this help and X quits.\n"
    "\n"
    "Log file: {log}\n"
    "Settings folder: {cfg}\n"
    "Problems: run the doctor (D), then report at github.com/MRITARI/Cogitator/issues and attach the log file.\n"
    "Not affiliated with Fatshark, Games Workshop or Nexus Mods.")

_MAIN_ACTIONS = {"help", "app_menu", "choose_theme", "link_all", "toggle_mods", "toggle_mod", "move_up", "move_down", "update_selected", "update_all", "recheck", "link", "import_archive",
                 "set_key", "cycle_sort", "quit_app", "framework_update", "doctor", "apply_update", "profiles", "toggle_mark", "bulk",
                 "launch", "edit_note", "toggle_fav", "filter", "crashlog", "bisect", "search"}
DEFAULT_THEME = "tokyo-night"
COGITATOR_THEMES = (
    Theme(name="black", primary="#4a90d9", secondary="#7f8c8d", accent="#f1c40f", foreground="#e8e8e8",
          background="#000000", surface="#000000", panel="#141414", boost="#1c1c1c", warning="#f1c40f", error="#e74c3c",
          success="#2ecc71", dark=True),
    Theme(name="cogitator-graphite", primary="#4a90d9", secondary="#95a5a6", accent="#f1c40f", foreground="#e8e8e8",
          background="#1b1b1f", surface="#1b1b1f", panel="#26262c", boost="#2f2f36", warning="#f1c40f", error="#e74c3c",
          success="#2ecc71", dark=True),
)
THEME_CHOICES = ("tokyo-night", "black", "cogitator-graphite", "textual-dark", "nord", "gruvbox", "dracula",
                 "monokai", "catppuccin-mocha", "solarized-dark", "textual-light", "solarized-light")
# bottom button bar: (label, action, tooltip). "Update Cogitator" only shows while an update is waiting or offered.
ACTION_BUTTONS = (("On/off", "toggle_mod", "Space - enable or disable the selected mod"),
                  ("Update", "update_selected", "U - update the selected mod"),
                  ("Update all", "update_all", "Ctrl+U - update every mod that has an update"),
                  ("Recheck", "recheck", "R - check Nexus for updates now"),
                  ("Import", "import_archive", "I - choose mod zip file(s) to install"),
                  ("Update Cogitator", "apply_update", "A - install the new Cogitator"))


# ------------------------------------------------------------------ pure view helpers (unit-testable)

def enabled_cell(mod: Mod, g: Glyphs) -> Text:
    if mod.state == ENABLED:
        return Text(g.on, "bold green")
    if mod.state == DISABLED:
        return Text(g.off, "bold red")
    return Text(g.unlisted, "dim")


def _where_text(where: str) -> str:
    return ("in the Windows credential store" if where == "keyring"
            else "in a plain-text file in your settings folder (the credential store was not available)")


def _config_recovered() -> list:
    from ..config import RECOVERED

    return list(RECOVERED)


def mod_cell(mod: Mod) -> Text:
    t = Text(mod.folder, "dim" if mod.state == UNLISTED else "", no_wrap=True, overflow="ellipsis")
    suffix = mod_suffix(mod)
    if suffix:
        t.append(*suffix)
    return t


def installed_text(row: ModRow | None, mtime: float | None) -> Text:
    """Version, "~version" for a guess, else the folder's last-modified date."""
    return Text(*installed_label(row, mtime))


def status_text(row: ModRow | None, g: Glyphs, mod: Mod | None = None) -> Text:
    return Text(*status_label(row, g.arrow, mod))


def report_text(checks: list[Check]) -> Text:
    return _segments_text(report_segments(checks))


def _segments_text(segments) -> Text:
    t = Text()
    for text, style in segments:
        t.append(text, style)
    return t


def fw_text(game_dir: Path | None, registry: Registry | None, fw: list[FrameworkUpdate], badge: str) -> Text:
    return _segments_text(fw_segments(game_dir, registry, fw, badge))


def nexus_text(key_set: bool, premium: bool | None, quota: str | None, checking: str = "") -> Text:
    return _segments_text(nexus_segments(key_set, premium, quota, checking))
