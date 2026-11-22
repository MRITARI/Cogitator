"""Threads and messages between the engine and the window. No widgets here, so it is testable without a display.

Rules: workers never touch widgets. They post tuples to a queue that the window drains with after(). A worker that needs an
answer (a confirmation, a choice, some text) posts an ("ask", ...) message and blocks only itself until the window answers.

Messages (first item is the kind):
  ("log", text, style)                a line for the log pane (already redacted)
  ("ui", fn, args, kwargs)            call fn on the UI thread
  ("ask", kind, payload, holder)      kind: confirm | text | choice; the window answers with Controller.answer(holder, value)
  ("error", job_name, text)           a worker raised; text is redacted
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

from cogitator.config import redact

log = logging.getLogger("cogitator.gui")

CONFIRM, TEXT, CHOICE = "confirm", "text", "choice"


class Controller:
    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()
        self.closing = threading.Event()  # set when the window closes: blocked workers give up
        self._jobs: dict[str, threading.Thread] = {}
        self._names: dict[str, str] = {}  # group -> name of the job started in it
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    # ---- messages to the window
    def post(self, *msg) -> None:
        if not self.closing.is_set():
            self.q.put(msg)

    def log(self, text: str, style: str = "") -> None:
        self.post("log", redact(str(text)), style)

    def ui(self, fn: Callable, *args, **kwargs) -> None:
        """Run fn(*args, **kwargs) on the UI thread."""
        self.post("ui", fn, args, kwargs)

    def drain(self) -> list[tuple]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    # ---- workers
    def busy(self, group: str) -> bool:
        with self._lock:
            t = self._jobs.get(group)
            return bool(t and t.is_alive())

    def run(self, name: str, fn: Callable[[], None], group: str | None = None) -> bool:
        """Start `fn` on a daemon thread. One job per group at a time: a second one is refused (False) and says so in the log.
        An exception in fn is reported as an ("error", ...) message, never lost and never raised into the window."""
        group = group or name
        with self._lock:
            old = self._jobs.get(group)
            if old is not None and old.is_alive():
                busy = True
                running = self._names.get(group, group)
            else:
                busy = False
                running = ""
                self._names[group] = name

                def wrap() -> None:
                    try:
                        fn()
                    except Exception as e:  # a worker must never die silently or take the window with it
                        log.exception("worker %s failed", name)
                        self.post("error", name, redact(f"{type(e).__name__}: {e}"))

                t = threading.Thread(target=wrap, name=f"gui-{name}", daemon=True)
                self._jobs[group] = t
                self._threads.append(t)
                t.start()
        if busy:
            self.log(f"Cannot start '{name}': '{running}' is still running ({group} task). Try again when it has finished.", "yellow")
            return False
        return True

    def join(self, timeout: float = 10.0) -> None:  # tests
        for t in list(self._threads):
            t.join(timeout)

    def close(self) -> None:
        self.closing.set()

    # ---- questions from a worker
    def ask(self, kind: str, **payload):
        """Called from a worker. Blocks that worker (never the window) until answered; None/False when the window closes."""
        holder = {"event": threading.Event(), "value": None}
        self.post("ask", kind, payload, holder)
        while not holder["event"].wait(0.1):
            if self.closing.is_set():
                return None
        return holder["value"]

    @staticmethod
    def answer(holder: dict, value) -> None:
        holder["value"] = value
        holder["event"].set()


class GuiUi:
    """`cogitator.actions.Ui` for the window: every call is made from a worker thread and goes through the controller."""

    def __init__(self, ctl: Controller):
        self.ctl = ctl

    def log(self, msg: str, style: str = "") -> None:
        self.ctl.log(msg, style)

    def confirm(self, title: str, body: str = "", default_yes: bool = True) -> bool:
        return bool(self.ctl.ask(CONFIRM, title=title, body=body, default_yes=default_yes))

    def ask_text(self, title: str, hint: str = "", body: str = "", allow_blank: bool = False) -> str | None:
        return self.ctl.ask(TEXT, title=title, hint=hint, body=body, allow_blank=allow_blank, password=False)

    def choose(self, question: str, options: list[str], selected: int = 0) -> int | None:
        return self.ctl.ask(CHOICE, question=question, options=list(options), selected=selected)
