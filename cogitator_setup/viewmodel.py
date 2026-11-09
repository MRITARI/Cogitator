"""Wizard page logic with no widgets: what each page shows, when Next is allowed, and the plan that Install builds.
The Nexus key is kept in one private attribute and never appears in repr, summaries or diagnostics."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from cogitator import __version__
from cogitator.frameworks import detect_framework
from cogitator.gamefinder import Resolution, resolve_game_dir, validate_game_dir
from cogitator.procs import darktide_running
from cogitator.registry import Registry
from cogitator.setup_run import (ComponentChoice, KeyCheck, SetupPlan, SetupResult, planned_steps, validate_key)
from cogitator.sources import SNAPSHOT_EXPLANATION, SourceOption, choose_sources

NOTICE = (
    "Darktide mods are not supported by Fatshark and are used at your own risk.\n"
    "Mods run inside the game with the game's permissions, and Nexus Mods does not scan their code for malware, "
    "so only install mods you trust.\n"
    "Fatshark does not allow mods that affect other players, progression or servers. Cogitator includes none.")

KEY_BLURB = ("Optional. A Nexus Mods API key lets Cogitator check your mods for updates. Open the Nexus page below, "
             "copy your key, and paste it here, or press Skip. You can add it later with the K key. "
             "Your key stays on this computer and is never shared.")
PATCH_SENTENCE = ("Mods are switched on by a small change to the game's files. Game updates switch them off again, "
                  "and Cogitator can switch them back on.")
NEXUS_API_PAGE = "https://www.nexusmods.com/settings/api-keys"  # opens the user's Nexus API settings


class Page(Enum):
    WELCOME = "welcome"
    GAME = "game"
    KEY = "key"
    COMPONENTS = "components"
    REVIEW = "review"
    PROGRESS = "progress"
    FINISH = "finish"


ORDER = [Page.WELCOME, Page.GAME, Page.KEY, Page.COMPONENTS, Page.REVIEW, Page.PROGRESS, Page.FINISH]


@dataclass
class ComponentState:
    kind: str  # app | dml | dmf
    title: str
    description: str
    status: str = "missing"  # missing | installed | incomplete | update
    version: str | None = None
    selected: bool = True
    required: bool = False

    @property
    def status_text(self) -> str:
        return {"missing": "not installed", "installed": f"already installed ({self.version or 'version unknown'})",
                "incomplete": "needs repair", "update": f"update available (installed {self.version or 'unknown'})"}[self.status]


@dataclass
class Services:
    """Everything with side effects, replaceable in tests."""

    find_game: Callable[[], Resolution] = lambda: resolve_game_dir(None, None)
    is_running: Callable[[], bool] = darktide_running
    installed_state: Callable[[Path, str], tuple[str, str | None]] = None  # (status, version); set in __post_init__
    resolve_options: Callable[[str, bool | None, str | None], tuple[list[SourceOption], str]] = None  # (options, problem text)
    check_key: Callable[[str], KeyCheck] = validate_key

    def __post_init__(self) -> None:
        if self.installed_state is None:
            self.installed_state = _default_installed_state
        if self.resolve_options is None:
            self.resolve_options = default_resolve_options


def _default_installed_state(game: Path, kind: str) -> tuple[str, str | None]:
    st = detect_framework(game, kind)
    reg = Registry.load()
    ver = (reg.framework(kind) or {}).get("version")
    if st.status == "installed":
        from cogitator.updates import cached_frameworks

        upd = next((u for u in cached_frameworks(reg) if u.kind == kind), None)
        return ("update" if upd and upd.status == "update available" else "installed"), ver
    return st.status, ver  # missing | incomplete


@dataclass
class ReviewRow:
    label: str
    lines: list[str]
    warning: str = ""


class SetupModel:
    def __init__(self, services: Services | None = None, *, version: str = __version__, maintenance: bool = False):
        self.svc = services or Services()
        self.version = version
        self.page = Page.WELCOME
        self.accepted = False
        self.game_dir: Path | None = None
        self.game_message = ""
        self.game_running = False
        self._key: str | None = None  # private: never in repr / summaries
        self.key_check: KeyCheck | None = None
        self.key_skipped = False
        self.components: dict[str, ComponentState] = {
            "app": ComponentState("app", "Cogitator", "The mod manager itself.", selected=True, required=True),
            "dml": ComponentState("dml", "Darktide Mod Loader", "Lets the game load mods. Recommended."),
            "dmf": ComponentState("dmf", "Darktide Mod Framework", "Shared code most mods need. Recommended."),
        }
        self.enable_mods = True
        self.desktop_icon = True
        self.start_menu = True
        self.options: dict[str, list[SourceOption]] = {}
        self.choice: dict[str, int] = {}
        self.manual_zip: dict[str, Path] = {}
        self.option_problem: dict[str, str] = {}
        self.confirmed = False
        self.result: SetupResult | None = None
        self.run_doctor = True
        self.launch = True
        self.first_run = False  # opened by the installer: the app and shortcuts are already in place; no Components page
        self.repair = False  # maintenance 'Repair': reinstall what is already there, too

    def __repr__(self) -> str:  # the key must never show up in a repr (logs, tracebacks)
        return f"<SetupModel page={self.page.value} game={self.game_dir} key={'set' if self._key else 'none'}>"

    # ---------------------------------------------------------------- navigation
    def can_next(self) -> tuple[bool, str]:
        p = self.page
        if p is Page.WELCOME:
            return (self.accepted, "" if self.accepted else "Tick \"I understand\" to continue.")
        if p is Page.GAME:
            if self.game_dir is None:
                return False, self.game_message or "Choose your Darktide folder."
            if self.game_running:
                return False, "Darktide is running. Close it, then press Retry."
            return True, ""
        if p is Page.KEY:
            if self._key and not (self.key_check and self.key_check.ok):
                return False, "Press Validate to check your key, or press Skip."
            return True, ""
        if p is Page.COMPONENTS:
            return True, ""
        if p is Page.REVIEW:
            return self.install_ready()
        return False, ""  # Progress and Finish are driven by the engine

    def _order(self) -> list[Page]:
        return [p for p in ORDER if not (self.first_run and p is Page.COMPONENTS)]

    def next(self) -> bool:
        ok, _ = self.can_next()
        if not ok or self.page in (Page.PROGRESS, Page.FINISH):
            return False
        order = self._order()
        self.page = order[order.index(self.page) + 1]
        return True

    def back(self) -> bool:
        if self.page in (Page.WELCOME, Page.PROGRESS, Page.FINISH):
            return False  # nothing can be undone once the run started
        self.confirmed = False
        order = self._order()
        self.page = order[order.index(self.page) - 1]
        return True

    # ---------------------------------------------------------------- game page
    def autodetect_game(self) -> bool:
        res = self.svc.find_game()
        if res.game_dir:
            return self.set_game(res.game_dir)
        self.game_dir = None
        self.game_message = (res.message or "Darktide was not found automatically.") + " Press Browse and choose the folder that contains \"bundle\" and \"binaries\"."
        if res.source == "ambiguous":
            self.game_message = "More than one Darktide install was found. Press Browse and choose the one you want."
        return False

    def set_game(self, path: Path | str) -> bool:
        ok, why = validate_game_dir(path)
        if not ok:
            self.game_dir, self.game_message = None, "That folder does not look like Darktide (it needs \"bundle\" and \"binaries\" inside)."
            return False
        self.game_dir, self.game_message = Path(path), ""
        self.refresh_running()
        self.detect_components()
        return True

    def refresh_running(self) -> bool:
        self.game_running = bool(self.svc.is_running())
        return self.game_running

    # ---------------------------------------------------------------- key page
    def set_key(self, text: str) -> None:
        self._key = text.strip() or None
        self.key_check, self.key_skipped = None, False

    def has_key(self) -> bool:
        return bool(self._key)

    def validate_key(self) -> KeyCheck:
        self.key_check = self.svc.check_key(self._key or "")
        if not self.key_check.ok:
            return self.key_check
        return self.key_check

    def skip_key(self) -> None:
        self._key, self.key_check, self.key_skipped = None, None, True

    def key_for_plan(self) -> str | None:
        """Only a key that validated is passed on; the model never echoes it."""
        return self._key if (self._key and self.key_check and self.key_check.ok) else None

    @property
    def premium(self) -> bool | None:
        return self.key_check.premium if (self.key_check and self.key_check.ok) else None

    # ---------------------------------------------------------------- components page
    def detect_components(self) -> None:
        if not self.game_dir:
            return
        for kind in ("dml", "dmf"):
            c = self.components[kind]
            c.status, c.version = self.svc.installed_state(self.game_dir, kind)
            # missing -> on; needs repair or update -> on; already fine -> off (nothing to do)
            c.selected = True if self.repair else c.status != "installed"

    def toggle(self, kind: str, value: bool) -> None:
        c = self.components[kind]
        if not c.required:
            c.selected = value
            self.options.pop(kind, None)
            self.confirmed = False

    def selected_kinds(self) -> list[str]:
        return [k for k in ("dml", "dmf") if self.components[k].selected]

    # ---------------------------------------------------------------- review page
    def load_options(self) -> None:
        """Resolve where each selected component would come from (cheap metadata, no download)."""
        self.options.clear()
        for kind in self.selected_kinds():
            opts, problem = self.svc.resolve_options(kind, self.premium, self.key_for_plan())
            self.options[kind], self.option_problem[kind] = opts, problem
            self.choice.setdefault(kind, 0)
            self.choice[kind] = 0

    def select_option(self, kind: str, index: int) -> None:
        self.choice[kind] = index
        self.confirmed = False

    def selected_option(self, kind: str) -> SourceOption | None:
        opts = self.options.get(kind) or []
        i = self.choice.get(kind, 0)
        return opts[i] if 0 <= i < len(opts) else None

    def set_manual_zip(self, kind: str, path: Path) -> None:
        self.manual_zip[kind] = Path(path)
        self.confirmed = False

    def _effective_option(self, kind: str) -> SourceOption | None:
        from cogitator.sources import local_option

        opt = self.selected_option(kind)
        if opt is not None and opt.kind == "manual":
            z = self.manual_zip.get(kind)
            return local_option(kind, z) if z else None
        return opt

    def install_ready(self) -> tuple[bool, str]:
        if not self.game_dir:
            return False, "Choose your Darktide folder first."
        dml = self.components["dml"]
        if self.enable_mods and not dml.selected and dml.status in ("missing", "incomplete"):
            # switching mods on needs a complete Mod Loader, so this is checked here rather than at the patch step
            return False, ("Mods can only be switched on once the Mod Loader is installed. "
                           "Tick the Mod Loader, or untick \"Switch mods on\".")
        for kind in self.selected_kinds():
            if self.selected_option(kind) is None:
                return False, self.option_problem.get(kind) or f"No source is available for the {self.components[kind].title}."
            if self._effective_option(kind) is None:
                return False, f"Choose the {self.components[kind].title} zip you downloaded."
        return True, ""

    def review_rows(self) -> list[ReviewRow]:
        rows = [ReviewRow("Darktide folder", [str(self.game_dir)]),
                ReviewRow("Cogitator", ["Already installed (done by the installer)." if self.first_run
                                        else "Installed for your Windows user only (no administrator rights)."])]
        for kind in ("dml", "dmf"):
            c = self.components[kind]
            if not c.selected:
                rows.append(ReviewRow(c.title, [c.status_text + " - left as it is."]))
                continue
            opt = self._effective_option(kind) or self.selected_option(kind)
            if opt is None:
                rows.append(ReviewRow(c.title, [self.option_problem.get(kind) or "No source available."]))
                continue
            lines = opt.review_lines()
            if opt.kind == "manual":
                lines = ["You will download it yourself from Nexus Mods and choose the zip here."]
            rows.append(ReviewRow(c.title + (" - UNRELEASED SNAPSHOT" if opt.unreleased else ""), lines,
                                  SNAPSHOT_EXPLANATION if opt.unreleased else ""))
        rows.append(ReviewRow("Mods", [PATCH_SENTENCE if self.enable_mods else "Left switched off. You can switch them on later with the T key."]))
        shortcuts = ["Already created by the installer."] if self.first_run else [n for n, on in (("Desktop icon", self.desktop_icon), ("Start Menu entry", self.start_menu)) if on]
        rows.append(ReviewRow("Shortcuts", shortcuts or ["None"]))
        rows.append(ReviewRow("Nexus key", ["Will be stored in Windows' credential store."] if self.key_for_plan() else ["Not set. Update checks stay off until you add it (K key)."]))
        return rows

    def confirm_install(self) -> bool:
        """The explicit Install click. Everything shown on the Review page is what gets confirmed."""
        ok, _ = self.install_ready()
        self.confirmed = ok
        return ok

    def build_plan(self, app_installer=None, shortcuts=None, **kw) -> SetupPlan:
        if not self.confirmed or not self.game_dir:
            raise RuntimeError("build_plan called before the user confirmed the Review page")
        comps = [ComponentChoice(self._effective_option(k), True) for k in self.selected_kinds()]
        return SetupPlan(self.game_dir, comps, enable_mods=self.enable_mods, api_key=self.key_for_plan(),
                         desktop_icon=self.desktop_icon, start_menu=self.start_menu, app_installer=app_installer,
                         shortcuts=shortcuts, run_doctor=self.run_doctor, **kw)

    # ---------------------------------------------------------------- express path (one big Install button)
    def express_lines(self) -> list[str]:
        """What the single Install button will do, shown right next to it. Includes the UNRELEASED label."""
        out = [f"Cogitator {self.version}, desktop icon and Start Menu entry"]
        for kind in self.selected_kinds():
            opt = self.selected_option(kind)
            if opt is None:
                out.append(f"{self.components[kind].title}: {self.option_problem.get(kind) or 'no source available'}")
            else:
                out.append(f"{self.components[kind].title}: {opt.label}" + (" [UNRELEASED SNAPSHOT]" if opt.unreleased else ""))
        if self.enable_mods:
            out.append("Switch mods on")
        return out

    def express_prepare(self) -> tuple[bool, str]:
        """Auto-detect the game, pick the defaults, resolve sources. (ok, message to show when it cannot proceed)."""
        self.accepted = True  # the express screen shows the notice next to the button; Install = I understand
        if not self.autodetect_game():
            return False, self.game_message
        if self.game_running:
            return False, "Darktide is running. Close it, then press Retry."
        self.load_options()
        return self.install_ready()

    # ---------------------------------------------------------------- finish page
    def finish_lines(self, result: SetupResult) -> list[str]:
        """At most three short lines (what was installed, how to start it, what to do after a game update)."""
        if result.dry_run:
            return ["Dry run finished: nothing was installed or changed.", "", ""]
        if result.cancelled:
            return ["Setup was cancelled and nothing was changed.", "Run Setup again whenever you like.", ""]
        if not result.ok:
            return [result.what, result.why, f"Next step: {result.fix}"]
        parts = [c.title for k, c in self.components.items() if k == "app" or c.selected]
        return [f"Installed: {', '.join(parts)}." + (" Mods are on." if result.mods_on else " Mods are off."),
                "Start it with the Cogitator icon on your desktop.",
                "If mods stop working after a game update, open Cogitator and press T."]


def default_resolve_options(kind: str, premium: bool | None, key: str | None = None) -> tuple[list[SourceOption], str]:
    """Real resolver for the GUI (network, run on a worker thread)."""
    from cogitator.loader import LoaderError, make_client
    from cogitator.nexus import NexusClient, NexusError
    from cogitator.sources import resolve_github, resolve_nexus

    github = nexus = None
    problem = ""
    try:
        with make_client(timeout=15.0, github_only=True) as client:
            github = resolve_github(kind, client)
    except LoaderError as e:
        problem = str(e)
    except Exception:
        problem = "Could not reach GitHub. You can still choose a zip you downloaded."
    if premium and key:
        try:
            with NexusClient(key) as nx:
                nexus = resolve_nexus(kind, nx)
        except (NexusError, LoaderError):
            nexus = None
    opts = choose_sources(kind, premium=premium, github=github, nexus=nexus)
    return opts, ("" if opts else problem or "No source available.")
