"""Mod/Nexus version ordering. One key function that always yields a totally ordered, type-homogeneous key, so sorting
never raises on tags like '1.2a' mixed with '1.10'. (App self-update versions have their own semver rules in selfupdate.)"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"[.\-_ ]+")
_RUNS = re.compile(r"\d+|[^\d]+")


def version_key(v: str | None) -> tuple[tuple[int, int | str], ...]:
    """'1.2a' -> ((0,1),(0,2),(1,'a')). Numbers sort before text at the same position; a longer key beats its prefix."""
    parts: list[tuple[int, int | str]] = []
    for chunk in _SPLIT.split((v or "").strip().lstrip("vV")):
        for run in _RUNS.findall(chunk):
            parts.append((0, int(run)) if run.isdigit() else (1, run))
    return tuple(parts)


def newer_versions(versions: list[str], installed: str | None) -> list[str]:
    """Versions newer than `installed`, newest first. Unknown installed -> the newest three; none newer -> just the newest."""
    ordered = sorted(versions, key=version_key, reverse=True)
    if not installed:
        return ordered[:3]
    k = version_key(installed)
    return [v for v in ordered if version_key(v) > k] or ordered[:1]
