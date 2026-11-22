"""State and rules for the main window: which rows to show, in what order, with which text and colour.

Every rule is unit-testable without a display; the web session sends what `ModListModel.rows()` returns to the page."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from cogitator import actions
from cogitator.mods import DISABLED, ENABLED, UNLISTED, Mod
from cogitator.updates import FAILED, PROBABLY_OUTDATED, PROBABLY_UP_TO_DATE, UP_TO_DATE, UPDATE, ModRow
from cogitator.viewtext import SORT_COLUMNS, installed_label, mod_suffix, status_label

ARROW = "→"  # the Windows UI fonts have it; the terminal UI uses "->" where it cannot be sure
ON_TEXT, OFF_TEXT, UNLISTED_TEXT = "On", "Off", "-"

TAG_BAD, TAG_WARN, TAG_OK, TAG_DIM = "t_bad", "t_warn", "t_ok", "t_dim"
STRIPES = ("even", "odd")


@dataclass(frozen=True)
class RowView:
    folder: str
    cells: tuple[str, str, str, str, str]  # On/Off, Mod, Installed, Latest, Status
    tags: tuple[str, ...]


class ModListModel:
    """The mod table: scanned mods, cached update rows, marks, filter and sort. `meta` returns a mod's registry record."""

    def __init__(self, meta: Callable[[str], dict] = lambda f: {}, mtime: Callable[[str], float | None] = lambda f: None):
        self.meta = meta
        self.mtime = mtime  # folder -> last-modified time, for the "Installed" column of mods Cogitator knows nothing about
        self.mods: list[Mod] = []
        self.urows: dict[str, ModRow] = {}
        self.marked: set[str] = set()
        self.filter_text = ""
        self.sort_col: str | None = None  # None = load order
        self.sort_desc = False

    # ---- data in
    def set_mods(self, mods: Iterable[Mod]) -> None:
        self.mods = list(mods)
        present = {m.folder for m in self.mods}
        self.marked &= present

    def set_rows(self, rows: Iterable[ModRow]) -> None:
        self.urows = {r.folder: r for r in rows}

    def apply_row(self, row: ModRow) -> None:
        self.urows[row.folder] = row

    def mod(self, folder: str | None) -> Mod | None:
        return next((m for m in self.mods if m.folder == folder), None)

    # ---- filter, marks, sort
    def visible(self, m: Mod) -> bool:
        return actions.mod_visible(m.folder, self.meta(m.folder), self.filter_text)

    def toggle_mark(self, folder: str) -> bool:
        self.marked.symmetric_difference_update({folder})
        return folder in self.marked

    def set_sort(self, col: str | None) -> None:
        """A header click: the same column again flips the direction; a new column sorts ascending; None = load order."""
        if col is None or col not in SORT_COLUMNS:
            self.sort_col, self.sort_desc = None, False
        elif col == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, False

    def cycle_sort(self) -> str | None:
        """The O key: load order, then each column in turn (same order as the terminal UI)."""
        i = -1 if self.sort_col is None else SORT_COLUMNS.index(self.sort_col)
        nxt = (i + 1) % len(SORT_COLUMNS)
        self.sort_col, self.sort_desc = SORT_COLUMNS[nxt], False
        return self.sort_col

    # ---- row text
    def name_cell(self, m: Mod) -> str:
        meta = self.meta(m.folder)
        prefix = ("+ " if m.folder in self.marked else "") + ("* " if meta.get("fav") else "")
        suffix = mod_suffix(m)
        pinned = f"  [pinned {meta['pinned_version']}]" if meta.get("pinned_version") else ""
        return prefix + m.folder + pinned + (suffix[0] if suffix else "")

    def cells(self, m: Mod) -> tuple[str, str, str, str, str]:
        r = self.urows.get(m.folder)
        mt = None if r is not None and r.installed else self.mtime(m.folder)
        en = ON_TEXT if m.state == ENABLED else (OFF_TEXT if m.state == DISABLED else UNLISTED_TEXT)
        return (en, self.name_cell(m), installed_label(r, mt)[0], (r.latest if r else None) or "-", status_label(r, ARROW, m, gui=True)[0])

    def status_tag(self, m: Mod) -> str:
        r = self.urows.get(m.folder)
        if not m.present or r is not None and r.status == FAILED:
            return TAG_BAD
        if r is not None and r.status in (UPDATE, PROBABLY_OUTDATED):
            return TAG_WARN
        if m.state != ENABLED:
            return TAG_DIM
        if r is not None and r.status in (UP_TO_DATE, PROBABLY_UP_TO_DATE):
            return TAG_OK
        return ""

    def _sort_key(self, m: Mod, cells: tuple[str, ...]):
        col = self.sort_col
        text = m.folder if col == "mod" else cells[SORT_COLUMNS_INDEX[col]]
        return text.casefold()

    def rows(self, keep: str | None = None) -> list[RowView]:
        """Rows to show, in order. A mod being kept selected stays in the list even if the filter hides it."""
        shown = [m for m in self.mods if self.visible(m) or m.folder == keep]
        built = [(m, self.cells(m)) for m in shown]
        if self.sort_col is not None:
            built.sort(key=lambda mc: self._sort_key(*mc), reverse=self.sort_desc)
        return [RowView(m.folder, c, (self.status_tag(m), STRIPES[i % 2])) for i, (m, c) in enumerate(built)]

    # ---- texts
    def hint(self, folder: str | None) -> str:
        from cogitator.viewtext import hint_for

        if not folder:
            return hint_for(None, None, gui=True)
        return hint_for(self.mod(folder), self.urows.get(folder), gui=True) + actions.meta_hint(self.meta(folder))

    def summary(self) -> str:
        shown = sum(1 for m in self.mods if self.visible(m))
        n_upd = sum(1 for r in self.urows.values() if r.status == UPDATE)
        bits = [f"{len(self.mods)} mod(s)"]
        if shown != len(self.mods):
            bits.append(f"{shown} shown")
        if n_upd:
            bits.append(f"{n_upd} update(s)")
        if self.marked:
            bits.append(f"{len(self.marked)} marked")
        return ", ".join(bits)

    def listed(self) -> list[str]:
        """Folders in the load order (the ones Move up/down and drag act on)."""
        return [m.folder for m in self.mods if m.state != UNLISTED]


SORT_COLUMNS_INDEX = {"en": 0, "mod": 1, "installed": 2, "latest": 3, "status": 4}


def move_steps(listed: list[str], src: str, dst: str) -> int | None:
    """How many single moves take `src` to where `dst` is in the load order (negative = up). None: `src` is not listed."""
    if src not in listed:
        return None
    return (listed.index(dst) if dst in listed else len(listed) - 1) - listed.index(src)
