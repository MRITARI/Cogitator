"""Window size and position, kept in `web_window.json` next to config.toml. config.toml is not used: `Settings.save()` writes only
its own fields, so an extra key there would be dropped (and its format is shared with the CLI and TUI, which stay untouched)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

from cogitator.config import config_dir

log = logging.getLogger("cogitator.web")

DEFAULT_SIZE = (1180, 760)
MIN_SIZE = (900, 560)
MAX_DIM = 10000


def state_path() -> Path:
    return config_dir() / "web_window.json"


def _int(v: object, lo: int, hi: int) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None


def sane_geometry(raw: object, screens: Iterable[tuple[int, int, int, int]] = ()) -> dict:
    """Whatever the file held -> {width, height[, x, y]} that is safe to use. The position is kept only when part of the title
    bar would still be on a known screen (a monitor may have been unplugged)."""
    raw = raw if isinstance(raw, dict) else {}
    w = _int(raw.get("width"), MIN_SIZE[0], MAX_DIM) or DEFAULT_SIZE[0]
    h = _int(raw.get("height"), MIN_SIZE[1], MAX_DIM) or DEFAULT_SIZE[1]
    out: dict = {"width": w, "height": h}
    x, y = _int(raw.get("x"), -32000, 32000), _int(raw.get("y"), -32000, 32000)
    scr = list(screens)
    if x is not None and y is not None and scr:
        if any(sx - w + 120 <= x <= sx + sw - 120 and sy <= y <= sy + sh - 60 for sx, sy, sw, sh in scr):
            out.update(x=x, y=y)
    return out


def load(screens: Iterable[tuple[int, int, int, int]] = ()) -> dict:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return sane_geometry(raw, screens)


def save(geometry: dict) -> None:
    keep = {k: geometry[k] for k in ("x", "y", "width", "height") if isinstance(geometry.get(k), int)}
    try:
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(keep), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        log.warning("could not save the window position: %s", e)
