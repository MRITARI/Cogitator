"""Doctor checks about the mods themselves: declared dependencies and their order, mods that replace the same game function,
risky content, edits made outside Cogitator, and the signed known-conflicts list. All heuristic: they warn, never block."""
from __future__ import annotations

from pathlib import Path

from . import integrity
from .doctor import OK, WARN, Check
from .mods import ENABLED, mods_dir, scan_mods
from .modfile import ModInfo, analyze_mod

_FRAMEWORK_NAMES = {"dmf", "dml", "base", "darktide mod framework"}


def _trim(items, n: int = 6) -> str:
    items = list(items)
    return ", ".join(items[:n]) + (f" (+{len(items) - n} more)" if len(items) > n else "")


def run(game: Path, current_build: str | None = None, known: list[dict] | None = None) -> list[Check]:
    mods = [m for m in scan_mods(game) if m.present]
    if not mods:
        return [Check("mod_checks", "Mod checks", OK, "no mods installed")]
    order = {m.folder.casefold(): i for i, m in enumerate(mods) if m.state != "unlisted"}
    state = {m.folder.casefold(): m.state for m in mods}
    infos: dict[str, ModInfo] = {m.folder: analyze_mod(mods_dir(game) / m.folder) for m in mods}
    ident: dict[str, str] = {}  # every name a mod answers to -> its folder
    for f, i in infos.items():
        ident[f.casefold()] = f
        for n in i.names:
            ident.setdefault(n.casefold(), f)
    out: list[Check] = []

    # declared dependencies must be installed, enabled and loaded first
    problems, soft = [], []
    for m in mods:
        if m.state != ENABLED:
            continue
        info = infos[m.folder]
        for kind, names in (("requires", info.requires), ("looks up", info.references)):
            for ref in sorted(names):
                if ref.casefold() in _FRAMEWORK_NAMES:
                    continue
                target = ident.get(ref.casefold())
                if kind == "requires":
                    if target is None:
                        problems.append(f"{m.folder} requires '{ref}', which is not installed")
                    elif state[target.casefold()] != ENABLED:
                        problems.append(f"{m.folder} requires {target}, which is {state[target.casefold()]}")
                    elif order.get(target.casefold(), 0) > order.get(m.folder.casefold(), 0):
                        problems.append(f"{m.folder} requires {target}, which loads after it")
                elif target and state[target.casefold()] == ENABLED and order.get(target.casefold(), 0) > order.get(m.folder.casefold(), 0):
                    soft.append(f"{m.folder} looks up {target}, which loads after it")
    if problems:
        out.append(Check("mod_deps", "Mod dependencies", WARN, "; ".join(problems[:6]) + (" ..." if len(problems) > 6 else ""),
                         "Install or enable the missing mod and move it above the one that needs it."))
    else:
        note = f" (note, may be harmless if they check for nil: {_trim(soft, 3)})" if soft else ""
        out.append(Check("mod_deps", "Mod dependencies", OK, "no declared dependency problems" + note))

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

    # signed known-conflicts list
    hits = []
    for e in known or []:
        a, b = (x.casefold() for x in e["mods"])
        if state.get(a) == ENABLED and state.get(b) == ENABLED:
            if e["kind"] == "conflict":
                hits.append(f"{e['mods'][0]} + {e['mods'][1]}: {e['note']}")
            elif order.get(a, 0) > order.get(b, 0):
                hits.append(f"{e['mods'][0]} must load before {e['mods'][1]}: {e['note']}")
    if hits:
        out.append(Check("mod_known", "Known conflicts", WARN, "; ".join(hits[:5]), "See the notes; disable one mod or fix the order."))
    elif known is not None:
        out.append(Check("mod_known", "Known conflicts", OK, f"none of the {len(known)} listed pairs is present"))
    return out
