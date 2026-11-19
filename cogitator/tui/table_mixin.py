"""Mod table, menus, themes, drag-reorder and sorting for the interface."""
from __future__ import annotations

import logging

import sys
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual.widgets import DataTable, Static

from .. import actions
from ..config import cache_dir
from ..linking import folder_mtime
from ..modinstall import (open_files_page)
from ..mods import Mod, mods_dir, ENABLED, DISABLED, UNLISTED, scan_mods, move_mod, ModError
from ..updates import (FrameworkUpdate, ModRow, UPDATE, PROBABLY_OUTDATED, cached_rows)
from .screens import ChoiceScreen, ConfirmScreen, MenuScreen
from .widgets import ModTable, TitleButton

log = logging.getLogger("cogitator.tui")
from ..i18n import tr  # noqa: E402
from .view import (EMPTY_STATE, SORT_COLUMNS, THEME_CHOICES, enabled_cell, installed_text, status_text, hint_for)  # noqa: E402


class TableMixin:
    """Methods of CogitatorApp; they use its attributes, so it is only meant to be mixed into it."""

    def refresh_all(self, keep: str | None = None) -> None:
        """Rescan mods and rebuild the table from cache (no network)."""
        if not self.game_dir or not self.registry:
            return
        keep = keep or self.selected()
        self.mods = scan_mods(self.game_dir)
        folders = [m.folder for m in self.mods if m.present]
        self.urows = {r.folder: r for r in cached_rows(folders, self.registry)}
        t = self.query_one("#mods", DataTable)
        t.clear()
        for m in self.mods:
            if self._visible(m) or m.folder == keep:
                self._add_row(t, m)
        if keep:
            try:
                t.move_cursor(row=t.get_row_index(keep))
            except Exception:
                pass
        if self._sort_i >= 0:
            self._apply_sort()
        self._update_hint()
        self.refresh_header()
        self.call_after_refresh(self._fit_columns)

    def _add_row(self, t: DataTable, m: Mod) -> None:
        r = self.urows.get(m.folder)
        t.add_row(enabled_cell(m, self.glyphs), self._mod_cell(m), self._installed_cell(m.folder, r),
                  Text((r.latest if r else None) or "-"), status_text(r, self.glyphs, m), key=m.folder)

    def _fit_columns(self) -> None:
        """One very long mod name must not push Installed / Latest / Status off a narrow console."""
        try:
            t = self.query_one("#mods", DataTable)
            col = t.columns.get("mod")
            if col is None or not t.size.width:
                return
            others = sum(c.get_render_width(t) for k, c in t.columns.items() if k != "mod")
            want = max(len(m.folder) + 24 for m in self.mods) if self.mods else 16  # name plus its "(not in load order)" tag
            avail = max(16, t.size.width - others - 2 * t.cell_padding - 2)
            width = min(want, avail)
            if col.auto_width or col.width != width:
                col.auto_width, col.width = False, width
                t._require_update_dimensions = True
                t.refresh(layout=True)
        except Exception:
            logging.getLogger("cogitator.tui").debug("column fit failed", exc_info=True)

    def on_resize(self, event) -> None:
        self.call_after_refresh(self._fit_columns)

    def _installed_cell(self, folder: str, r: ModRow | None) -> Text:
        m = None if r is not None and r.installed else folder_mtime(mods_dir(self.game_dir) / folder)
        return installed_text(r, m)

    def _mod(self, folder: str | None) -> Mod | None:
        return next((m for m in self.mods if m.folder == folder), None)

    def selected(self) -> str | None:
        try:
            t = self.query_one("#mods", DataTable)
            if t.row_count == 0:
                return None
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None

    def _update_hint(self) -> None:
        f = self.selected()
        self.query_one("#hint", Static).update(hint_for(self._mod(f), self.urows.get(f)) + self._meta_hint(f) if f else tr("empty.state", EMPTY_STATE))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._update_hint()

    def _apply_row(self, r: ModRow) -> None:
        """In-place update of one row as its result arrives."""
        self.urows[r.folder] = r
        t = self.query_one("#mods", DataTable)
        m = self._mod(r.folder)
        try:
            t.update_cell(r.folder, "installed", self._installed_cell(r.folder, r))  # Text: versions are never markup
            t.update_cell(r.folder, "latest", Text(r.latest or "-"))
            t.update_cell(r.folder, "status", status_text(r, self.glyphs, m))
        except Exception:
            return  # row not in the table (e.g. folder removed meanwhile)
        if self.selected() == r.folder:
            self._update_hint()

    def _apply_fw(self, u: FrameworkUpdate) -> None:
        self.fw_updates = [x for x in self.fw_updates if x.kind != u.kind] + [u]
        self.refresh_header()

    def _refresh_mod_row(self, folder: str) -> None:
        self.mods = scan_mods(self.game_dir)
        m = self._mod(folder)
        t = self.query_one("#mods", DataTable)
        if m is None:
            return
        try:
            t.update_cell(folder, "en", enabled_cell(m, self.glyphs))
            t.update_cell(folder, "mod", self._mod_cell(m))
        except Exception:
            self.refresh_all(keep=folder)
        self._update_hint()

    # -------------------------------------------------------------- mouse: title bar, menus, right-click, drag
    def on_title_button_pressed(self, msg: TitleButton.Pressed) -> None:
        if len(self.screen_stack) > 1:
            return
        bid = msg.button.id
        if bid.startswith("ab_"):
            act = bid[3:]
            if act == "quit":
                act = "quit_app"
            if self.check_action(act, ()):
                self.call_later(getattr(self, f"action_{act}"))
            return
        if bid == "tb_menu":
            self.action_app_menu()
        elif bid == "tb_help":
            self.action_help()
        elif bid == "tb_close":
            self.action_quit_app()

    def _open_menu(self, items: list, x: int, y: int, title: str, then: Callable[[str], None]) -> None:
        def chosen(aid):
            if aid:
                self.call_later(then, aid)

        self.push_screen(MenuScreen(items, x, y, title), chosen)

    def action_app_menu(self) -> None:
        if len(self.screen_stack) > 1:
            return
        have_game = bool(self.game_dir and self.registry)
        upd = bool(self.update_ready() or self._update_offer())
        items = [(tr("menu.import_a_mod_or_framework_zip", "Import a mod or framework zip...   I"), "import_archive", have_game),
                 (tr("menu.check_for_updates", "Check for updates   R"), "recheck", have_game),
                 (tr("menu.update_all_mods", "Update all mods   Ctrl+U"), "update_all", have_game),
                 (tr("menu.update_mod_loader_framework", "Update Mod Loader / Framework   F"), "framework_update", have_game),
                 (tr("menu.link_all_mods_to_nexus", "Link all mods to Nexus   M"), "link_all", have_game),
                 None,
                 (tr("menu.mods_on_off_game_patch", "Mods on / off (game patch)   T"), "toggle_mods", have_game),
                 (tr("menu.nexus_api_key", "Nexus API key...   K"), "set_key", True),
                 (tr("menu.doctor_diagnose_problems", "Doctor (diagnose problems)   D"), "doctor", True),
                 (tr("menu.profiles", "Profiles...   P"), "profiles", have_game),
                 (tr("menu.launch_darktide", "Launch Darktide   G"), "launch", have_game),
                 (tr("menu.crash_log_leads", "Crash-log leads   C"), "crashlog", have_game),
                 (tr("menu.find_a_crashing_mod_bisect", "Find a crashing mod (bisect)..."), "bisect", have_game),
                 (tr("menu.filter_the_list", "Filter the list   /"), "filter", have_game),
                 (tr("menu.search_nexus", "Search Nexus Mods...   F3"), "search", have_game),
                 (tr("menu.sort_the_list", "Sort the list   O"), "cycle_sort", have_game),
                 (tr("menu.theme_colours", "Theme (colours)..."), "choose_theme", True)]
        if upd:
            items += [None, (tr("menu.update_cogitator", "Update Cogitator   A"), "apply_update", True)]
        items += [None, (tr("menu.help", "Help   ?"), "help", True), (tr("menu.quit", "Quit   Q"), "quit_app", True)]
        self._open_menu(items, 0, 1, "Cogitator", lambda aid: getattr(self, f"action_{aid}")())

    def action_choose_theme(self) -> None:
        if len(self.screen_stack) > 1:
            return
        names = [n for n in THEME_CHOICES if n in self.available_themes]
        cur = names.index(self.theme) if self.theme in names else 0

        def chosen(i):
            if i is None:
                return
            self.theme = names[i]
            self.settings.theme = names[i]
            try:
                self.settings.save()
            except OSError as e:
                self.log_line(f"Could not save the theme: {e}", "bold yellow")
            self.log_line(f"Theme: {names[i]}", "dim")

        self.push_screen(ChoiceScreen("Choose a theme", names, selected=cur), chosen)

    def on_mod_table_context_requested(self, msg: ModTable.ContextRequested) -> None:
        if len(self.screen_stack) > 1 or not self._need_game():
            return
        f = msg.folder
        m, row = self._mod(f), self.urows.get(f)
        if m is None:
            return
        listed = m.state != UNLISTED
        can_update = row is not None and row.status in (UPDATE, PROBABLY_OUTDATED)
        linked = bool(row is not None and row.nexus_id)
        items = [("Disable" if m.state == ENABLED else ("Enable" if m.state == DISABLED else "Add to load order and enable"),
                  "toggle_mod", m.present),
                 ("Move up", "move_up", listed and m.present), ("Move down", "move_down", listed and m.present), None,
                 ("Update" + (f" to {row.latest}" if can_update and row.latest else ""), "update_selected", can_update),
                 ("Open Nexus page" if linked else "Link to Nexus...", "open_page" if linked else "link", m.present),
                 ("Open folder in Explorer" if sys.platform == "win32" else "Open folder", "open_folder", m.present), None,
                 ("Remove...", "remove", True)]
        meta = self._meta(f)
        extra = [("Unmark" if f in self.marked else "Mark for bulk actions", "toggle_mark", True),
                 ("Remove favourite" if meta.get("fav") else "Favourite", "toggle_fav", True),
                 ("Tags and note...", "edit_note", True)]
        if linked:
            extra += [("Show changelog", "changelog", True), ("Unpin version" if meta.get("pinned_version") else "Pin version", "pin", True)]
        items[len(items) - 2:len(items) - 2] = extra + [None]
        if (self.registry.mod(f) or {}).get("previous"):
            items.insert(len(items) - 2, ("Roll back last update", "rollback", m.present))
        self._open_menu(items, msg.x, msg.y, f, lambda aid, f=f: self._mod_menu_action(f, aid))

    def _mod_menu_action(self, folder: str, aid: str) -> None:
        if self._mod(folder) is None:
            return
        self._keep_cursor(folder)
        if aid == "open_folder":
            self._open_folder(mods_dir(self.game_dir) / folder)
        elif aid == "open_page":
            row = self.urows.get(folder)
            if row is not None and row.nexus_id:
                self.log_line(f"Opened {open_files_page(row.nexus_id)}", "cyan")
        elif aid == "changelog":
            self._show_changelog(folder)
        elif aid == "pin":
            self._toggle_pin(folder)
        elif aid in ("toggle_mark", "toggle_fav", "edit_note"):
            getattr(self, f"action_{aid}")()
        elif aid == "rollback":
            self._bg(self._job_rollback, folder, name="roll back", group="install", exclusive=True)
        elif aid == "remove":
            self.push_screen(ConfirmScreen(f"Remove {folder}?",
                                           "The mod is taken out of the load order and its folder is moved (not deleted) to "
                                           f"{cache_dir() / 'removed'} so you can restore it.", default_yes=False),
                             lambda ok, f=folder: ok and self._bg(self._job_remove, f, name="remove mod", group="install", exclusive=True))
        else:
            getattr(self, f"action_{aid}")()

    def _open_folder(self, path: Path) -> None:
        import os
        import subprocess

        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606 - opens the folder in Explorer
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        except OSError as e:
            self.log_line(f"Could not open {path}: {e}", "bold red")

    def _job_rollback(self, folder: str) -> None:
        self._apply_outcome(actions.roll_back(self._actx(), self._tui_ui(), folder))

    def _job_remove(self, folder: str) -> None:
        self._apply_outcome(actions.remove_one(self._actx(), self._tui_ui(), folder))

    def _apply_outcome(self, out) -> None:
        if out is not None and out.refresh:
            self._ui(self._after_registry_change, out.folder)

    def on_mod_table_dragging(self, msg: ModTable.Dragging) -> None:
        if msg.over != msg.folder:
            self.query_one("#hint", Static).update(f"Drop to move '{msg.folder}' to where '{msg.over}' is in the load order.")

    def on_mod_table_dropped(self, msg: ModTable.Dropped) -> None:
        if not self._need_game():
            return
        src, dst = msg.folder, msg.target
        listed = [m.folder for m in self.mods if m.state != UNLISTED]
        if src not in listed:
            self.log_line(f"{src} is not in the load order yet. Press Space to add it first.", "bold yellow")
            self._update_hint()
            return
        steps = (listed.index(dst) if dst in listed else len(listed) - 1) - listed.index(src)
        last = f"{src}: stays where it is."
        from .. import snapshots

        snapshots.auto(self.game_dir, "reorder", once=True, registry=self.registry)
        try:
            for _ in range(abs(steps)):
                last = move_mod(self.game_dir, src, 1 if steps > 0 else -1)
            self.log_line(last, "green")
        except ModError as e:
            self.log_line(str(e), "bold red")
        except OSError as e:
            self.log_line(f"Could not write mod_load_order.txt: {e}", "bold red")
        self._sort_i = -1  # show the load order again so the move is visible
        self.refresh_all(keep=src)
        self.call_after_refresh(lambda: self._keep_cursor(src))

    def _keep_cursor(self, folder: str) -> None:
        try:
            t = self.query_one("#mods", DataTable)
            t.move_cursor(row=t.get_row_index(folder))
        except Exception:
            pass

    # -------------------------------------------------------------- sorting
    def action_cycle_sort(self) -> None:
        self._sort_i = (self._sort_i + 1) % len(SORT_COLUMNS)
        self._apply_sort()
        self.log_line(f"Sorted by {SORT_COLUMNS[self._sort_i]}", "dim")

    def _apply_sort(self) -> None:
        col = SORT_COLUMNS[self._sort_i]
        self.query_one("#mods", DataTable).sort(col, key=lambda c: (c.plain if isinstance(c, Text) else str(c)).casefold())

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        if event.column_key.value in SORT_COLUMNS:
            self._sort_i = SORT_COLUMNS.index(event.column_key.value)
            self._apply_sort()
