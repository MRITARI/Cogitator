"""Uninstaller / maintenance-mode page logic with no widgets.

Pages: (maintenance choice) -> What to remove -> Review -> Progress -> Finish. All deletion logic is in the engine
(`cogitator.uninstall_run`); this only decides what is allowed, what is shown, and which plan gets built.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from cogitator import __version__
from cogitator.config import Settings
from cogitator.frameworks import detect_framework
from cogitator.mods import load_order_path
from cogitator.patcher import ON, patch_state
from cogitator.procs import darktide_running
from cogitator.registry import Registry
from cogitator.uninstall import load_order_removable, user_mod_folders
from cogitator.uninstall_run import (AppRemover, FullUninstallPlan, FullUninstallResult, PreviewItem, data_dirs, human,
                                     preview_uninstall)


class UPage(Enum):
    MAINTENANCE = "maintenance"
    CHOOSE = "choose"
    REVIEW = "review"
    PROGRESS = "progress"
    FINISH = "finish"


UORDER = [UPage.CHOOSE, UPage.REVIEW, UPage.PROGRESS, UPage.FINISH]
SAFE_NOTE = "Your installed mods and your mod list (mod_load_order.txt) are kept unless you choose otherwise below."


@dataclass
class UServices:
    game_dir: Callable[[], Path | None] = lambda: (Path(Settings.load().game_dir) if Settings.load().game_dir else None)
    registry: Callable[[], Registry] = Registry.load
    is_running: Callable[[], bool] = darktide_running
    app_remover: AppRemover | None = None  # supplied by the installed app (build steps 7 and 9)
    data_dirs: Callable[[], list[tuple[Path, int]]] = data_dirs
    selfupdater: Callable[[], object | None] = lambda: None  # a SelfUpdater for the installed app; None until the installed layout exists


@dataclass
class Detected:
    game_dir: Path | None
    app_installed: bool
    dml: str  # installed | incomplete | missing
    dmf: str
    patched: bool | None
    user_mods: list[str] = field(default_factory=list)
    any_installed: bool = False


def detect(svc: UServices) -> Detected:
    game = svc.game_dir()
    reg = svc.registry()
    if game is None or not game.is_dir():
        return Detected(game, svc.app_remover is not None, "missing", "missing", None, [], svc.app_remover is not None)
    dml, dmf = detect_framework(game, "dml").status, detect_framework(game, "dmf").status
    return Detected(game, svc.app_remover is not None, dml, dmf, patch_state(game) == ON if dml != "missing" else None,
                    user_mod_folders(game), svc.app_remover is not None or dml != "missing" or dmf != "missing" or bool(reg.data["frameworks"]))


class UninstallModel:
    def __init__(self, services: UServices | None = None, *, version: str = __version__):
        self.svc = services or UServices()
        self.version = version
        self.page = UPage.CHOOSE
        self.det: Detected | None = None
        # what to remove (defaults: the framework, the key and the app; never user data)
        self.remove_app = True
        self.remove_dmf = True
        self.remove_dml = True  # includes switching mods off
        self.remove_key = True
        self.remove_data = False
        self.remove_user_mods = False
        self.user_mods_selected: list[str] = []
        self.remove_load_order = False
        self.user_mods_confirmed = False
        self.game_running = False
        self.dry_run = False

    # ---------------------------------------------------------------- loading
    def load(self) -> Detected:
        self.det = detect(self.svc)
        d = self.det
        self.remove_dml = d.dml != "missing"
        self.remove_dmf = d.dmf != "missing"
        self.remove_app = self.svc.app_remover is not None
        self.game_running = bool(self.svc.is_running())
        return d

    def data_summary(self) -> list[tuple[Path, str]]:
        return [(p, human(n)) for p, n in self.svc.data_dirs()]

    # ---------------------------------------------------------------- user mods and the load order
    def set_remove_user_mods(self, on: bool) -> None:
        self.remove_user_mods = on
        self.user_mods_selected = list(self.det.user_mods) if (on and self.det) else []
        self._changed()

    def toggle_user_mod(self, name: str, on: bool) -> None:
        s = [m for m in self.user_mods_selected if m != name]
        self.user_mods_selected = s + ([name] if on else [])
        self._changed()

    def _changed(self) -> None:
        self.user_mods_confirmed = False  # any change withdraws the second confirmation
        if not self.load_order_offered():
            self.remove_load_order = False

    def load_order_offered(self) -> bool:
        """Deleting mod_load_order.txt is offered only if it holds no mod entries, or all user mods are being removed too."""
        d = self.det
        if not (d and d.game_dir and load_order_path(d.game_dir).is_file()):
            return False
        all_gone = self.remove_user_mods and bool(d.user_mods) and set(self.user_mods_selected) == set(d.user_mods)
        return load_order_removable(d.game_dir, self.svc.registry(), removing_all_user_mods=all_gone)

    def set_remove_load_order(self, on: bool) -> None:
        self.remove_load_order = bool(on and self.load_order_offered())

    def user_mod_confirmation_text(self) -> str:
        """The second confirmation lists every folder that will be deleted."""
        folders = "\n".join(f"  mods/{m}/" for m in self.user_mods_selected)
        return (f"These {len(self.user_mods_selected)} mod folder(s) will be permanently deleted from your Darktide folder:\n\n{folders}\n\n"
                "Their saved settings inside those folders go too. This cannot be undone from Cogitator.")

    def confirm_user_mods(self) -> None:
        if self.remove_user_mods and self.user_mods_selected:
            self.user_mods_confirmed = True

    @property
    def needs_user_mod_confirmation(self) -> bool:
        return self.remove_user_mods and bool(self.user_mods_selected) and not self.user_mods_confirmed

    # ---------------------------------------------------------------- navigation
    def something_selected(self) -> bool:
        return any([self.remove_app and self.svc.app_remover, self.remove_dmf, self.remove_dml, self.remove_key, self.remove_data,
                    self.remove_user_mods and self.user_mods_selected, self.remove_load_order])

    def can_ask_confirmation(self) -> bool:
        """True when the only thing missing is the second confirmation (so the Next button must stay clickable to ask it)."""
        return self.page is UPage.CHOOSE and self.needs_user_mod_confirmation and self._choose_ok(ignore_confirmation=True)[0]

    def can_next(self) -> tuple[bool, str]:
        if self.page is UPage.CHOOSE:
            return self._choose_ok(ignore_confirmation=False)
        if self.page is UPage.REVIEW:
            return True, ""
        return False, ""

    def _choose_ok(self, ignore_confirmation: bool) -> tuple[bool, str]:
        if not self.something_selected():
            return False, "Choose what to remove."
        if (self.remove_dml or self.remove_dmf or self.remove_user_mods) and self.game_running:
            return False, "Darktide is running. Close it, then press Retry."
        if self.remove_user_mods and not self.user_mods_selected:
            return False, "Tick at least one of your mods, or untick the option."
        if self.needs_user_mod_confirmation and not ignore_confirmation:
            return False, "Confirm which of your mods will be deleted."
        return True, ""

    def next(self) -> bool:
        if not self.can_next()[0] or self.page not in (UPage.CHOOSE, UPage.REVIEW):
            return False
        self.page = UORDER[UORDER.index(self.page) + 1]
        return True

    def back(self) -> bool:
        if self.page is UPage.REVIEW:
            self.page = UPage.CHOOSE
            return True
        return False

    # ---------------------------------------------------------------- plan and review
    def build_plan(self) -> FullUninstallPlan:
        d = self.det
        return FullUninstallPlan(d.game_dir if d else None, self.remove_dml, self.remove_dmf,
                                 list(self.user_mods_selected) if self.remove_user_mods else [],
                                 self.remove_load_order, self.remove_key, self.remove_data,
                                 self.svc.app_remover if self.remove_app else None)

    def review_items(self) -> list[PreviewItem]:
        return preview_uninstall(self.build_plan(), self.svc.registry())

    def review_text(self) -> list[tuple[str, list[str]]]:
        """Grouped, plain-language lines for the Review page."""
        titles = {"mods_off": "Mods", "framework": "Mod Loader and Mod Framework files", "user_mod": "Your mods",
                  "load_order": "Your mod list", "key": "Nexus key", "app": "Cogitator", "data": "Cogitator's settings, caches and rollback copies"}
        groups: dict[str, list[str]] = {}
        for it in self.review_items():
            groups.setdefault(it.group, []).append(it.text + (f"   [{it.note}]" if it.note else ""))
        out = [(titles[g], lines) for g, lines in groups.items()]
        if not (self.remove_user_mods and self.user_mods_selected):
            out.append(("Kept", [SAFE_NOTE]))
        return out

    # ---------------------------------------------------------------- finish
    def finish_lines(self, r: FullUninstallResult) -> list[str]:
        if getattr(r, "dry_run", False):
            return ["Dry run finished: nothing was removed or changed.", "", ""]
        if r.cancelled:
            return ["Uninstall was cancelled." + (" What was already removed stays removed." if r.removed else " Nothing was changed."),
                    "Run the uninstaller again to finish.", ""]
        if not r.ok:
            return [r.what, r.why, f"Next step: {r.fix}"]
        parts = []
        if self.remove_dml:
            parts.append("mods are switched off and the Mod Loader is removed")
        if self.remove_dmf:
            parts.append("the Mod Framework is removed")
        if self.remove_app:
            parts.append("Cogitator is removed")
        first = ("Done: " + "; ".join(parts) + ".") if parts else "Done."
        kept = ("Your mods and mod list were not touched." if not (self.remove_user_mods and self.user_mods_selected)
                else "The mods you chose were deleted; the others were not touched.")
        return [first, kept, "If anything looks unusual, use Steam: right-click Darktide > Properties > Installed Files > Verify integrity."]


class MaintenanceModel:
    """The choice screen shown when Setup finds Cogitator already installed."""

    CHOICES = ("repair", "update", "uninstall")

    def __init__(self, detected: Detected, updater=None):
        self.det, self.updater = detected, updater

    def info(self):
        """The release to announce (None = nothing newer, or the user chose to be reminded later / skip it)."""
        return self.updater.announcement() if self.updater else None

    def enabled(self, choice: str) -> bool:
        if choice == "update":
            return self.info() is not None
        return choice in ("repair", "uninstall")

    def notes(self) -> str:
        i = self.info()
        return f"Cogitator v{i.version} is available.\n\n{i.notes or '(no release notes)'}" if i else ""

    def skip(self) -> None:
        if (i := self.info()):
            self.updater.skip_version(str(i.version))

    def remind_later(self) -> None:
        if self.updater:
            self.updater.remind_later()

    def install(self) -> str:
        """Download + verify if needed, then hand over to the helper. Returns a message; raises UpdateError on refusal."""
        u, i = self.updater, self.info()
        if not u or not i:
            raise RuntimeError("no update to install")
        if not u.layout.in_place:
            return u.layout.guidance()
        if not u.ready() or u.ready().version != str(i.version):
            u.stage(i)
        u.apply()
        return f"Installing v{i.version}. Cogitator will restart."

    def summary(self) -> str:
        d = self.det
        bits = [f"Mod Loader: {d.dml}", f"Mod Framework: {d.dmf}"]
        if d.patched is not None:
            bits.append("mods are " + ("on" if d.patched else "off"))
        return "Cogitator is already installed. " + "; ".join(bits) + "."
