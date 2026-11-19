"""Widget-free state and rules for the main window: which rows to show, in what order, with which text and colour.

Nothing in here imports tkinter, so every rule is unit-testable without a display. The window (`app`) only copies what
`ModListModel.rows()` returns into the Treeview."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from cogitator import actions
from cogitator.mods import DISABLED, ENABLED, UNLISTED, Mod
from cogitator.updates import FAILED, PROBABLY_OUTDATED, PROBABLY_UP_TO_DATE, UP_TO_DATE, UPDATE, ModRow
from cogitator.viewtext import SORT_COLUMNS, installed_label, mod_suffix, status_label

ARROW = "→"  # the Windows UI fonts have it; the terminal UI uses "->" where it cannot be sure
ON_TEXT, OFF_TEXT, UNLISTED_TEXT = "On", "Off", "-"

# (column id, heading, width in px, stretch, anchor)
COLUMNS = (("en", "On/Off", 64, False, "center"), ("mod", "Mod", 330, True, "w"), ("installed", "Installed", 110, False, "w"),
           ("latest", "Latest", 110, False, "w"), ("status", "Status", 300, True, "w"))

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


# ---------------------------------------------------------------- text styles (rich-style strings from the shared flows)

PALETTES = {
    "light": {"fg": "#1c1c1c", "bg": "#fafafa", "stripe": "#f1f1f1", "dim": "#616161", "green": "#17713a", "red": "#c62828",
              "yellow": "#8a5a00", "cyan": "#0b6e99", "black": "#000000", "white": "#ffffff", "log_bg": "#ffffff",
              "ok_bg": "#dff3e3", "ok_fg": "#0e5a24", "warn_bg": "#fff1c2", "warn_fg": "#6b4600", "bad_bg": "#fde0e2", "bad_fg": "#9b1c24"},
    "dark": {"fg": "#fafafa", "bg": "#1c1c1c", "stripe": "#252525", "dim": "#9a9a9a", "green": "#4cc26a", "red": "#ff7b7b",
             "yellow": "#f1c40f", "cyan": "#5ac8e8", "black": "#000000", "white": "#ffffff", "log_bg": "#161616",
             "ok_bg": "#17381f", "ok_fg": "#8be3a3", "warn_bg": "#44370f", "warn_fg": "#f7d56b", "bad_bg": "#4a1f22", "bad_fg": "#ffa3a3"},
}

# rich styles of the header's status words -> the badge (chip) look. Text stays the same; the colours come from the palette.
BADGE_OF_STYLE = {"green": "ok", "bold black on green": "ok", "bold red": "bad", "bold white on red": "bad",
                  "yellow": "warn", "bold yellow": "warn", "bold black on yellow": "warn"}


def badge_style(kind: str) -> str:
    return f"badge_{kind}"


def badgify(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Header segments -> the same text, with status words as padded badges (a one-space pad each side where not already there)."""
    out = []
    for text, style in segments:
        kind = BADGE_OF_STYLE.get(style)
        if kind and text.strip():
            text = " " + text.strip() + " " if style not in ("bold black on green", "bold white on red", "bold black on yellow") else text
            style = badge_style(kind)
        out.append((text, style))
    return out


def style_options(style: str, palette: dict[str, str]) -> dict[str, object]:
    """rich style ("bold black on green") -> {'foreground', 'background', 'bold'} for a Tk text tag; unknown words are ignored."""
    out: dict[str, object] = {}
    words = style.split()
    on = False
    for w in words:
        if w == "on":
            on = True
        elif w == "bold":
            out["bold"] = True
        elif w.startswith("badge_"):
            kind = w[6:]
            out.update(foreground=palette[kind + "_fg"], background=palette[kind + "_bg"], bold=True)
        elif w == "dim":
            out["foreground"] = palette["dim"]
        elif w in palette:
            out["background" if on else "foreground"] = palette[w]
            on = False
    return out


def style_tag(style: str) -> str:
    return "s_" + style.replace(" ", "_") if style else ""


# ---------------------------------------------------------------- theme

THEMES = ("light", "dark")


def normalize_theme(value: object) -> str:
    """Whatever the settings file holds -> a theme we have (light is the default)."""
    return value if isinstance(value, str) and value in PALETTES else "light"


def ttk_styles(palette: dict[str, str]) -> dict[str, dict[str, str]]:
    """ttk style options for one palette (applied with Style.configure). Widget-free so it can be tested."""
    p = palette
    return {
        ".": {"background": p["bg"], "foreground": p["fg"], "fieldbackground": p["bg"]},
        "TFrame": {"background": p["bg"]},
        "TLabel": {"background": p["bg"], "foreground": p["fg"]},
        "Hint.TLabel": {"background": p["bg"], "foreground": p["dim"]},
        "TButton": {"background": p["stripe"], "foreground": p["fg"]},
        "TEntry": {"fieldbackground": p["log_bg"], "foreground": p["fg"], "insertcolor": p["fg"]},
        "Treeview": {"background": p["bg"], "fieldbackground": p["bg"], "foreground": p["fg"]},
        "Treeview.Heading": {"background": p["stripe"], "foreground": p["fg"]},
        "TScrollbar": {"background": p["stripe"], "troughcolor": p["bg"]},
        "TPanedwindow": {"background": p["bg"]},
    }


def ttk_state_maps(palette: dict[str, str]) -> dict[str, dict[str, list]]:
    """Style.map options: selection and hover colours that the base theme would otherwise keep."""
    sel = "#2d5a8c" if palette is PALETTES["dark"] else "#cce4ff"
    return {"Treeview": {"background": [("selected", sel)], "foreground": [("selected", palette["fg"])]},
            "TButton": {"background": [("active", palette["stripe"])]}}
