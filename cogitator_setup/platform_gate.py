"""Cogitator Setup supports Windows 11 (build 22000 or later, x64) only. Anything else gets one message and a clean exit
with nothing changed."""
from __future__ import annotations

import sys

MIN_BUILD = 22000  # Windows 11 is 10.0.22000 and later
MESSAGE = "Cogitator Setup supports Windows 11 only"


def windows_build() -> int | None:
    """The Windows build number, or None when this is not Windows."""
    if sys.platform != "win32":
        return None
    try:
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return None


def is_supported(build: int | None) -> bool:
    return build is not None and build >= MIN_BUILD
