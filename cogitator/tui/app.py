"""Cogitator's terminal UI.

Network, subprocess and long file work run in thread workers and report back through `_ui`; rows update in place as
results arrive. The Nexus key only passes through the masked modal and the worker that validates it, and everything
shown or logged goes through `redact`."""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, RichLog, Static
from textual.worker import get_current_worker

from .. import __version__
from ..archives import parse_archive_name
from ..config import InstanceLock, LockError, Settings, config_dir, get_api_key, log_path, redact, set_api_key
from ..frameworks import (describe_plan, plan_local, classify_zip)
from ..fwupdate import install_framework_plan, needs_repatch
from ..gamefinder import resolve_game_dir, validate_game_dir
from ..glyphs import Glyphs, detect, glyphs_for
from ..linking import apply_guess, choose_baseline, folder_mtime, link_mod, rank_candidates
from ..loader import LoaderError
from ..modinstall import (ModImportError, NotPremium, apply_update_premium, dropped_paths, import_archive, inspect_mod_zip, mod_page_url, newest_download, open_files_page,
                          wait_for_download)
from ..mods import INTERNAL_FOLDERS, Mod, mods_dir, toggle_mod, move_mod, ModError
from ..nexus import InvalidKey, NexusClient, NexusError, RateLimited, latest_main_file, parse_nexus_ref
from ..patcher import ON, PatcherError, banner as patch_banner, default_runner, patch_state
from ..patcher import toggle as toggle_mods
from ..procs import darktide_running
from ..registry import Registry
from ..updates import (CheckBundle, FrameworkUpdate, ModRow, UPDATE, NOT_LINKED,
                       PROBABLY_OUTDATED, cached_frameworks, quota_text, run_checks)
from .screens import ChoiceScreen, ConfirmScreen, InputScreen, NoticeScreen
from .widgets import ModTable, TitleButton

log = logging.getLogger("cogitator.tui")
from .view import (  # noqa: F401  (re-exported: tests and callers import these from here)
    NOTICE_TITLE,
    NOTICE_BODY,
    EMPTY_STATE,
    HELP_BODY,
    _MAIN_ACTIONS,
    SORT_COLUMNS,
    DEFAULT_THEME,
    COGITATOR_THEMES,
    THEME_CHOICES,
    ACTION_BUTTONS,
    enabled_cell,
    _where_text,
    _config_recovered,
    mod_cell,
    _STATUS_STYLE,
    BASELINE_UNKNOWN_LABEL,
    installed_text,
    status_text,
    hint_for,
    report_text,
    fw_text,
    nexus_text)
from .selfupdate_mixin import SelfUpdateMixin
from .table_mixin import TableMixin
from .framework_mixin import FrameworkMixin
from .profile_mixin import ProfileMixin
from .workflow_mixin import WorkflowMixin



def pick_files_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("tkinter") is not None


def pick_files(initial: Path | None = None) -> list[Path] | None:
    """Open the system "Open" dialog for one or more zips. [] = cancelled, None = no dialog available here."""
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            names = filedialog.askopenfilenames(parent=root, title="Choose mod zip file(s) to install",
                                                initialdir=str(initial) if initial and Path(initial).is_dir() else None,
                                                filetypes=[("Mod archives", "*.zip"), ("All files", "*.*")])
        finally:
            root.destroy()
    except Exception:  # no display / no Tk: the caller falls back to typing a path
        logging.getLogger("cogitator.tui").debug("file dialog unavailable", exc_info=True)
        return None
    return [Path(n) for n in names] if names else []


# ------------------------------------------------------------------ app

class CogitatorApp(SelfUpdateMixin, TableMixin, FrameworkMixin, ProfileMixin, WorkflowMixin, App):
    TITLE = "Cogitator"
    ENABLE_COMMAND_PALETTE = False  # keeps the footer for our keys
    CSS = """
    Screen { background: $surface; }
    #titlebar { height: 1; background: $panel; color: $text; }
    #titlebar #tb_title { width: 1fr; padding: 0; content-align: center middle; text-style: bold; }
    .tb { width: auto; padding: 0 2; margin: 0 1; background: #5b5b5b; color: #ffffff; text-style: bold; }
    .tb:hover { background: #7d7d7d; }
    #tb_close { background: #c62828; }
    #tb_close:hover { background: #ef5350; }
    #actionbar { height: 1; background: $panel; }
    #actionbar .ab { width: auto; padding: 0 2; margin: 0 1; background: #3b5b8c; color: #ffffff; text-style: bold; }
    #actionbar .ab:hover { background: #5a82bd; }
    #actionbar #ab_spacer { width: 1fr; }
    #actionbar #ab_quit { background: #5b5b5b; }
    #actionbar #ab_quit:hover { background: #7d7d7d; }
    #hdr { height: auto; padding: 0 1; background: $boost; }
    #banner { display: none; height: auto; padding: 0 1; background: $error; color: $text; text-style: bold; }
    #banner.on { display: block; }
    #mods { height: 1fr; }
    #hint { height: auto; padding: 0 1; color: $text-muted; }
    #log { height: 6; border-top: solid $primary; padding: 0 1; }
    """
    BINDINGS = [
        Binding("question_mark,f1", "help", "Help", key_display="?"),  # Help and Quit come first: a narrow footer cuts the end
        Binding("q", "quit_app", "Quit", key_display="Q"),
        Binding("Q", "quit_app", show=False),
        Binding("t", "toggle_mods", "Mods", key_display="T", show=False),
        Binding("T", "toggle_mods", show=False),
        Binding("space", "toggle_mod", "On/off", key_display="Space"),
        # Case never matters: u/U (and Caps Lock) always mean "update the selected mod". Update-all is Ctrl+U.
        Binding("shift+up", "move_up", "Move up", key_display="Shift+Up", show=False),
        Binding("shift+down", "move_down", "Move down", key_display="Shift+Down", show=False),
        Binding("[", "move_up", show=False),
        Binding("]", "move_down", show=False),
        Binding("u", "update_selected", "Update", key_display="U"),
        Binding("U", "update_selected", show=False),
        Binding("ctrl+u", "update_all", "All", key_display="Ctrl+U"),
        Binding("f", "framework_update", "Framework", key_display="F", show=False),
        Binding("F", "framework_update", show=False),
        Binding("d", "doctor", "Doctor", key_display="D", show=False),
        Binding("D", "doctor", show=False),
        Binding("r", "recheck", "Recheck", key_display="R"),
        Binding("R", "recheck", show=False),
        Binding("l", "link", "Link", key_display="L", show=False),
        Binding("L", "link", show=False),
        Binding("m", "link_all", "Link all", key_display="M", show=False),
        Binding("M", "link_all", show=False),
        Binding("i", "import_archive", "Import", key_display="I"),
        Binding("I", "import_archive", show=False),
        Binding("k", "set_key", "Key", key_display="K", show=False),
        Binding("K", "set_key", show=False),
        Binding("a", "apply_update", "Update Cogitator", key_display="A"),
        Binding("A", "apply_update", show=False),
        Binding("o", "cycle_sort", "Sort", key_display="O", show=False),
        Binding("p", "profiles", "Profiles", key_display="P", show=False),
        Binding("P", "profiles", show=False),
        Binding("x,X", "toggle_mark", "Mark", key_display="X", show=False),
        Binding("b,B", "bulk", "Bulk", key_display="B", show=False),
        Binding("g,G", "launch", "Launch", key_display="G", show=False),
        Binding("n,N", "edit_note", "Note", key_display="N", show=False),
        Binding("asterisk", "toggle_fav", "Favourite", key_display="*", show=False),
        Binding("slash", "filter", "Filter", key_display="/", show=False),
        Binding("c,C", "crashlog", "Crash log", key_display="C", show=False),
        Binding("f10", "app_menu", "Menu", key_display="F10"),
    ]

    def __init__(self, game_dir: Path | None = None, *, game_override: str | None = None, check_updates: bool = True,
                 ascii_glyphs: bool | None = None):
        super().__init__()
        self._given_game = Path(game_dir) if game_dir else None
        self._override = game_override
        self._check_on_start = check_updates
        self._ascii = ascii_glyphs
        self.game_dir: Path | None = None
        self.settings = Settings.load()
        self.registry: Registry | None = None
        self.marked: set[str] = set()  # mods marked with X for a bulk action
        self.filter_text = ""
        self._last_patch_state: str | None = None
        self.glyphs: Glyphs = glyphs_for("utf-8", force_ascii=ascii_glyphs) if ascii_glyphs is not None else detect()
        self.mods: list[Mod] = []
        self.urows: dict[str, ModRow] = {}
        self.fw_updates: list[FrameworkUpdate] = []
        self.checking = False
        self.last_bundle: CheckBundle | None = None
        self._lock = threading.RLock()  # serialises registry mutation between workers; the UI thread never takes it
        self._instance_lock: InstanceLock | None = None
        self._exiting = False
        self._spin = 0
        self._sort_i = -1
        # seams for tests
        self.game_running: Callable[[], bool] = darktide_running
        self.patch_runner = default_runner
        self.download_wait_s = 180.0
        self.doctor_offline = False
        self.selfupdater = None  # SelfUpdater; created once the registry is loaded. Tests inject their own.
        self.selfupdate_on_start = True
        self.show_notice = True
        self.pick_files = pick_files if pick_files_available() else None  # seam: tests set None/their own

    # -------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        with Horizontal(id="titlebar"):
            yield TitleButton(("\u2630 " if not self.glyphs.ascii else "= ") + "Menu", id="tb_menu", tip="All actions (F10)")
            yield Static(f"Cogitator {__version__}", id="tb_title", markup=False)
            yield TitleButton("?", id="tb_help", tip="Help")
            yield TitleButton("X", id="tb_close", tip="Quit")
        with Vertical():
            yield Static("", id="hdr", markup=False)
            yield Static("", id="banner", markup=False)
            yield ModTable(id="mods", cursor_type="row", zebra_stripes=True)
            yield Static("", id="hint", markup=False)
            yield RichLog(id="log", wrap=True, markup=False, highlight=False)
        with Horizontal(id="actionbar"):
            for label, act, tip in ACTION_BUTTONS:
                yield TitleButton(label, id=f"ab_{act}", tip=tip, classes="ab")
            yield Static("", id="ab_spacer")
            yield TitleButton("Quit", id="ab_quit", tip="Q", classes="ab")

    def on_mount(self) -> None:
        t = self.query_one("#mods", DataTable)
        g = self.glyphs
        t.add_column(Text(g.on + "/" + g.off), key="en", width=len(g.on + "/" + g.off) + 1)  # Text: "[x]" must not be parsed as markup
        t.add_column("Mod", key="mod")
        t.add_column("Installed", key="installed")
        t.add_column("Latest", key="latest")
        t.add_column("Status", key="status")
        for th in COGITATOR_THEMES:
            self.register_theme(th)
        self.theme = self.settings.theme if self.settings.theme in self.available_themes else DEFAULT_THEME
        self.set_interval(0.15, self._tick)
        self.set_interval(5.0, self._watch)
        self.log_line(f"Cogitator {__version__}   log file: {log_path()}   Press ? for help.", "dim")
        for bad in _config_recovered():
            self.log_line(f"Your settings file could not be read, so defaults are used. The old file was kept as {bad}.", "bold yellow")
        if self._given_game:
            self._game_ready(self._given_game, persist=False)
        else:
            self._handle_resolution(resolve_game_dir(self._override, self.settings.game_dir))

    # -------------------------------------------------------------- start-up chain
    def _handle_resolution(self, res) -> None:
        if res.game_dir:
            self._game_ready(res.game_dir, persist=res.source in ("steam", "override"))
        elif res.source == "ambiguous":
            def picked(i):
                if i is None:
                    self.exit()
                else:
                    self._game_ready(res.candidates[i], persist=True)

            self.push_screen(ChoiceScreen("Several Darktide installs found - which one?", [str(c) for c in res.candidates]), picked)
        else:
            self._ask_game_path(res.message or "Darktide was not found automatically.")

    def _ask_game_path(self, why: str) -> None:
        def got(value):
            if value is None:
                self.exit()
                return
            ok, reason = validate_game_dir(value.strip('"'))
            if ok:
                self._game_ready(Path(value.strip('"')), persist=True)
            else:
                self.log_line(f"Not a Darktide folder: {reason}", "red")
                self._ask_game_path(why)

        self.push_screen(InputScreen("Where is Darktide installed?", r"e.g. D:\SteamLibrary\steamapps\common\Warhammer 40,000 DARKTIDE",
                                     body=why), got)

    def _game_ready(self, path: Path, persist: bool) -> None:
        self.game_dir = Path(path)
        if persist and self.settings.game_dir != str(path):
            self.settings.game_dir = str(path)
            self.settings.save()
        self._instance_lock = InstanceLock(self.game_dir)
        try:
            self._instance_lock.acquire()
        except LockError as e:
            self.push_screen(NoticeScreen("Cogitator is already running", str(e)), lambda _: self.exit())
            return
        self.registry = Registry.load()
        self.refresh_all()
        if self.show_notice and not self.settings.notice_accepted:
            def accepted(_):
                self.settings.notice_accepted = True
                self.settings.save()
                self._after_notice()

            self.push_screen(NoticeScreen(NOTICE_TITLE, NOTICE_BODY), accepted)
        else:
            self._after_notice()

    def _after_notice(self) -> None:
        self._offer_bootstrap()
        self.start_selfupdate_check()
        if self._check_on_start and self.settings.check_updates_on_start:
            self.start_check()

    # -------------------------------------------------------------- rendering
    def log_line(self, msg: str, style: str = "") -> None:
        msg = redact(str(msg))
        self.query_one("#log", RichLog).write(Text(msg, style))
        log.info(msg)

    def _ui(self, fn, *a, **k):
        if self._exiting:
            return None
        try:
            return self.call_from_thread(fn, *a, **k)
        except Exception:  # app is shutting down
            return None

    def refresh_header(self) -> None:
        if not self.game_dir or not self.registry:
            return
        state = patch_state(self.game_dir)
        fw = self.fw_updates or cached_frameworks(self.registry)
        hdr = Text()
        hdr.append("Game: ", "bold")
        hdr.append(str(self.game_dir) + "\n")
        hdr.append_text(fw_text(self.game_dir, self.registry, fw, state))
        hdr.append("\n")
        spin = ""
        if self.checking:
            spin = f"{self.glyphs.spinner[self._spin % len(self.glyphs.spinner)]} checking for updates{self.glyphs.ellipsis}"
        ann = self._update_banner()
        if ann:
            hdr.append(ann + "\n", "bold")
        upd = self.registry.data["updates"]
        hdr.append_text(nexus_text(bool(get_api_key()), upd.get("premium"), quota_text(self.registry), spin))
        self.query_one("#hdr", Static).update(hdr)
        self.query_one("#ab_apply_update").display = bool(self.update_ready() or self._update_offer())
        b = patch_banner(self.game_dir)
        bn = self.query_one("#banner", Static)
        bn.update(Text(b or ""))
        bn.set_class(bool(b), "on")


    def _tick(self) -> None:
        if self.checking:
            self._spin += 1
            self.refresh_header()


    # -------------------------------------------------------------- guards / workers
    def check_action(self, action: str, parameters) -> bool | None:
        if action == "apply_update":
            if len(self.screen_stack) > 1 or not (self.update_ready() or self._update_offer()):
                return False  # the key only appears while an update is waiting or offered
            return True
        if action in _MAIN_ACTIONS and len(self.screen_stack) > 1:
            return False  # a dialog is open: main-screen keys are inert
        return True

    def _bg(self, fn, *args, name: str, group: str = "net", exclusive: bool = False):
        def runner():
            try:
                fn(*args)
            except Exception as e:  # never crash the TUI
                logging.getLogger("cogitator.tui").exception("worker %s failed", name)
                self._ui(self.log_line, f"{name} failed: {e}", "bold red")

        return self.run_worker(runner, thread=True, group=group, name=name, exclusive=exclusive, exit_on_error=False)

    def ask(self, screen):
        """Called from a worker thread: show `screen` and block *this worker* until it is dismissed."""
        ev, box = threading.Event(), []
        self._ui(lambda: self.push_screen(screen, lambda r: (box.append(r), ev.set())))
        w = get_current_worker()
        while not ev.wait(0.1):
            if w.is_cancelled or self._exiting:
                return None
        return box[0] if box else None

    def _need_game(self) -> bool:
        if self.game_dir and self.registry:
            return True
        self.log_line("No game folder selected yet.", "yellow")
        return False

    # -------------------------------------------------------------- T: global toggle
    def action_toggle_mods(self) -> None:
        if not self._need_game():
            return
        if patch_state(self.game_dir) == ON:
            self.push_screen(ConfirmScreen("Disable ALL mods?",
                                           "This un-patches the game database so no mod loads until you press T again.",
                                           default_yes=False),
                             lambda ok: ok and self._bg(self._job_toggle, name="toggle mods", group="patch", exclusive=True))
        else:
            self._bg(self._job_toggle, name="toggle mods", group="patch", exclusive=True)

    def _job_toggle(self) -> None:
        try:
            with self._lock:
                r = toggle_mods(self.game_dir, self.registry, confirm_unpatch=lambda: True,
                                runner=self.patch_runner, is_running=self.game_running)
            self._ui(self.log_line, f"{r.message}  MODS: {patch_state(self.game_dir)}", "green" if r.changed else "yellow")
        except PatcherError as e:
            self._ui(self.log_line, f"Patch failed: {e}", "bold red")
        self._ui(self.refresh_header)

    # -------------------------------------------------------------- Space: per-mod toggle
    def action_toggle_mod(self) -> None:
        f = self.selected()
        if not (self._need_game() and f):
            return
        try:
            msg = toggle_mod(self.game_dir, f)
            self.log_line(msg, "green")
        except ModError as e:
            self.log_line(str(e), "bold red")
        except OSError as e:
            self.log_line(f"Could not write mod_load_order.txt: {e}", "bold red")
        self._refresh_mod_row(f)

    # -------------------------------------------------------------- Shift+Up / Shift+Down: load order
    def _move(self, delta: int) -> None:
        f = self.selected()
        if not (self._need_game() and f):
            return
        from .. import snapshots

        snapshots.auto(self.game_dir, "reorder", once=True, registry=self.registry)
        try:
            self.log_line(move_mod(self.game_dir, f, delta), "green")
        except ModError as e:
            self.log_line(str(e), "bold red")
        except OSError as e:
            self.log_line(f"Could not write mod_load_order.txt: {e}", "bold red")
        self._sort_i = -1  # the table shows the load order again, so the move is visible
        self.refresh_all(keep=f)

    def action_move_up(self) -> None:
        self._move(-1)

    def action_move_down(self) -> None:
        self._move(1)

    # -------------------------------------------------------------- R: recheck / background check
    def action_recheck(self) -> None:
        if self._need_game():
            self.start_check(force=True)

    def start_check(self, force: bool = False) -> None:
        if not self._need_game():
            return
        self.checking = True
        self.refresh_header()
        self._bg(self._job_check, force, name="update check", group="check", exclusive=True)

    def _job_check(self, force: bool) -> None:
        try:
            with self._lock:
                folders = [m.folder for m in self.mods if m.present]
                mt = {f: folder_mtime(mods_dir(self.game_dir) / f) for f in folders}
                b = run_checks(folders, self.registry, get_api_key(), ttl_s=self.settings.update_ttl_minutes * 60.0,
                               force=force, mtimes=mt, on_row=lambda r: self._ui(self._apply_row, r),
                               on_framework=lambda u: self._ui(self._apply_fw, u))
            self._ui(self._check_done, b)
        except Exception as e:
            self._ui(self._check_failed, e)
            raise

    def _check_failed(self, e: Exception) -> None:
        self.checking = False
        self.refresh_header()

    def _check_done(self, b: CheckBundle) -> None:
        self.checking = False
        self.last_bundle = b
        n = sum(1 for r in b.mods.rows if r.status == UPDATE) + sum(1 for u in b.frameworks if u.status == UPDATE)
        if not b.key_set:
            self.log_line("No Nexus API key: updates cannot be checked. Press K to set one.", "yellow")
        elif b.mods.rate_limited:
            self.log_line(f"Rate limited by Nexus - showing cached results. {b.mods.error}", "bold yellow")
        elif b.mods.error:
            self.log_line(f"Update check: {b.mods.error}", "bold yellow")
        else:
            src = "cache" if not b.live else f"{b.requests} request(s)"
            self.log_line(f"Update check done ({src}): {n} update(s) available.", "green" if not n else "bold yellow")
        if n and b.live:
            self.notify(f"{n} update(s) available. Press U to update the selected mod or Ctrl+U for all.", title="Updates",
                        severity="information", timeout=10)
        self.refresh_header()
        self._update_hint()

    # -------------------------------------------------------------- K: key entry (masked, never logged)
    def action_set_key(self) -> None:
        self.push_screen(InputScreen("Nexus personal API key", "paste key (hidden)", password=True,
                                     body="Create one in Nexus account settings > API. It is stored in your OS keyring "
                                          "(or a 0600 file) and never logged."), self._key_entered)

    def _key_entered(self, key: str | None) -> None:
        if not key:
            return
        self._bg(self._job_key, key, name="validate key", group="key", exclusive=True)

    def _job_key(self, key: str) -> None:
        try:
            with NexusClient(key) as nx:
                who = nx.validate()
        except NexusError as e:
            self._ui(self.log_line, redact(f"Key not saved: {e}", key), "bold red")
            return
        where = set_api_key(key)
        with self._lock:
            upd = self.registry.data["updates"]
            upd["premium"], upd["premium_checked"] = bool(who.get("is_premium")), time.time()
            self.registry.save()
        self._ui(self.log_line, f"Key valid ({'premium' if who.get('is_premium') else 'free'}); stored {_where_text(where)}.", "green")
        self._ui(self.refresh_header)
        self._ui(lambda: self.start_check(force=True))

    # -------------------------------------------------------------- L: link
    def action_link(self) -> None:
        f = self.selected()
        if not (self._need_game() and f):
            return
        if f in INTERNAL_FOLDERS:
            self.log_line(f"'{f}' belongs to the loader/framework.", "yellow")
            return
        row = self.urows.get(f)
        if row is not None and row.nexus_id and row.baseline in (None, "guessed"):  # linked; just set the version
            self._bg(self._job_set_version, f, row.nexus_id, name="set version", group="link", exclusive=True)
            return
        self.push_screen(InputScreen(f"Link '{f}' to its Nexus page", "https://www.nexusmods.com/warhammer40kdarktide/mods/<id>  or  numeric id"),
                         lambda ref: self._link_ref(f, ref))

    def _link_ref(self, folder: str, ref: str | None) -> None:
        if not ref:
            return
        mod_id, problem = parse_nexus_ref(ref)
        if mod_id is None:
            self.log_line(problem or "Bad Nexus reference.", "bold red")
            return
        self._bg(self._job_link, folder, mod_id, name="link", group="link", exclusive=True)

    def _job_link(self, folder: str, mod_id: int) -> None:
        key = get_api_key()
        if not key:
            self._ui(self.log_line, "Linking validates through the API - press K to set your Nexus key first.", "yellow")
            return
        try:
            with NexusClient(key) as nx:
                info, files = nx.mod(mod_id), nx.files(mod_id)
        except NexusError as e:
            self._ui(self.log_line, f"Link failed: {e}", "bold red")
            return
        name = info.get("name") or f"mod {mod_id}"
        latest = latest_main_file(files)
        lv = latest.version if latest else "n/a"
        choice = self.ask(ChoiceScreen(
            f"Nexus mod {mod_id}: {name} (latest {lv}). Link '{folder}' to it?",
            ["Link and mark current as installed (only newer uploads will be flagged)",
             "Link, but leave the installed file unknown (status stays 'unknown')", "Cancel"]))
        if choice not in (0, 1):
            self._ui(self.log_line, "Link cancelled.", "dim")
            return
        with self._lock:
            rec = link_mod(self.registry, folder, mod_id, name, files, via="manual",
                           baseline="current" if choice == 0 else "none")
        self._ui(self._after_registry_change, folder)
        self._ui(self.log_line, f"Linked '{folder}' to {name}." + ("" if rec["installed_file_id"] else " Baseline unknown."), "green")

    # -------------------------------------------------------------- M: link every unlinked mod
    def action_link_all(self) -> None:
        if self._need_game():
            self._bg(self._job_link_all, name="link all", group="link", exclusive=True)

    def _job_link_all(self) -> None:
        key = get_api_key()
        if not key:
            self._ui(self.log_line, "Linking validates through the API - press K to set your Nexus key first.", "yellow")
            return
        todo = [m.folder for m in self.mods if m.present and m.folder not in INTERNAL_FOLDERS
                and not (self.registry.mod(m.folder) or {}).get("nexus_id")]
        if not todo:
            self._ui(self.log_line, "Every mod is already linked.", "dim")
            return
        done = 0
        try:
            with NexusClient(key) as nx:
                for folder in todo:
                    r = self._link_one(nx, folder)
                    if r is None:
                        break
                    done += bool(r)
        except NexusError as e:
            self._ui(self.log_line, f"Link all stopped: {e}", "bold red")
        self._ui(self._after_registry_change)
        self._ui(self.log_line, f"Link all finished: {done} linked.", "green")

    def _link_one(self, nx: NexusClient, folder: str) -> bool | None:
        """True linked, False skipped, None stop. Never links without the user's confirmation."""
        mod_id = None
        try:
            cands = rank_candidates(folder, nx.search_mods(folder.replace("_", " ")))
        except (InvalidKey, RateLimited):
            raise
        except NexusError as e:
            self._ui(self.log_line, f"Name search unavailable ({e}); paste a URL or id.", "yellow")
            cands = []
        if cands:
            opts = [f"Yes - link to: {cands[0]['name']} (mod {cands[0]['mod_id']})"]
            opts += [f"Use instead: {c['name']} (mod {c['mod_id']})" for c in cands[1:]]
            opts += ["No - paste a Nexus URL or id", "Skip this mod", "Stop"]
            c = self.ask(ChoiceScreen(f"Link '{folder}'?", opts))
            if c is None or c == len(opts) - 1:
                return None
            if c == len(opts) - 2:
                return False
            if c < len(cands):
                mod_id = cands[c]["mod_id"]
        if mod_id is None:
            ref = self.ask(InputScreen(f"Link '{folder}' to its Nexus page", "URL or numeric id (blank = skip)", allow_blank=True))
            if ref is None:
                return None
            if not ref:
                return False
            mod_id, problem = parse_nexus_ref(ref)
            if mod_id is None:
                self._ui(self.log_line, f"{folder}: {problem}", "bold red")
                return False
        try:
            info, files = nx.mod(mod_id), nx.files(mod_id)
        except (InvalidKey, RateLimited):
            raise
        except NexusError as e:
            self._ui(self.log_line, f"{folder}: {e}", "bold red")
            return False
        with self._lock:
            link_mod(self.registry, folder, mod_id, info.get("name") or f"mod {mod_id}", files, via="manual", baseline="none")
            apply_guess(self.registry, folder, files, folder_mtime(mods_dir(self.game_dir) / folder))
        self._pick_version(folder, files)
        self._ui(self.log_line, f"Linked '{folder}' to {info.get('name') or mod_id}.", "green")
        return True

    def _pick_version(self, folder: str, files) -> None:
        """Version picker with dates; the current guess is preselected. The last option keeps whatever is stored."""
        pool = sorted((f for f in files if f.file_id is not None), key=lambda f: (f.uploaded or 0, f.file_id), reverse=True)[:40]
        if not pool:
            return
        guess = (self.registry.mod(folder) or {}).get("installed_file_id")
        opts = [f"{f.version or '?'}   {time.strftime('%Y-%m-%d', time.localtime(f.uploaded)) if f.uploaded else '?'}   "
                f"{f.category or ''} {f.name or f.file_name or ''}".rstrip() + ("   <- guess" if f.file_id == guess else "")
                for f in pool] + ["I do not know"]
        sel = next((i for i, f in enumerate(pool) if f.file_id == guess), 0)
        c = self.ask(ChoiceScreen(f"Which version of '{folder}' is installed?", opts, selected=sel))
        if c is not None and c < len(pool):
            with self._lock:
                choose_baseline(self.registry, folder, files, pool[c].file_id)

    def _job_set_version(self, folder: str, mod_id: int) -> None:
        key = get_api_key()
        if not key:
            self._ui(self.log_line, "Press K to set your Nexus key first.", "yellow")
            return
        try:
            with NexusClient(key) as nx:
                files = nx.files(mod_id)
        except NexusError as e:
            self._ui(self.log_line, f"Could not load versions: {e}", "bold red")
            return
        with self._lock:
            apply_guess(self.registry, folder, files, folder_mtime(mods_dir(self.game_dir) / folder))
        self._pick_version(folder, files)
        self._ui(self._after_registry_change, folder)

    def _after_registry_change(self, folder: str | None = None) -> None:
        self.refresh_all(keep=folder)

    # -------------------------------------------------------------- I: import (mods and framework zips)
    def action_import_archive(self) -> None:
        if not self._need_game():
            return
        if self.pick_files is not None:
            self._bg(self._job_pick_import, name="choose files", group="pick", exclusive=True)
        else:
            self._import_by_typing()

    def _job_pick_import(self) -> None:
        """Windows' own file chooser (several zips can be picked at once); falls back to typing a path."""
        paths = self.pick_files(self._downloads_dir())
        if paths is None:
            self._ui(self._import_by_typing)
        elif paths:
            self._ui(lambda: self._bg(self._job_import_many, paths, name="import", group="install", exclusive=True))

    def _import_by_typing(self) -> None:
        self.push_screen(InputScreen("Import archive (.zip)", r"path to a mod, DML or DMF archive, e.g. C:\Users\you\Downloads\file.zip",
                                     body="Drop the file onto this window or paste its path. If the path does not arrive, "
                                          "press Enter on the empty box to use the newest .zip in your Downloads folder (last hour).",
                                     allow_blank=True),
                         lambda p: p is not None and self._bg(self._job_import, p, None, name="import", group="install", exclusive=True))

    def on_paste(self, event) -> None:
        """A file dropped on the window (the terminal pastes its path) imports without pressing I first."""
        if len(self.screen_stack) > 1 or not (self.game_dir and self.registry):
            return
        found = dropped_paths(event.text)
        if found:
            event.stop()
            self._bg(self._job_import_many, found, name="import", group="install", exclusive=True)

    def _job_import_many(self, paths: list[Path]) -> None:
        for p in paths:
            self._job_import(str(p), None)

    def _downloads_dir(self) -> Path:
        return Path(self.settings.downloads_dir) if self.settings.downloads_dir else Path.home() / "Downloads"

    def _job_import(self, raw: str, mod_id: int | None) -> None:
        raw = raw.strip()
        if not raw:
            p = newest_download(self._downloads_dir())
            if p is None:
                self._ui(self.log_line, f"No .zip from the last hour in {self._downloads_dir()}.", "bold red")
                return
            self._ui(self.log_line, f"Using the newest download: {p.name}", "dim")
        else:
            found = dropped_paths(raw)
            if len(found) > 1:
                for f in found:
                    self._job_import(str(f), None)
                return
            p = found[0] if found else Path(raw.strip('"'))
        if not p.is_file():
            self._ui(self.log_line, f"No such file: {p}", "bold red")
            return
        try:
            kind = classify_zip(p)[0]
        except LoaderError:
            kind = None
        if kind:  # DML / DMF archive
            plan = plan_local(p)
            body = describe_plan(plan)
            if needs_repatch(plan, self.game_dir):
                body += "\n\nMods are currently ON: Cogitator will un-patch the game, install, then re-patch it."
            if not self.ask(ConfirmScreen(f"Install {kind.upper()} into the game folder?", body)):
                return
            try:
                with self._lock:
                    fi = install_framework_plan(plan, self.game_dir, self.registry, runner=self.patch_runner,
                                                is_running=self.game_running,
                                                max_bytes=self.settings.max_extract_mb * 1024 * 1024,
                                                max_files=self.settings.max_extract_files)
            except PatcherError as e:
                self._ui(self.log_line, f"{kind.upper()}: {e}", "bold red")
                self._ui(self.refresh_header)
                return
            self._ui(self.log_line, f"{kind.upper()} installed ({fi.result.written} files).", "green")
            for n in fi.notes:
                self._ui(self.log_line, n, "green")
            self._ui(self._after_registry_change, None)
            self._ui(self.start_check)
            return
        info = inspect_mod_zip(p)  # ModImportError surfaces through _bg's handler
        parsed = parse_archive_name(p.name)
        if mod_id is None and parsed.mod_id is None and len(info.folders) == 1 \
                and not (self.registry.mod(info.folders[0]) or {}).get("nexus_id"):
            ref = self.ask(InputScreen(f"'{p.name}' does not say which Nexus mod it is",
                                       "Nexus URL or id (blank = install without linking)"))
            if ref:
                mod_id, problem = parse_nexus_ref(ref)
                if mod_id is None:
                    self._ui(self.log_line, problem or "Bad reference.", "bold red")
                    return
        if not self.ask(ConfirmScreen(f"Install {', '.join(info.folders)}?", f"From {p.name}\nInto {self.game_dir / 'mods'}")):
            return
        key = get_api_key()
        nx = NexusClient(key) if key else None
        try:
            with self._lock:
                r = import_archive(p, self.game_dir, self.registry, nx, mod_id=mod_id,
                                   choose=lambda q, o: self.ask(ChoiceScreen(q, o)), is_running=self.game_running,
                                   max_bytes=self.settings.max_extract_mb * 1024 * 1024, max_files=self.settings.max_extract_files)
        finally:
            if nx:
                nx.close()
        msg = f"Installed {', '.join(r.folders)}" + (f"; added to load order: {', '.join(r.added_to_load_order)}" if r.added_to_load_order else "")
        self._ui(self.log_line, msg, "green")
        for n in r.notes:
            self._ui(self.log_line, n, "yellow")
        self._ui(self._after_registry_change, r.linked or (r.folders[0] if r.folders else None))

    # -------------------------------------------------------------- U / Shift+U: updates
    def _premium(self) -> bool:
        return bool(self.registry and self.registry.data["updates"].get("premium") and get_api_key())

    def action_update_selected(self) -> None:
        f = self.selected()
        if not (self._need_game() and f):
            return
        row = self.urows.get(f)
        if row is None or row.status not in (UPDATE, PROBABLY_OUTDATED):
            self.log_line(f"'{f}': nothing to update ({row.status if row else NOT_LINKED}).", "yellow")
            return
        if self._premium():
            self.push_screen(ConfirmScreen(f"Download and install the update for {f}?", row.reason),
                             lambda ok: ok and self._bg(self._job_premium, [f], name="update", group="install", exclusive=True))
        else:
            self._bg(self._job_manual, f, name="manual update", group="download", exclusive=True)

    def action_update_all(self) -> None:
        if not self._need_game():
            return
        targets = [f for f, r in self.urows.items() if r.status == UPDATE]
        if not targets:
            self.log_line("No mods have an update available.", "yellow")
            return
        if self._premium():
            self.push_screen(ConfirmScreen(f"Download and install {len(targets)} update(s)?", "\n".join(targets)),
                             lambda ok: ok and self._bg(self._job_premium, targets, name="update all", group="install", exclusive=True))
        else:
            self.log_line("Free account: download each file from its page, then press I to import it.", "yellow")
            for f in targets:
                self.log_line(f"  {f}: {mod_page_url(self.urows[f].nexus_id)}")

    def _job_premium(self, folders: list[str]) -> None:
        key = get_api_key()
        with NexusClient(key) as nx:
            for f in folders:
                try:
                    with self._lock:
                        r = apply_update_premium(f, self.game_dir, self.registry, nx, premium=self._premium(),
                                                 is_running=self.game_running, max_bytes=self.settings.max_extract_mb * 1024 * 1024,
                                                 max_files=self.settings.max_extract_files)
                    self._ui(self.log_line, f"{f}: updated to {r.version or r.file_id}.", "green")
                except (ModImportError, LoaderError, NexusError, NotPremium) as e:
                    self._ui(self.log_line, f"{f}: {e}", "bold red")
        self._ui(self._after_registry_change, None)
        self._ui(lambda: self.start_check(force=True))

    def _watch_download(self, mod_id: int, latest: dict, label: str) -> Path | None:
        """Free-account path: open the files page, then wait (cancellably) for the finished download to appear."""
        since = time.time()
        url = open_files_page(mod_id)
        dl = Path(self.settings.downloads_dir) if self.settings.downloads_dir else Path.home() / "Downloads"
        self._ui(self.log_line, f"Opened {url} - download '{latest.get('file_name')}' for {label}. Watching {dl} ...", "cyan")
        w = get_current_worker()
        found = wait_for_download(dl, mod_id, latest, since=since, timeout_s=self.download_wait_s,
                                  cancelled=lambda: w.is_cancelled or self._exiting)
        if not found:
            self._ui(self.log_line, "No matching download seen. When it finishes, press I and enter its path.", "yellow")
        return found

    def _job_manual(self, folder: str) -> None:
        rec = self.registry.mod(folder) or {}
        latest = rec.get("latest") or {}
        if not rec.get("nexus_id") or not latest:
            self._ui(self.log_line, f"{folder}: not linked or not checked yet.", "yellow")
            return
        found = self._watch_download(rec["nexus_id"], latest, folder)
        if found and self.ask(ConfirmScreen(f"Import {found.name}?", f"Update {folder} from the file you just downloaded.")):
            self._job_import(str(found), rec["nexus_id"])

    # -------------------------------------------------------------- quit
    def _release(self) -> None:
        self._exiting = True
        if self._instance_lock:
            self._instance_lock.release()

    def action_help(self) -> None:
        self.push_screen(NoticeScreen("Cogitator " + __version__, HELP_BODY.format(log=log_path(), cfg=config_dir())), lambda _: None)

    def action_quit_app(self) -> None:
        self._release()
        self.exit()

    def on_unmount(self) -> None:
        self._release()


def run_tui(game_override: str | None = None, check_updates: bool = True) -> int:
    from ..config import SchemaTooNew
    from ..registry import check_data_versions

    try:
        check_data_versions()
    except SchemaTooNew as e:
        print(str(e), file=sys.stderr)
        return 2
    CogitatorApp(game_override=game_override, check_updates=check_updates).run()
    return 0
