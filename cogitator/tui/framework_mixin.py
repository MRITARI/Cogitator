"""Framework update, doctor and bootstrap flows for the interface."""
from __future__ import annotations

import logging

from pathlib import Path


from ..config import get_api_key
from ..doctor import exit_code as doctor_exit_code, run_doctor
from ..frameworks import (describe_plan, detect_framework, plan_github)
from ..fwupdate import install_framework_plan
from ..loader import LoaderError, NEXUS_PAGES, make_client
from ..modinstall import (ModImportError, NotPremium, fetch_premium_download)
from ..nexus import NexusClient, NexusError
from ..registry import Registry
from ..updates import (FRAMEWORK_MODS, FrameworkUpdate, UPDATE, cached_frameworks)
from .screens import ChoiceScreen, ConfirmScreen, DoctorScreen, InputScreen

log = logging.getLogger("cogitator.tui")
from .view import (report_text)


class FrameworkMixin:
    """Methods of CogitatorApp; they use its attributes, so it is only meant to be mixed into it."""

    # -------------------------------------------------------------- F: framework update (through the import flow)
    def _pending_framework_updates(self) -> list[FrameworkUpdate]:
        ups = self.fw_updates or (cached_frameworks(self.registry) if self.registry else [])
        return [u for u in ups if u.status == UPDATE and u.latest_file_id is not None]

    def action_framework_update(self) -> None:
        if not self._need_game():
            return
        ups = self._pending_framework_updates()
        if not ups:
            seen = self.fw_updates or cached_frameworks(self.registry)
            st = ", ".join(f"{u.kind.upper()} {u.status}" for u in seen) or "not installed"
            self.log_line(f"No framework update to apply ({st}). Press R to recheck.", "yellow")
            return
        how = ("Cogitator downloads it through your Premium account, then shows the install source for a final confirmation."
               if self._premium() else
               "Cogitator opens the Nexus files page; download the MAIN file and it is imported from your Downloads folder "
               "after a final confirmation.")
        lines = "\n".join(f"{u.kind.upper()}: {u.installed or 'unknown'} -> {u.latest}" for u in ups)
        self.push_screen(ConfirmScreen(f"Update {' and '.join(u.kind.upper() for u in ups)}?", f"{lines}\n\n{how}"),
                         lambda ok: ok and self._bg(self._job_framework, ups, name="framework update", group="download",
                                                    exclusive=True))

    def _job_framework(self, ups: list[FrameworkUpdate]) -> None:
        import shutil
        import tempfile

        key = get_api_key()
        for u in sorted(ups, key=lambda x: x.kind != "dml"):  # loader before framework
            mod_id = FRAMEWORK_MODS[u.kind]
            latest = {"file_name": u.latest_file_name, "version": u.latest, "file_id": u.latest_file_id}
            tmp: Path | None = None
            try:
                if self._premium():
                    tmp = Path(tempfile.mkdtemp(prefix="fw-", dir=self._work_dir()))
                    with NexusClient(key) as nx:
                        found = fetch_premium_download(nx, mod_id, u.latest_file_id, u.latest_file_name, u.latest_size, tmp,
                                                       premium=True)
                else:
                    found = self._watch_download(mod_id, latest, u.kind.upper())
                if found:
                    self._job_import(str(found), None)  # the existing import flow: shows source, asks, installs, re-patches
            except (ModImportError, LoaderError, NexusError, NotPremium) as e:
                self._ui(self.log_line, f"{u.kind.upper()} update: {e}", "bold red")
            finally:
                if tmp:
                    shutil.rmtree(tmp, ignore_errors=True)

    def _work_dir(self) -> Path:
        from ..config import cache_dir

        d = cache_dir() / "work"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -------------------------------------------------------------- D: doctor
    def action_doctor(self) -> None:
        self.log_line("Running doctor ...", "dim")
        self._bg(self._job_doctor, name="doctor", group="doctor", exclusive=True)

    def _job_doctor(self) -> None:
        checks = run_doctor(str(self.game_dir) if self.game_dir else None, running=self.game_running, offline=self.doctor_offline,
                            registry=Registry.load() if self.game_dir else None)
        from ..doctor import record_history

        record_history(checks)
        self._ui(lambda: self.push_screen(DoctorScreen(report_text(checks), doctor_exit_code(checks))))

    # -------------------------------------------------------------- bootstrap: install DML + DMF
    def _offer_bootstrap(self) -> None:
        if not self.game_dir:
            return
        missing = [k for k in ("dml", "dmf") if detect_framework(self.game_dir, k).status != "installed"]
        if not missing:
            return
        self.push_screen(ChoiceScreen(
            "Install Darktide Mod Loader and Mod Framework?",
            ["Import the Nexus zip(s) I downloaded (recommended)",
             "Fetch from GitHub (opt-in: unreleased source archives; you confirm each source)", "Not now"]),
            lambda i: self._bootstrap_choice(i, missing))

    def _bootstrap_choice(self, i: int | None, missing: list[str]) -> None:
        pages = ", ".join(f"{k.upper()}: {NEXUS_PAGES[k]}" for k in missing)
        if i == 0:
            self.log_line(f"Download from Nexus: {pages}", "cyan")
            self.push_screen(InputScreen("Path to the DML or DMF zip", r"C:\Users\you\Downloads\Darktide Mod Loader ... .zip"),
                             lambda p: p and self._bg(self._job_import, p, None, name="install framework", group="install", exclusive=True))
        elif i == 1:
            self._bg(self._job_github, missing, name="github install", group="install", exclusive=True)
        else:
            self.log_line(f"Mod Loader/Framework not installed. {pages}", "yellow")

    def _job_github(self, missing: list[str]) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="cogitator-gh-") as wd, make_client(github_only=True) as client:
            for kind in sorted(missing, key=lambda k: k != "dml"):
                plan = plan_github(kind, client, Path(wd))
                if not self.ask(ConfirmScreen(f"Install {kind.upper()} from GitHub?", describe_plan(plan))):
                    self._ui(self.log_line, f"{kind.upper()} install declined.", "yellow")
                    continue
                with self._lock:
                    fi = install_framework_plan(plan, self.game_dir, self.registry, runner=self.patch_runner,
                                                is_running=self.game_running)
                self._ui(self.log_line, f"{kind.upper()} installed ({fi.result.written} files).", "green")
        self._ui(self._after_registry_change, None)
