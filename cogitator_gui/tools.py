"""Setup and housekeeping workflows of the main window: profiles, snapshots, bulk actions, bisect, crash-log leads, the game-update
watcher, the theme and Cogitator's own updates.

A mixin of `CogitatorGui`. Rules live in `cogitator.actions_setup` (shared with the terminal UI) and `cogitator.repair`; the
window's methods here ask what the UI thread can ask without blocking and start the jobs. Dialogs that need a list use
`Dialogs.pick_action`, which returns the pressed button so the window can act and show the list again."""
from __future__ import annotations

import logging
from typing import Callable

from cogitator import actions, actions_setup, profiles, repair, snapshots
from cogitator.mods import ModError
from cogitator.patcher import patch_state

from .viewmodel import PALETTES, normalize_theme, ttk_state_maps, ttk_styles

log = logging.getLogger("cogitator.gui")

PROFILE_BUTTONS = [("switch", "Switch to", True), ("save", "Save current setup...", False), ("delete", "Delete", True),
                   ("share", "Share code...", False), ("import", "Import code...", False)]
SNAPSHOT_BUTTONS = [("restore", "Restore", True), ("create", "Save snapshot now", False), ("prune", "Delete old ones...", False)]
WATCH_MS = 5000


class ToolFlows:
    def _log_lines(self, lines: list[tuple[str, str]]) -> None:
        for text, style in lines:
            self.log_line(text, style)

    def _job_lines(self, name: str, group: str, fn: Callable[[], list[tuple[str, str]]]) -> None:
        """Run `fn` on a worker; its (text, style) lines go to the log and the table is refreshed afterwards."""

        def job() -> None:
            for text, style in fn():
                self.ctl.log(text, style)
            self._after_outcome_ui(actions.Outcome(refresh=True))

        self.run_job(name, group, job)

    # ---------------------------------------------------------------- profiles
    def profiles_window(self) -> None:
        if not self.need_game():
            return
        sel = 0
        while True:
            try:
                rows = profiles.list_profiles()
            except OSError as e:
                self.log_line(f"Could not read profiles: {e}", "bold red")
                return
            res = self._modal_call(self.dlg.pick_action, "Profiles",
                                   "Profiles are named setups of your mods (which are on, and in what order).",
                                   actions_setup.profile_labels(rows), PROFILE_BUTTONS, sel)
            if res is None:
                return
            bid, idx = res
            name = rows[idx]["name"] if idx is not None and idx < len(rows) else None
            sel = idx or 0
            if bid == "switch" and name:
                self._profile_switch(name)
                return
            if bid == "save":
                self._profile_save()
            elif bid == "delete" and name:
                if self._modal_call(self.dlg.confirm, f"Delete profile '{name}'?", "The setup it describes is not changed.", False):
                    self._log_lines(actions_setup.delete_profile(name))
            elif bid == "share":
                self._profile_share()
            elif bid == "import":
                self._profile_import()

    def _profile_switch(self, name: str) -> None:
        ctx = self.ctx()
        try:
            prof, body = actions_setup.prepare_switch(ctx, name)
        except (ModError, OSError) as e:
            self.log_line(str(e), "bold red")
            return
        if self._modal_call(self.dlg.confirm, f"Switch to profile {name}?", body, False):
            ui = self.ui
            self.run_job("switch profile", "install", lambda: self._after_outcome_ui(actions_setup.switch_profile(ctx, ui, prof)))

    def _profile_save(self) -> None:
        name = self._modal_call(self.dlg.ask_text, "Profile name", "e.g. hordes")
        if not name:
            return
        if actions_setup.profile_exists(name) and not self._modal_call(self.dlg.confirm, f"A profile named '{name}' exists. Replace it?", "", False):
            return
        self._log_lines(actions_setup.save_profile(self.ctx(), name))

    def _profile_share(self) -> None:
        try:
            code = actions_setup.share_code(self.ctx())
        except (ModError, OSError) as e:
            self.log_line(str(e), "bold red")
            return
        self.log_line("Setup code ready: copy it from the window that opened.", "green")
        self._modal_call(self.dlg.show_code, "Share code for the current setup", code, "Send this to someone: Profiles > Import code.")

    def _profile_import(self) -> None:
        code = self._modal_call(self.dlg.ask_text, "Paste a setup code", "cog1:...")
        if code:
            self._log_lines(actions_setup.import_code(self.ctx(), code))

    # ---------------------------------------------------------------- snapshots
    def snapshots_window(self) -> None:
        if not self.need_game():
            return
        try:
            rows = snapshots.list_snapshots()
        except OSError as e:
            self.log_line(f"Could not read snapshots: {e}", "bold red")
            return
        res = self._modal_call(self.dlg.pick_action, "Snapshots",
                               "Snapshots are taken before risky actions (remove, update, profile switch...). Restoring one "
                               "snapshots your current setup first.",
                               [actions_setup.snapshot_label(r) for r in rows], SNAPSHOT_BUTTONS, 0)
        if res is None:
            return
        bid, idx = res
        ctx, ui = self.ctx(), self.ui
        if bid == "restore" and idx is not None and idx < len(rows):
            name = rows[idx]["name"]
            self.run_job("restore snapshot", "install", lambda: self._after_outcome_ui(actions_setup.restore_snapshot(ctx, ui, name)))
        elif bid == "create":
            reason = self._modal_call(self.dlg.ask_text, "Save a snapshot", "a few words to remember it by", "", True, False, "manual")
            if reason is not None:
                self._job_lines("snapshot", "install", lambda: actions_setup.create_snapshot(ctx, reason or "manual"))
        elif bid == "prune":
            keep = self._modal_call(self.dlg.ask_text, "Delete old snapshots", "how many of the newest to keep (at least 1)", "", False,
                                    False, str(max(1, self.settings.snapshot_keep)))
            if keep is None:
                return
            try:
                n = int(keep)
            except ValueError:
                n = 0
            self._log_lines(actions_setup.prune_snapshots(n))

    # ---------------------------------------------------------------- bulk actions
    def bulk_actions(self) -> None:
        if not self.need_game():
            return
        marked = sorted(self.model.marked)
        if not marked:
            self.log_line("Mark mods first (X, or right-click > Mark for bulk actions). Marked mods show a +.", "yellow")
            return
        i = self._modal_call(self.dlg.choose, f"{len(marked)} marked mod(s)", actions_setup.bulk_options(len(marked)), 0)
        if i is None:
            return
        ctx = self.ctx()
        if i in (actions_setup.BULK_ENABLE, actions_setup.BULK_DISABLE):
            enable = i == actions_setup.BULK_ENABLE
            self._job_lines("bulk", "bulk", lambda: actions_setup.bulk_set_enabled(ctx, marked, enable))
        elif i == actions_setup.BULK_UPDATE:
            self.update_marked()
        elif i == actions_setup.BULK_TAG:
            text = self._modal_call(self.dlg.ask_text, "Tag the marked mods", "tag names separated by spaces")
            if text:
                self._log_lines(actions_setup.bulk_tag(ctx, marked, text))
                self.refresh_all()
        elif i == actions_setup.BULK_REMOVE:
            body = ", ".join(marked) + "\nTheir folders are moved (not deleted) so you can restore them."
            if self._modal_call(self.dlg.confirm, f"Remove {len(marked)} mod(s)?", body, False):
                ui = self.ui

                def job() -> None:
                    out = actions_setup.remove_many(ctx, ui, marked)
                    self.ctl.ui(self.model.marked.difference_update, marked)
                    self._after_outcome_ui(out)

                self.run_job("remove marked", "install", job)
        else:
            self.model.marked.clear()
            self.refresh_all()

    # ---------------------------------------------------------------- bisect and crash log
    def bisect_wizard(self) -> None:
        if not self.need_game():
            return
        ctx, ui = self.ctx(), self.ui
        self.run_job("bisect", "bisect", lambda: self._after_outcome_ui(actions_setup.bisect_flow(ctx, ui)))

    def crashlog_window(self) -> None:
        if not self.need_game():
            return
        game_dir, folders = self.game_dir, {m.folder for m in self.model.mods}

        def job() -> None:
            lines = actions_setup.crashlog_lines(game_dir, folders)
            self.ctl.ui(lambda: self._modal_call(self.dlg.show_text, "Crash-log leads", [(t + "\n", s) for t, s in lines],
                                                 "Close", "760x460", True, self.palette))

        self.run_job("crash log", "net", job)

    # ---------------------------------------------------------------- game-update watcher
    def watch_tick(self) -> None:
        """UI timer: read the patch state OFF the UI thread, then act on the result back here."""
        if self.exiting or not (self.game_dir and self.registry) or self.ctl.busy("watch"):
            return
        game_dir, registry, mode = self.game_dir, self.registry, self.settings.auto_repair
        last = self._last_patch_state

        def job() -> None:
            try:
                st = patch_state(game_dir)
            except Exception:
                return
            step = repair.watch_step(last, st, game_dir, registry, mode, bool(self.game_running()))
            if step is not None:
                self.ctl.ui(self._watch_result, last, step)

        self.ctl.run("game-update watcher", job, "watch")

    def _watch_result(self, was: str | None, step: tuple[str, bool, str]) -> None:
        if self._modal or self._pending_asks or was != self._last_patch_state:
            return  # a dialog is open (or another reading won): look again on the next tick
        self._last_patch_state, redraw, act = step
        if redraw:
            self.refresh_header()
        if act == "patch":
            ctx = self.ctx()
            self.run_job("repair", "patch", lambda: self._repaired(ctx))
        elif act == "notify":
            self.log_line(actions_setup.REPAIR_NOTICE + " Use Mods > Mods on / off (Ctrl+T) to turn them on again.", "bold yellow")

    def _repaired(self, ctx: actions.Ctx) -> None:
        for text, style in actions_setup.repair_patch(ctx):
            self.ctl.log(text, style)
        self.ctl.ui(self.refresh_header)

    # ---------------------------------------------------------------- theme
    def apply_theme(self, name: str, save: bool = False) -> None:
        """`name` is the choice: system | light | dark. system follows the Windows app theme (light if it cannot be read)."""
        from tkinter import ttk

        from . import theme

        choice = theme.normalize_choice(name)
        resolved = theme.resolve_theme(choice)
        pal = PALETTES[normalize_theme(resolved)]
        self.theme_choice, self.theme_name, self.palette = choice, resolved, pal
        style = ttk.Style(self.root)
        if getattr(self, "sv_ok", False) and self._load_sv_theme(resolved):
            self._setup_fonts()  # keeps the Treeview row height and heading font after a theme switch
        else:
            self.sv_ok = False
            names = style.theme_names()
            target = "clam" if resolved == "dark" and "clam" in names else self._base_theme
            try:
                style.theme_use(target)
            except Exception:
                log.debug("theme %s not applicable", target, exc_info=True)
            for sty, opts in ttk_styles(pal).items():
                style.configure(sty, **opts)
            for sty, opts in ttk_state_maps(pal).items():
                style.map(sty, **opts)
        style.configure("Banner.TLabel", background="#c62828", foreground="#ffffff", padding=(10, 6))
        style.configure("Update.TLabel", background="#0b6e99", foreground="#ffffff", padding=(10, 6))
        style.configure("Hint.TLabel", foreground=pal["dim"])
        self.root.configure(bg=pal["bg"])
        for opt, val in (("Listbox.background", pal["log_bg"]), ("Listbox.foreground", pal["fg"]), ("Text.background", pal["log_bg"]),
                         ("Text.foreground", pal["fg"])):
            self.root.option_add("*" + opt, val)
        theme.set_titlebar_dark(self.root, resolved == "dark")
        self.hdr.restyle(pal)
        self.logbox.restyle(pal)
        self._configure_tree_tags()
        if hasattr(self, "theme_var"):
            self.theme_var.set(choice)
        if save and self.settings.gui_theme != choice:
            self.settings.gui_theme = choice
            try:
                self.settings.save()
            except OSError as e:
                self.log_line(f"Could not save the theme: {e}", "bold yellow")

    # ---------------------------------------------------------------- Cogitator's own updates
    def _selfupdater(self):
        if self.selfupdater is None and self.registry is not None:
            from cogitator.selfupdate import SelfUpdater

            self.selfupdater = SelfUpdater(self.registry, self.settings)
        return self.selfupdater

    def update_ready(self):
        u = self.selfupdater
        return u.ready() if u and u.layout.in_place else None

    def _update_offer(self):
        u = self.selfupdater
        return u.announcement() if u and u.layout.in_place and u.settings.update_mode != "off" else None

    def update_banner(self) -> str:
        return actions_setup.update_banner_text(self.selfupdater, self.update_ready(), gui=True)

    def start_selfupdate_check(self) -> None:
        if not (self.selfupdate_on_start and self.check_on_start) or self.settings.update_mode == "off" or self._selfupdater() is None:
            return
        self.run_job("cogitator update check", "selfupdate", self._job_selfupdate)

    def _job_selfupdate(self) -> None:
        u = self.selfupdater
        with self.lock:
            last = u.absorb_result()
            res = u.run_automatic()
        if last:
            self.ctl.ui(self._report_update_result, last)
        self.ctl.ui(self.refresh_header)
        if res.status == "available" and res.latest:
            self.ctl.log(f"Cogitator v{res.latest.version} is available.", "bold")
        if u.should_apply_automatically():
            self.ctl.ui(self._apply_update_now)

    def _report_update_result(self, r: dict) -> None:
        self._log_lines(actions_setup.update_result_lines(r))

    def apply_update(self) -> None:
        """Help > Update Cogitator: offer a download, or (when one is staged) install / remind / skip."""
        u = self.selfupdater
        if u is None:
            self.log_line("Cogitator's update check has not run.", "yellow")
            return
        rd = self.update_ready()
        if not rd:
            offer = self._update_offer()
            if not offer:
                self.log_line("No Cogitator update is waiting.", "dim")
                return
            if self._modal_call(self.dlg.choose, actions_setup.offer_question(offer), actions_setup.OFFER_CHOICES, 0) == 0:
                ctx, ui = self.ctx(), self.ui
                self.run_job("download update", "selfupdate", lambda: self._job_download_update(ctx, ui, offer))  # nothing is downloaded unasked
            return
        i = self._modal_call(self.dlg.choose, actions_setup.ready_question(u, rd), actions_setup.READY_CHOICES, 0)
        if i is None or i > actions_setup.SKIP:
            return
        if i == actions_setup.INSTALL:
            self._apply_update_now()
        else:
            ctx, version = self.ctx(), rd.version
            # runs on a worker: the UI thread must never wait on the registry lock
            self.run_job("update choice", "selfupdate", lambda: (actions_setup.remember_choice(ctx, u, i, version), self.ctl.ui(self.refresh_header)))

    def _job_download_update(self, ctx, ui, offer) -> None:
        if actions_setup.download_update(ctx, ui, self.selfupdater, offer):
            self.ctl.ui(self.refresh_header)
            self.ctl.ui(self.apply_update)

    def _apply_update_now(self) -> None:
        if actions_setup.apply_update(self.selfupdater, self.ui_now()):
            self.quit()

    def ui_now(self):
        """An `actions.Ui` for code that runs on the UI thread itself: it only logs (a confirm there would deadlock)."""
        return _UiThreadUi(self)


class _UiThreadUi:
    def __init__(self, app):
        self.app = app

    def log(self, msg: str, style: str = "") -> None:
        self.app.log_line(msg, style)

    def confirm(self, *a, **k) -> bool:  # pragma: no cover - not used on the UI thread
        raise RuntimeError("no confirmation can be asked from the UI thread")

    ask_text = choose = confirm

