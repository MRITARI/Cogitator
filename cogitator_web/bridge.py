"""Python -> page messages and page -> worker answers. No `webview` import: the window is any object with `evaluate_js(script)`.

Threading: API calls arrive on pywebview's threads and workers run on their own. Neither touches the window. Everything goes
onto the queue of one `Controller` and ONE pusher thread drains it: it runs `ui` callbacks, turns questions into `ask`
payloads and sends batches with `window.evaluate_js` (JSON from `json.dumps`, never string-built). A worker that asks a
question (confirm / text / choice) blocks only itself on an Event until the page calls `answer`."""
from __future__ import annotations

import itertools
import json
import logging
import threading

from cogitator.config import redact
from cogitator_gui.controller import CHOICE, CONFIRM, TEXT, Controller

log = logging.getLogger("cogitator.web")

ENTRY = "window.cogitatorPush"  # defined by ui/js/store.js
BATCH = 64


class Bridge:
    def __init__(self) -> None:
        self.ctl = Controller()
        self._window = None
        self._thread: threading.Thread | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[str, dict, dict]] = {}
        self._plock = threading.Lock()
        self._wake = threading.Event()

    # ---- going out
    def emit(self, payload: dict) -> None:
        """Queue one payload for the page (any thread)."""
        if not self.ctl.closing.is_set():
            self.ctl.q.put(("emit", payload))

    def log(self, text: str, style: str = "") -> None:
        self.ctl.log(text, style)  # redacted by the controller

    def attach(self, window) -> None:
        """Start pushing to `window` (call once the page has loaded; earlier messages wait in the queue)."""
        self._window = window
        if self._thread is None:
            self._thread = threading.Thread(target=self._pump, name="web-pusher", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self.ctl.close()
        with self._plock:  # release every blocked worker
            for _, _, holder in self._pending.values():
                Controller.answer(holder, None)
            self._pending.clear()

    # ---- the one pusher
    def _pump(self) -> None:
        while not self.ctl.closing.is_set():
            try:
                first = self.ctl.q.get(timeout=0.2)
            except Exception:
                continue
            msgs = [first]
            while len(msgs) < BATCH:
                try:
                    msgs.append(self.ctl.q.get_nowait())
                except Exception:
                    break
            out: list[dict] = []
            for m in msgs:
                p = self._translate(m)
                if p is not None:
                    out.append(p)
            if out:
                self._send(out)

    def _translate(self, msg: tuple) -> dict | None:
        kind = msg[0]
        try:
            if kind == "emit":
                return msg[1]
            if kind == "log":
                return {"type": "log", "text": msg[1], "style": msg[2]}
            if kind == "error":
                return {"type": "log", "text": f"{msg[1]} failed: {msg[2]}", "style": "bold red"}
            if kind == "ui":  # fn(*args) on the pusher thread, like Tk's main thread; keep these short
                _, fn, args, kwargs = msg
                fn(*args, **kwargs)
                return None
            if kind == "ask":
                _, qkind, payload, holder = msg
                qid = next(self._ids)
                with self._plock:
                    self._pending[qid] = (qkind, payload, holder)
                return {"type": "ask", "id": qid, "kind": qkind, **_jsonable(payload)}
        except Exception:
            log.exception("could not handle message %s", kind)
        return None

    def _send(self, payloads: list[dict]) -> None:
        try:
            script = f"{ENTRY}({json.dumps(payloads)})"
        except (TypeError, ValueError):
            log.exception("payload is not JSON-serialisable; dropped")
            return
        try:
            self._window.evaluate_js(script)
        except Exception as e:  # window closing, page reloading: never take the pusher down
            log.warning("push failed: %s", redact(str(e)))

    # ---- answers from the page
    def answer(self, qid: object, value: object) -> bool:
        """Validate and deliver the answer to question `qid`. False when the id is unknown or the value has the wrong shape."""
        if not isinstance(qid, int) or isinstance(qid, bool):
            return False
        with self._plock:
            item = self._pending.get(qid)
        if item is None:
            return False
        kind, payload, holder = item
        if kind == CONFIRM:
            if not isinstance(value, bool):
                return False
        elif kind == TEXT:
            if value is not None and not (isinstance(value, str) and len(value) <= 4000):
                return False
        elif kind == CHOICE:
            n = len(payload.get("options") or [])
            if value is not None and not (isinstance(value, int) and not isinstance(value, bool) and 0 <= value < n):
                return False
        elif kind == "notice":
            if value is not True and value is not None:
                return False
        else:
            return False
        with self._plock:
            self._pending.pop(qid, None)
        Controller.answer(holder, value)
        return True


def _jsonable(payload: dict) -> dict:
    out = {}
    for k, v in payload.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [x if isinstance(x, (str, int, float, bool, type(None))) else str(x) for x in v]
        else:
            out[k] = str(v)
    return out
