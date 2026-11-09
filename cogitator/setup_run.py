"""The install orchestrator behind Cogitator Setup (no UI).

Order: check game -> check game is closed -> app files -> DML -> DMF -> patch -> register -> shortcuts -> doctor. A failure
or cancel unwinds this run's steps in reverse and verifies the result. Every download source must be confirmed first."""
from __future__ import annotations

import copy
import logging
import platform
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from . import __version__
from .config import Settings, cache_dir, clear_api_key, get_api_key, redact, set_api_key
from .doctor import Check, run_doctor
from .frameworks import detect_framework
from .fsutil import atomic_write_bytes
from .fwupdate import install_framework_plan
from .gamefinder import validate_game_dir
from .installer import Transaction
from .loader import LoaderError, UnsafeArchive, make_client
from .nexus import InvalidKey, NexusClient, NexusError, RateLimited
from .patcher import ON, OFF, PatcherError, bak_path, db_path, default_runner, patch, patch_state
from .procs import darktide_running
from .registry import Registry
from .sources import SNAPSHOT_EXPLANATION, SourceOption, fetch_source

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ interfaces the GUI/installer layer provides

class AppInstaller(Protocol):
    """Puts the Cogitator application files in place for the current user."""

    def install(self) -> str: ...
    def rollback(self) -> None: ...


class ShortcutMaker(Protocol):
    def create(self, desktop: bool, start_menu: bool) -> list[Path]: ...
    def remove(self, paths: list[Path]) -> None: ...


# ------------------------------------------------------------------ plan / result

@dataclass
class ComponentChoice:
    option: SourceOption
    confirmed: bool = False  # set only by an explicit user action (the Install click)


@dataclass
class SetupPlan:
    game_dir: Path
    components: list[ComponentChoice] = field(default_factory=list)  # DML and/or DMF
    enable_mods: bool = True
    api_key: str | None = None  # already validated by the key page; stored through the existing key store only
    desktop_icon: bool = True
    start_menu: bool = True
    app_installer: AppInstaller | None = None
    shortcuts: ShortcutMaker | None = None
    run_doctor: bool = True
    doctor_offline: bool = False
    max_bytes: int = 500 * 1024 * 1024
    max_files: int = 20000


@dataclass
class SetupDeps:
    """Everything with side effects outside the files we own, injectable for tests."""

    client_factory: Callable[[], object] = make_client
    nexus_factory: Callable[[str], NexusClient] = NexusClient
    patch_runner: Callable = default_runner
    is_running: Callable[[], bool] = darktide_running
    registry: Registry | None = None
    doctor_fn: Callable[..., list[Check]] = run_doctor
    sleep: Callable[[float], None] = time.sleep
    # Asked (from the worker thread) when a download turns out NOT to be what was promised. Default: no.
    confirm: Callable[[str, str], bool] = lambda title, body: False


@dataclass
class ProgressEvent:
    step: str
    state: str  # start | done | skipped | failed | rolled_back
    index: int
    total: int
    message: str = ""

    @property
    def fraction(self) -> float:
        return min(1.0, (self.index + (0 if self.state == "start" else 1)) / max(1, self.total))


@dataclass
class StepResult:
    name: str
    state: str
    message: str = ""


@dataclass
class SetupResult:
    ok: bool = False
    cancelled: bool = False
    steps: list[StepResult] = field(default_factory=list)
    failed_step: str | None = None
    what: str = ""  # plain language: what happened
    why: str = ""  # why it matters
    fix: str = ""  # the single next step
    rolled_back: bool = False
    rollback_errors: list[str] = field(default_factory=list)
    doctor: list[Check] | None = None
    installed: dict = field(default_factory=dict)  # component -> version/source summary
    rollback_dirs: list[Path] = field(default_factory=list)
    mods_on: bool | None = None
    shortcut_paths: list[Path] = field(default_factory=list)
    dry_run: bool = False  # a simulated run (the --dry-run window): nothing was changed

    def diagnostics(self) -> str:
        """Redacted text for the 'Copy details' button."""
        lines = [f"Cogitator Setup {__version__} on {platform.platform()} / Python {platform.python_version()}",
                 f"ok={self.ok} cancelled={self.cancelled} failed_step={self.failed_step} rolled_back={self.rolled_back}"]
        lines += [f"  [{s.state:<11}] {s.name}: {s.message}" for s in self.steps]
        if self.what:
            lines += [f"what: {self.what}", f"why:  {self.why}", f"fix:  {self.fix}"]
        lines += [f"rollback problem: {e}" for e in self.rollback_errors]
        return redact("\n".join(lines))


class SetupError(Exception):
    def __init__(self, what: str, why: str = "", fix: str = ""):
        super().__init__(what)
        self.what, self.why, self.fix = what, why, fix


class SetupCancelled(Exception):
    pass


# ------------------------------------------------------------------ plain-language errors

def explain(e: BaseException) -> tuple[str, str, str]:
    """(what happened, why it matters, the single next step). Never includes secrets."""
    if isinstance(e, SetupError):
        return e.what, e.why, e.fix
    msg = str(e)
    if isinstance(e, UnsafeArchive):
        return ("A download contained files that tried to write outside the game folder, so it was rejected.",
                "Installing it could have damaged other files.", "Nothing was changed. Download the mod loader from Nexus Mods instead.")
    if isinstance(e, (LoaderError,)) and "running" in msg.lower():
        return ("Darktide is running.", "Changing game files while the game runs can corrupt them.", "Close Darktide, then press Retry.")
    if isinstance(e, LoaderError) and "rate limit" in msg.lower():
        return ("GitHub is limiting requests from your connection right now.", "Downloads from GitHub are paused for a while.",
                "Wait a few minutes and retry, or download the zips from Nexus Mods yourself.")
    if isinstance(e, LoaderError):
        return (msg, "The download could not be completed or verified, so nothing was installed.",
                "Check your internet connection and retry, or choose \"I'll download it myself\".")
    if isinstance(e, PatcherError):
        return (msg, "Without this step the installed mods will not load.",
                "Everything was left switched off. In Cogitator press T later, or in Steam use \"Verify integrity of game files\".")
    if isinstance(e, NexusError):
        return ("Nexus Mods could not be reached or refused the request.", "Update checks and Premium downloads need it.",
                "Retry later. You can add your key afterwards with the K key in Cogitator.")
    if isinstance(e, PermissionError):
        return ("Windows refused permission to write a file.", "Setup needs to write to the game folder and your user folder.",
                "Close anything using the game folder (like antivirus scans) and retry.")
    if isinstance(e, OSError):
        return (f"A file operation failed: {e.strerror or 'unknown error'}.", "Setup could not finish writing files.",
                "Check free disk space and retry.")
    return ("Something unexpected went wrong.", "Setup stopped and undid what it had done.", "Press \"Copy details\" and report it.")


# ------------------------------------------------------------------ key validation (page 3)

@dataclass
class KeyCheck:
    ok: bool
    premium: bool | None = None
    quota: str | None = None
    message: str = ""


def validate_key(key: str, nexus_factory: Callable[[str], NexusClient] = NexusClient) -> KeyCheck:
    """Live check for the key page. The returned text never contains the key."""
    key = (key or "").strip()
    if not key:
        return KeyCheck(False, message="Paste your key first, or press Skip.")
    try:
        with nexus_factory(key) as nx:
            who = nx.validate()
            prem = bool(who.get("is_premium"))
            return KeyCheck(True, prem, str(nx.quota), f"Key ok ({'premium' if prem else 'free'} account).")
    except InvalidKey:
        return KeyCheck(False, message="Nexus did not accept this key. Check that you copied all of it, or press Skip.")
    except RateLimited:
        return KeyCheck(False, message="Nexus is limiting requests right now. Try again in a few minutes, or press Skip.")
    except NexusError:
        return KeyCheck(False, message="Could not reach Nexus Mods. Check your connection, or press Skip and add the key later with the K key.")


# ------------------------------------------------------------------ the orchestrator

def planned_steps(plan: SetupPlan) -> list[str]:
    s = ["check_game", "check_running"]
    if plan.app_installer:
        s.append("app")
    s += [c.option.component for c in sorted(plan.components, key=lambda c: c.option.component != "dml")]
    if plan.enable_mods:
        s.append("patch")
    s.append("register")
    if plan.shortcuts and (plan.desktop_icon or plan.start_menu):
        s.append("shortcuts")
    if plan.run_doctor:
        s.append("doctor")
    return s


def run_setup(plan: SetupPlan, progress: Callable[[ProgressEvent], None] | None = None,
              cancel: threading.Event | None = None, deps: SetupDeps | None = None) -> SetupResult:
    deps = deps or SetupDeps()
    emit = progress or (lambda ev: None)
    cancel = cancel or threading.Event()
    game = Path(plan.game_dir)
    names = planned_steps(plan)
    res = SetupResult()
    undo: list[tuple[str, Callable[[], None]]] = []

    reg = deps.registry or Registry.load()
    snap_registry = copy.deepcopy(reg.data)
    settings_path = Settings.path()
    prev_settings = settings_path.read_bytes() if settings_path.is_file() else None
    prev_key = get_api_key()
    work = cache_dir() / "work"
    work.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="setup-", dir=work))
    keep_dirs: list[Path] = []
    current = names[0]

    def step_done(name: str, idx: int, state: str, msg: str = "") -> None:
        res.steps.append(StepResult(name, state, msg))
        emit(ProgressEvent(name, state, idx, len(names), msg))

    # ---- the steps
    def check_game() -> str:
        for c in plan.components:
            if not c.confirmed:
                raise SetupError("A download source was not confirmed.", "Cogitator never installs from a source you have not approved.",
                                 "Go back to the Review page and press Install.")
        ok, why = validate_game_dir(game)
        if not ok:
            raise SetupError("That folder does not look like a Darktide install.", why, "Choose the folder that contains bundle and binaries.")
        return str(game)

    def check_running() -> str:
        if deps.is_running():
            raise SetupError("Darktide is running.", "Changing game files while the game runs can corrupt them.", "Close Darktide, then press Retry.")
        return "game is not running"

    def app() -> str:
        where = plan.app_installer.install()
        undo.append(("app", plan.app_installer.rollback))
        return where

    def framework(choice: ComponentChoice) -> str:
        opt = choice.option
        with (deps.client_factory() if opt.kind.startswith("github") else _NullCtx()) as client:
            nx = None
            try:
                if opt.kind == "nexus-premium":
                    nx = deps.nexus_factory(plan.api_key or get_api_key())
                fp = fetch_source(opt, tmp, client=client, nexus=nx, sleep=deps.sleep)
            finally:
                if nx:
                    nx.close()
        if fp.source.verified is False:  # the pinned commit did not match the official release: fall back to a warning + confirm
            nl = chr(10)
            body = (f"The files downloaded for {opt.component.upper()} do not match the official Nexus release they were expected to equal, "
                    "so this is an UNRELEASED SNAPSHOT." + nl + nl + SNAPSHOT_EXPLANATION + nl + nl +
                    "Install it anyway? (Choosing No installs nothing; you can use the Nexus zip instead.)")
            if not deps.confirm("This download does not match the official release", body):
                raise SetupError("You chose not to install a download that does not match the official release.",
                                 "Unreleased code can behave differently from the version other players use.",
                                 "Download the zip from Nexus Mods and choose \"I'll download it myself\".")
        fi = install_framework_plan(fp, game, reg, runner=deps.patch_runner, is_running=deps.is_running,
                                    max_bytes=plan.max_bytes, max_files=plan.max_files)
        d = fi.result.backup_dir
        keep_dirs.append(d)
        res.rollback_dirs.append(d)
        undo.append((opt.component, lambda d=d: Transaction.from_journal(d).rollback()))
        res.installed[opt.component] = (fp.source.description if fp.source.verified is not None
                                        else f"{fp.source.version or 'unknown version'} ({fp.source.kind})")
        return f"{opt.component.upper()} {fp.source.version or ''} from {fp.source.kind}, {fi.result.written} files"

    def patch_step() -> str:
        if patch_state(game) == ON:
            res.mods_on = True
            return "SKIP:mods were already on"
        rb = cache_dir() / "rollback" / f"{time.strftime('%Y%m%d-%H%M%S')}-setup-db"
        rb.mkdir(parents=True, exist_ok=True)
        db, bak = db_path(game), bak_path(game)
        shutil.copy2(db, rb / "db")
        had_bak = bak.is_file()
        if had_bak:
            shutil.copy2(bak, rb / "bak")

        def restore() -> None:
            atomic_write_bytes(db, (rb / "db").read_bytes())
            if had_bak:
                atomic_write_bytes(bak, (rb / "bak").read_bytes())
            elif bak.exists():
                bak.unlink()
            if patch_state(game) != OFF:
                raise SetupError("The game could not be switched back to its previous state.", "", "In Steam use Verify integrity of game files.")
            shutil.rmtree(rb, ignore_errors=True)

        undo.append(("patch", restore))
        keep_dirs.append(rb)
        patch(game, reg, runner=deps.patch_runner, is_running=deps.is_running)
        res.mods_on = True
        return "mods are now ON (game database patched)"

    def register() -> str:
        if plan.api_key:
            set_api_key(plan.api_key)
            undo.append(("key", (lambda: set_api_key(prev_key)) if prev_key else clear_api_key))
        s = Settings.load()
        s.game_dir, s.notice_accepted = str(game), True
        s.save()
        undo.append(("settings", lambda: (atomic_write_bytes(settings_path, prev_settings) if prev_settings is not None
                                          else settings_path.unlink(missing_ok=True))))
        reg.save()
        return "settings saved" + (", Nexus key stored" if plan.api_key else "")

    def shortcuts() -> str:
        paths = plan.shortcuts.create(plan.desktop_icon, plan.start_menu)
        res.shortcut_paths = list(paths)
        undo.append(("shortcuts", lambda: plan.shortcuts.remove(list(paths))))
        return ", ".join(p.name for p in paths)

    def doctor_step() -> str:
        try:
            res.doctor = deps.doctor_fn(str(game), offline=plan.doctor_offline, registry=reg, running=deps.is_running)
            bad = sum(1 for c in res.doctor if c.level != "ok")
            return "no problems found" if not bad else f"{bad} thing(s) to look at"
        except Exception as e:  # diagnostics must never fail the install
            log.warning("doctor failed: %s", e)
            return "skipped (could not run)"

    fns: dict[str, Callable[[], str]] = {"check_game": check_game, "check_running": check_running, "app": app,
                                         "patch": patch_step, "register": register, "shortcuts": shortcuts, "doctor": doctor_step}
    for c in plan.components:
        fns[c.option.component] = (lambda c=c: framework(c))

    # ---- run, unwinding on failure or cancel
    err: BaseException | None = None
    try:
        for i, name in enumerate(names):
            current = name
            if cancel.is_set():
                raise SetupCancelled()
            emit(ProgressEvent(name, "start", i, len(names)))
            out = fns[name]()
            if out.startswith("SKIP:"):
                step_done(name, i, "skipped", out[5:])
            else:
                step_done(name, i, "done", out)
    except SetupCancelled:
        res.cancelled = True
        res.what, res.why, res.fix = ("Setup was cancelled.", "", "Nothing was left behind; run Setup again whenever you like.")
    except BaseException as e:  # noqa: BLE001 - everything must unwind
        err = e
        res.failed_step = current
        res.what, res.why, res.fix = explain(e)
        res.steps.append(StepResult(current, "failed", res.what))
        emit(ProgressEvent(current, "failed", names.index(current), len(names), res.what))
        log.error("setup step %s failed: %s", current, redact(repr(e)))

    if res.cancelled or err is not None:
        for name, fn in reversed(undo):
            try:
                fn()
                emit(ProgressEvent(name, "rolled_back", names.index(name) if name in names else 0, len(names)))
            except Exception as e:  # keep unwinding; report what could not be undone
                res.rollback_errors.append(f"{name}: {redact(str(e))}")
        reg.data.clear()
        reg.data.update(snap_registry)
        try:
            reg.save()
        except OSError:
            pass
        res.rolled_back = True
        res.mods_on = patch_state(game) == ON if game.is_dir() else None
        if err is not None and isinstance(err, KeyboardInterrupt):
            raise err
    else:
        res.ok = True
        for d in keep_dirs:
            if d.name.endswith("-setup-db"):  # the pre-patch copies are only for unwinding this run
                shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return res


class _NullCtx:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False
