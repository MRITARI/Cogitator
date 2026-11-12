"""UI glyphs with ASCII fallbacks for consoles that cannot encode them (legacy code pages, some CI/SSH terminals)."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Glyphs:
    on: str
    off: str
    unlisted: str
    arrow: str
    ellipsis: str
    spinner: tuple[str, ...]
    ascii: bool


UNICODE = Glyphs("✓", "✗", "·", "→", "…", tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"), False)
ASCII = Glyphs("[x]", "[ ]", ".", "->", "...", ("|", "/", "-", "\\"), True)


def _can_encode(encoding: str | None) -> bool:
    if not encoding:
        return False
    try:
        "".join(g for g in (UNICODE.on, UNICODE.off, UNICODE.unlisted, UNICODE.arrow, UNICODE.ellipsis)
                + UNICODE.spinner).encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def glyphs_for(encoding: str | None, force_ascii: bool | None = None) -> Glyphs:
    """ASCII when forced (COGITATOR_ASCII=1) or when `encoding` cannot represent every glyph we use."""
    if force_ascii is None:
        force_ascii = os.environ.get("COGITATOR_ASCII", "").strip() not in ("", "0", "false")
    return ASCII if force_ascii or not _can_encode(encoding) else UNICODE


def detect(console_encoding: str | None = None) -> Glyphs:
    return glyphs_for(console_encoding or getattr(sys.stdout, "encoding", None))
