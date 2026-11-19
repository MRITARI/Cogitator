"""Cogitator's own update flow for the interface."""
from __future__ import annotations

import logging



from .. import actions_setup
from .screens import ChoiceScreen

log = logging.getLogger("cogitator.tui")


class SelfUpdateMixin:
    """Methods of CogitatorApp; they use its attributes, so it is only meant to be mixed into it."""

    # -------------------------------------------------------------- Cogitator's own updates
    def _selfupdater(self):
        if self.selfupdater is None and self.registry is not None:
            from ..selfupdate import SelfUpdater

            self.selfupdater = SelfUpdater(self.registry, self.settings)
        return self.selfupdater

    def update_ready(self):
        u = self.selfupdater
        return u.ready() if u and u.layout.in_place else None

    def _update_banner(self) -> str:
        return actions_setup.update_banner_text(self.selfupdater, self.update_ready())

    def start_selfupdate_check(self) -> None:
        if not (self.selfupdate_on_start and self._check_on_start) or self.settings.update_mode == "off" or self._selfupdater() is None:
            return
        self._bg(self._job_selfupdate, name="cogitator update check", group="selfupdate", exclusive=True)

    def _job_selfupdate(self) -> None:
        u = self.selfupdater
        with self._lock:
            last = u.absorb_result()
            res = u.run_automatic()
        if last:
            self._ui(self._report_update_result, last)
        self._ui(self.refresh_header)
        self._ui(self.refresh_bindings)
        if res.status == "available" and res.latest:
            self._ui(self.log_line, f"Cogitator v{res.latest.version} is available.", "bold")
        if u.should_apply_automatically():
            self._ui(self._apply_update_now)

    def _report_update_result(self, r: dict) -> None:
        """What the update helper did last time, in plain words (it ran after Cogitator had closed)."""
        for text, style in actions_setup.update_result_lines(r):
            self.log_line(text, style)

    def _update_offer(self):
        u = self.selfupdater
        return u.announcement() if u and u.layout.in_place and u.settings.update_mode != "off" else None

    def action_apply_update(self) -> None:
        u, a = self.selfupdater, None
        rd = self.update_ready()
        if not rd:
            offer = self._update_offer()
            if offer:  # nothing is downloaded without asking
                mb = f" (about {offer.zip_size // (1024 * 1024)} MB)" if offer.zip_size else ""
                self.push_screen(ChoiceScreen(f"Cogitator v{offer.version} is available{mb}.\n\n{offer.notes or '(no release notes)'}",
                                              ["Download it from GitHub now", "Cancel"]),
                                 lambda i, o=offer: self._bg(self._job_download_update, o, name="download update", group="selfupdate")
                                 if i == 0 else None)
            return
        a = u.latest() or u.announcement()
        notes = (a.notes if a else rd.notes) or "(no release notes)"
        self.push_screen(ChoiceScreen(f"Cogitator v{rd.version}\n\n{notes}", ["Install now and restart", "Remind me later", "Skip this version", "Cancel"]),
                         self._update_choice)

    def _job_download_update(self, info) -> None:
        from ..selfupdate import UpdateError

        self._ui(self.log_line, f"Downloading Cogitator v{info.version} ...")
        try:
            with self._lock:
                self.selfupdater.stage(info)
        except UpdateError as e:
            self._ui(self.log_line, str(e), "bold red")
            return
        self._ui(self.refresh_header)
        self._ui(self.refresh_bindings)
        self._ui(self.action_apply_update)

    def _update_choice(self, i) -> None:
        rd = self.update_ready()
        if i is None or not rd:
            return
        # Runs on the UI thread, which must never wait on self._lock (a worker holding it may be waiting on us): hand off.
        self._bg(self._job_update_choice, i, rd.version, name="update choice", group="selfupdate")

    def _job_update_choice(self, i: int, version: str) -> None:
        u = self.selfupdater
        with self._lock:
            if i == 0:
                self._ui(self._apply_update_now)  # runs on the UI thread; it takes no lock
            elif i == 1:
                u.remind_later()
            elif i == 2:
                u.skip_version(version)
        self._ui(self.refresh_header)
        self._ui(self.refresh_bindings)

    def _apply_update_now(self) -> None:
        from ..selfupdate import UpdateError

        try:
            rd = self.selfupdater.apply()
        except UpdateError as e:
            self.log_line(str(e), "bold red")
            return
        self.log_line(f"Installing v{rd.version}; Cogitator will restart.", "bold")
        self.action_quit_app()
