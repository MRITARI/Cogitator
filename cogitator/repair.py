"""Game-update repair: a game update (or Steam 'verify files') restores the original bundle database, which switches every
mod off. When Cogitator patched this game earlier and did not un-patch it since, re-patching is the safe, expected fix."""
from __future__ import annotations

from pathlib import Path

from .patcher import OFF, patch_state

MODES = ("off", "ask", "auto")


def needs_repair(game_dir: Path, registry, state: str | None = None) -> bool:
    if (state or patch_state(game_dir)) != OFF:
        return False
    rec = registry.data.get("patch") or {}
    patched, unpatched = rec.get("last_patch_at"), rec.get("last_unpatch_at") or 0
    return isinstance(patched, (int, float)) and patched > unpatched


def decide(mode: str, game_running: bool) -> str:
    """'none' | 'notify' | 'patch': what to do about a detected game update."""
    if mode == "off":
        return "none"
    if mode == "auto" and not game_running:
        return "patch"
    return "notify"
