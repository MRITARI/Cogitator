"""Static, heuristic reading of a mod folder: what it names, which game functions it replaces,
and anything that could run code outside the game. Nothing here executes Lua, and every finding is a hint, not a verdict."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .fsutil import is_link

MAX_FILES = 3000
MAX_FILE_BYTES = 2 * 1024 * 1024

_BLOCK_COMMENT = re.compile(r"--\[(=*)\[.*?\]\1\]", re.S)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_NEW_MOD = re.compile(r"""\bnew_mod\(\s*["']([^"']{1,80})["']""")
_HOOK_ORIGIN = re.compile(r"""\bhook_origin\(\s*(?:["']([\w.]+)["']|([\w.]+))\s*,\s*["']([\w.:]+)["']""")
RISKY_PATTERNS = {
    "os.execute": re.compile(r"\bos\.execute\b"),
    "io.popen": re.compile(r"\bio\.popen\b"),
    "package.loadlib": re.compile(r"\bpackage\.loadlib\b"),
    "ffi.load/cdef": re.compile(r"\bffi\.(?:load|cdef)\b"),
}
EXECUTABLE_EXTS = {".dll", ".exe", ".bat", ".cmd", ".ps1", ".vbs", ".scr", ".msi", ".com", ".lnk"}


@dataclass
class ModInfo:
    folder: str
    names: set[str] = field(default_factory=set)  # what new_mod(...) registers
    hook_origins: set[str] = field(default_factory=set)  # "Class:method" targets it replaces outright
    risky: dict[str, list[str]] = field(default_factory=dict)  # pattern -> files
    executables: list[str] = field(default_factory=list)
    truncated: bool = False


def strip_lua_comments(text: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def analyze_mod(path: Path) -> ModInfo:
    info = ModInfo(path.name)
    if not path.is_dir() or is_link(path):
        return info
    n = 0
    for p in sorted(path.rglob("*")):
        if is_link(p) or not p.is_file():
            continue
        n += 1
        if n > MAX_FILES:
            info.truncated = True
            break
        rel = p.relative_to(path).as_posix()
        ext = p.suffix.lower()
        if ext in EXECUTABLE_EXTS:
            info.executables.append(rel)
            continue
        if ext not in (".lua", ".mod"):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                info.truncated = True
                continue
            text = strip_lua_comments(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        info.names.update(m.group(1) for m in _NEW_MOD.finditer(text))
        for m in _HOOK_ORIGIN.finditer(text):
            info.hook_origins.add(f"{m.group(1) or m.group(2)}:{m.group(3)}")
        for label, rx in RISKY_PATTERNS.items():
            if rx.search(text):
                info.risky.setdefault(label, []).append(rel)
    info.names.discard(path.name)
    return info
