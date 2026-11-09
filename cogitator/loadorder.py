"""Parse and edit mod_load_order.txt without disturbing anything but the touched line.

Line grammar (a heuristic, not DML's own parser):
  blank / whitespace-only        -> blank
  '--' + (end | whitespace | '-') -> comment        e.g. '-- notes', '--- header'
  '--' + token                    -> DISABLED mod   e.g. '--my_mod'
  '#...'                          -> comment
  token [anything]                -> ENABLED mod; text after the first token is preserved"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .fsutil import atomic_write_bytes

LOAD_ORDER_NAME = "mod_load_order.txt"
BACKUP_SUFFIX = ".cogitator.bak"

_EOL = re.compile(r"(\r\n|\n|\r)")
_backed_up: set[str] = set()  # paths backed up during this process ("first write of each run")


@dataclass
class Line:
    text: str  # without line ending
    eol: str  # '', '\n', '\r\n' (or lone '\r')

    @property
    def parsed(self) -> tuple[str, str | None, bool | None]:
        """(kind, name, enabled) with kind in blank|comment|mod."""
        s = self.text.strip()
        if not s:
            return "blank", None, None
        if s.startswith("--"):
            rest = s[2:]
            if not rest or rest[0].isspace() or rest[0] == "-":
                return "comment", None, None
            return "mod", rest.split()[0], False
        if s.startswith("#"):
            return "comment", None, None
        return "mod", s.split()[0], True

    @property
    def name(self) -> str | None:
        return self.parsed[1]

    def is_named(self, name: str) -> bool:
        """Windows folder names ignore case, so `Alpha` and `alpha` are the same mod."""
        mine = self.parsed[1]
        return mine is not None and mine.casefold() == name.casefold()

    @property
    def enabled(self) -> bool | None:
        return self.parsed[2]


class LoadOrder:
    def __init__(self, lines: list[Line], bom: bool = False, path: Path | None = None):
        self.lines = lines
        self.bom = bom
        self.path = path

    # ---- io
    @classmethod
    def parse(cls, data: bytes | str, path: Path | None = None) -> "LoadOrder":
        bom = False
        if isinstance(data, bytes):
            if data.startswith(b"\xef\xbb\xbf"):
                bom, data = True, data[3:]
            text = data.decode("utf-8", errors="surrogateescape")
        else:
            text = data
        parts = _EOL.split(text)  # text, eol, text, eol, ..., text
        lines: list[Line] = []
        for i in range(0, len(parts) - 1, 2):
            lines.append(Line(parts[i], parts[i + 1]))
        if parts[-1] != "":  # final line without terminator
            lines.append(Line(parts[-1], ""))
        return cls(lines, bom, path)

    @classmethod
    def load(cls, path: Path) -> "LoadOrder":
        try:
            return cls.parse(Path(path).read_bytes(), Path(path))
        except FileNotFoundError:
            return cls([], False, Path(path))

    def render(self) -> bytes:
        text = "".join(l.text + l.eol for l in self.lines)
        return (b"\xef\xbb\xbf" if self.bom else b"") + text.encode("utf-8", errors="surrogateescape")

    def save(self, path: Path | None = None, backup: bool = True) -> None:
        path = Path(path or self.path)  # type: ignore[arg-type]
        if backup and path.is_file() and str(path) not in _backed_up:
            bak = path.with_name(path.name + BACKUP_SUFFIX)
            if bak.is_file():  # keep the previous backup too: the newest one may already hold a mistake
                atomic_write_bytes(bak.with_name(bak.name + ".1"), bak.read_bytes())
            atomic_write_bytes(bak, path.read_bytes())
            _backed_up.add(str(path))
        atomic_write_bytes(path, self.render())

    # ---- queries
    @property
    def eol(self) -> str:
        """Dominant line ending (first one seen), default LF."""
        for l in self.lines:
            if l.eol:
                return l.eol
        return "\n"

    def entries(self) -> list[tuple[str, bool]]:
        """Mod entries in order: (name, enabled)."""
        return [(l.name, l.enabled) for l in self.lines if l.name]  # type: ignore[misc]

    def state(self, name: str) -> bool | None:
        """True enabled, False disabled, None unlisted. Duplicates: first wins."""
        for l in self.lines:
            if l.is_named(name):
                return l.enabled
        return None

    # ---- edits (touch only matching lines)
    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Returns True if any line changed. Unlisted -> False (use add)."""
        changed = False
        for l in self.lines:
            if not l.is_named(name) or l.enabled == enabled:
                continue
            indent = l.text[: len(l.text) - len(l.text.lstrip())]
            body = l.text.lstrip()
            l.text = indent + ("--" + body if not enabled else body[2:])
            changed = True
        return changed

    def remove(self, name: str) -> int:
        """Delete every line for `name` (enabled or disabled). Returns how many lines were removed."""
        keep = [l for l in self.lines if not l.is_named(name)]
        n = len(self.lines) - len(keep)
        self.lines = keep
        return n

    def add(self, name: str, enabled: bool = True) -> None:
        if self.lines and self.lines[-1].eol == "":
            self.lines[-1].eol = self.eol
        self.lines.append(Line(("" if enabled else "--") + name, self.eol))
