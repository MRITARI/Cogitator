"""Threads and messages between the engine and the Tk UI. No widgets here, so it is testable without a display.

Rules: workers never touch widgets; they post tuples to a queue that the UI drains with after(). A worker that needs an
answer (the "hashes differ" question) posts a ("confirm", ...) message and blocks only itself until the UI answers.
"""
from __future__ import annotations

import dataclasses
import logging
import queue
import threading
from typing import Callable

from cogitator.config import redact
from cogitator.setup_run import ProgressEvent, SetupDeps, SetupPlan, SetupResult, run_setup

from .viewmodel import SetupModel

log = logging.getLogger(__name__)


class Controller:
    def __init__(self, model: SetupModel, deps: SetupDeps | None = None,
                 run_fn: Callable[..., SetupResult] = run_setup):
        self.model = model
        self.deps = deps or SetupDeps()
        self.run_fn = run_fn
        self.q: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self._threads: list[threading.Thread] = []

    # ---- plumbing
    def _spawn(self, name: str, fn: Callable[[], None]) -> threading.Thread:
        def wrap() -> None:
            try:
                fn()
            except Exception as e:  # a worker must never die silently or take the UI with it
                log.exception("worker %s failed", name)
                self.q.put(("error", name, redact(f"{type(e).__name__}: {e}")))

        t = threading.Thread(target=wrap, name=f"setup-{name}", daemon=True)
        self._threads.append(t)
        t.start()
        return t

    def drain(self) -> list[tuple]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def join(self, timeout: float = 10.0) -> None:  # tests
        for t in list(self._threads):
            t.join(timeout)

    # ---- model-backed async actions
    def prepare_express(self) -> None:
        self._spawn("express", lambda: self.q.put(("express", *self.model.express_prepare())))

    def detect_game(self) -> None:
        def work() -> None:
            ok = self.model.autodetect_game()
            self.q.put(("game", ok, self.model.game_message))
        self._spawn("detect", work)

    def validate_key(self) -> None:
        self._spawn("key", lambda: self.q.put(("key", self.model.validate_key())))

    def load_options(self) -> None:
        def work() -> None:
            self.model.load_options()
            self.q.put(("options", self.model.install_ready()))
        self._spawn("options", work)

    # ---- install run
    def ask(self, title: str, body: str) -> bool:
        """Called from the worker thread by the engine. Blocks that worker (not the UI) until answered."""
        holder = {"event": threading.Event(), "value": False}
        self.q.put(("confirm", title, body, holder))
        while not holder["event"].wait(0.1):
            if self.cancel_event.is_set():
                return False
        return bool(holder["value"])

    @staticmethod
    def answer(holder: dict, value: bool) -> None:
        holder["value"] = bool(value)
        holder["event"].set()

    def install(self, plan: SetupPlan) -> None:
        if self.running:
            return
        self.running = True
        self.cancel_event.clear()
        deps = dataclasses.replace(self.deps, confirm=self.ask)

        def work() -> None:
            try:
                res = self.run_fn(plan, lambda ev: self.q.put(("progress", ev)), self.cancel_event, deps)
            finally:
                self.running = False
            self.q.put(("done", res))

        self._spawn("install", work)

    def cancel(self) -> None:
        self.cancel_event.set()
