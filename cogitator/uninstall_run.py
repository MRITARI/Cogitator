"""Everything the uninstaller window can remove, behind one entry point (no UI).

Order: the game side first (un-patch and verify, then remove DML/DMF and any listed user mods; all-or-nothing) -> the
stored Nexus key -> Cogitator itself -> Cogitator's own data last (it holds the rollback copies and the log). If the game
side fails or is cancelled it is already undone and nothing after it runs; later steps are best effort and every failure
is reported."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from . import __version__
from .config import cache_dir, clear_api_key, config_dir, redact
from .gamefinder import validate_game_dir
from .loadorder import LoadOrder
from .mods import load_order_path, mods_dir
from .patcher import ON, default_runner, patch_state
from .procs import darktide_running
from .registry import Registry
from .setup_run import ProgressEvent, StepResult, explain
from .uninstall import (UninstallPlan, UninstallResult, _sha, load_order_removable, owned_items, remove_app_data,
                        uninstall_framework, user_mod_folders)

log = logging.getLogger(__name__)


class AppRemover(Protocol):
    """Removes Cogitator itself for the current user (shortcuts, Apps & features entry, app folder)."""

    def describe(self) -> list[str]: ...
    def remove(self) -> list[str]: ...


@dataclass
class UninstallDeps:
    runner: Callable = default_runner
    is_running: Callable[[], bool] = darktide_running
    registry: Registry | None = None
    # asked (from the worker thread) before deleting files the user changed after install; default: keep them
    confirm: Callable[[str, str], bool] = lambda title, body: False


@dataclass
class FullUninstallPlan:
    game_dir: Path | None
    remove_dml: bool = True
    remove_dmf: bool = True
    user_mods: list[str] = field(default_factory=list)
    remove_load_order: bool = False
    remove_key: bool = True
    remove_data: bool = False
    app_remover: AppRemover | None = None  # present = remove Cogitator itself too

    @property
    def touches_game(self) -> bool:
        return bool(self.remove_dml or self.remove_dmf or self.user_mods or self.remove_load_order)


@dataclass
class FullUninstallResult:
    ok: bool = False
    cancelled: bool = False
    steps: list[StepResult] = field(default_factory=list)
    framework: UninstallResult | None = None
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    failed_step: str | None = None
    what: str = ""
    why: str = ""
    fix: str = ""
    mods_off: bool | None = None  # database not patched afterwards
    rollback_dir: Path | None = None
    dry_run: bool = False

    def diagnostics(self) -> str:
        lines = [f"Cogitator uninstall {__version__}", f"ok={self.ok} cancelled={self.cancelled} failed_step={self.failed_step}"]
        lines += [f"  [{s.state:<8}] {s.name}: {s.message}" for s in self.steps]
        if self.what:
            lines += [f"what: {self.what}", f"why:  {self.why}", f"fix:  {self.fix}"]
        lines += [f"failure: {f}" for f in self.failures]
        return redact("\n".join(lines))


@dataclass
class PreviewItem:
    group: str  # mods_off | framework | user_mod | load_order | key | app | data
    text: str
    path: str | None = None  # game-relative, for framework/user items
    note: str = ""


def dir_size(p: Path) -> int:
    total = 0
    try:
        for f in Path(p).rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def human(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def data_dirs() -> list[tuple[Path, int]]:
    return [(d, dir_size(d)) for d in (config_dir(), cache_dir()) if d.exists()]


def preview_uninstall(plan: FullUninstallPlan, registry: Registry | None = None) -> list[PreviewItem]:
    """The exact list shown on the Review page. For the game folder it is computed with the same functions the removal uses,
    so what is listed is what is deleted."""
    reg = registry or Registry.load()
    out: list[PreviewItem] = []
    game = plan.game_dir
    if game and plan.touches_game and validate_game_dir(game)[0]:
        if plan.remove_dml and patch_state(game) == ON:
            out.append(PreviewItem("mods_off", "Switch mods off first (the game's database is restored and checked before anything is deleted)"))
        for comp, on in (("dml", plan.remove_dml), ("dmf", plan.remove_dmf)):
            if not on:
                continue
            items, _ = owned_items(comp, reg)
            for it in items:
                p = game / it.rel
                if it.kind == "file" and p.is_file():
                    changed = bool(it.sha256 and _sha(p) != it.sha256)
                    out.append(PreviewItem("framework", it.rel, it.rel, "changed since it was installed - kept unless you approve" if changed else ""))
                elif it.kind == "dir" and p.is_dir():
                    out.append(PreviewItem("framework", it.rel + "/", it.rel + "/"))
        for name in plan.user_mods:
            out.append(PreviewItem("user_mod", f"mods/{name}/  (your mod, and its line in mod_load_order.txt)", f"mods/{name}/"))
        if plan.remove_load_order:
            remaining = [m for m in user_mod_folders(game) if m not in plan.user_mods]
            if load_order_removable(game, reg, removing_all_user_mods=not remaining) and load_order_path(game).is_file():
                out.append(PreviewItem("load_order", "mods/mod_load_order.txt", "mods/mod_load_order.txt"))
    if plan.remove_key:
        out.append(PreviewItem("key", "Your stored Nexus API key"))
    if plan.app_remover:
        out += [PreviewItem("app", t) for t in plan.app_remover.describe()]
    if plan.remove_data:
        out += [PreviewItem("data", f"{d}  ({human(n)})") for d, n in data_dirs()]
    return out


STEP_NAMES = {"check": "Checking", "mods_off": "Switching mods off", "remove": "Removing files", "finish": "Updating records",
              "key": "Removing your Nexus key", "app": "Removing Cogitator", "data": "Removing settings and caches"}


def planned_steps(plan: FullUninstallPlan) -> list[str]:
    s = ["check", "mods_off", "remove", "finish"] if plan.touches_game else []
    if plan.remove_key:
        s.append("key")
    if plan.app_remover:
        s.append("app")
    if plan.remove_data:
        s.append("data")
    return s


def run_uninstall(plan: FullUninstallPlan, progress: Callable[[ProgressEvent], None] | None = None,
                  cancel: threading.Event | None = None, deps: UninstallDeps | None = None) -> FullUninstallResult:
    deps = deps or UninstallDeps()
    emit = progress or (lambda e: None)
    cancel = cancel or threading.Event()
    names = planned_steps(plan)
    total = len(names)
    res = FullUninstallResult()
    reg = deps.registry or Registry.load()

    def done(name: str, state: str, msg: str = "") -> None:
        res.steps.append(StepResult(name, state, msg))
        emit(ProgressEvent(name, state, names.index(name), total, msg))

    if plan.touches_game:
        last = {"step": None}

        def forward(ev: ProgressEvent) -> None:  # the framework's own 4 steps, re-indexed into the overall list
            if ev.state == "start":
                if last["step"]:  # a step only reports its start; the next start means the previous one finished
                    emit(ProgressEvent(last["step"], "done", names.index(last["step"]), total))
                last["step"] = ev.step
            emit(ProgressEvent(ev.step, ev.state, ev.index, total, ev.message))

        def ask_modified(rels: list[str]) -> set[str]:
            body = ("These files were changed after Cogitator installed them:" + chr(10) + chr(10) + chr(10).join(rels) + chr(10) + chr(10) +
                    "Delete them too? (No keeps them.)")
            return set(rels) if deps.confirm("Files you changed", body) else set()

        fplan = UninstallPlan(Path(plan.game_dir), plan.remove_dml, plan.remove_dmf, list(plan.user_mods), plan.remove_load_order,
                              confirm_modified=ask_modified)
        fr = uninstall_framework(fplan, forward, cancel, registry=reg, runner=deps.runner, is_running=deps.is_running)
        res.framework, res.rollback_dir = fr, fr.rollback_dir
        res.removed += fr.removed
        res.warnings += fr.warnings
        if last["step"]:
            emit(ProgressEvent(last["step"], "done" if fr.ok else "failed", names.index(last["step"]), total))
        for st in ("check", "mods_off", "remove", "finish"):
            res.steps.append(StepResult(st, "done" if fr.ok else ("failed" if st == fr.failed_step else "skipped"), ""))
        if not fr.ok:
            res.cancelled = fr.cancelled
            res.failed_step, res.what, res.why, res.fix = fr.failed_step, fr.what, fr.why, fr.fix
            return res  # nothing after the framework phase may run
        res.mods_off = (patch_state(plan.game_dir) != ON) if plan.remove_dml else None

    def stage(name: str, fn: Callable[[], list[str] | None], label: str) -> None:
        if cancel.is_set():
            res.cancelled = True
            res.steps.append(StepResult(name, "skipped", "cancelled"))
            return
        emit(ProgressEvent(name, "start", names.index(name), total))
        try:
            got = fn() or []
            res.removed += got
            done(name, "done", ", ".join(got))
        except Exception as e:  # keep going: one stuck item must not strand the rest
            what, why, fix = explain(e)
            res.failures.append(f"{label}: {what}")
            if res.failed_step is None:
                res.failed_step, res.what, res.why, res.fix = name, f"{label}: {what}", why, fix
            done(name, "failed", what)

    if plan.remove_key:
        stage("key", lambda: (clear_api_key(), ["Nexus key"])[1], "Your Nexus key")
    if plan.app_remover:
        stage("app", plan.app_remover.remove, "Cogitator")
    if plan.remove_data:
        stage("data", lambda: remove_app_data(key=False, data=True), "Settings and caches")  # last: it holds the log and rollback copies
    res.ok = not res.failures and not res.cancelled
    if res.cancelled and not res.what:
        res.what, res.fix = "Uninstall was cancelled before it finished. What was already removed stays removed.", "Run the uninstaller again to finish."
    return res
