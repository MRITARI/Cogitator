"""Mod-related workflows of the main window: link, updates, frameworks, nxm links, Nexus search, changelog.

A mixin of `CogitatorGui` (it uses the window's attributes). The rules live in `cogitator.actions_nexus`, which the terminal UI
calls too; here only the questions the UI thread can ask without blocking and the jobs it starts."""
from __future__ import annotations

from cogitator import actions, actions_nexus, nxm
from cogitator.nexus import parse_nexus_ref
from cogitator.updates import PROBABLY_OUTDATED, UPDATE, cached_frameworks


def plan_update_selected(row, premium: bool) -> tuple[str, str]:
    """('nothing', why) | ('premium', confirm body) | ('manual', '') for the Update entry on one mod."""
    if row is None or row.status not in (UPDATE, PROBABLY_OUTDATED):
        return "nothing", f"nothing to update ({row.status if row else 'not linked'})."
    return ("premium", row.reason) if premium else ("manual", "")


def plan_update_all(urows: dict, premium: bool) -> tuple[str, list[str]]:
    """('nothing', []) | ('premium', folders) | ('manual', folders): what 'update all' does for this account."""
    targets = actions_nexus.update_candidates(urows)
    if not targets:
        return "nothing", []
    return ("premium" if premium else "manual"), targets


class ModFlows:
    # ---- link
    def link_selected(self, folder: str | None = None) -> None:
        f = folder or self.selected()
        if not (self.need_game() and f):
            return
        why = actions_nexus.link_refusal(f)
        if why:
            self.log_line(why, "yellow")
            return
        row = self.model.urows.get(f)
        if actions_nexus.link_needs_version_only(row):
            ctx, ui, mod_id = self.ctx(), self.ui, row.nexus_id
            self.run_job("set version", "link", lambda: self._after_outcome_ui(actions_nexus.set_version(ctx, ui, f, mod_id)))
            return
        ref = self._modal_call(self.dlg.ask_text, f"Link '{f}' to its Nexus page",
                               "https://www.nexusmods.com/warhammer40kdarktide/mods/<id>  or  numeric id")
        if not ref:
            return
        mod_id, problem = parse_nexus_ref(ref)
        if mod_id is None:
            self.log_line(problem or "Bad Nexus reference.", "bold red")
            return
        ctx, ui = self.ctx(), self.ui
        self.run_job("link", "link", lambda: self._after_outcome_ui(actions_nexus.link_one_mod(ctx, ui, f, mod_id)))

    def link_all_mods(self) -> None:
        if not self.need_game():
            return
        ctx, ui, present = self.ctx(), self.ui, [m.folder for m in self.model.mods if m.present]
        self.run_job("link all", "link", lambda: self._after_outcome_ui(actions_nexus.link_all(ctx, ui, present)))

    # ---- updates
    def _premium(self) -> bool:
        return actions_nexus.is_premium(self.ctx())

    def update_selected(self, folder: str | None = None) -> None:
        f = folder or self.selected()
        if not (self.need_game() and f):
            return
        kind, body = plan_update_selected(self.model.urows.get(f), self._premium())
        if kind == "nothing":
            self.log_line(f"'{f}': {body}", "yellow")
        elif kind == "premium":
            if self._modal_call(self.dlg.confirm, f"Download and install the update for {f}?", body):
                self._start_premium("update", [f])
        else:
            ctx, ui = self.ctx(), self.ui
            self.run_job("manual update", "download",
                         lambda: actions_nexus.manual_update(ctx, ui, f, self._after_outcome_ui))

    def update_all(self) -> None:
        if not self.need_game():
            return
        kind, targets = plan_update_all(self.model.urows, self._premium())
        if kind == "nothing":
            self.log_line("No mods have an update available.", "yellow")
        elif kind == "premium":
            if self._modal_call(self.dlg.confirm, f"Download and install {len(targets)} update(s)?", "\n".join(targets)):
                self._start_premium("update all", targets)
        else:
            actions_nexus.free_update_all_help(self.ui, self.model.urows, targets)

    def update_marked(self) -> None:
        """Bulk 'update them (Premium)' for the marked mods."""
        todo = actions_nexus.update_candidates(self.model.urows, sorted(self.model.marked))
        if not self._premium():
            self.log_line("Free account: updates need a download for each mod. Right-click one and choose Update.", "yellow")
        elif not todo:
            self.log_line("None of the marked mods has an update available.", "yellow")
        else:
            self._start_premium("update marked", todo)

    def _start_premium(self, name: str, folders: list[str]) -> None:
        ctx, ui = self.ctx(), self.ui

        def job() -> None:
            out = actions_nexus.premium_update(ctx, ui, folders)
            self._after_outcome_ui(actions.Outcome(refresh=True))
            if out.recheck:
                self.ctl.ui(lambda: self.start_check(force=True))

        self.run_job(name, "install", job)

    # ---- frameworks
    def fw_pending(self) -> list:
        fw = self.fw_updates or (cached_frameworks(self.registry) if self.registry else [])
        return actions_nexus.pending_framework_updates(fw)

    def framework_update(self) -> None:
        if not self.need_game():
            return
        ups = self.fw_pending()
        if not ups:
            seen = self.fw_updates or cached_frameworks(self.registry)
            st = ", ".join(f"{u.kind.upper()} {u.status}" for u in seen) or "not installed"
            self.log_line(f"No framework update to apply ({st}). Use Nexus > Check for updates.", "yellow")
            return
        if self._modal_call(self.dlg.confirm, f"Update {' and '.join(u.kind.upper() for u in ups)}?",
                            actions_nexus.framework_update_body(ups, self._premium())):
            ctx, ui = self.ctx(), self.ui
            self.run_job("framework update", "download", lambda: actions_nexus.framework_update(ctx, ui, ups, self._after_outcome_ui))

    def bootstrap_github(self, missing: list[str]) -> None:
        ctx, ui = self.ctx(), self.ui
        self.run_job("github install", "install", lambda: self._after_outcome_ui(actions_nexus.install_from_github(ctx, ui, missing)))

    # ---- nxm links from the Nexus website
    def nxm_tick(self) -> None:
        """Every few seconds (UI timer): pick up a queued link. One at a time, and never while a dialog is open."""
        if self.exiting or self._nxm_busy or self._modal or self._pending_asks or not (self.game_dir and self.registry):
            return
        if self.ctl.busy("install"):
            return  # try again on the next tick instead of logging a refusal every two seconds
        items = nxm.pending(on_expired=lambda lk: self.log_line(
            f"Nexus link for mod {lk.mod_id} file {lk.file_id}: link expired. Click the button on the website again.", "yellow"))
        if not items:
            return
        path, link = items[0]
        self._nxm_busy = True
        ctx, ui = self.ctx(), self.ui

        def job() -> None:
            try:
                actions_nexus.nxm_install(ctx, ui, path, link, self._after_outcome_ui)
            finally:
                self._nxm_busy = False

        if not self.run_job("nxm link", "install", job):
            self._nxm_busy = False

    # ---- search and changelog
    def search_nexus(self) -> None:
        if not self.need_game():
            return
        text = self._modal_call(self.dlg.ask_text, "Search Nexus Mods", "part of a mod's name")
        if text:
            ctx, ui = self.ctx(), self.ui
            self.run_job("search", "net", lambda: actions_nexus.search_nexus(ctx, ui, text))

    def show_changelog(self, folder: str | None = None) -> None:
        f = folder or self.selected()
        if not (self.need_game() and f):
            return
        reg = self.registry

        def job() -> None:
            res = actions_nexus.changelog_for(reg, f)
            if res.ok:
                self.ctl.ui(lambda: self._modal_call(self.dlg.show_text, f"Changelog - {f}", [(t + "\n", s) for t, s in res.lines],
                                                     "Close", "700x480", False, self.palette))
            else:
                for t, s in res.lines:
                    self.ctl.log(t, s)

        self.run_job("changelog", "net", job)
