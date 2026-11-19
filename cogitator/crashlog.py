"""Read the newest Darktide console log and point at the mods most likely to blame. Heuristic: it counts how often each
mod's folder is named in error lines. A mod named often is a lead, not proof (an error can pass through several mods)."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .doctor import _ERR, _MAX_LOG_BYTES

# Folder names may contain inner spaces ("My Cool Mod") but never start/end with one, so prose like "mods/ and /x" cannot match.
_MOD_PATH = re.compile(r"(?:scripts/mods|\.\./mods|mods)/([^/\\\s\"'\]:](?:[^/\\\"'\]:]*[^/\\\s\"'\]:])?)/", re.I)
_LOADING = re.compile(r"Loading \./\.\./mods/([^/\\\s](?:[^/\\]*[^/\\\s])?)/", re.I)
_INTERNAL = {"base", "dmf"}


@dataclass
class CrashReport:
    log: Path | None = None
    suspects: list[tuple[str, int]] = field(default_factory=list)
    samples: dict[str, str] = field(default_factory=dict)
    error_lines: int = 0
    last_loaded: str | None = None
    loaded: list[str] = field(default_factory=list)


def newest_log(dirs: list[Path]) -> Path | None:
    logs: list[Path] = []
    for d in dirs:
        try:
            logs += [p for p in d.glob("*.log") if p.is_file()]
        except OSError:
            continue
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def analyze(path: Path, installed: set[str] | None = None) -> CrashReport:
    rep = CrashReport(log=path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(_MAX_LOG_BYTES)
    except OSError:
        return rep
    known = {i.casefold(): i for i in installed} if installed else None
    counts: Counter[str] = Counter()
    for line in text.splitlines():
        m = _LOADING.search(line)
        if m and m.group(1).casefold() not in _INTERNAL:
            rep.loaded.append(m.group(1))
        if not _ERR.search(line) or "Loading ./" in line:
            continue
        mods = {x.group(1) for x in _MOD_PATH.finditer(line)}
        mods = {x for x in mods if x.casefold() not in _INTERNAL}
        if known is not None:
            mods = {known[x.casefold()] for x in mods if x.casefold() in known}
        if not mods:
            continue
        rep.error_lines += 1
        for x in mods:
            counts[x] += 1
            rep.samples.setdefault(x, line.strip()[:220])
    rep.suspects = counts.most_common(10)
    rep.last_loaded = rep.loaded[-1] if rep.loaded else None
    return rep


def format_report(rep: CrashReport, load_order: list[tuple[str, bool]] | None = None) -> list[str]:
    if rep.log is None:
        return ["No Darktide console logs found yet (start the game once)."]
    out = [f"Log: {rep.log.name}  ({rep.error_lines} error line(s) that name a mod)"]
    pos = {n.casefold(): i + 1 for i, (n, _) in enumerate(load_order or [])}
    if not rep.suspects:
        out.append("No mod is named in the errors. If the game crashes, try 'cogitator bisect start'.")
    for name, n in rep.suspects:
        where = f", load position {pos[name.casefold()]}" if name.casefold() in pos else ""
        out.append(f"  {name}: named in {n} error line(s){where}\n      e.g. {rep.samples[name]}")
    if rep.last_loaded:
        out.append(f"Last mod the log shows loading: {rep.last_loaded} (a crash right after it can point at the next mod in the load order).")
    out.append("These are leads, not proof. Disable the top suspect (Space), restart, and check; or run 'cogitator bisect start'.")
    return out
