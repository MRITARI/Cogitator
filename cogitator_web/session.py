"""Everything the page can do, without a window: state (game folder, registry, mod table model) and the flows.

It reuses the engine and the shared pieces: `cogitator.actions` (flows), `cogitator.viewtext` (wording), `cogitator_web.viewmodel`
(filter / sort) and `cogitator_web.controller` (workers, questions). It talks to the page only through `Bridge.emit`.

Threading: short operations (toggle, move...) run on the calling API thread under `_mlock`. Anything that waits (network,
zips, questions) runs on a Controller worker. Callbacks scheduled with `ctl.ui` run on the pusher thread and must stay short and
never take `self.lock` (the flow lock a worker may hold for a long time)."""
from __future__ import annotations

import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable

from cogitator import __version__, actions, actions_nexus, actions_setup, profiles, snapshots
from cogitator import nexus_info, nxm, repair
from cogitator.config import InstanceLock, LockError, Settings, cache_dir, config_dir, get_api_key, log_path, redact
from cogitator.frameworks import detect_framework
from cogitator.gamefinder import resolve_game_dir, validate_game_dir
from cogitator.linking import folder_mtime
from cogitator.loader import NEXUS_PAGES
from cogitator.modinstall import open_files_page
from cogitator.nexus import GAME, parse_nexus_ref
from cogitator.mods import DISABLED, ENABLED, UNLISTED, ModError, mods_dir, move_mod, move_mod_to, scan_mods, toggle_mod
from cogitator.patcher import ON, PatcherError, banner as patch_banner, default_runner, patch_state
from cogitator.patcher import toggle as toggle_mods
from cogitator.procs import darktide_running
from cogitator.registry import Registry
from cogitator.updates import (PROBABLY_OUTDATED, UPDATE, CheckBundle, FrameworkUpdate, ModRow, cached_frameworks, cached_rows,
                               quota_text, run_checks)
from cogitator.viewtext import (NOTICE_BODY, NOTICE_TITLE, gui_words, installed_label, mod_suffix, report_segments, status_kind, status_label)
from cogitator_web import background
from cogitator_web.controller import TEXT, GuiUi
from cogitator_web.viewmodel import ARROW, TAG_BAD, TAG_DIM, TAG_OK, TAG_WARN, ModListModel

from .bridge import Bridge

log = logging.getLogger("cogitator.web")

NOTICE = "notice"  # a question kind only the web page knows: a message with one button (answer: True)
TONE = {TAG_BAD: "bad", TAG_WARN: "warn", TAG_OK: "ok", TAG_DIM: "dim", "": ""}
GUI_THEMES = ("system", "light", "dark")
AUTO_REPAIR = ("off", "ask", "auto")
GUI_BACKGROUNDS = ("default", "plain", "custom")
# boolean / choice settings the page may change: name -> allowed values (bool = a switch)
SETTING_CHOICES = {"gui_theme": GUI_THEMES, "auto_repair": AUTO_REPAIR, "gui_background": GUI_BACKGROUNDS}
SETTING_BOOLS = ("check_updates_on_start", "update_consent")
ROW_ACTIONS = ("toggle", "open_page", "open_folder", "pin", "rollback", "remove",
               "link", "update")
GLOBAL_ACTIONS = ("update_all", "link_all", "framework_update")
BULK_IDS = ("enable", "disable", "update", "remove")
HELP_BODY = (
    "Space or double-click   turn the selected mod on or off\n"
    "Ctrl+Up / Ctrl+Down     move it in the load order\n"
    "Right-click a mod       every action for that mod\n"
    "Click, Ctrl+click, Shift+click, a dragged box or Ctrl+A select several mods; the bar under the list acts on all of them\n"
    "R or F5                 check Nexus for updates      I or Ctrl+I   import zip(s)\n"
    "G or Ctrl+G             launch Darktide              D or Ctrl+D   doctor (diagnose problems)\n"
    "T or Ctrl+T             all mods on / off (the game patch)\n"
    "K or Ctrl+K             Nexus API key (Settings)\n"
    "N   tags and note       *   favourite                / or Ctrl+F   filter the list\n"
    "Ctrl+U   update all     M   link all mods to Nexus     F   update Mod Loader / Framework\n"
    "F3  search Nexus Mods (the Tools page also has doctor, find a crashing mod, crash-log leads)\n"
    "O   sort (cycle)        F1  this help\n"
    "\n"
    "Log file: {log}\n"
    "Settings folder: {cfg}\n"
    "Problems: run the doctor, then report at github.com/MRITARI/Cogitator/issues and attach the log file.\n"
    "Not affiliated with Fatshark, Games Workshop or Nexus Mods.")
BAD_NAME_CHARS = set('\\/:*?"<>|\0')


def valid_folder_name(name: object) -> bool:
    """A mod folder name is one plain path component: no separators, drive colon, wildcard or dot-dot."""
    return (isinstance(name, str) and 0 < len(name) <= 255 and name not in (".", "..") and not (set(name) & BAD_NAME_CHARS)
            and name == name.strip())


class Session:
    def __init__(self, bridge: Bridge, settings: Settings | None = None, *, game_dir: Path | None = None,
                 game_override: str | None = None, check_updates: bool = True, show_notice: bool = True) -> None:
        self.bridge = bridge
        self.ctl = bridge.ctl
        self.ui = GuiUi(self.ctl)
        self.settings = settings or Settings.load()
        self.given_game = Path(game_dir) if game_dir else None
        self.override = game_override
        self.check_on_start = check_updates
        self.show_notice = show_notice
        self.game_dir: Path | None = None
        self.registry: Registry | None = None
        self.model = ModListModel(meta=lambda f: actions.mod_meta(self.registry, f),
                                  mtime=lambda f: folder_mtime(mods_dir(self.game_dir) / f) if self.game_dir else None)
        self.lock = threading.RLock()  # flow lock: serialises multi-step registry work between workers
        self._mlock = threading.RLock()  # guards `model` only; always short
        self.instance_lock: InstanceLock | None = None
        self.fw_updates: list[FrameworkUpdate] = []
        self.checking = False
        self.started = False
        # seams: the window supplies the native dialogs; tests replace anything here
        self.pick_folder: Callable[[], str | None] = lambda: None
        self.pick_image: Callable[[], str | None] = lambda: None
        self.pick_files: Callable[[Path], list[str] | None] = lambda start: None
        self.on_quit: Callable[[], None] = lambda: None
        self.game_running: Callable[[], bool] = darktide_running
        self.patch_runner = default_runner
        self.open_url: Callable[[str], object] = webbrowser.open
        self.open_path: Callable[[Path], object] = _startfile
        self.download_wait_s = 180.0
        self._nxm_busy = False
        self._dlock = threading.Lock()  # the details worker: one at a time, the newest request wins
        self._details_want: str | None = None
        self._details_busy = False
        self._last_patch_state: str | None = None
        self._ticker: threading.Thread | None = None
        self.selfupdater = None  # SelfUpdater, created once the registry is loaded; tests inject their own

    # ================================================================== helpers
    def say(self, text: str, style: str = "") -> None:
        self.ctl.log(text, style)

    def ready(self) -> bool:
        if self.game_dir and self.registry:
            return True
        self.say("No game folder selected yet.", "yellow")
        return False

    def ctx(self) -> actions.Ctx:
        return actions.Ctx(self.game_dir, self.registry, self.settings, self.lock, self.patch_runner, self.game_running,
                           cancelled=self.ctl.closing.is_set, open_url=self.open_url, download_wait_s=self.download_wait_s)

    def job(self, name: str, group: str, fn: Callable[[], None]) -> bool:
        return self.ctl.run(name, fn, group)

    def mod_known(self, folder: object, present: bool = False) -> bool:
        if not valid_folder_name(folder):
            return False
        with self._mlock:
            m = self.model.mod(folder)
        return m is not None and (m.present or not present)

    # ================================================================== payloads
    def row_payload(self, folder: str) -> dict | None:
        """One table row as plain data (the page formats nothing itself)."""
        with self._mlock:
            m = self.model.mod(folder)
            if m is None:
                return None
            r = self.model.urows.get(folder)
            meta = self.model.meta(folder)
            installed = installed_label(r, None if r is not None and r.installed else self.model.mtime(folder))[0]
            status = status_label(r, ARROW, m, gui=True)[0]
            suffix = mod_suffix(m)
            return {"folder": folder, "en": "on" if m.state == ENABLED else ("off" if m.state == DISABLED else "unlisted"),
                    "present": m.present, "suffix": (suffix[0].strip() if suffix else ""), "installed": installed,
                    "latest": (r.latest if r else None) or "-", "status": status, "tone": TONE.get(self.model.status_tag(m), ""),
                    "kind": status_kind(r, m),
                    "pinned": meta.get("pinned_version") or "",
                    "rollback": bool((self.registry.mod(folder) or {}).get("previous")) if self.registry else False, "linked": bool(r is not None and r.nexus_id),
                    "hint": self.model.hint(folder)}

    def header_payload(self) -> dict | None:
        if not self.game_dir or not self.registry:
            return None
        state = patch_state(self.game_dir)
        by = {u.kind: u for u in (self.fw_updates or cached_frameworks(self.registry))}
        fw = {}
        for kind in ("dml", "dmf"):
            st = detect_framework(self.game_dir, kind)
            rec = self.registry.framework(kind) or {}
            u = by.get(kind)
            fw[kind] = {"status": st.status, "version": rec.get("version") or ("" if st.status != "installed" else "unknown"),
                        "update": (u.latest or "") if u is not None and u.status == UPDATE else ""}
        banner = patch_banner(self.game_dir)
        return {"game_dir": str(self.game_dir), "frameworks": fw, "mods": state, "banner": gui_words(banner) if banner else "",
                "nexus": self.nexus_payload(), "checking": self.checking, "update_banner": self.update_banner(),
                "update_version": self.update_version()}

    def nexus_payload(self) -> dict:
        premium = self.registry.data["updates"].get("premium") if self.registry else None
        return {"key_set": bool(get_api_key()), "premium": premium, "quota": (quota_text(self.registry) if self.registry else None) or ""}

    def emit_header(self) -> None:
        h = self.header_payload()
        if h is not None:
            self.bridge.emit({"type": "header", "header": h})

    def emit_state(self, keep: str | None = None) -> None:
        """The whole table (order and filter applied). The page reconciles it row by row."""
        with self._mlock:
            order = [rv.folder for rv in self.model.rows(keep)]
            summary, sort = self.model.summary(), {"col": self.model.sort_col, "desc": self.model.sort_desc}
            filt = self.model.filter_text
        rows = [p for p in (self.row_payload(f) for f in order) if p is not None]
        self.bridge.emit({"type": "state", "rows": rows, "summary": summary, "sort": sort, "filter": filt, "keep": keep,
                          "empty": self.model.hint(None)})
        self.emit_header()

    def emit_row(self, folder: str) -> None:
        p = self.row_payload(folder)
        if p is not None:
            with self._mlock:
                summary = self.model.summary()
            self.bridge.emit({"type": "row", "row": p, "summary": summary})

    # ================================================================== refresh
    def refresh_all(self, keep: str | None = None) -> None:
        """Rescan mods and rebuild the table from cache (no network)."""
        if not self.game_dir or not self.registry:
            return
        mods = scan_mods(self.game_dir)
        with self._mlock:
            self.model.set_mods(mods)
            self.model.set_rows(cached_rows([m.folder for m in self.model.mods if m.present], self.registry))
        self.emit_state(keep)

    def _rescan_one(self, folder: str) -> None:
        """One row changed (on/off, mark, note...): re-read the mod list, push that row only."""
        mods = scan_mods(self.game_dir)
        with self._mlock:
            self.model.set_mods(mods)
            known = self.model.mod(folder) is not None
        if known:
            self.emit_row(folder)
        else:
            self.emit_state()

    def after_outcome(self, out: actions.Outcome | None) -> None:
        if out is None:
            return
        if out.refresh:
            self.refresh_all(keep=out.folder)
        elif out.header:
            self.emit_header()
        if out.recheck:
            self.start_check()

    def _after_outcome_ui(self, out: actions.Outcome | None) -> None:
        self.ctl.ui(self.after_outcome, out)

    # ================================================================== start-up chain (runs on one worker)
    def start(self) -> bool:
        if self.started:
            return False
        self.started = True
        return self.job("start", "start", self._job_start)

    def _job_start(self) -> None:
        from cogitator.config import RECOVERED

        self.say(f"Cogitator {__version__}   log file: {log_path()}   Press F1 for help.", "dim")
        for bad in list(RECOVERED):
            self.say(f"Your settings file could not be read, so defaults are used. The old file was kept as {bad}.", "bold yellow")
        if self.given_game:
            self._game_ready(self.given_game, persist=False)
            return
        res = resolve_game_dir(self.override, self.settings.game_dir)
        if res.game_dir:
            self._game_ready(res.game_dir, persist=res.source in ("steam", "override"))
        elif res.source == "ambiguous":
            i = self.ui.choose("Several Darktide installs found - which one?", [str(c) for c in res.candidates], 0)
            if i is None:
                self.quit()
            else:
                self._game_ready(res.candidates[i], persist=True)
        else:
            self._ask_game_path(res.message or "Darktide was not found automatically.")

    def _ask_game_path(self, why: str) -> None:
        while not self.ctl.closing.is_set():
            self.say(why + " Choose the Darktide folder.", "yellow")
            value = self.pick_folder()
            if not value:
                self.quit()
                return
            value = str(value).strip('"')
            ok, reason = validate_game_dir(value)
            if ok:
                self._game_ready(Path(value), persist=True)
                return
            self.say(f"Not a Darktide folder: {reason}", "bold red")
            why = f"Not a Darktide folder ({reason})."

    def _game_ready(self, path: Path, persist: bool) -> None:
        self.game_dir = Path(path)
        if persist and self.settings.game_dir != str(path):
            self.settings.game_dir = str(path)
            self.settings.save()
        self.instance_lock = InstanceLock(self.game_dir)
        try:
            self.instance_lock.acquire()
        except LockError as e:
            self.instance_lock = None
            self.ctl.ask(NOTICE, title="Cogitator is already running", body=str(e), button="Close", dismissable=True)
            self.quit()
            return
        self.registry = Registry.load()
        self.refresh_all()
        if self.show_notice and not self.settings.notice_accepted:
            from cogitator.i18n import tr

            self.ctl.ask(NOTICE, title=tr("notice.title", NOTICE_TITLE), body=tr("notice.body", NOTICE_BODY), button="I understand",
                         dismissable=False)
            self.settings.notice_accepted = True
            self.settings.save()
        self._start_ticker()
        self._offer_bootstrap()
        if self.check_on_start and self.settings.check_updates_on_start:
            self.ctl.ui(self.start_check)
            self.start_selfupdate_check()

    # ================================================================== Cogitator's own updates
    def _selfupdater(self):
        if self.selfupdater is None and self.registry is not None:
            from cogitator.selfupdate import SelfUpdater

            self.selfupdater = SelfUpdater(self.registry, self.settings)
        return self.selfupdater

    def update_ready(self):
        u = self.selfupdater
        return u.ready() if u and u.layout.in_place else None

    def _update_offer(self):
        u = self.selfupdater
        return u.announcement() if u and u.layout.in_place and u.settings.update_mode != "off" else None

    def update_version(self) -> str:
        """The version the banner announces ("" when there is no banner)."""
        u = self.selfupdater
        a = u.announcement() if u and u.settings.update_mode != "off" else None
        return str(a.version) if a else ""

    def update_banner(self) -> str:
        text = actions_setup.update_banner_text(self.selfupdater, self.update_ready(), gui=True)
        return text.replace("Use Help > Update Cogitator", "Use the Update button on the Tools page")

    def start_selfupdate_check(self) -> None:
        if not (self.check_on_start and self.settings.check_updates_on_start) or self.settings.update_mode == "off":
            return
        if self._selfupdater() is not None:
            self.job("cogitator update check", "selfupdate", self._job_selfupdate)

    def _job_selfupdate(self) -> None:
        u = self.selfupdater
        with self.lock:
            last = u.absorb_result()
            res = u.run_automatic()
        if last:
            for text, style in actions_setup.update_result_lines(last):
                self.say(text, style)
        self.emit_header()
        if res.status == "available" and res.latest:
            self.say(f"Cogitator v{res.latest.version} is available.", "bold")
        if u.should_apply_automatically() and actions_setup.apply_update(u, self.ui):
            self.quit()

    def cogitator_update(self) -> None:
        """Tools > Update Cogitator: offer a download, or (when one is staged) install / remind / skip. All on a worker."""
        ctx, ui = self.ctx(), self.ui
        self.job("cogitator update", "selfupdate", lambda: self._job_cogitator_update(ctx, ui))

    def _job_cogitator_update(self, ctx, ui) -> None:
        u = self.selfupdater
        if u is None:
            self.say("Cogitator's update check has not run.", "yellow")
            return
        rd = self.update_ready()
        if not rd:
            offer = self._update_offer()
            if not offer:
                self.say("No Cogitator update is waiting.", "dim")
                return
            if ui.choose(actions_setup.offer_question(offer), actions_setup.OFFER_CHOICES, 0) != 0:
                return  # nothing is downloaded unasked
            if not actions_setup.download_update(ctx, ui, u, offer):
                return
            self.emit_header()
            rd = self.update_ready()
            if not rd:
                return
        i = ui.choose(actions_setup.ready_question(u, rd), actions_setup.READY_CHOICES, 0)
        if i is None or i > actions_setup.SKIP:
            return
        if i == actions_setup.INSTALL:
            if actions_setup.apply_update(u, ui):
                self.quit()
        else:
            actions_setup.remember_choice(ctx, u, i, rd.version)
            self.emit_header()

    # ================================================================== background ticks
    def _start_ticker(self) -> None:
        if self._ticker is not None and self._ticker.is_alive():
            return
        self._ticker = threading.Thread(target=self._tick_loop, name="web-ticks", daemon=True)
        self._ticker.start()

    def _tick_loop(self) -> None:
        n = 0
        while not self.ctl.closing.wait(1.0):
            n += 1
            for every, fn in ((2, self.nxm_tick), (5, self.watch_tick)):
                if n % every:
                    continue
                try:
                    fn()
                except Exception:
                    log.exception("background tick %s failed", fn.__name__)
                    self.say(f"{fn.__name__} failed; see the log file.", "yellow")

    def _question_pending(self) -> bool:
        b = self.bridge
        with b._plock:
            return bool(b._pending)

    def nxm_tick(self) -> None:
        """Pick up one queued nxm link; never while a question is open or an install is running."""
        if self._nxm_busy or self.ctl.closing.is_set() or self._question_pending() or not (self.game_dir and self.registry):
            return
        if self.ctl.busy("install"):
            return
        items = nxm.pending(on_expired=lambda lk: self.say(
            f"Nexus link for mod {lk.mod_id} file {lk.file_id}: link expired. Click the button on the website again.", "yellow"))
        if not items:
            return
        path, link = items[0]
        self._nxm_busy = True
        ctx, ui = self.ctx(), self.ui

        def work() -> None:
            try:
                actions_nexus.nxm_install(ctx, ui, path, link, self._after_outcome_ui)
            finally:
                self._nxm_busy = False

        try:
            started = self.job("nxm link", "install", work)
        except BaseException:
            self._nxm_busy = False
            raise
        if not started:
            self._nxm_busy = False

    def watch_tick(self) -> None:
        """Read the patch state in a worker, then act on a change (repair, or tell the user)."""
        if self.ctl.closing.is_set() or not (self.game_dir and self.registry) or self.ctl.busy("watch"):
            return
        game_dir, registry, mode = self.game_dir, self.registry, self.settings.auto_repair
        last = self._last_patch_state

        def work() -> None:
            try:
                st = patch_state(game_dir)
            except Exception:
                return
            step = repair.watch_step(last, st, game_dir, registry, mode, bool(self.game_running()))
            if step is not None:
                self.ctl.ui(self._watch_result, last, step)

        self.job("game-update watcher", "watch", work)

    def _watch_result(self, was: str | None, step: tuple[str, bool, str]) -> None:
        if self._question_pending() or was != self._last_patch_state:
            return  # look again on the next tick
        self._last_patch_state, redraw, act = step
        if redraw:
            self.emit_header()
        if act == "patch":
            ctx = self.ctx()
            self.job("repair", "patch", lambda: self._repaired(ctx))
        elif act == "notify":
            self.say(actions_setup.REPAIR_NOTICE + " Use the Mods on/off button to turn them on again.", "bold yellow")

    def _repaired(self, ctx: actions.Ctx) -> None:
        for text, style in actions_setup.repair_patch(ctx):
            self.say(text, style)
        self.ctl.ui(self.emit_header)

    # ================================================================== tools
    def bisect(self) -> None:
        if self.ready():
            ctx, ui = self.ctx(), self.ui
            self.job("bisect", "bisect", lambda: self._after_outcome_ui(actions_setup.bisect_flow(ctx, ui)))

    def crashlog(self) -> None:
        if not self.ready():
            return
        game_dir = self.game_dir
        with self._mlock:
            folders = {m.folder for m in self.model.mods}

        def work() -> None:
            lines = actions_setup.crashlog_lines(game_dir, folders)
            self.ctl.ask(NOTICE, title="Crash-log leads", body="\n".join(t for t, _ in lines), button="Close", dismissable=True)

        self.job("crash log", "net", work)

    def search_nexus(self, text: str) -> None:
        if self.ready():
            ctx, ui = self.ctx(), self.ui
            self.job("search", "net", lambda: actions_nexus.search_nexus(ctx, ui, text))

    def _offer_bootstrap(self) -> None:
        missing = [k for k in ("dml", "dmf") if detect_framework(self.game_dir, k).status != "installed"]
        if not missing:
            return
        pages = ", ".join(f"{k.upper()}: {NEXUS_PAGES[k]}" for k in missing)
        i = self.ui.choose("Install Darktide Mod Loader and Mod Framework?", ["Import the Nexus zip(s) I downloaded (recommended)",
                                 "Fetch from GitHub (opt-in: unreleased source archives; you confirm each source)", "Not now"], 0)
        if i == 0:
            self.say(f"Download from Nexus: {pages}", "cyan")
            self._job_import(None)
        elif i == 1:
            self._after_outcome_ui(actions_nexus.install_from_github(self.ctx(), self.ui, missing))
        else:
            self.say(f"Mod Loader/Framework not installed. {pages}", "yellow")

    def quit(self) -> None:
        self.release()
        self.on_quit()

    def release(self) -> None:
        self.ctl.close()
        self.bridge.close()
        if self.instance_lock:
            self.instance_lock.release()
            self.instance_lock = None

    # ================================================================== table state
    def set_filter(self, text: str) -> None:
        with self._mlock:
            self.model.filter_text = text[:200]
        self.emit_state()

    # ================================================================== per-mod (short, on the API thread)
    def toggle(self, folder: str) -> None:
        if not self.ready():
            return
        try:
            self.say(toggle_mod(self.game_dir, folder), "green")
        except ModError as e:
            self.say(str(e), "bold red")
        except OSError as e:
            self.say(f"Could not write mod_load_order.txt: {e}", "bold red")
        self._rescan_one(folder)

    def move(self, folder: str, delta: int) -> None:
        if not self.ready():
            return
        snapshots.auto(self.game_dir, "reorder", once=True, registry=self.registry)
        try:
            self.say(move_mod(self.game_dir, folder, delta), "green")
        except ModError as e:
            self.say(str(e), "bold red")
        except OSError as e:
            self.say(f"Could not write mod_load_order.txt: {e}", "bold red")
        with self._mlock:
            if self.model.sort_col is not None:  # the table shows the load order again, so the move is visible
                self.model.set_sort(None)
        self.refresh_all(keep=folder)

    def move_to(self, folder: str, target: str, after: bool) -> None:
        """Drag and drop: put `folder` before/after `target` in the load order."""
        if not self.ready():
            return
        snapshots.auto(self.game_dir, "reorder", once=True, registry=self.registry)
        try:
            self.say(move_mod_to(self.game_dir, folder, target, after), "green")
        except ModError as e:
            self.say(str(e), "bold red")
        except OSError as e:
            self.say(f"Could not write mod_load_order.txt: {e}", "bold red")
        with self._mlock:
            if self.model.sort_col is not None:
                self.model.set_sort(None)
        self.refresh_all(keep=folder)

    def pin(self, folder: str) -> None:
        if not self.ready():
            return
        with self._mlock:
            r = self.model.urows.get(folder)
        if not (r and r.nexus_id):
            self.say(f"{folder}: link the mod to Nexus before pinning a version.", "yellow")
            return
        self.say(actions.toggle_pin(self.registry, folder), "green")
        self.refresh_all(keep=folder)

    def open_folder(self, folder: str | None = None) -> None:
        if not self.ready():
            return
        path = mods_dir(self.game_dir) / folder if folder else self.game_dir
        try:
            self.open_path(path)
        except OSError as e:
            self.say(f"Could not open {path}: {e}", "bold red")

    def open_page(self, folder: str) -> None:
        with self._mlock:
            r = self.model.urows.get(folder)
        if r is not None and r.nexus_id:
            self.say(f"Opened {open_files_page(r.nexus_id, self.open_url)}", "cyan")

    # ---- details panel (Nexus data; runs on a worker, the answer arrives as a "details" / "versions" push)
    def nexus_id_of(self, folder: str) -> int | None:
        rec = (self.registry.mod(folder) or {}) if self.registry else {}
        v = rec.get("nexus_id")
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None

    def _details_push(self, folder: str, res: nexus_info.Result, image: str | None = None) -> None:
        self.bridge.emit({"type": "details", "folder": folder, "nexus_id": self.nexus_id_of(folder), "status": res.status, "message": res.message,
                          "stale": res.stale, "why": res.extra.get("why", ""), "at": res.at, "data": res.data, "image": image})

    def request_details(self, folder: str) -> None:
        if self.nexus_id_of(folder) is None:
            self._details_push(folder, nexus_info.Result(nexus_info.UNLINKED, None, nexus_info.MESSAGES[nexus_info.UNLINKED]))
            return
        with self._dlock:
            self._details_want = folder
            if self._details_busy:
                return  # the running worker takes the newest request when it is done
            self._details_busy = True
        self.ctl.run("mod details", self._details_loop, "details")

    def _details_loop(self) -> None:
        try:
            while True:
                with self._dlock:
                    folder, self._details_want = self._details_want, None
                    if folder is None:
                        self._details_busy = False
                        return
                try:
                    res = nexus_info.details_for(self.nexus_id_of(folder), self.registry)
                    url = (res.data or {}).get("picture_url") or ""
                    image = nexus_info.cached_image(url) if url else None
                    if url and image is None and self._details_want is None:  # only fetch while this panel is still the one shown
                        image = nexus_info.fetch_image(url)
                    self._details_push(folder, res, image)
                except Exception:
                    log.exception("details for %s failed", folder)
                    self._details_push(folder, nexus_info.Result(nexus_info.ERROR, None, nexus_info.MESSAGES[nexus_info.ERROR]))
        finally:
            with self._dlock:
                self._details_busy = False

    def request_versions(self, folder: str) -> None:
        mod_id = self.nexus_id_of(folder)

        def work() -> None:
            res = nexus_info.changes_for(mod_id, self.registry)
            self.bridge.emit({"type": "versions", "folder": folder, "status": res.status, "message": res.message, "stale": res.stale,
                              "versions": (res.data or {}).get("versions", [])})

        self.job("mod versions", "versions", work)

    def open_nexus_page(self, folder: str) -> None:
        mod_id = self.nexus_id_of(folder)
        if mod_id:
            self.open_url(f"https://www.nexusmods.com/{GAME}/mods/{mod_id}")

    def row_menu(self, folder: str) -> list[dict | None]:
        """(label, id, enabled) entries of the right-click menu: the ones the window offers."""
        with self._mlock:
            m, row = self.model.mod(folder), self.model.urows.get(folder)
        if m is None or not self.registry:
            return []
        meta = actions.mod_meta(self.registry, folder)
        linked = bool(row is not None and row.nexus_id)
        can_update = row is not None and row.status in (UPDATE, PROBABLY_OUTDATED)
        can_link = m.present and actions_nexus.link_refusal(folder) is None

        def item(label: str, aid: str, enabled: bool = True) -> dict:
            return {"label": label, "id": aid, "enabled": bool(enabled)}

        pinned = bool(meta.get("pinned_version"))
        items: list[dict | None] = [
            item("Disable" if m.state == ENABLED else ("Enable" if m.state != UNLISTED else "Add to load order and enable"), "toggle", m.present), None,
            item("Update" + (f" to {row.latest}" if can_update and row.latest else ""), "update", can_update and m.present)]
        if (self.registry.mod(folder) or {}).get("previous"):
            items.append(item("Roll back last update", "rollback", m.present))
        if linked:
            items.append(item("Unpin version" if pinned else "Pin version", "pin"))
        items.append(None)
        if linked:
            items.append(item("Set installed version...", "link", can_link and actions_nexus.link_needs_version_only(row)))
            items.append(item("Open Nexus page", "open_page", m.present))
        else:
            items.append(item("Link to Nexus...", "link", can_link))
        items += [None, item("Open folder in Explorer" if sys.platform == "win32" else "Open folder", "open_folder", m.present), None,
                  item("Remove...", "remove")]
        return items

    def row_action(self, folder: str, aid: str) -> None:
        if aid == "toggle":
            self.toggle(folder)
        elif aid == "open_page":
            self.open_page(folder)
        elif aid == "open_folder":
            self.open_folder(folder)
        elif aid == "pin":
            self.pin(folder)
        elif aid == "rollback":
            self.job("roll back", "install", lambda: self._after_outcome_ui(actions.roll_back(self.ctx(), self.ui, folder)))
        elif aid == "remove":
            self.remove(folder)
        elif aid == "link":
            self.link(folder)
        elif aid == "update":
            self.update(folder)

    # ================================================================== Nexus flows (workers)
    def link(self, folder: str) -> None:
        if not self.ready():
            return
        why = actions_nexus.link_refusal(folder)
        if why:
            self.say(why, "yellow")
            return
        with self._mlock:
            row = self.model.urows.get(folder)
        ctx, ui = self.ctx(), self.ui

        def work() -> None:
            if actions_nexus.link_needs_version_only(row):
                self._after_outcome_ui(actions_nexus.set_version(ctx, ui, folder, row.nexus_id))
                return
            ref = ui.ask_text(f"Link '{folder}' to its Nexus page", "https://www.nexusmods.com/warhammer40kdarktide/mods/<id>  or  numeric id")
            if not ref:
                return
            mod_id, problem = parse_nexus_ref(ref)
            if mod_id is None:
                self.say(problem or "Bad Nexus reference.", "bold red")
                return
            self._after_outcome_ui(actions_nexus.link_one_mod(ctx, ui, folder, mod_id))

        self.job("link", "link", work)

    def link_all(self) -> None:
        if not self.ready():
            return
        with self._mlock:
            present = [m.folder for m in self.model.mods if m.present]
        ctx, ui = self.ctx(), self.ui
        self.job("link all", "link", lambda: self._after_outcome_ui(actions_nexus.link_all(ctx, ui, present)))

    def _premium_update(self, ctx, folders: list[str]) -> None:
        out = actions_nexus.premium_update(ctx, self.ui, folders)
        self._after_outcome_ui(actions.Outcome(refresh=True))
        if out.recheck:
            self.ctl.ui(lambda: self.start_check(force=True))

    def update(self, folder: str) -> None:
        if not self.ready():
            return
        with self._mlock:
            row = self.model.urows.get(folder)
        if row is None or row.status not in (UPDATE, PROBABLY_OUTDATED):
            self.say(f"'{folder}': nothing to update ({row.status if row else 'not linked'}).", "yellow")
            return
        ctx, ui = self.ctx(), self.ui

        def work() -> None:
            if actions_nexus.is_premium(ctx):
                if ui.confirm(f"Download and install the update for {folder}?", row.reason):
                    self._premium_update(ctx, [folder])
            else:
                actions_nexus.manual_update(ctx, ui, folder, self._after_outcome_ui)

        self.job("update", "install", work)

    def update_all(self) -> None:
        if not self.ready():
            return
        with self._mlock:
            urows = dict(self.model.urows)
        targets = actions_nexus.update_candidates(urows)
        if not targets:
            self.say("No mods have an update available.", "yellow")
            return
        ctx, ui = self.ctx(), self.ui

        def work() -> None:
            if not actions_nexus.is_premium(ctx):
                actions_nexus.free_update_all_help(ctx, ui, urows, targets)
            elif ui.confirm(f"Download and install {len(targets)} update(s)?", "\n".join(targets)):
                self._premium_update(ctx, targets)

        self.job("update all", "install", work)

    def framework_update(self) -> None:
        if not self.ready():
            return
        seen = self.fw_updates or cached_frameworks(self.registry)
        ups = actions_nexus.pending_framework_updates(seen)
        if not ups:
            st = ", ".join(f"{u.kind.upper()} {u.status}" for u in seen) or "not installed"
            self.say(f"No framework update to apply ({st}). Check for updates first.", "yellow")
            return
        ctx, ui = self.ctx(), self.ui

        def work() -> None:
            body = actions_nexus.framework_update_body(ups, actions_nexus.is_premium(ctx))
            if ui.confirm(f"Update {' and '.join(u.kind.upper() for u in ups)}?", body):
                actions_nexus.framework_update(ctx, ui, ups, self._after_outcome_ui)

        self.job("framework update", "download", work)

    # ================================================================== profiles, snapshots, bulk
    def _lines(self, lines) -> None:
        for t, st in lines:
            self.say(t, st)

    def _job_lines(self, name: str, group: str, fn: Callable) -> None:
        self.job(name, group, lambda: (self._lines(fn()), self.ctl.ui(self.refresh_all)))

    def profiles_list(self) -> list[dict]:
        rows = profiles.list_profiles()
        return [{"name": r["name"], "label": lab} for r, lab in zip(rows, actions_setup.profile_labels(rows))]

    def snapshots_list(self) -> list[dict]:
        rows = snapshots.list_snapshots()
        return [{"name": r["name"], "label": actions_setup.snapshot_label(r)} for r in rows]

    def profile_known(self, name: object) -> bool:
        try:
            return isinstance(name, str) and any(p["name"] == name for p in self.profiles_list())
        except OSError:
            return False

    def snapshot_known(self, name: object) -> bool:
        try:
            return isinstance(name, str) and any(p["name"] == name for p in self.snapshots_list())
        except OSError:
            return False

    def profile_switch(self, name: str) -> None:
        if not self.ready():
            return
        ctx, ui = self.ctx(), self.ui

        def work() -> None:
            try:
                prof, body = actions_setup.prepare_switch(ctx, name)
            except (ModError, OSError) as e:
                self.say(str(e), "bold red")
                return
            if ui.confirm(f"Switch to profile {name}?", body, False):
                self._after_outcome_ui(actions_setup.switch_profile(ctx, ui, prof))

        self.job("switch profile", "install", work)

    def profile_save(self, name: str) -> None:
        if not self.ready():
            return
        ctx, ui = self.ctx(), self.ui

        def work() -> None:
            if actions_setup.profile_exists(name) and not ui.confirm(f"A profile named '{name}' exists. Replace it?", "", False):
                return
            self._lines(actions_setup.save_profile(ctx, name))

        self.job("save profile", "profile", work)

    def profile_delete(self, name: str) -> None:
        ui = self.ui

        def work() -> None:
            if ui.confirm(f"Delete profile '{name}'?", "The setup it describes is not changed.", False):
                self._lines(actions_setup.delete_profile(name))

        self.job("delete profile", "profile", work)

    def profile_share(self) -> str | None:
        if not self.ready():
            return None
        try:
            code = actions_setup.share_code(self.ctx())
        except (ModError, OSError) as e:
            self.say(str(e), "bold red")
            return None
        self.say("Setup code ready.", "green")
        return code

    def profile_import_code(self, code: str) -> None:
        if not self.ready():
            return
        ctx = self.ctx()
        self.job("import code", "profile", lambda: self._lines(actions_setup.import_code(ctx, code)))

    def snapshot_create(self, reason: str = "manual") -> None:
        if not self.ready():
            return
        ctx = self.ctx()
        self.job("snapshot", "install", lambda: self._lines(actions_setup.create_snapshot(ctx, reason or "manual")))

    def snapshot_restore(self, name: str) -> None:
        if not self.ready():
            return
        ctx, ui = self.ctx(), self.ui
        self.job("restore snapshot", "install", lambda: self._after_outcome_ui(actions_setup.restore_snapshot(ctx, ui, name)))

    def bulk(self, action: str, folders: list[str]) -> None:
        if not self.ready():
            return
        folders = [f for f in dict.fromkeys(folders) if self.mod_known(f)]
        if not folders:
            self.say("Select some mods first.", "yellow")
            return
        ctx, ui = self.ctx(), self.ui
        if action in ("enable", "disable"):
            self._job_lines("bulk", "bulk", lambda: actions_setup.bulk_set_enabled(ctx, folders, action == "enable"))
        elif action == "update":
            self.update_many(folders)
        else:
            def remove() -> None:
                body = ", ".join(folders) + "\nTheir folders are moved (not deleted) so you can restore them."
                if ui.confirm(f"Remove {len(folders)} mod(s)?", body, False):
                    self._after_outcome_ui(actions_setup.remove_many(ctx, ui, folders))

            self.job("remove selected", "install", remove)

    def update_many(self, folders: list[str]) -> None:
        ctx, ui = self.ctx(), self.ui
        with self._mlock:
            urows = dict(self.model.urows)
        todo = actions_nexus.update_candidates(urows, folders)

        def work() -> None:
            if not todo:
                self.say("None of the selected mods has an update available.", "yellow")
            elif not actions_nexus.is_premium(ctx):
                actions_nexus.free_update_all_help(ctx, ui, urows, todo)
            else:
                self._premium_update(ctx, todo)

        self.job("update selected", "install", work)

    # ================================================================== per-mod flows that ask (workers)
    def remove(self, folder: str) -> None:
        if not self.ready():
            return

        def work() -> None:
            if self.ui.confirm(f"Remove {folder}?", "The mod is taken out of the load order and its folder is moved (not deleted) to "
                               f"{cache_dir() / 'removed'} so you can restore it.", False):
                self._after_outcome_ui(actions.remove_one(self.ctx(), self.ui, folder))

        self.job("remove mod", "install", work)

    # ================================================================== global actions (workers)
    def toggle_global(self) -> None:
        if self.ready():
            self.job("toggle mods", "patch", self._job_toggle)

    def _job_toggle(self) -> None:
        if patch_state(self.game_dir) == ON and not self.ui.confirm(
                "Disable ALL mods?", "This un-patches the game database so no mod loads until you turn mods on again.", False):
            return
        try:
            with self.lock:
                r = toggle_mods(self.game_dir, self.registry, confirm_unpatch=lambda: True, runner=self.patch_runner,
                                is_running=self.game_running)
            self.say(f"{r.message}  MODS: {patch_state(self.game_dir)}", "green" if r.changed else "yellow")
        except PatcherError as e:
            self.say(f"Patch failed: {e}", "bold red")
        self.ctl.ui(self.emit_header)

    def launch(self) -> None:
        if self.ready():
            self.job("launch", "launch", self._job_launch)

    def _job_launch(self) -> None:
        from cogitator.launch import LaunchError, launch

        self.say("Checking the setup before launching...", "dim")
        try:
            with self.lock:
                r = launch(self.game_dir, self.registry, is_running=self.game_running, runner=self.patch_runner)
        except (LaunchError, ModError, OSError) as e:
            self.say(str(e), "bold red")
            return
        for n in r.notes:
            self.say(n, "yellow")
        self.say("Asked Steam to start Darktide.", "green")
        self.ctl.ui(self.emit_header)

    def doctor(self) -> None:
        if self.ready():
            self.say("Running doctor ...", "dim")
            self.job("doctor", "doctor", self._job_doctor)

    def _job_doctor(self) -> None:
        from cogitator.doctor import exit_code, record_history, run_doctor

        checks = run_doctor(str(self.game_dir), running=self.game_running, offline=False, registry=Registry.load())
        record_history(checks)
        segs = [[t, s] for t, s in report_segments(checks)]
        self.bridge.emit({"type": "report", "title": "Doctor", "segments": segs, "code": exit_code(checks)})

    def import_zips(self) -> None:
        if self.ready():
            self.job("import", "install", lambda: self._job_import(self.ctx().downloads_dir()))

    def _job_import(self, start: Path | None) -> None:
        ctx = self.ctx()
        files = self.pick_files(start or ctx.downloads_dir())  # the native file dialog; paths come only from there
        for p in files or []:
            actions.import_file(ctx, self.ui, str(p), None, self._after_outcome_ui)

    # ================================================================== Nexus key and update check
    def validate_key(self, key: str) -> bool:
        if not self.ready():
            return False

        def work() -> None:
            try:
                r = actions.store_key(self.ctx(), key)
                msg = redact(r.message, key)
            except Exception as e:  # never let the key travel in an error text
                raise RuntimeError(redact(f"{type(e).__name__}: {e}", key)) from None
            self.say(msg, "green" if r.ok else "bold red")
            self.bridge.emit({"type": "key_result", "ok": bool(r.ok), "message": msg})
            if r.ok:
                self.ctl.ui(self.emit_header)
                self.ctl.ui(lambda: self.start_check(force=True))

        return self.job("validate key", "key", work)

    def recheck(self) -> None:
        if self.ready():
            self.start_check(force=True)

    def start_check(self, force: bool = False) -> None:
        if not (self.game_dir and self.registry) or self.checking:
            return
        self.checking = True
        self.emit_header()
        if not self.job("update check", "check", lambda: self._job_check(force)):
            self.checking = False
            self.emit_header()

    def _apply_row(self, r: ModRow) -> None:
        with self._mlock:
            self.model.apply_row(r)
        self.emit_row(r.folder)

    def _apply_fw(self, u: FrameworkUpdate) -> None:
        self.fw_updates = [x for x in self.fw_updates if x.kind != u.kind] + [u]
        self.emit_header()

    def _job_check(self, force: bool) -> None:
        try:
            with self.lock:
                with self._mlock:
                    folders = [m.folder for m in self.model.mods if m.present]
                mt = {f: folder_mtime(mods_dir(self.game_dir) / f) for f in folders}
                b = run_checks(folders, self.registry, get_api_key(), ttl_s=self.settings.update_ttl_minutes * 60.0, force=force,
                               mtimes=mt, on_row=lambda r: self.ctl.ui(self._apply_row, r),
                               on_framework=lambda u: self.ctl.ui(self._apply_fw, u))
            self.ctl.ui(self._check_done, b)
        except Exception:
            self.ctl.ui(self._check_failed)
            raise

    def _check_failed(self) -> None:
        self.checking = False
        self.emit_header()

    def _check_done(self, b: CheckBundle) -> None:
        self.checking = False
        n = sum(1 for r in b.mods.rows if r.status == UPDATE) + sum(1 for u in b.frameworks if u.status == UPDATE)
        if not b.key_set:
            self.say("No Nexus API key: updates cannot be checked. Open Settings to set one.", "yellow")
        elif b.mods.rate_limited:
            self.say(f"Rate limited by Nexus - showing cached results. {b.mods.error}", "bold yellow")
        elif b.mods.error:
            self.say(f"Update check: {b.mods.error}", "bold yellow")
        else:
            src = "cache" if not b.live else f"{b.requests} request(s)"
            self.say(f"Update check done ({src}): {n} update(s) available.", "green" if not n else "bold yellow")
        self.emit_header()
        self.bridge.emit({"type": "check_done"})

    # ================================================================== settings page
    def settings_payload(self) -> dict:
        s = self.settings
        return {"version": __version__, "gui_theme": s.gui_theme if s.gui_theme in GUI_THEMES else "system",
                "gui_background": self.background_mode(),
                "check_updates_on_start": s.check_updates_on_start, "update_consent": s.update_consent, "auto_repair": s.auto_repair,
                "downloads_dir": s.downloads_dir, "downloads_effective": str(self.ctx().downloads_dir()) if self.game_dir else
                (s.downloads_dir or str(Path.home() / "Downloads")), "nexus": self.nexus_payload(),
                "log_path": str(log_path()), "config_dir": str(config_dir())}

    def background_mode(self) -> str:
        mode = self.settings.gui_background
        if mode not in GUI_BACKGROUNDS or (mode == "custom" and background.stored() is None):
            return "default"
        return mode

    def choose_background(self) -> str | None:
        """The native file dialog, then validate and keep the image. Returns an error text, None on success or cancel (see `chosen`)."""
        picked = self.pick_image()
        if not picked:
            return None
        try:
            background.store(Path(str(picked)))
        except background.BackgroundError as e:
            return str(e)
        self.settings.gui_background = "custom"
        self.settings.save()
        return None

    def reset_background(self) -> None:
        background.reset()
        self.settings.gui_background = "default"
        self.settings.save()

    def set_setting(self, name: str, value: object) -> str | None:
        """Change one setting the page is allowed to touch. Returns an error text, or None when saved."""
        if name in SETTING_BOOLS:
            if not isinstance(value, bool):
                return f"{name} must be true or false."
        elif name in SETTING_CHOICES:
            if value not in SETTING_CHOICES[name]:
                return f"{name} must be one of: {', '.join(SETTING_CHOICES[name])}."
            if name == "gui_background" and value == "custom" and background.stored() is None:
                return "Choose an image first."
        elif name == "downloads_dir":
            if not isinstance(value, str) or len(value) > 1000 or "\0" in value:
                return "downloads_dir must be a folder path."
            value = value.strip().strip('"')
            if value and not Path(value).is_dir():
                return "That folder does not exist."
        else:
            return f"Unknown setting: {name}"
        setattr(self.settings, name, value)
        self.settings.save()
        return None

    def help_text(self) -> dict:
        return {"title": f"Cogitator {__version__}", "body": HELP_BODY.format(log=log_path(), cfg=config_dir())}


def _startfile(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opens the folder in Explorer
    else:
        import subprocess

        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
