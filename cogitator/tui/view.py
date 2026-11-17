"""Pure view helpers, constants and themes for the interface (no app state)."""
from __future__ import annotations

import logging

import time
from pathlib import Path

from rich.text import Text
from textual.theme import Theme

from ..config import redact
from ..doctor import Check
from ..frameworks import (detect_framework)
from ..glyphs import Glyphs
from ..mods import Mod, ENABLED, DISABLED, UNLISTED
from ..patcher import ON, OFF
from ..registry import Registry
from ..updates import (FrameworkUpdate, ModRow, UP_TO_DATE, UPDATE, UNKNOWN, FAILED, NOT_LINKED,
                       PROBABLY_OUTDATED, PROBABLY_UP_TO_DATE)

log = logging.getLogger("cogitator.tui")

NOTICE_TITLE = "Before you begin"
NOTICE_BODY = (
    "Darktide mods are NOT supported by Fatshark and are used at your own risk.\n\n"
    "Mods run as Lua inside the game with the game's account permissions, and Nexus Mods does not scan Lua for "
    "malware. Only install mods you trust.\n\n"
    "Fatshark disallows mods that affect other players, progression, or servers. Cogitator does not include or "
    "recommend any such mods.\n\n"
    "Cogitator only writes inside the game folder and its own config/cache folders. It contacts GitHub to look for new "
    "versions of itself and Nexus Mods (with your key, if you set one) to look for mod updates; nothing else leaves your "
    "computer, there is no telemetry, and nothing is downloaded without you asking.")

EMPTY_STATE = ("No mods installed yet. Download mods from nexusmods.com/warhammer40kdarktide, then press I to import the "
               "zip (or drop it onto this window). Press ? for help.")

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
                 "launch", "edit_note", "toggle_fav", "filter", "crashlog", "bisect"}
SORT_COLUMNS = ("mod", "status", "installed", "latest", "en")
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
    if not mod.present:
        t.append("  (folder missing)", "red")
    elif not mod.has_mod_file:
        t.append("  (no .mod file!)", "yellow")
    elif mod.state == UNLISTED:
        t.append("  (not in load order)", "dim")
    return t


_STATUS_STYLE = {UP_TO_DATE: "green", UPDATE: "bold yellow", UNKNOWN: "dim", FAILED: "bold red", NOT_LINKED: "dim",
                 PROBABLY_UP_TO_DATE: "green", PROBABLY_OUTDATED: "bold yellow", "pinned": "cyan"}
BASELINE_UNKNOWN_LABEL = "version unknown - press L to set it"


def installed_text(row: ModRow | None, mtime: float | None) -> Text:
    """Version, "~version" for a guess, else the folder's last-modified date."""
    if row is not None and row.installed:
        return Text(row.installed)
    return Text(time.strftime("%Y-%m-%d", time.localtime(mtime)) if mtime else "-", "dim")


def status_text(row: ModRow | None, g: Glyphs, mod: Mod | None = None) -> Text:
    if mod is not None and not mod.present:
        return Text("folder missing", "red")
    if row is None:
        return Text("not linked", "dim")
    label = row.status
    if row.status == UNKNOWN and row.baseline_unknown:
        label = BASELINE_UNKNOWN_LABEL
    elif row.status in (UPDATE, PROBABLY_OUTDATED):
        label = f"{row.status} ({row.installed or '?'} {g.arrow} {row.latest or '?'})"
    return Text(label, _STATUS_STYLE.get(row.status, ""))


def hint_for(mod: Mod | None, row: ModRow | None) -> str:
    if mod is None:
        return EMPTY_STATE
    if not mod.present:
        return f"'{mod.folder}' is listed in mod_load_order.txt but its folder is missing."
    if not mod.has_mod_file:
        return f"Warning: {mod.folder}/{mod.folder}.mod is missing - the archive may have been extracted badly."
    if mod.state == UNLISTED:
        return "Not in mod_load_order.txt. Press Space to add it to the load order (enabled)."
    if row is None or row.status == NOT_LINKED:
        return "Not linked to Nexus. Press L to link it so updates can be detected."
    return f"{row.status}: {row.reason}"


def report_text(checks: list[Check]) -> Text:
    """Doctor report with ASCII level markers (they survive any console) and colour on top."""
    style = {"ok": "green", "warn": "bold yellow", "fail": "bold red"}
    t = Text()
    for c in checks:
        t.append({"ok": "[OK]   ", "warn": "[WARN] ", "fail": "[FAIL] "}[c.level], style[c.level])
        t.append(f"{c.name}: ", "bold")
        t.append(redact(c.detail) + "\n")
        if c.fix and c.level != "ok":
            t.append(f"       fix: {c.fix}\n", "cyan")
    n = {k: sum(1 for c in checks if c.level == k) for k in ("ok", "warn", "fail")}
    t.append(f"\n{n['ok']} ok, {n['warn']} warning(s), {n['fail']} failure(s).", "bold")
    return t


def fw_text(game_dir: Path | None, registry: Registry | None, fw: list[FrameworkUpdate], badge: str) -> Text:
    t = Text()
    if game_dir is None or registry is None:
        return t
    by = {u.kind: u for u in fw}
    for kind in ("dml", "dmf"):
        st = detect_framework(game_dir, kind)
        rec = registry.framework(kind) or {}
        ver = rec.get("version") or ("version unknown" if st.status == "installed" else "")
        style = "green" if st.status == "installed" else "bold red"
        t.append(f"{kind.upper()} ", "bold")
        t.append(f"{st.status}" + (f" {ver}" if ver else ""), style)
        u = by.get(kind)
        if u is not None and u.status == UPDATE:
            t.append(f" (update available -> {u.latest})", "bold yellow")
        t.append("   ")
    colour = {ON: "bold black on green", OFF: "bold white on red"}.get(badge, "bold black on yellow")
    t.append(f" MODS: {badge} ", colour)
    return t


def nexus_text(key_set: bool, premium: bool | None, quota: str | None, checking: str = "") -> Text:
    t = Text("Nexus: ")
    if not key_set:
        t.append("no API key - press K", "bold yellow")
        return t
    acct = "account unknown" if premium is None else ("premium" if premium else "free")
    t.append(f"key {'ok' if premium is not None else 'set'} ({acct})", "green")
    t.append("   quota: " + (quota or "unknown"), "")
    if checking:
        t.append("   " + checking, "cyan")
    return t
