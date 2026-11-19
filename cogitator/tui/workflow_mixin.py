"""Workflow features for the interface: launch, game-update repair and notifications, bisect, crash-log leads, marks and
bulk actions, notes/tags/favourites, filter, pinning and changelogs."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text

from .. import actions, actions_nexus, bisect, crashlog, gamefinder, nxm, repair
from ..config import cache_dir, get_api_key
from ..doctor import console_log_dirs
from ..loadorder import LoadOrder
from ..mods import ModError, load_order_path, set_mod_enabled
from ..nexus import NexusClient, NexusError
from ..patcher import OFF, PatcherError, patch, patch_state
from ..updates import UPDATE
from .screens import ChoiceScreen, ConfirmScreen, InputScreen
from .view import mod_cell


class WorkflowMixin:
    """Methods of CogitatorApp; they use its attributes, so it is only meant to be mixed into it."""

    if TYPE_CHECKING:  # provided by CogitatorApp
        _ui: Any
        log_line: Any

    # ------------------------------------------------------------ row display, marks, filter
    def _meta(self, folder: str) -> dict:
        return (self.registry.mod(folder) if self.registry else None) or {}

    def _mod_cell(self, m) -> Text:
        t = mod_cell(m)
        meta = self._meta(m.folder)
        prefix = ("+ " if m.folder in self.marked else "") + ("* " if meta.get("fav") else "")
        return Text(prefix) + t if prefix else t

    def _visible(self, m) -> bool:
        return actions.mod_visible(m.folder, self._meta(m.folder), self.filter_text)

    def _meta_hint(self, folder: str) -> str:
        return actions.meta_hint(self._meta(folder))

    def action_toggle_mark(self) -> None:
        f = self.selected()
        if not (self._need_game() and f):
            return
        self.marked.symmetric_difference_update({f})
        self._refresh_mod_row(f)
        self.log_line(f"{len(self.marked)} mod(s) marked. Press B to act on them.", "dim")

    def action_filter(self) -> None:
        if self._need_game():
            self.push_screen(InputScreen("Filter mods", "name, tag, note, 'fav' or 'pinned' (empty = show all)", allow_blank=True),
                             self._filter_entered)

    def _filter_entered(self, text: str | None) -> None:
        if text is None:
            return
        self.filter_text = text
        self.refresh_all()
        shown = sum(1 for m in self.mods if self._visible(m))
        self.log_line(f"Filter '{text}': showing {shown} of {len(self.mods)} mod(s)." if text else "Filter cleared.", "dim")

    # ------------------------------------------------------------ notes, tags, favourites, pinning
    def action_edit_note(self) -> None:
        f = self.selected()
        if self._need_game() and f:
            self.push_screen(InputScreen(f"Tags and note for {f}", "#ui #hud  free text note (empty = clear)", allow_blank=True),
                             lambda t, f=f: self._note_entered(f, t))

    def _note_entered(self, folder: str, text: str | None) -> None:
        if text is None:
            return
        msg = actions.set_note(self.registry, folder, text)
        self._refresh_mod_row(folder)
        self.log_line(msg, "green")

    def action_toggle_fav(self) -> None:
        f = self.selected()
        if not (self._need_game() and f):
            return
        msg = actions.toggle_fav(self.registry, f)
        self._refresh_mod_row(f)
        self.log_line(msg, "green")

    def _toggle_pin(self, folder: str) -> None:
        self.log_line(actions.toggle_pin(self.registry, folder), "green")
        self._after_registry_change(folder)

    def _show_changelog(self, folder: str) -> None:
        self._bg(self._job_changelog, folder, name="changelog", group="net")

    def _job_changelog(self, folder: str) -> None:
        for text, style in actions_nexus.changelog_for(self.registry, folder).lines:
            self._ui(self.log_line, text, style)

    # ------------------------------------------------------------ bulk actions on marked mods
    def action_bulk(self) -> None:
        if not self._need_game():
            return
        if not self.marked:
            self.log_line("Mark mods with X first (shown with a +), then press B.", "yellow")
            return
        opts = [f"Enable {len(self.marked)} mod(s)", f"Disable {len(self.marked)} mod(s)", "Update them (Premium)",
                "Add a tag...", "Remove them...", "Clear marks"]
        self.push_screen(ChoiceScreen(f"{len(self.marked)} marked mod(s)", opts), self._bulk_choice)

    def _bulk_choice(self, i: int | None) -> None:
        marked = sorted(self.marked)
        if i is None or not marked:
            return
        if i in (0, 1):
            from .. import snapshots

            snapshots.auto(self.game_dir, "bulk", registry=self.registry, once=True)
            done = 0
            for f in marked:
                try:
                    set_mod_enabled(self.game_dir, f, i == 0)
                    done += 1
                except ModError as e:
                    self.log_line(str(e), "bold red")
            self.log_line(f"{'Enabled' if i == 0 else 'Disabled'} {done} mod(s).", "green")
            self.refresh_all()
        elif i == 2:
            todo = [f for f in marked if self.urows.get(f) is not None and self.urows[f].status == UPDATE]
            if not self._premium():
                self.log_line("Free account: updates need a download for each mod. Select one and press U.", "yellow")
            elif not todo:
                self.log_line("None of the marked mods has an update available.", "yellow")
            else:
                self._bg(self._job_premium, todo, name="update marked", group="install", exclusive=True)
        elif i == 3:
            self.push_screen(InputScreen("Tag the marked mods", "tag names separated by spaces"), self._bulk_tag)
        elif i == 4:
            self.push_screen(ConfirmScreen(f"Remove {len(marked)} mod(s)?", ", ".join(marked) + "\nTheir folders are moved (not deleted) so you can restore them.",
                                           default_yes=False),
                             lambda ok, m=marked: ok and self._bg(self._job_remove_many, m, name="remove marked", group="install", exclusive=True))
        else:
            self.marked.clear()
            self.refresh_all()

    def _bulk_tag(self, text: str | None) -> None:
        tags = [t.lstrip("#").lower()[:30] for t in (text or "").split() if t.lstrip("#")]
        if not tags:
            return
        for f in sorted(self.marked):
            cur = [t for t in self._meta(f).get("tags") or [] if isinstance(t, str)]
            self.registry.set_mod(f, tags=cur + [t for t in tags if t not in cur])
        self.log_line(f"Tagged {len(self.marked)} mod(s): {', '.join(tags)}", "green")
        self.refresh_all()

    def _job_remove_many(self, folders: list[str]) -> None:
        for f in folders:
            self._job_remove(f)
            self.marked.discard(f)
        self._ui(self.refresh_all)

    # ------------------------------------------------------------ launch
    def action_launch(self) -> None:
        if self._need_game():
            self._bg(self._job_launch, name="launch", group="launch", exclusive=True)

    def _job_launch(self) -> None:
        from ..launch import LaunchError, launch

        self._ui(self.log_line, "Checking the setup before launching...", "dim")
        try:
            with self._lock:
                r = launch(self.game_dir, self.registry, is_running=self.game_running, runner=self.patch_runner)
        except (LaunchError, ModError, OSError) as e:
            self._ui(self.log_line, str(e), "bold red")
            return
        for n in r.notes:
            self._ui(self.log_line, n, "yellow")
        self._ui(self.log_line, "Asked Steam to start Darktide.", "green")
        self._ui(self.refresh_header)

    # ------------------------------------------------------------ game-update watcher
    def _watch(self) -> None:
        """Every few seconds: notice the game being updated (mods switched off) and, per the auto_repair setting, tell the user
        or patch again."""
        if not (self.game_dir and self.registry) or len(self.screen_stack) > 1:
            return
        try:
            st = patch_state(self.game_dir)
        except Exception:
            return
        if st == self._last_patch_state:
            return
        first = self._last_patch_state is None
        self._last_patch_state = st
        if not first:
            self.refresh_header()
        if st != OFF or not repair.needs_repair(self.game_dir, self.registry):
            return
        act = repair.decide(self.settings.auto_repair, bool(self.game_running()))
        if act == "patch":
            self._bg(self._job_repair, name="repair", group="patch", exclusive=True)
        elif act == "notify":
            self.notify("The game was updated and your mods are switched off. Press T to turn them on again.",
                        title="Game updated", severity="warning", timeout=15)

    def _job_repair(self) -> None:
        try:
            with self._lock:
                r = patch(self.game_dir, self.registry, runner=self.patch_runner, is_running=self.game_running)
            self._ui(self.log_line, "Game was updated: " + r.message, "green" if r.changed else "yellow")
        except PatcherError as e:
            self._ui(self.log_line, f"Could not re-enable mods after the game update: {e}", "bold red")
        self._ui(self.refresh_header)

    # ------------------------------------------------------------ nxm:// links from the Nexus website
    def _nxm_tick(self) -> None:
        """Pick up links queued by the nxm handler. One at a time, and never while a dialog is open."""
        if self._nxm_busy or not (self.game_dir and self.registry) or len(self.screen_stack) > 1:
            return
        items = nxm.pending(on_expired=lambda lk: self._ui(
            self.log_line, f"Nexus link for mod {lk.mod_id} file {lk.file_id}: link expired. Click the button on the website again.", "yellow"))
        if items:
            self._nxm_busy = True
            path, link = items[0]
            self._bg(self._job_nxm, path, link, name="nxm link", group="install", exclusive=True)

    def _job_nxm(self, path, link) -> None:
        try:
            actions_nexus.nxm_install(self._actx(), self._tui_ui(), path, link, self._import_done)
        finally:
            self._nxm_busy = False

    # ------------------------------------------------------------ search Nexus
    def action_search(self) -> None:
        if self._need_game():
            self.push_screen(InputScreen("Search Nexus Mods", "part of a mod's name"), self._search_entered)

    def _search_entered(self, text: str | None) -> None:
        if text:
            self._bg(self._job_search, text, name="search", group="net", exclusive=True)

    def _job_search(self, text: str) -> None:
        actions_nexus.search_nexus(self._actx(), self._tui_ui(), text)

    # ------------------------------------------------------------ crash log leads
    def action_crashlog(self) -> None:
        if self._need_game():
            self._bg(self._job_crashlog, name="crash log", group="net")

    def _job_crashlog(self) -> None:
        log = crashlog.newest_log(console_log_dirs(gamefinder.steam_roots()))
        rep = crashlog.analyze(log, {m.folder for m in self.mods}) if log else crashlog.CrashReport()
        lo = LoadOrder.load(load_order_path(self.game_dir)).entries()
        for line in crashlog.format_report(rep, lo):
            self._ui(self.log_line, line, "yellow" if line.startswith("  ") else "")

    # ------------------------------------------------------------ bisect
    def action_bisect(self) -> None:
        if not self._need_game():
            return
        s = bisect.load()
        if s is None:
            self.push_screen(ConfirmScreen("Find a crashing mod?",
                                           "Cogitator will switch half of your enabled mods off, you start the game and see whether the "
                                           "problem is still there, and it narrows the list down. A snapshot is taken first and you can "
                                           "restore your load order at any time. Make sure the problem happens with all mods on.",
                                           default_yes=False),
                             lambda ok: ok and self._bisect_step("start"))
            return
        head = bisect.describe(s).splitlines()[0]
        opts = ["The problem still happens", "The problem is gone", "Stop and restore my load order", "Close"]
        self.push_screen(ChoiceScreen(head, opts), lambda i: self._bisect_step(("bad", "good", "reset")[i]) if i in (0, 1, 2) else None)

    def _bisect_step(self, what: str) -> None:
        try:
            if what == "start":
                s = bisect.start(self.game_dir, self.registry)
            elif what == "reset":
                for line in bisect.reset(self.game_dir, self.registry):
                    self.log_line(line, "green")
                self.refresh_all()
                return
            else:
                s = bisect.answer(self.game_dir, still_broken=what == "bad")
        except ModError as e:
            self.log_line(str(e), "bold red")
            return
        for line in bisect.describe(s).splitlines():
            self.log_line(line, "bold" if s.get("culprit") else "")
        self.refresh_all()
