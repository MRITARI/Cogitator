"""Doctor checks about the mods themselves: mods that replace the same game function, risky content, and edits made outside
Cogitator. All heuristic: they warn, never block."""
from __future__ import annotations

from pathlib import Path

from . import integrity
from .doctor import OK, WARN, Check
from .mods import ENABLED, mods_dir, scan_mods
from .modfile import ModInfo, analyze_mod


def _trim(items, n: int = 6) -> str:
    items = list(items)
    return ", ".join(items[:n]) + (f" (+{len(items) - n} more)" if len(items) > n else "")


def run(game: Path, current_build: str | None = None) -> list[Check]:
    mods = [m for m in scan_mods(game) if m.present]
    if not mods:
        return [Check("mod_checks", "Mod checks", OK, "no mods installed")]
    infos: dict[str, ModInfo] = {m.folder: analyze_mod(mods_dir(game) / m.folder) for m in mods}
    out: list[Check] = []

    # two mods replacing the same game function
    owners: dict[str, list[str]] = {}
    for m in mods:
        if m.state == ENABLED:
            for t in infos[m.folder].hook_origins:
                owners.setdefault(t, []).append(m.folder)
    clash = {t: o for t, o in owners.items() if len(o) > 1}
    if clash:
        out.append(Check("mod_hooks", "Mods replacing the same function", WARN,
                         "; ".join(f"{' and '.join(o)} both replace {t}" for t, o in list(clash.items())[:5]),
                         "Only one hook_origin can win. Disable one of them (this is a heuristic: check the mods' pages)."))
    else:
        out.append(Check("mod_hooks", "Mods replacing the same function", OK, "no overlapping hook_origin targets found"))

    # content that can run code outside the game's Lua sandbox
    risky = []
    for f, i in infos.items():
        bits = [f"file {x}" for x in i.executables[:3]] + [f"{k} in {v[0]}" for k, v in i.risky.items()]
        if bits:
            risky.append(f"{f}: {', '.join(bits)}")
    if risky:
        out.append(Check("mod_risky", "Mod content to review", WARN, "; ".join(risky[:5]) + (" ..." if len(risky) > 5 else ""),
                         "Not proof of anything bad, but these mods can reach outside the game. Keep only mods you trust."))
    else:
        out.append(Check("mod_risky", "Mod content to review", OK, "no executables or process/library calls found"))

    # edits made outside Cogitator
    changed = integrity.check(game)
    if changed:
        out.append(Check("mod_integrity", "Mods changed since install", WARN, _trim(sorted(changed)),
                         "If you edited them on purpose: cogitator mod rebaseline --all. Otherwise reinstall the mod."))
    else:
        n = len(integrity.load())
        out.append(Check("mod_integrity", "Mods changed since install", OK,
                         f"{n} mod(s) match what Cogitator installed" if n else "no baselines yet (taken when Cogitator installs or updates a mod)"))
    stale = integrity.stale_build(game, current_build)
    if stale:
        out.append(Check("mod_build", "Mods older than the game build", WARN, _trim(stale),
                         "The game was updated after these mods were installed. Check for updates (R); an old mod can break after a game patch."))

    return out
