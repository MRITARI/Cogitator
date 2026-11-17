"""Find the mod that causes a crash by halving: enable half of the suspects, you test, and the answer narrows the list.
State lives in <config>/bisect.json so a session survives closing Cogitator. A snapshot is taken first and 'reset' puts the
load order back exactly as it was."""
from __future__ import annotations

import json
from pathlib import Path

from . import snapshots
from .config import config_dir
from .fsutil import atomic_write_text
from .loadorder import LoadOrder
from .mods import INTERNAL_FOLDERS, ModError, load_order_path, scan_mods


def state_path() -> Path:
    return config_dir() / "bisect.json"


def load() -> dict | None:
    try:
        d = json.loads(state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and "candidates" in d else None
    except (OSError, ValueError):
        return None


def _save(s: dict) -> None:
    atomic_write_text(state_path(), json.dumps(s, indent=1))


def _apply(game_dir: Path, s: dict) -> None:
    """Enable the test set; disable the other suspects; leave every other mod alone."""
    test = set(s["test"])
    path = load_order_path(game_dir)
    lo = LoadOrder.load(path)
    for f in s["all"]:
        lo.set_enabled(f, f in test)
    lo.save(path)


def _next_test(game_dir: Path, s: dict) -> None:
    c = s["candidates"]
    half = c[: max(1, len(c) // 2)]
    s["test"] = list(half)
    s["tested_half"] = half
    s["step"] += 1


def describe(s: dict) -> str:
    if s.get("culprit"):
        return f"Found it: '{s['culprit']}' is the mod that causes the problem. Run 'bisect reset' to restore your load order."
    if s.get("inconclusive"):
        return ("No single mod explains it: the problem needs a combination of mods (or is not caused by mods). "
                "Run 'bisect reset' to restore your load order.")
    return (f"Step {s['step']}: {len(s['candidates'])} suspect(s) left. Only these are enabled now: {', '.join(s['test'])}.\n"
            "Start the game and try to reproduce the problem, then run 'bisect bad' if it still happens or 'bisect good' if it is gone.")


def start(game_dir: Path, registry=None) -> dict:
    if load():
        raise ModError("A bisect session is already running. Use 'bisect status', or 'bisect reset' to stop it.")
    enabled = [m.folder for m in scan_mods(game_dir) if m.present and m.state == "enabled" and m.folder not in INTERNAL_FOLDERS]
    if len(enabled) < 2:
        raise ModError("Bisecting needs at least two enabled mods.")
    snap = snapshots.create(game_dir, "bisect-start", registry=registry)
    s = {"snapshot": snap.name, "all": enabled, "candidates": list(enabled), "step": 0, "culprit": None, "inconclusive": False}
    _next_test(game_dir, s)
    _apply(game_dir, s)
    _save(s)
    return s


def answer(game_dir: Path, still_broken: bool) -> dict:
    s = load()
    if not s:
        raise ModError("No bisect session is running. Start one with 'bisect start'.")
    if s.get("culprit") or s.get("inconclusive"):
        raise ModError(describe(s))
    before, test = s["candidates"], set(s["test"])
    # bad: the culprit is among the enabled suspects. good: those are cleared, it is in the half that was off.
    c = [m for m in before if (m in test) == still_broken]
    s["candidates"] = c
    if len(c) == 1:
        s["culprit"], s["test"] = c[0], []
    elif not c or len(c) == len(before):  # nothing left, or no progress is possible
        s["inconclusive"], s["test"] = True, []
    else:
        _next_test(game_dir, s)
    if not s.get("culprit") and not s.get("inconclusive"):
        _apply(game_dir, s)
    _save(s)
    return s


def reset(game_dir: Path, registry=None) -> list[str]:
    s = load()
    if not s:
        raise ModError("No bisect session is running.")
    msgs = snapshots.restore(game_dir, s["snapshot"], registry)
    state_path().unlink(missing_ok=True)
    return msgs or ["load order restored"]
