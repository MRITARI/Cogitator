"""The uninstaller / maintenance screens for the web window. All rules stay in `cogitator_setup.uninstall_vm` (UninstallModel,
MaintenanceModel) and the engine `cogitator.uninstall_run`; this module only turns their state into one JSON dict (`state()`),
pushes it after every change and forwards validated calls.

The page never sends a path, a mod folder that is not in `det.user_mods` or a "confirmed" flag. The second confirmation (deleting
the user's mods) is asked by Python through a modal; `model.confirm_user_mods()` runs only after Python received True."""
from __future__ import annotations

import logging
import threading
from typing import Callable

from cogitator.config import redact
from cogitator.setup_run import ProgressEvent
from cogitator.uninstall_run import STEP_NAMES, UninstallDeps, planned_steps, run_uninstall
from cogitator_setup.controller import Controller
from cogitator_setup.uninstall_vm import SAFE_NOTE, MaintenanceModel, UPage, UServices, UninstallModel

from .bridge import Bridge
from .setup_session import Reject, _call, _flag, _locked

log = logging.getLogger("cogitator.web")

TITLE = "Cogitator Uninstall"
POLL_SECONDS = 0.04
LOG_MAX = 500
OPTIONS = ("app", "dmf", "dml", "key", "data", "mods", "order")
CONFIRM_HINT = "Press Next to confirm which of your mods will be deleted."
REPAIR_NOTE = ("Repair reinstalls what Cogitator put in place and keeps your mods and mod list. In-app updates do not refresh this "
               "Setup program, so it may be out of date; to get the newest, run the latest Cogitator installer from the releases page.")
QUIT_DELAY = 0.3
STEPS = ["choose", "review", "progress", "finish"]


class UninstallSession:
    def __init__(self, bridge: Bridge | None = None, services: UServices | None = None, deps: UninstallDeps | None = None,
                 dry_run: bool = False, start: str = "choose", model: UninstallModel | None = None,
                 controller: Controller | None = None) -> None:
        from cogitator_setup.dryrun import simulated_uninstall

        self.bridge = bridge or Bridge()
        self.model = model or UninstallModel(services)
        self.model.dry_run = dry_run
        self.dry_run = dry_run
        self.ctl = controller or Controller(self.model, deps or UninstallDeps(),
                                            run_fn=simulated_uninstall if dry_run else run_uninstall)
        self._lock = threading.RLock()
        self.on_quit: Callable[[], None] = lambda: None
        self.quit_delay = QUIT_DELAY
        self.action: str | None = None  # "repair" / "update": what the caller does after the window closed
        self.result = None
        self.steps: list[dict] = []
        self.fraction = 0.0
        self.log_lines: list[str] = []
        self.review: list[tuple[str, list[str]]] = []
        self.closing_ok = False
        self.confirming = False
        self.update_msg = ""
        self.update_busy = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.model.load()
        self.maint: MaintenanceModel | None = None
        self.maintenance = start == "maintenance"
        if self.maintenance:
            self.maint = MaintenanceModel(self.model.det, self.model.svc.selfupdater())
        self.model.page = UPage.CHOOSE

    # ------------------------------------------------------------------ life cycle
    def start(self) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._poll, name="uninstall-poll", daemon=True)
                self._thread.start()
            self._emit()

    def release(self) -> None:
        self._stop.set()
        self.bridge.close()

    def _poll(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.pump()
            except Exception:
                log.exception("uninstall poll failed")

    def pump(self) -> bool:
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

    def _worker(self, name: str, fn: Callable[[], None]) -> None:
        def run() -> None:
            try:
                fn()
            except Exception as e:
                log.exception("uninstall worker %s failed", name)
                self._post_ask("notice", {"title": TITLE, "body": redact(f"Something went wrong ({type(e).__name__}: {e}).")},
                               {"event": threading.Event(), "value": None})

        threading.Thread(target=run, name=f"uninstall-{name}", daemon=True).start()

    # ------------------------------------------------------------------ controller messages (as the tkinter `_poll`)
    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "progress":
            self._progress(msg[1])
        elif kind == "confirm":
            self._post_ask("confirm", {"title": msg[1], "body": msg[2]}, msg[3])
        elif kind == "done":
            self._show_finish(msg[1])
        elif kind == "error":
            self._post_ask("notice", {"title": TITLE, "body": f"Something went wrong ({msg[1]}). {msg[2]}"},
                           {"event": threading.Event(), "value": None})

    def _progress(self, ev: ProgressEvent) -> None:
        for s in self.steps:
            if s["name"] == ev.step:
                s["state"] = {"start": "running"}.get(ev.state, ev.state)
                s["detail"] = ev.message if ev.state != "start" else ""
        self.fraction = ev.fraction
        self.log_lines.append(redact(f"{ev.step}: {ev.state} {ev.message}".strip()))
        del self.log_lines[:-LOG_MAX]

    def _show_finish(self, res) -> None:
        self.result = res
        self._goto(UPage.FINISH)

    def _goto(self, page: UPage) -> None:
        self.maintenance = False
        self.model.page = page
        self.review = self.model.review_text() if page is UPage.REVIEW else []

    def _cancelling(self) -> bool:
        return self.ctl.cancel_event.is_set()

    def _page(self) -> str:
        return "maintenance" if self.maintenance else self.model.page.value

    # ------------------------------------------------------------------ maintenance
    def _update_enabled(self) -> bool:
        return bool(self.maintenance and self.maint and self.maint.enabled("update"))

    @_locked
    def repair(self) -> None:
        if not self.maintenance:
            raise Reject("Repair is only offered on the first screen.")
        self.action = "repair"
        self.closing_ok = True
        self.on_quit()

    @_locked
    def go_uninstall(self) -> None:
        if not self.maintenance:
            raise Reject("Uninstall is only offered on the first screen.")
        self._goto(UPage.CHOOSE)
        self._emit()

    @_locked
    def update(self) -> None:
        if not self._update_enabled() or self.update_busy:
            raise Reject("There is no update to install.")
        self.update_busy = True
        self.update_msg = "Downloading and checking the update..."
        self._emit()
        self._worker("update", self._job_update)

    def _job_update(self) -> None:
        from cogitator.selfupdate import UpdateError

        quit_after = False
        try:
            msg = self.maint.install()
            quit_after = bool(self.maint.updater.layout.in_place)
        except UpdateError as e:
            msg = str(e)
        except Exception as e:
            log.exception("update failed")
            msg = redact(f"The update could not be installed ({type(e).__name__}: {e}).")
        with self._lock:
            self.update_msg = msg
            self.update_busy = False
            if quit_after:
                self.action = "update"
            self._emit()
        if quit_after:
            if self._stop.wait(self.quit_delay):
                return
            with self._lock:
                self.closing_ok = True
            self.on_quit()

    @_locked
    def remind(self) -> None:
        if not self._update_enabled() or self.update_busy:
            raise Reject("There is no update to postpone.")
        self.maint.remind_later()
        self.update_msg = ""
        self._emit()

    @_locked
    def skip(self) -> None:
        if not self._update_enabled() or self.update_busy:
            raise Reject("There is no update to skip.")
        self.maint.skip()
        self.update_msg = ""
        self._emit()

    # ------------------------------------------------------------------ choose
    def _option_state(self, name: str) -> tuple[bool, bool]:
        """(checked, enabled) for one fixed option; the same rules as the tkinter page."""
        m, d = self.model, self.model.det
        if name == "app":
            return bool(m.remove_app), m.svc.app_remover is not None
        if name == "dmf":
            return bool(m.remove_dmf), d.dmf != "missing"
        if name == "dml":
            return bool(m.remove_dml), d.dml != "missing"
        if name == "key":
            return bool(m.remove_key), True
        if name == "data":
            return bool(m.remove_data), True
        if name == "mods":
            return bool(m.remove_user_mods), bool(d.user_mods)
        return bool(m.remove_load_order), bool(m.load_order_offered())

    @_locked
    def toggle(self, name: str, value: bool) -> None:
        if self.maintenance or self.model.page is not UPage.CHOOSE:
            raise Reject("Options can only be changed on the first page.")
        if name not in OPTIONS:
            raise Reject("Unknown option.")
        if not self._option_state(name)[1]:
            raise Reject("That option is not available.")
        m = self.model
        if name == "mods":
            m.set_remove_user_mods(value)
        elif name == "order":
            m.set_remove_load_order(value)
        else:
            setattr(m, f"remove_{name}", value)
        self._emit()

    @_locked
    def toggle_mod(self, name: str, value: bool) -> None:
        m = self.model
        if self.maintenance or m.page is not UPage.CHOOSE or not m.remove_user_mods:
            raise Reject("Tick \"My installed mods\" first.")
        if name not in m.det.user_mods:
            raise Reject("Unknown mod folder.")
        m.toggle_user_mod(name, value)
        self._emit()

    @_locked
    def retry(self) -> None:
        self.model.game_running = bool(self.model.svc.is_running())
        self._emit()

    # ------------------------------------------------------------------ navigation
    def next(self) -> None:
        with self._lock:
            m = self.model
            if self.maintenance or self.ctl.running:
                return
            if m.page is UPage.CHOOSE and m.needs_user_mod_confirmation:
                if self.confirming or not m.can_ask_confirmation():
                    self._emit()
                    return
                self.confirming = True
                text = m.user_mod_confirmation_text()
                self._worker("confirm", lambda: self._confirm_then_next(text))
                return
            if m.page is UPage.REVIEW:
                self._start_run()
            elif m.page is UPage.CHOOSE and m.next():
                self._goto(m.page)
            self._emit()

    def _confirm_then_next(self, text: str) -> None:
        try:
            yes = self._ask_blocking("confirm", {"title": "Delete these mods?", "body": text}) is True
        finally:
            with self._lock:
                self.confirming = False
        with self._lock:
            m = self.model
            # only on True, and only if what was asked is still what would be deleted
            if yes and m.page is UPage.CHOOSE and m.needs_user_mod_confirmation and m.user_mod_confirmation_text() == text:
                m.confirm_user_mods()
                if m.next():
                    self._goto(m.page)
            self._emit()

    def _start_run(self) -> None:
        m = self.model
        if self.ctl.running or not m.can_next()[0] or m.needs_user_mod_confirmation:
            return
        plan = m.build_plan()
        self.steps = [{"name": n, "label": STEP_NAMES.get(n, n), "state": "pending", "detail": ""} for n in planned_steps(plan)]
        self.fraction = 0.0
        self.log_lines = []
        self._goto(UPage.PROGRESS)
        self.ctl.install(plan)

    @_locked
    def back(self) -> None:
        if not self.maintenance and not self.ctl.running and self.model.back():
            self._goto(self.model.page)
        self._emit()

    @_locked
    def cancel(self) -> None:
        if self.model.page is UPage.PROGRESS and not self.maintenance:
            self.ctl.cancel()
            self._emit()
        else:
            self._quit()

    @_locked
    def close(self) -> None:
        if self.ctl.running:
            self.ctl.cancel()
            self._emit()
        else:
            self._quit()

    def _quit(self) -> None:
        self.closing_ok = True
        self.on_quit()

    def escape(self) -> None:
        """Esc closes only on Choose and Review (as tkinter)."""
        with self._lock:
            if self.maintenance or self.model.page not in (UPage.CHOOSE, UPage.REVIEW) or self.ctl.running:
                return
            self._quit()

    def on_close(self) -> bool:
        """The window's close button. False keeps the window open (a run is asked to stop at a safe boundary instead)."""
        with self._lock:
            if self.closing_ok:
                return True
            if self.ctl.running:
                self.ctl.cancel()
                self._emit()
                return False
        return True

    @_locked
    def copy_details(self) -> str:
        return self.result.diagnostics() if self.result is not None and hasattr(self.result, "diagnostics") else ""

    # ------------------------------------------------------------------ the one state dict
    def _nav(self) -> dict:
        m, p = self.model, self.model.page
        if self.maintenance:
            return {"visible": False, "reason": ""}
        ok, why = m.can_next()
        asks = bool(not ok and m.can_ask_confirmation())
        if asks:
            ok, why = True, ""  # Next asks the second confirmation before it goes on
        running = p in (UPage.PROGRESS, UPage.FINISH)
        cancelling = self._cancelling() and p is UPage.PROGRESS
        return {
            "visible": True,
            "back": {"label": "Back", "enabled": p is UPage.REVIEW},
            "next": {"label": "Remove" if p is UPage.REVIEW else "Next", "enabled": bool(ok and not running and not self.confirming)},
            "cancel": {"label": "Cancelling..." if cancelling else ("Close" if p is UPage.FINISH else "Cancel"), "enabled": not cancelling},
            "reason": (CONFIRM_HINT if asks else "") if (ok or running) else why,
        }

    def _maintenance(self) -> dict:
        mt = self.maint
        if mt is None:
            return {}
        has = bool(mt.enabled("update"))
        return {"summary": mt.summary(), "repair": mt.enabled("repair"), "update": has and not self.update_busy,
                "uninstall": mt.enabled("uninstall"), "update_available": has, "notes": mt.notes(), "message": self.update_msg,
                "busy": self.update_busy, "repair_note": REPAIR_NOTE}

    def _choose(self) -> dict:
        m, d = self.model, self.model.det
        labels = {"app": "Cogitator and its shortcuts", "dmf": "Darktide Mod Framework",
                  "dml": "Darktide Mod Loader, and switch mods off (the game goes back to normal)",
                  "key": "My stored Nexus key", "data": "Cogitator's settings, caches and rollback copies",
                  "mods": "My installed mods (this deletes their folders)", "order": "Also delete my mod list (mod_load_order.txt)"}
        notes = {"app": "" if m.svc.app_remover is not None else "Removed by the installed version.",
                 "dmf": "Not installed." if d.dmf == "missing" else "", "dml": "Not installed." if d.dml == "missing" else ""}
        opts = []
        for name in OPTIONS:
            checked, enabled = self._option_state(name)
            opts.append({"name": name, "label": labels[name], "checked": checked, "enabled": enabled, "note": notes.get(name, "")})
        info = [f"{p} ({s})" for p, s in m.data_summary()]
        return {"options": opts, "mods": [{"name": n, "checked": n in m.user_mods_selected} for n in d.user_mods] if m.remove_user_mods else [],
                "data": info, "safe_note": SAFE_NOTE,
                "running": bool(m.game_running), "running_text": "Darktide is running. Close it, then press Retry." if m.game_running else ""}

    def _finish(self) -> dict:
        res = self.result
        if res is None:
            return {}
        return {"ok": bool(res.ok), "cancelled": bool(res.cancelled), "dry_run": bool(getattr(res, "dry_run", False)),
                "title": "Done" if res.ok else ("Uninstall was cancelled" if res.cancelled else "Uninstall could not finish"),
                "lines": [x for x in self.model.finish_lines(res) if x]}

    def state(self) -> dict:
        with self._lock:
            m, page = self.model, self._page()
            return {
                "type": "uninstall_state", "title": TITLE, "version": m.version, "dry_run": self.dry_run, "page": page,
                "steps": [] if self.maintenance else STEPS, "nav": self._nav(),
                "maintenance": self._maintenance() if self.maintenance else {},
                "choose": self._choose() if page == "choose" else {},
                "review": [{"title": t, "lines": list(ls)} for t, ls in self.review] if page == "review" else [],
                "progress": {"steps": [dict(s) for s in self.steps], "fraction": self.fraction, "log": list(self.log_lines),
                             "cancelling": self._cancelling(), "running": bool(self.ctl.running)},
                "finish": self._finish() if page == "finish" else {},
            }


# ---------------------------------------------------------------------------------------------- the page-facing object
class UninstallApi:
    """`window.pywebview.api` for uninstall.html. Public methods are page calls; everything else starts with an underscore."""

    def __init__(self, session: UninstallSession, on_ready: Callable[[], None] | None = None) -> None:
        self._w = session
        self._on_ready = on_ready or (lambda: None)

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
    def repair(self):
        self._w.repair()

    @_call
    def update(self):
        self._w.update()

    @_call
    def remind(self):
        self._w.remind()

    @_call
    def skip(self):
        self._w.skip()

    @_call
    def go_uninstall(self):
        self._w.go_uninstall()

    @_call
    def toggle(self, name, value):
        if not isinstance(name, str) or name not in OPTIONS:
            raise Reject("Unknown option.")
        self._w.toggle(name, _flag(value))

    @_call
    def toggle_mod(self, name, value):
        det = self._w.model.det
        if not isinstance(name, str) or not det or name not in det.user_mods:
            raise Reject("Unknown mod folder.")
        self._w.toggle_mod(name, _flag(value))

    @_call
    def retry(self):
        self._w.retry()

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
    def copy_details(self):
        return {"ok": True, "text": self._w.copy_details()}

    @_call
    def close(self):
        self._w.close()
