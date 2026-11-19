"""Display text shared by every interface (terminal and graphical): constants and plain (text, style) pieces.

No rich, textual or tkinter in here. A "style" is a rich-style string such as "bold green"; the terminal UI hands it to rich
and the graphical UI maps it to a text tag. Keeping the wording in one place means both show the same thing."""
from __future__ import annotations

import time
from pathlib import Path

from .config import redact
from .doctor import Check
from .frameworks import detect_framework
from .mods import Mod, UNLISTED
from .patcher import OFF, ON
from .registry import Registry
from .updates import (FAILED, NOT_LINKED, PROBABLY_OUTDATED, PROBABLY_UP_TO_DATE, UNKNOWN, UP_TO_DATE, UPDATE, FrameworkUpdate, ModRow)

Segment = tuple[str, str]  # (text, rich-style)

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
BASELINE_UNKNOWN_LABEL = "version unknown - press L to set it"
SORT_COLUMNS = ("mod", "status", "installed", "latest", "en")

STATUS_STYLE = {UP_TO_DATE: "green", UPDATE: "bold yellow", UNKNOWN: "dim", FAILED: "bold red", NOT_LINKED: "dim",
                PROBABLY_UP_TO_DATE: "green", PROBABLY_OUTDATED: "bold yellow", "pinned": "cyan"}


def mod_suffix(mod: Mod) -> Segment | None:
    if not mod.present:
        return "  (folder missing)", "red"
    if not mod.has_mod_file:
        return "  (no .mod file!)", "yellow"
    if mod.state == UNLISTED:
        return "  (not in load order)", "dim"
    return None


def installed_label(row: ModRow | None, mtime: float | None) -> Segment:
    """Version, "~version" for a guess, else the folder's last-modified date."""
    if row is not None and row.installed:
        return row.installed, ""
    return (time.strftime("%Y-%m-%d", time.localtime(mtime)) if mtime else "-"), "dim"


def status_label(row: ModRow | None, arrow: str, mod: Mod | None = None) -> Segment:
    if mod is not None and not mod.present:
        return "folder missing", "red"
    if row is None:
        return "not linked", "dim"
    label = row.status
    if row.status == UNKNOWN and row.baseline_unknown:
        label = BASELINE_UNKNOWN_LABEL
    elif row.status in (UPDATE, PROBABLY_OUTDATED):
        label = f"{row.status} ({row.installed or '?'} {arrow} {row.latest or '?'})"
    return label, STATUS_STYLE.get(row.status, "")


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


def fw_segments(game_dir: Path | None, registry: Registry | None, fw: list[FrameworkUpdate], badge: str) -> list[Segment]:
    out: list[Segment] = []
    if game_dir is None or registry is None:
        return out
    by = {u.kind: u for u in fw}
    for kind in ("dml", "dmf"):
        st = detect_framework(game_dir, kind)
        rec = registry.framework(kind) or {}
        ver = rec.get("version") or ("version unknown" if st.status == "installed" else "")
        out.append((f"{kind.upper()} ", "bold"))
        out.append((f"{st.status}" + (f" {ver}" if ver else ""), "green" if st.status == "installed" else "bold red"))
        u = by.get(kind)
        if u is not None and u.status == UPDATE:
            out.append((f" (update available -> {u.latest})", "bold yellow"))
        out.append(("   ", ""))
    colour = {ON: "bold black on green", OFF: "bold white on red"}.get(badge, "bold black on yellow")
    out.append((f" MODS: {badge} ", colour))
    return out


def nexus_segments(key_set: bool, premium: bool | None, quota: str | None, checking: str = "") -> list[Segment]:
    out: list[Segment] = [("Nexus: ", "")]
    if not key_set:
        out.append(("no API key - press K", "bold yellow"))
        return out
    acct = "account unknown" if premium is None else ("premium" if premium else "free")
    out.append((f"key {'ok' if premium is not None else 'set'} ({acct})", "green"))
    out.append(("   quota: " + (quota or "unknown"), ""))
    if checking:
        out.append(("   " + checking, "cyan"))
    return out


def where_text(where: str) -> str:
    return ("in the Windows credential store" if where == "keyring"
            else "in a plain-text file in your settings folder (the credential store was not available)")


def report_segments(checks: list[Check]) -> list[Segment]:
    """Doctor report with ASCII level markers (they survive any console) and colour on top."""
    style = {"ok": "green", "warn": "bold yellow", "fail": "bold red"}
    out: list[Segment] = []
    for c in checks:
        out.append(({"ok": "[OK]   ", "warn": "[WARN] ", "fail": "[FAIL] "}[c.level], style[c.level]))
        out.append((f"{c.name}: ", "bold"))
        out.append((redact(c.detail) + "\n", ""))
        if c.fix and c.level != "ok":
            out.append((f"       fix: {c.fix}\n", "cyan"))
    n = {k: sum(1 for c in checks if c.level == k) for k in ("ok", "warn", "fail")}
    out.append((f"\n{n['ok']} ok, {n['warn']} warning(s), {n['fail']} failure(s).", "bold"))
    return out
