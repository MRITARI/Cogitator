"""The setup wizard's screen layer for the web window. All logic stays in `cogitator_setup` (SetupModel + Controller); this
module only turns their state into one JSON dict (`state()`), pushes it after every change, and validates the page's calls.

Nothing here knows the Nexus key: the model keeps it private and no payload contains it. The page never passes a path:
folders and zips come only from the native dialogs (seams), the Nexus page and a source's own URL come from the model."""
from __future__ import annotations

import functools
import logging
import threading
from pathlib import Path
from typing import Callable

from cogitator.config import redact
from cogitator.setup_run import ProgressEvent, SetupResult, planned_steps
from cogitator_setup.controller import Controller
from cogitator_setup.viewmodel import KEY_BLURB, NEXUS_API_PAGE, NOTICE, Page, Services, SetupModel

from .bridge import Bridge

log = logging.getLogger("cogitator.web")

TITLE = "Cogitator Setup"
STEP_LABELS = {
    "check_game": "Check the game folder", "check_running": "Make sure Darktide is closed", "app": "Install Cogitator",
    "dml": "Install the Darktide Mod Loader", "dmf": "Install the Darktide Mod Framework", "patch": "Switch mods on",
    "register": "Save your settings", "shortcuts": "Create shortcuts", "doctor": "Check everything",
}
KINDS = ("dml", "dmf")
OPTIONS = ("enable_mods", "desktop_icon", "start_menu", "run_doctor", "launch")
GAME_MAX, KEY_MAX = 500, 200
POLL_SECONDS = 0.04
LOG_MAX = 500


class Reject(Exception):
    pass


def _locked(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)

    return wrapper


def _launch_cogitator() -> None:
    from cogitator_setup.launch import launch_cogitator

    launch_cogitator()


class WizardSession:
    def __init__(self, bridge: Bridge | None = None, *, services: Services | None = None, model: SetupModel | None = None,
                 controller: Controller | None = None, dry_run: bool = False, repair: bool = False, first_run: bool = False) -> None:
        from cogitator_setup.dryrun import simulated_setup

        self.bridge = bridge or Bridge()
        self.model = model or SetupModel(services)
        self.model.repair, self.model.first_run = repair, first_run
        self.dry_run = dry_run
        self.ctl = controller or Controller(self.model, **({"run_fn": simulated_setup} if dry_run else {}))
        self._lock = threading.RLock()
        # seams (the app wires the native dialogs; tests replace them)
        self.pick_folder: Callable[[], str | None] = lambda: None
        self.pick_zip: Callable[[str], str | None] = lambda what: None
        self.open_url: Callable[[str], object] = lambda url: None
        self.launch: Callable[[], object] = (lambda: None) if dry_run else _launch_cogitator
        self.on_quit: Callable[[], None] = lambda: None
        self.in_wizard = False
        self.express = {"loading": False, "ok": False, "message": ""}
        self.game_loading = False
        self.key_busy = False
        self.key_status = ""
        self.review_loading = False
        self.zip_error = ""
        self.steps: list[dict] = []
        self.fraction = 0.0
        self.log_lines: list[str] = []
        self.result: SetupResult | None = None
        self.closing_ok = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ life cycle
    def start(self) -> None:
        """The page has rendered: start the poll thread, then open the express screen (or the wizard when the installer launched us)."""
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._poll, name="setup-poll", daemon=True)
                self._thread.start()
            if self.model.first_run:
                self._show_wizard()
            else:
                self._start_express()
            self._emit()

    def release(self) -> None:
        self._stop.set()
        self.bridge.close()

    def _poll(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.pump()
            except Exception:
                log.exception("setup poll failed")

    def pump(self) -> bool:
        """Drain controller messages and handle them (the poll thread; tests call it directly). True when anything was handled."""
        msgs = self.ctl.drain()
        if not msgs:
            return False
        with self._lock:
            for m in msgs:
                self._handle(m)
            self._emit()
        return True

    def _emit(self) -> None:
        self.bridge.emit(self.state())

    def _post_ask(self, kind: str, payload: dict, holder: dict) -> None:
        self.bridge.ctl.q.put(("ask", kind, payload, holder))

    def _ask_blocking(self, kind: str, payload: dict):
        holder = {"event": threading.Event(), "value": None}
        self._post_ask(kind, payload, holder)
        while not holder["event"].wait(0.1):
            if self._stop.is_set():
                return None
        return holder["value"]

    # ------------------------------------------------------------------ controller messages (same mapping as the tkinter `_handle`)
    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "express":
            self.express.update(loading=False, ok=bool(msg[1]), message=msg[2] or "")
            if self.model.game_dir:
                self.model.refresh_running()
        elif kind == "game":
            self.game_loading = False
        elif kind == "key":
            check = msg[1]
            self.key_busy = False
            self.key_status = check.message + (f"  Quota: {check.quota}." if check.ok and check.quota else "")
        elif kind == "options":
            self.review_loading = False
        elif kind == "progress":
            self._progress(msg[1])
        elif kind == "confirm":
            self._post_ask("confirm", {"title": msg[1], "body": msg[2]}, msg[3])  # Bridge.answer -> Controller.answer-compatible holder
        elif kind == "done":
            self._show_finish(msg[1])
        elif kind == "error":
            self.express["loading"] = self.game_loading = self.key_busy = self.review_loading = False
            self._post_ask("notice", {"title": TITLE, "body": f"Something went wrong ({msg[1]}). {msg[2]}"}, {"event": threading.Event(), "value": None})

    def _progress(self, ev: ProgressEvent) -> None:
        for s in self.steps:
            if s["name"] == ev.step:
                s["state"] = {"start": "running"}.get(ev.state, ev.state)
                s["detail"] = ev.message if ev.state != "start" else ""
        self.fraction = ev.fraction
        self.log_lines.append(redact(f"{ev.step}: {ev.state} {ev.message}".strip()))
        del self.log_lines[:-LOG_MAX]

    def _show_finish(self, res: SetupResult) -> None:
        self.result = res
        if not res.ok:
            self.model.launch = False
        if not res.doctor:
            self.model.run_doctor = False
        self._goto(Page.FINISH)

    # ------------------------------------------------------------------ navigation
    def _start_express(self) -> None:
        self.in_wizard = False
        self.express.update(loading=True, ok=False, message="")
        self.ctl.prepare_express()

    def _show_wizard(self) -> None:
        self.in_wizard = True
        self.model.page = Page.WELCOME
        self.model.accepted = False
        self._goto(Page.WELCOME)

    def _goto(self, page: Page) -> None:
        self.model.page = page
        if page is Page.GAME:
            self.game_loading = True
            self.ctl.detect_game()
        elif page is Page.REVIEW:
            self.review_loading = True
            self.zip_error = ""
            self.ctl.load_options()

    def _cancelling(self) -> bool:
        return self.ctl.cancel_event.is_set()

    # ------------------------------------------------------------------ page calls
    @_locked
    def accept(self, value: bool) -> None:
        self.model.accepted = bool(value)
        self._emit()

    @_locked
    def to_wizard(self) -> None:
        self._show_wizard()
        self._emit()

    @_locked
    def retry_express(self) -> None:
        self._start_express()
        self._emit()

    @_locked
    def next(self) -> None:
        p = self.model.page
        if p is Page.REVIEW:
            self._install()
        elif self.in_wizard and self.model.next():
            self._goto(self.model.page)
        self._emit()

    @_locked
    def back(self) -> None:
        if self.in_wizard and self.model.back():
            self._goto(self.model.page)
        self._emit()

    @_locked
    def cancel(self) -> None:
        if self.model.page is Page.PROGRESS and self.in_wizard:
            self.ctl.cancel()
            self._emit()
        else:
            self.on_quit()  # nothing has been changed before the Progress page

    def escape(self) -> None:
        """Esc: same rules as tkinter. Only before Progress; from Game onward it asks first."""
        with self._lock:
            if not self.in_wizard or self.model.page in (Page.PROGRESS, Page.FINISH):
                return
            ask = self.model.page is not Page.WELCOME
        if not ask:
            self.cancel()
            return

        def work() -> None:
            if self._ask_blocking("confirm", {"title": "Close setup?", "body": "Nothing has been installed yet. Close this window and lose what you entered?"}):
                self.cancel()

        threading.Thread(target=work, name="setup-escape", daemon=True).start()

    def on_close(self) -> bool:
        """The window's close button. False keeps the window open (a run in progress is asked to stop at a safe boundary instead)."""
        with self._lock:
            if self.closing_ok:
                return True
            if self.ctl.running:
                self.ctl.cancel()
                self._emit()
                return False
        return True

    # ---- game
    @_locked
    def retry_game(self) -> None:
        if self.model.game_dir:
            self.model.refresh_running()
        else:
            self.game_loading = True
            self.ctl.detect_game()
        self._emit()

    def browse_game(self) -> None:
        d = self.pick_folder()
        if d:
            with self._lock:
                self.model.set_game(d)
                self._emit()

    @_locked
    def typed_game(self, text: str) -> None:
        text = text.strip().strip('"')
        if text and (self.model.game_dir is None or str(self.model.game_dir) != text):
            self.model.set_game(text)
        self._emit()

    # ---- key (never echoed, never logged)
    @_locked
    def set_key(self, text: str) -> None:
        self.model.set_key(text)
        self.key_status = ""

    @_locked
    def validate_key(self) -> None:
        if not self.model.has_key():
            self.key_status = "Paste your key first, or press Skip."
        else:
            self.key_status = "Checking with Nexus Mods..."
            self.key_busy = True
            self.ctl.validate_key()
        self._emit()

    @_locked
    def skip_key(self) -> None:
        self.model.skip_key()
        self.key_status = "Skipped. You can add the key later with the K key in Cogitator."
        if self.model.page is Page.KEY and self.model.next():
            self._goto(self.model.page)
        self._emit()

    def open_key_page(self) -> None:
        self.open_url(NEXUS_API_PAGE)

    # ---- components and review
    @_locked
    def toggle(self, kind: str, value: bool) -> None:
        self.model.toggle(kind, bool(value))
        self._emit()

    @_locked
    def set_option(self, name: str, value: bool) -> None:
        setattr(self.model, name, bool(value))
        self._emit()

    @_locked
    def select_source(self, kind: str, index: int) -> None:
        self.model.select_option(kind, index)
        self.zip_error = ""
        self._emit()

    @_locked
    def open_source(self, kind: str) -> None:
        opt = self.model.selected_option(kind)
        if opt is not None and opt.url:
            self.open_url(opt.url)

    def choose_zip(self, kind: str) -> None:
        z = self.pick_zip(self.model.components[kind].title)
        if not z:
            return
        with self._lock:
            try:
                self.model.set_manual_zip(kind, Path(z))
                self.model.install_ready()
                self.zip_error = ""
            except Exception as e:
                self.zip_error = redact(str(e))
                self.model.manual_zip.pop(kind, None)
            self._emit()

    # ---- install
    @_locked
    def install(self) -> None:
        if self.model.page in (Page.REVIEW,) or not self.in_wizard:
            self._install()
        self._emit()

    def _install(self) -> None:
        if self.ctl.running or not self.model.confirm_install():
            return
        plan = self.model.build_plan()  # same arguments as the tkinter wizard: none
        self.in_wizard = True
        self.steps = [{"name": n, "label": STEP_LABELS.get(n, n), "state": "pending", "detail": ""} for n in planned_steps(plan)]
        self.fraction = 0.0
        self.log_lines = []
        self.model.page = Page.PROGRESS
        self.ctl.install(plan)

    # ---- finish
    @_locked
    def copy_details(self) -> str:
        return self.result.diagnostics() if self.result else ""  # redacted

    def finish(self) -> None:
        """As the tkinter `on_finish`: show the check-up results, start Cogitator, then close the window."""
        with self._lock:
            res = self.result
            show_doctor = bool(res and res.ok and self.model.run_doctor and res.doctor)
            do_launch = bool(res and res.ok and self.model.launch)

        def work() -> None:
            if show_doctor:
                from cogitator.doctor import format_report

                self._ask_blocking("notice", {"title": "Check-up results", "body": format_report(res.doctor), "button": "Close"})
            if do_launch:
                try:
                    self.launch()
                except OSError:
                    self._ask_blocking("notice", {"title": TITLE, "body": "Cogitator could not be started. Open it from the Start Menu."})
            self.on_quit()

        threading.Thread(target=work, name="setup-finish", daemon=True).start()

    # ------------------------------------------------------------------ the one state dict
    def _nav(self) -> dict:
        m, p = self.model, self.model.page
        ok, why = m.can_next()
        run_pages = p in (Page.PROGRESS, Page.FINISH)
        if not self.in_wizard:
            return {"visible": False, "reason": ""}
        if p is Page.REVIEW and self.review_loading:
            ok, why = False, "Working out where each download comes from..."
        if p is Page.GAME and self.game_loading and not m.game_dir:
            ok = False
        cancelling = self._cancelling() and p is Page.PROGRESS
        return {
            "visible": True,
            "back": {"label": "Back", "enabled": not (p is Page.WELCOME or run_pages)},
            "next": {"label": "Install" if p is Page.REVIEW else "Next", "enabled": bool(ok and not run_pages)},
            "cancel": {"label": "Cancelling..." if cancelling else ("Close" if p is Page.FINISH else "Cancel"), "enabled": not cancelling},
            "reason": "" if ok or run_pages else why,
        }

    def _game(self) -> dict:
        m = self.model
        if m.game_dir:
            msg = "Darktide is running. Close it, then press Retry." if m.game_running else "Found your Darktide folder."
        else:
            msg = "Looking for Darktide..." if self.game_loading else m.game_message
        return {"path": str(m.game_dir) if m.game_dir else "", "message": msg, "running": bool(m.game_running), "loading": self.game_loading}

    def _components(self) -> list[dict]:
        return [{"kind": k, "title": c.title, "description": c.description, "status": c.status, "status_text": c.status_text,
                 "selected": bool(c.selected), "required": bool(c.required)} for k, c in self.model.components.items() if k in KINDS]

    def _sources(self) -> list[dict]:
        out = []
        for kind in self.model.selected_kinds():
            opts = self.model.options.get(kind) or []
            if len(opts) <= 1 and not (opts and opts[0].kind == "manual"):
                continue
            chosen = self.model.selected_option(kind) or opts[0]
            out.append({"kind": kind, "title": self.model.components[kind].title, "choice": self.model.choice.get(kind, 0),
                        "options": [o.label for o in opts], "manual": chosen.kind == "manual", "has_url": bool(chosen.url)})
        return out

    def _review(self) -> dict:
        m = self.model
        ok, why = m.install_ready()
        status = "Working out where each download comes from..." if self.review_loading else (
            "Check what will happen, then press Install." if ok else why)
        return {"loading": self.review_loading, "ready": bool(ok), "status": status, "error": self.zip_error, "sources": self._sources(),
                "rows": [{"label": r.label, "lines": list(r.lines), "warning": r.warning} for r in m.review_rows()] if not self.review_loading else []}

    def _express(self) -> dict:
        m, e = self.model, self.express
        if e["loading"]:
            status = "Looking for Darktide and checking where the files will come from..."
        elif e["ok"]:
            status = f"Darktide found: {m.game_dir}\nThis will be installed:"
        else:
            status = e["message"] or "Cogitator could not get ready."
        return {"loading": e["loading"], "ok": e["ok"], "status": status, "lines": m.express_lines() if e["ok"] else [],
                "hint": "Clicking Install accepts the notice above and the downloads listed."}

    def _finish(self) -> dict:
        res = self.result
        if res is None:
            return {}
        lines = self.model.finish_lines(res)
        return {"ok": bool(res.ok), "cancelled": bool(res.cancelled), "dry_run": bool(res.dry_run),
                "title": "All done" if res.ok else ("Setup was cancelled" if res.cancelled else "Setup could not finish"),
                "lines": [x for x in lines if x], "doctor_available": bool(res.doctor), "launch_available": bool(res.ok),
                "run_doctor": bool(self.model.run_doctor), "launch": bool(self.model.launch)}

    def state(self) -> dict:
        with self._lock:
            m = self.model
            express_screen = not self.in_wizard and m.page is Page.WELCOME
            return {
                "type": "setup_state", "title": TITLE, "version": m.version, "dry_run": self.dry_run,
                "page": "express" if express_screen else m.page.value,
                "steps": [p.value for p in m._order()] if self.in_wizard else [],
                "nav": self._nav(), "notice": NOTICE, "accepted": bool(m.accepted),
                "express": self._express(), "game": self._game(),
                "key": {"blurb": KEY_BLURB, "has_key": m.has_key(), "busy": self.key_busy, "status": self.key_status,
                        "ok": bool(m.key_check and m.key_check.ok), "skipped": bool(m.key_skipped)},
                "components": self._components(),
                "enable_mods": bool(m.enable_mods), "desktop_icon": bool(m.desktop_icon), "start_menu": bool(m.start_menu),
                "review": self._review() if m.page is Page.REVIEW else {},
                "progress": {"steps": [dict(s) for s in self.steps], "fraction": self.fraction, "log": list(self.log_lines),
                             "cancelling": self._cancelling(), "running": bool(self.ctl.running)},
                "finish": self._finish() if m.page is Page.FINISH else {},
            }


# ---------------------------------------------------------------------------------------------- the page-facing object
def _call(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            out = fn(self, *args, **kwargs)
            return {"ok": True} if out is None else out
        except Reject as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("setup api call %s failed", fn.__name__)
            return {"ok": False, "error": redact(f"{type(e).__name__}: {e}")}

    return wrapper


def _flag(v: object, what: str = "The value") -> bool:
    if not isinstance(v, bool):
        raise Reject(f"{what} must be true or false.")
    return v


def _text(v: object, limit: int, what: str) -> str:
    if not isinstance(v, str) or len(v) > limit:
        raise Reject(f"{what} must be text of at most {limit} characters.")
    return v


class SetupApi:
    """`window.pywebview.api` for setup.html. Public methods are page calls; everything else starts with an underscore."""

    def __init__(self, session: WizardSession, on_ready: Callable[[], None] | None = None) -> None:
        self._w = session
        self._on_ready = on_ready or (lambda: None)

    def _kind(self, kind: object) -> str:
        if not isinstance(kind, str) or kind not in KINDS:
            raise Reject("Unknown component.")
        return kind

    @_call
    def boot(self):
        return {"ok": True, "state": self._w.state()}

    @_call
    def ready(self):
        self._on_ready()
        self._w.start()

    @_call
    def answer(self, qid, value):
        if not self._w.bridge.answer(qid, value):
            raise Reject("That answer does not match an open question.")

    @_call
    def accept(self, value):
        self._w.accept(_flag(value))

    @_call
    def to_wizard(self):
        self._w.to_wizard()

    @_call
    def retry_express(self):
        self._w.retry_express()

    @_call
    def next(self):
        self._w.next()

    @_call
    def back(self):
        self._w.back()

    @_call
    def cancel(self):
        self._w.cancel()

    @_call
    def escape(self):
        self._w.escape()

    @_call
    def retry_game(self):
        self._w.retry_game()

    @_call
    def browse_game(self):
        self._w.browse_game()

    @_call
    def typed_game(self, text):
        self._w.typed_game(_text(text, GAME_MAX, "The folder"))

    @_call
    def set_key(self, text):
        self._w.set_key(_text(text, KEY_MAX, "The key"))

    @_call
    def validate_key(self):
        self._w.validate_key()

    @_call
    def skip_key(self):
        self._w.skip_key()

    @_call
    def open_key_page(self):
        self._w.open_key_page()

    @_call
    def toggle(self, kind, value):
        self._w.toggle(self._kind(kind), _flag(value))

    @_call
    def set_option(self, name, value):
        if not isinstance(name, str) or name not in OPTIONS:
            raise Reject("Unknown option.")
        self._w.set_option(name, _flag(value))

    @_call
    def select_source(self, kind, index):
        kind = self._kind(kind)
        n = len(self._w.model.options.get(kind) or [])
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < n:
            raise Reject("Unknown source.")
        self._w.select_source(kind, index)

    @_call
    def open_source(self, kind):
        self._w.open_source(self._kind(kind))

    @_call
    def choose_zip(self, kind):
        self._w.choose_zip(self._kind(kind))

    @_call
    def install(self):
        self._w.install()

    @_call
    def copy_details(self):
        return {"ok": True, "text": self._w.copy_details()}

    @_call
    def finish(self):
        self._w.finish()
