"""Cogitator's main window. Rules live in `viewmodel` (what the table shows) and `cogitator.actions` (the flows); this file
draws widgets and moves messages between the UI thread and the worker threads (`controller`).

Threading: workers never touch widgets. They post to the controller's queue and `_poll` (after() on the UI thread) applies
the messages. A worker that needs an answer blocks only itself; its dialog is shown here, one at a time."""
from __future__ import annotations

import logging
import os
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from collections import deque
from pathlib import Path
from tkinter import ttk
from typing import Callable

from cogitator import __version__, actions, actions_nexus, snapshots
from cogitator.config import (InstanceLock, LockError, Settings, config_dir, get_api_key, log_path, redact)
from cogitator.frameworks import detect_framework
from cogitator.gamefinder import resolve_game_dir, validate_game_dir
from cogitator.linking import folder_mtime
from cogitator.loader import NEXUS_PAGES
from cogitator.modinstall import open_files_page
from cogitator.mods import (ENABLED, UNLISTED, ModError, mods_dir, move_mod, scan_mods, toggle_mod)
from cogitator.patcher import ON, PatcherError, banner as patch_banner, default_runner, patch_state
from cogitator.patcher import toggle as toggle_mods
from cogitator.procs import darktide_running
from cogitator.registry import Registry
from cogitator.updates import PROBABLY_OUTDATED, UPDATE, CheckBundle, FrameworkUpdate, ModRow, cached_frameworks, cached_rows, quota_text, run_checks
from cogitator.viewtext import gui_words, NOTICE_BODY, NOTICE_TITLE, fw_segments, nexus_segments, report_segments

from .controller import CHOICE, CONFIRM, TEXT, Controller, GuiUi
from .dialogs import Dialogs
from .tools import WATCH_MS, ToolFlows
from .workflows import ModFlows
from .viewmodel import COLUMNS, PALETTES, STRIPES, TAG_BAD, TAG_DIM, TAG_OK, TAG_WARN, ModListModel, move_steps, style_options, style_tag

log = logging.getLogger("cogitator.gui")

TITLE = "Cogitator"
POLL_MS = 40
NXM_MS = 2000
KEY_BLURB = ("Create one in your Nexus account settings, under API. It is stored in your OS keyring (or a private file) "
             "and is never logged or shown.")
HELP_BODY = (
    "Space or double-click   turn the selected mod on or off\n"
    "Ctrl+Up / Ctrl+Down     move it in the load order (or drag it in the list)\n"
    "Right-click a mod       every action for that mod\n"
    "R or F5                 check Nexus for updates      I or Ctrl+I   import zip(s)\n"
    "G or Ctrl+G             launch Darktide              D or Ctrl+D   doctor (diagnose problems)\n"
    "T or Ctrl+T             all mods on / off (the game patch)\n"
    "K or Ctrl+K             Nexus API key                X   mark a mod for bulk actions\n"
    "N   tags and note       *   favourite                / or Ctrl+F   filter the list\n"
    "U   update the selected mod        Ctrl+U   update all mods\n"
    "L   link to Nexus                  M   link all        F   update Mod Loader / Framework\n"
    "F3  search Nexus Mods\n"
    "B   bulk actions on marked mods    P   profiles    C   crash-log leads\n"
    "O   sort                Q or Ctrl+Q   quit\n"
    "\n"
    "Log file: {log}\n"
    "Settings folder: {cfg}\n"
    "Problems: run the doctor, then report at github.com/MRITARI/Cogitator/issues and attach the log file.\n"
    "Not affiliated with Fatshark, Games Workshop or Nexus Mods.")

_CTRL, _ALT = 0x4, 0x20000  # Tk event.state bits (Windows)


class StyledText(ttk.Frame):
    """A read-only text area whose runs carry rich-style strings ("bold green"). Used by the header and the log pane."""

    def __init__(self, master, height: int, palette: dict[str, str], mono: bool = False, scroll: bool = True):
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.pal = palette
        self.mono = mono
        base = tkfont.nametofont("TkFixedFont" if mono else "TkDefaultFont")
        self.font, self.bold = base, base.copy()
        self.bold.configure(weight="bold")
        self.text = tk.Text(self, height=height, wrap="word", state="disabled", relief="flat", padx=6, pady=3, font=self.font,
                            bg=palette["log_bg"] if mono else palette["bg"], fg=palette["fg"], cursor="arrow", takefocus=0)
        self.text.grid(row=0, column=0, sticky="nsew")
        if scroll:
            sb = ttk.Scrollbar(self, command=self.text.yview)
            self.text.configure(yscrollcommand=sb.set)
            sb.grid(row=0, column=1, sticky="ns")
        self._styles: dict[str, str] = {}

    def _tag(self, style: str) -> str:
        tag = style_tag(style)
        if tag and tag not in self._styles:
            self._configure_tag(tag, style)
            self._styles[tag] = style
        return tag

    def _configure_tag(self, tag: str, style: str) -> None:
        opts = style_options(style, self.pal)
        kw: dict = {"foreground": self.pal["fg"] if "foreground" not in opts else opts["foreground"]}
        if "background" in opts:
            kw["background"] = opts["background"]
        if opts.get("bold"):
            kw["font"] = self.bold
        self.text.tag_configure(tag, **kw)

    def restyle(self, palette: dict[str, str]) -> None:
        """Theme switch: new colours for the area and for every run already in it."""
        self.pal = palette
        self.text.configure(bg=palette["log_bg"] if self.mono else palette["bg"], fg=palette["fg"])
        for tag, style in self._styles.items():
            self._configure_tag(tag, style)

    def set(self, segments: list[tuple[str, str]]) -> None:
        self.clear()
        self.append(segments)

    def append(self, segments: list[tuple[str, str]], follow: bool = False) -> None:
        self.text.configure(state="normal")
        for seg, style in segments:
            tag = self._tag(style)
            self.text.insert("end", seg, (tag,) if tag else ())
        self.text.configure(state="disabled")
        if follow:
            self.text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def contents(self) -> str:
        return self.text.get("1.0", "end-1c")


class CogitatorGui(ModFlows, ToolFlows):
    def __init__(self, root: tk.Tk | None = None, *, game_dir: Path | None = None, game_override: str | None = None,
                 check_updates: bool = True, dialogs: Dialogs | None = None, controller: Controller | None = None,
                 settings: Settings | None = None, show_notice: bool = True):
        from cogitator.config import RECOVERED  # noqa: F401  (read in start-up)
        from cogitator.i18n import detect as detect_language, set_language

        self.root = root or tk.Tk()
        self.given_game = Path(game_dir) if game_dir else None
        self.override = game_override
        self.check_on_start = check_updates
        self.show_notice = show_notice
        self.settings = settings or Settings.load()
        set_language(detect_language(self.settings.language))
        self.dlg = dialogs or Dialogs(self.root)
        self.ctl = controller or Controller()
        self.ui = GuiUi(self.ctl)
        self.game_dir: Path | None = None
        self.registry: Registry | None = None
        self.model = ModListModel(meta=lambda f: actions.mod_meta(self.registry, f),
                                  mtime=lambda f: folder_mtime(mods_dir(self.game_dir) / f) if self.game_dir else None)
        self.lock = threading.RLock()  # serialises multi-step registry work between workers (the UI thread never takes it)
        self.instance_lock: InstanceLock | None = None
        self.fw_updates: list[FrameworkUpdate] = []
        self.checking = False
        self.last_bundle: CheckBundle | None = None
        self.exit_code = 0
        self.exiting = False
        self.palette = PALETTES["light"]
        self._pending_asks: deque = deque()
        self._modal = 0
        self._nxm_busy = False
        self._last_patch_state: str | None = None
        self.selfupdater = None  # SelfUpdater; created once the registry is loaded. Tests inject their own.
        self.selfupdate_on_start = True
        self.theme_name = "light"
        self._drag: dict | None = None
        # seams for tests
        self.game_running: Callable[[], bool] = darktide_running
        self.patch_runner = default_runner
        self.open_url: Callable[[str], object] = webbrowser.open
        self.open_path: Callable[[Path], object] = self._startfile
        self.pick_files_ok = True
        self.download_wait_s = 180.0

        self._build_window()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.report_callback_exception = self._tk_error
        self.root.after(POLL_MS, self._poll)
        self.root.after(NXM_MS, self._nxm_loop)
        self.root.after(WATCH_MS, self._watch_loop)
        self.root.after(0, self.start)

    # ================================================================== layout
    def _build_window(self) -> None:
        r = self.root
        r.title(f"{TITLE} {__version__}")
        r.geometry("1040x700")
        r.minsize(760, 480)
        try:
            from cogitator_setup.app import set_window_icon

            set_window_icon(r)
        except Exception:
            log.debug("no window icon", exc_info=True)
        self._base_theme = "clam"
        style = ttk.Style(r)
        for theme in ("vista", "xpnative", "winnative", "clam"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    self._base_theme = theme
                    break
                except tk.TclError:
                    continue
        rowh = max(22, tkfont.nametofont("TkDefaultFont").metrics("linespace") + 8)
        style.configure("Treeview", rowheight=rowh)
        style.configure("Banner.TLabel", background="#c62828", foreground="#ffffff", padding=(8, 4))
        style.configure("Hint.TLabel", foreground=self.palette["dim"])
        r.columnconfigure(0, weight=1)
        r.rowconfigure(2, weight=1)

        self._build_menu()
        # header: game folder, frameworks, Nexus
        head = ttk.Frame(r, padding=(8, 6, 8, 0))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        self.hdr = StyledText(head, height=3, palette=self.palette, scroll=False)
        self.hdr.grid(row=0, column=0, sticky="ew")
        self.banner = ttk.Label(head, text="", style="Banner.TLabel", wraplength=900, justify="left")
        self.banner.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.banner.grid_remove()
        self.update_label = ttk.Label(head, text="", style="Update.TLabel", wraplength=900, justify="left")
        self.update_label.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.update_label.grid_remove()

        # toolbar + filter
        bar = ttk.Frame(r, padding=(8, 6))
        bar.grid(row=1, column=0, sticky="ew")
        self.tool_buttons: dict[str, ttk.Button] = {}
        for key, label, cmd in (("toggle", "On/Off", self.toggle_selected), ("up", "Move up", lambda: self.move_selected(-1)),
                                ("down", "Move down", lambda: self.move_selected(1)), ("recheck", "Check updates", self.recheck),
                                ("import", "Import zip...", self.import_archives), ("launch", "Launch game", self.launch)):
            b = ttk.Button(bar, text=label, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.tool_buttons[key] = b
        ttk.Label(bar, text="Filter:").pack(side="left", padx=(14, 4))
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(bar, textvariable=self.filter_var, width=28)
        self.filter_entry.pack(side="left")
        self.filter_var.trace_add("write", lambda *_: self._filter_changed())
        self.filter_entry.bind("<Escape>", lambda e: (self.filter_var.set(""), self.tree.focus_set()))
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=110)
        self.progress.pack(side="right")

        # table + log in a paned window
        pane = ttk.PanedWindow(r, orient="vertical")
        pane.grid(row=2, column=0, sticky="nsew", padx=8)
        tf = ttk.Frame(pane)
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(tf, columns=[c[0] for c in COLUMNS], show="headings", selectmode="browse")
        for cid, text, width, stretch, anchor in COLUMNS:
            self.tree.heading(cid, text=text, command=lambda c=cid: self.sort_by(c))
            self.tree.column(cid, width=width, minwidth=48, stretch=stretch, anchor=anchor)
        ysb = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        self._configure_tree_tags()
        pane.add(tf, weight=4)
        self.logbox = StyledText(pane, height=7, palette=self.palette, mono=True)
        pane.add(self.logbox, weight=1)

        # status line
        st = ttk.Frame(r, padding=(8, 4))
        st.grid(row=3, column=0, sticky="ew")
        st.columnconfigure(0, weight=1)
        self.hint = ttk.Label(st, text="", style="Hint.TLabel", anchor="w", justify="left")
        self.hint.grid(row=0, column=0, sticky="ew")
        self.summary = ttk.Label(st, text="", anchor="e")
        self.summary.grid(row=0, column=1, padx=(12, 0))
        st.bind("<Configure>", lambda e: self.hint.configure(wraplength=max(200, e.width - 240)))

        self._bind_events()
        self.apply_theme(self.settings.gui_theme)

    def _configure_tree_tags(self) -> None:
        p = self.palette
        t = self.tree
        t.tag_configure("even", background=p["bg"])
        t.tag_configure("odd", background=p["stripe"])
        t.tag_configure(TAG_BAD, foreground=p["red"])
        t.tag_configure(TAG_WARN, foreground=p["yellow"])
        t.tag_configure(TAG_OK, foreground=p["green"])
        t.tag_configure(TAG_DIM, foreground=p["dim"])

    def _build_menu(self) -> None:
        m = tk.Menu(self.root)
        self.menus: dict[str, tk.Menu] = {}

        def menu(name: str, items: list, post=None) -> tk.Menu:
            sub = tk.Menu(m, tearoff=False, postcommand=post)
            for it in items:
                if it is None:
                    sub.add_separator()
                else:
                    label, cmd, accel, key = it
                    sub.add_command(label=label, command=cmd, accelerator=accel or "")
                    if key:
                        self.menu_index[key] = (sub, sub.index("end"))
            m.add_cascade(label=name, menu=sub)
            self.menus[name] = sub
            return sub

        self.menu_index: dict[str, tuple[tk.Menu, int]] = {}
        menu("File", [("Import zip(s)...", self.import_archives, "Ctrl+I", "import"), ("Open game folder", self.open_game_folder, "", "gamedir"),
                      None, ("Quit", self.quit, "Ctrl+Q", None)])
        menu("Mods", [("Turn on / off", self.toggle_selected, "Space", "toggle"),
                      ("Move up", lambda: self.move_selected(-1), "Ctrl+Up", "up"), ("Move down", lambda: self.move_selected(1), "Ctrl+Down", "down"),
                      None, ("Mark for bulk actions", self.toggle_mark, "X", "mark"), ("Favourite", self.toggle_fav, "*", "fav"),
                      ("Tags and note...", self.edit_note, "N", "note"), ("Pin version", self.toggle_pin, "", "pin"),
                      None, ("Filter...", self.focus_filter, "Ctrl+F", None), ("Sort (cycle)", self.cycle_sort, "O", None),
                      ("Bulk actions on marked mods...", self.bulk_actions, "B", "bulk"),
                      None, ("Mods on / off (game patch)", self.toggle_global, "Ctrl+T", "global")])
        menu("Nexus", [("Check for updates", self.recheck, "F5", "recheck"), ("Update all mods", self.update_all, "Ctrl+U", "update_all"),
                       ("Update Mod Loader / Framework", self.framework_update, "", "framework"), None,
                       ("Link all mods to Nexus...", self.link_all_mods, "", "link_all"), ("Search Nexus Mods...", self.search_nexus, "F3", "search"),
                       None, ("Nexus API key...", self.set_key, "Ctrl+K", None)])
        menu("Tools", [("Doctor (diagnose problems)", self.doctor, "Ctrl+D", "doctor"), ("Launch Darktide", self.launch, "Ctrl+G", "launch"),
                       None, ("Profiles...", self.profiles_window, "P", "profiles"), ("Snapshots...", self.snapshots_window, "", "snapshots"),
                       None, ("Find a crashing mod (bisect)...", self.bisect_wizard, "", "bisect"), ("Crash-log leads...", self.crashlog_window, "C", "crashlog")])
        view = tk.Menu(m, tearoff=False)
        self.theme_var = tk.StringVar(value="light")
        for label, val in (("Light theme", "light"), ("Dark theme", "dark")):
            view.add_radiobutton(label=label, value=val, variable=self.theme_var, command=lambda v=val: self.apply_theme(v, save=True))
        m.add_cascade(label="View", menu=view)
        self.menus["View"] = view
        menu("Help", [("Help", self.show_help, "F1", None), ("Update Cogitator...", self.apply_update, "", "selfupdate")])
        self.root.configure(menu=m)

    def _bind_events(self) -> None:
        r, t = self.root, self.tree
        r.bind("<F5>", lambda e: self.recheck())
        r.bind("<F1>", lambda e: self.show_help())
        r.bind("<F3>", lambda e: self.search_nexus())
        for seq, fn in (("<Control-i>", self.import_archives), ("<Control-g>", self.launch), ("<Control-d>", self.doctor),
                        ("<Control-t>", self.toggle_global), ("<Control-u>", self.update_all), ("<Control-k>", self.set_key), ("<Control-f>", self.focus_filter),
                        ("<Control-q>", self.quit)):
            r.bind(seq, lambda e, f=fn: (f(), "break")[1])
        t.bind("<Control-Up>", lambda e: (self.move_selected(-1), "break")[1])
        t.bind("<Control-Down>", lambda e: (self.move_selected(1), "break")[1])
        t.bind("<space>", lambda e: (self.toggle_selected(), "break")[1])
        t.bind("<Double-1>", self._on_double)
        t.bind("<<TreeviewSelect>>", lambda e: self._update_hint())
        t.bind("<Button-3>", self._on_context)
        t.bind("<Button-1>", self._on_press, add="+")
        t.bind("<B1-Motion>", self._on_drag, add="+")
        t.bind("<ButtonRelease-1>", self._on_release, add="+")
        # plain letters act only while the table has focus (typing in the filter box must not trigger anything)
        for keys, fn in ((("r", "R"), self.recheck), (("i", "I"), self.import_archives), (("g", "G"), self.launch),
                         (("d", "D"), self.doctor), (("t", "T"), self.toggle_global), (("k", "K"), self.set_key),
                         (("u", "U"), self.update_selected), (("l", "L"), self.link_selected), (("m", "M"), self.link_all_mods),
                         (("f", "F"), self.framework_update), (("x", "X"), self.toggle_mark), (("b", "B"), self.bulk_actions), (("p", "P"), self.profiles_window),
                         (("c", "C"), self.crashlog_window), (("n", "N"), self.edit_note), (("asterisk",), self.toggle_fav),
                         (("slash",), self.focus_filter), (("o", "O"), self.cycle_sort), (("q", "Q"), self.quit)):
            for k in keys:
                t.bind(f"<Key-{k}>", lambda e, f=fn: self._plain_key(e, f))

    @staticmethod
    def _plain_key(event, fn) -> str | None:
        if event.state & (_CTRL | _ALT):  # Ctrl+I etc. are handled by the global binding
            return None
        fn()
        return "break"

    def _watch_loop(self) -> None:
        try:
            self.watch_tick()
        except Exception:
            log.exception("game-update watcher failed")
        if not self.exiting:
            try:
                self.root.after(WATCH_MS, self._watch_loop)
            except tk.TclError:
                pass

    def _nxm_loop(self) -> None:
        try:
            self.nxm_tick()
        except Exception:
            log.exception("nxm poll failed")
        if not self.exiting:
            try:
                self.root.after(NXM_MS, self._nxm_loop)
            except tk.TclError:
                pass

    # ================================================================== message pump (UI thread only)
    def _poll(self) -> None:
        try:
            for msg in self.ctl.drain():
                self._handle(msg)
            self._service_asks()
        except tk.TclError:
            return  # window destroyed
        except Exception:
            log.exception("message pump failed")
        finally:
            if not self.exiting:
                try:
                    self.root.after(POLL_MS, self._poll)
                except tk.TclError:
                    pass

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "log":
            self.log_line(msg[1], msg[2])
        elif kind == "ui":
            msg[1](*msg[2], **msg[3])
        elif kind == "ask":
            self._pending_asks.append(msg[1:])
        elif kind == "error":
            self.log_line(f"{msg[1]} failed: {msg[2]}", "bold red")

    def _service_asks(self) -> None:
        while self._pending_asks and not self._modal:
            kind, payload, holder = self._pending_asks.popleft()
            value = None
            self._modal += 1
            try:
                value = self._dialog_for(kind, payload)
            except Exception:
                log.exception("dialog failed")
            finally:
                self._modal -= 1
            self.ctl.answer(holder, value)

    def _dialog_for(self, kind: str, p: dict):
        if kind == CONFIRM:
            return self.dlg.confirm(p["title"], p.get("body", ""), p.get("default_yes", True))
        if kind == TEXT:
            return self.dlg.ask_text(p["title"], p.get("hint", ""), p.get("body", ""), p.get("allow_blank", False), p.get("password", False))
        if kind == CHOICE:
            return self.dlg.choose(p["question"], p["options"], p.get("selected", 0))
        return None

    def _modal_call(self, fn: Callable, *a, **k):
        """A dialog opened by the UI thread itself: workers' questions wait until it is closed."""
        self._modal += 1
        try:
            return fn(*a, **k)
        finally:
            self._modal -= 1

    def _tk_error(self, exc, val, tb) -> None:
        log.error("UI callback failed", exc_info=(exc, val, tb))
        try:
            self.log_line(f"Internal error: {type(val).__name__}: {val}. Details are in the log file.", "bold red")
        except Exception:
            pass

    # ================================================================== log, header, hint
    def log_line(self, msg: str, style: str = "") -> None:
        msg = redact(str(msg))
        self.logbox.append([(msg + "\n", style)], follow=True)
        log.info(msg)

    def refresh_header(self) -> None:
        if not self.game_dir or not self.registry:
            return
        state = patch_state(self.game_dir)
        fw = self.fw_updates or cached_frameworks(self.registry)
        segs: list[tuple[str, str]] = [("Game: ", "bold"), (str(self.game_dir) + "\n", "")]
        segs += fw_segments(self.game_dir, self.registry, fw, state) + [("\n", "")]
        spin = "checking for updates..." if self.checking else ""
        segs += nexus_segments(bool(get_api_key()), self.registry.data["updates"].get("premium"), quota_text(self.registry), spin, gui=True)
        self.hdr.set(segs)
        b = patch_banner(self.game_dir)
        if b:
            self.banner.configure(text=gui_words(b))
            self.banner.grid()
        else:
            self.banner.grid_remove()
        ann = self.update_banner()
        if ann:
            self.update_label.configure(text=ann)
            self.update_label.grid()
        else:
            self.update_label.grid_remove()
        if self.checking:
            self.progress.start(14)
        else:
            self.progress.stop()

    def _update_hint(self) -> None:
        f = self.selected()
        self.hint.configure(text=self.model.hint(f) if f else self.model.hint(None))
        self.summary.configure(text=self.model.summary())

    # ================================================================== start-up chain
    def start(self) -> None:
        if getattr(self, "_started", False):
            return
        self._started = True
        from cogitator.config import RECOVERED

        self.log_line(f"Cogitator {__version__}   log file: {log_path()}   Press F1 for help.", "dim")
        for bad in list(RECOVERED):
            self.log_line(f"Your settings file could not be read, so defaults are used. The old file was kept as {bad}.", "bold yellow")
        if self.given_game:
            self._game_ready(self.given_game, persist=False)
        else:
            self._handle_resolution(resolve_game_dir(self.override, self.settings.game_dir))

    def _handle_resolution(self, res) -> None:
        if res.game_dir:
            self._game_ready(res.game_dir, persist=res.source in ("steam", "override"))
        elif res.source == "ambiguous":
            i = self._modal_call(self.dlg.choose, "Several Darktide installs found - which one?", [str(c) for c in res.candidates], 0)
            if i is None:
                self.quit()
            else:
                self._game_ready(res.candidates[i], persist=True)
        else:
            self._ask_game_path(res.message or "Darktide was not found automatically.")

    def _ask_game_path(self, why: str) -> None:
        while True:
            self.log_line(why + " Choose the Darktide folder.", "yellow")
            value = self._modal_call(self.dlg.pick_folder)
            if not value:
                self.quit()
                return
            value = value.strip('"')
            ok, reason = validate_game_dir(value)
            if ok:
                self._game_ready(Path(value), persist=True)
                return
            self.log_line(f"Not a Darktide folder: {reason}", "bold red")
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
            self._modal_call(self.dlg.notice, "Cogitator is already running", str(e))
            self.quit()
            return
        self.registry = Registry.load()
        self.refresh_all()
        if self.show_notice and not self.settings.notice_accepted:
            from cogitator.i18n import tr

            self._modal_call(self.dlg.notice, tr("notice.title", NOTICE_TITLE), tr("notice.body", NOTICE_BODY), "I understand")
            self.settings.notice_accepted = True
            self.settings.save()
        self._after_notice()

    def _after_notice(self) -> None:
        self._offer_bootstrap()
        self.start_selfupdate_check()
        if self.check_on_start and self.settings.check_updates_on_start:
            self.start_check()

    def _offer_bootstrap(self) -> None:
        if not self.game_dir:
            return
        missing = [k for k in ("dml", "dmf") if detect_framework(self.game_dir, k).status != "installed"]
        if not missing:
            return
        pages = ", ".join(f"{k.upper()}: {NEXUS_PAGES[k]}" for k in missing)
        i = self._modal_call(self.dlg.choose, "Install Darktide Mod Loader and Mod Framework?",
                             ["Import the Nexus zip(s) I downloaded (recommended)",
                              "Fetch from GitHub (opt-in: unreleased source archives; you confirm each source)", "Not now"], 0)
        if i == 0:
            self.log_line(f"Download from Nexus: {pages}", "cyan")
            self.import_archives()
        elif i == 1:
            self.bootstrap_github(missing)
        else:
            self.log_line(f"Mod Loader/Framework not installed. {pages}", "yellow")

    # ================================================================== table
    def selected(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def need_game(self) -> bool:
        if self.game_dir and self.registry:
            return True
        self.log_line("No game folder selected yet.", "yellow")
        return False

    def ctx(self) -> actions.Ctx:
        return actions.Ctx(self.game_dir, self.registry, self.settings, self.lock, self.patch_runner, self.game_running,
                           cancelled=self.ctl.closing.is_set, open_url=self.open_url, download_wait_s=self.download_wait_s)

    def refresh_all(self, keep: str | None = None) -> None:
        """Rescan mods and rebuild the table from cache (no network)."""
        if not self.game_dir or not self.registry:
            return
        keep = keep or self.selected()
        self.model.set_mods(scan_mods(self.game_dir))
        self.model.set_rows(cached_rows([m.folder for m in self.model.mods if m.present], self.registry))
        self._fill_tree(keep)
        self._update_hint()
        self.refresh_header()
        self._update_headings()

    def _fill_tree(self, keep: str | None) -> None:
        top = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        for rv in self.model.rows(keep):
            self.tree.insert("", "end", iid=rv.folder, values=rv.cells, tags=rv.tags)
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
            self.tree.focus(keep)
            self.tree.see(keep)
        else:
            self.tree.yview_moveto(top)

    def _update_headings(self) -> None:
        for cid, text, *_ in COLUMNS:
            arrow = ""
            if cid == self.model.sort_col:
                arrow = "  ▼" if self.model.sort_desc else "  ▲"
            self.tree.heading(cid, text=text + arrow)

    def _refresh_mod_row(self, folder: str) -> None:
        """One row changed (on/off, mark, note...): redraw it without rebuilding the table."""
        self.model.set_mods(scan_mods(self.game_dir))
        m = self.model.mod(folder)
        if m is None or not self.tree.exists(folder):
            self.refresh_all(keep=folder)
            return
        stripe = next((t for t in self.tree.item(folder, "tags") if t in STRIPES), STRIPES[0])
        self.tree.item(folder, values=self.model.cells(m), tags=(self.model.status_tag(m), stripe))
        self._update_hint()

    def _apply_row(self, r: ModRow) -> None:
        """In-place update of one row as its result arrives."""
        self.model.apply_row(r)
        if not self.tree.exists(r.folder):
            return
        m = self.model.mod(r.folder)
        if m is None:
            return
        stripe = next((t for t in self.tree.item(r.folder, "tags") if t in STRIPES), STRIPES[0])
        self.tree.item(r.folder, values=self.model.cells(m), tags=(self.model.status_tag(m), stripe))
        if self.selected() == r.folder:
            self._update_hint()
        self.summary.configure(text=self.model.summary())

    def _apply_fw(self, u: FrameworkUpdate) -> None:
        self.fw_updates = [x for x in self.fw_updates if x.kind != u.kind] + [u]
        self.refresh_header()

    def _after_outcome(self, out: actions.Outcome | None) -> None:
        if out is None:
            return
        if out.refresh:
            self.refresh_all(keep=out.folder)
        elif out.header:
            self.refresh_header()
        if out.recheck:
            self.start_check()

    def sort_by(self, col: str) -> None:
        self.model.set_sort(col)
        self.refresh_all(keep=self.selected())

    def cycle_sort(self) -> None:
        col = self.model.cycle_sort()
        self.refresh_all(keep=self.selected())
        self.log_line(f"Sorted by {col}", "dim")

    def _filter_changed(self) -> None:
        self.model.filter_text = self.filter_var.get()
        if self.game_dir:
            self._fill_tree(self.selected())
            self._update_hint()

    def focus_filter(self) -> None:
        self.filter_entry.focus_set()
        self.filter_entry.selection_range(0, "end")

    # ---- mouse
    def _row_at(self, event) -> str | None:
        return self.tree.identify_row(event.y) or None

    def _on_double(self, event) -> str | None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) == "#1":
            return "break"  # the On/Off cell toggles on a single click; a double click must not toggle twice
        row = self._row_at(event)
        if row:
            self.tree.selection_set(row)
            self.toggle_selected()
        return "break"

    def _on_press(self, event) -> None:
        self._drag = None
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        row = self._row_at(event)
        if not row:
            return
        if self.tree.identify_column(event.x) == "#1":  # the On/Off column acts as a checkbox
            self.tree.selection_set(row)
            self.toggle_selected()
            return
        self._drag = {"folder": row, "moved": False, "y": event.y}

    def _on_drag(self, event) -> None:
        d = self._drag
        if not d:
            return
        if abs(event.y - d["y"]) > 6:
            d["moved"] = True
        over = self._row_at(event)
        if d["moved"] and over and over != d["folder"]:
            self.hint.configure(text=f"Drop to move '{d['folder']}' to where '{over}' is in the load order.")

    def _on_release(self, event) -> None:
        d, self._drag = self._drag, None
        if not d or not d["moved"]:
            return
        over = self._row_at(event)
        if over and over != d["folder"]:
            self.drop_move(d["folder"], over)
        else:
            self._update_hint()

    def drop_move(self, src: str, dst: str) -> None:
        if not self.need_game():
            return
        if self.model.sort_col is not None:
            self.log_line("Drag-and-drop changes the load order: clear the sort first (Mods > Sort, or click the sorted heading until it is off).", "yellow")
            self._update_hint()
            return
        steps = move_steps(self.model.listed(), src, dst)
        if steps is None:
            self.log_line(f"{src} is not in the load order yet. Press Space to add it first.", "bold yellow")
            self._update_hint()
            return
        self._do_move(src, steps)

    # ---- context menu
    def _on_context(self, event) -> None:
        row = self._row_at(event)
        if not row or not self.need_game():
            return
        self.tree.selection_set(row)
        self.tree.focus(row)
        menu = self.build_row_menu(row)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def row_menu_items(self, folder: str) -> list[tuple[str, str, bool] | None]:
        """(label, action id, enabled) for the right-click menu; None is a separator. Same entries as the terminal UI."""
        m, row = self.model.mod(folder), self.model.urows.get(folder)
        if m is None:
            return []
        meta = actions.mod_meta(self.registry, folder)
        listed = m.state != UNLISTED
        linked = bool(row is not None and row.nexus_id)
        can_update = row is not None and row.status in (UPDATE, PROBABLY_OUTDATED)
        items: list = [
            ("Disable" if m.state == ENABLED else ("Enable" if m.state != UNLISTED else "Add to load order and enable"), "toggle", m.present),
            ("Move up", "up", listed and m.present), ("Move down", "down", listed and m.present), None,
            ("Update" + (f" to {row.latest}" if can_update and row.latest else ""), "update", can_update and m.present),
            ("Open Nexus page" if linked else "Link to Nexus...", "open_page" if linked else "link", m.present),
            *([("Set installed version...", "link", m.present and actions_nexus.link_needs_version_only(row))] if linked else []),
            ("Open folder in Explorer" if sys.platform == "win32" else "Open folder", "open_folder", m.present), None,
            ("Unmark" if folder in self.model.marked else "Mark for bulk actions", "mark", True),
            ("Remove favourite" if meta.get("fav") else "Favourite", "fav", True),
            ("Tags and note...", "note", True)]
        if linked:
            items += [("Show changelog", "changelog", True),
                      ("Unpin version" if meta.get("pinned_version") else "Pin version", "pin", True)]
        items.append(None)
        if (self.registry.mod(folder) or {}).get("previous"):
            items.append(("Roll back last update", "rollback", m.present))
        items.append(("Remove...", "remove", True))
        return items

    def build_row_menu(self, folder: str) -> tk.Menu:
        menu = tk.Menu(self.root, tearoff=False)
        for it in self.row_menu_items(folder):
            if it is None:
                menu.add_separator()
            else:
                label, aid, enabled = it
                menu.add_command(label=label, state="normal" if enabled else "disabled", command=lambda a=aid, f=folder: self.row_action(f, a))
        return menu

    def row_action(self, folder: str, aid: str) -> None:
        if self.model.mod(folder) is None:
            return
        if self.tree.exists(folder):
            self.tree.selection_set(folder)
        if aid == "toggle":
            self.toggle_selected()
        elif aid in ("up", "down"):
            self.move_selected(-1 if aid == "up" else 1)
        elif aid == "open_page":
            row = self.model.urows.get(folder)
            if row is not None and row.nexus_id:
                self.log_line(f"Opened {open_files_page(row.nexus_id, self.open_url)}", "cyan")
        elif aid == "open_folder":
            self._open_folder(mods_dir(self.game_dir) / folder)
        elif aid == "link":
            self.link_selected(folder)
        elif aid == "update":
            self.update_selected(folder)
        elif aid == "changelog":
            self.show_changelog(folder)
        elif aid == "mark":
            self.toggle_mark()
        elif aid == "fav":
            self.toggle_fav()
        elif aid == "note":
            self.edit_note()
        elif aid == "pin":
            self.toggle_pin()
        elif aid == "rollback":
            self.run_job("roll back", "install", lambda: self._after_outcome_ui(actions.roll_back(self.ctx(), self.ui, folder)))
        elif aid == "remove":
            self.remove_mod(folder)

    # ================================================================== actions
    def run_job(self, name: str, group: str, fn: Callable[[], None]) -> bool:
        return self.ctl.run(name, fn, group)

    def _after_outcome_ui(self, out: actions.Outcome | None) -> None:
        self.ctl.ui(self._after_outcome, out)

    # ---- per-mod
    def toggle_selected(self) -> None:
        f = self.selected()
        if not (self.need_game() and f):
            return
        try:
            self.log_line(toggle_mod(self.game_dir, f), "green")
        except ModError as e:
            self.log_line(str(e), "bold red")
        except OSError as e:
            self.log_line(f"Could not write mod_load_order.txt: {e}", "bold red")
        self._refresh_mod_row(f)

    def move_selected(self, delta: int) -> None:
        f = self.selected()
        if not (self.need_game() and f):
            return
        self._do_move(f, delta)

    def _do_move(self, folder: str, steps: int) -> None:
        if steps == 0:
            self.log_line(f"{folder}: stays where it is.", "dim")
            return
        snapshots.auto(self.game_dir, "reorder", once=True, registry=self.registry)
        last = f"{folder}: stays where it is."
        try:
            for _ in range(abs(steps)):
                last = move_mod(self.game_dir, folder, 1 if steps > 0 else -1)
            self.log_line(last, "green")
        except ModError as e:
            self.log_line(str(e), "bold red")
        except OSError as e:
            self.log_line(f"Could not write mod_load_order.txt: {e}", "bold red")
        if self.model.sort_col is not None:  # the table shows the load order again, so the move is visible
            self.model.set_sort(None)
        self.refresh_all(keep=folder)

    def toggle_mark(self) -> None:
        f = self.selected()
        if not (self.need_game() and f):
            return
        self.model.toggle_mark(f)
        self._refresh_mod_row(f)
        self.log_line(f"{len(self.model.marked)} mod(s) marked.", "dim")
        self.summary.configure(text=self.model.summary())

    def toggle_fav(self) -> None:
        f = self.selected()
        if not (self.need_game() and f):
            return
        msg = actions.toggle_fav(self.registry, f)
        self._refresh_mod_row(f)
        self.log_line(msg, "green")

    def toggle_pin(self) -> None:
        f = self.selected()
        if not (self.need_game() and f):
            return
        if not (self.model.urows.get(f) and self.model.urows[f].nexus_id):
            self.log_line(f"{f}: link the mod to Nexus before pinning a version.", "yellow")
            return
        msg = actions.toggle_pin(self.registry, f)
        self.log_line(msg, "green")
        self.refresh_all(keep=f)

    def edit_note(self) -> None:
        f = self.selected()
        if not (self.need_game() and f):
            return
        meta = actions.mod_meta(self.registry, f)
        cur = " ".join([f"#{t}" for t in meta.get("tags") or []] + ([meta["notes"]] if meta.get("notes") else []))
        text = self._modal_call(self.dlg.ask_text, f"Tags and note for {f}", "#ui #hud  free text note (empty = clear)", "", True, False, cur)
        if text is None:
            return
        msg = actions.set_note(self.registry, f, text)
        self._refresh_mod_row(f)
        self.log_line(msg, "green")

    def remove_mod(self, folder: str) -> None:
        from cogitator.config import cache_dir

        if not self._modal_call(self.dlg.confirm, f"Remove {folder}?",
                                "The mod is taken out of the load order and its folder is moved (not deleted) to "
                                f"{cache_dir() / 'removed'} so you can restore it.", False):
            return
        self.run_job("remove mod", "install", lambda: self._after_outcome_ui(actions.remove_one(self.ctx(), self.ui, folder)))

    def _open_folder(self, path: Path) -> None:
        try:
            self.open_path(path)
        except OSError as e:
            self.log_line(f"Could not open {path}: {e}", "bold red")

    @staticmethod
    def _startfile(path: Path) -> None:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606 - opens the folder in Explorer
        else:
            import subprocess

            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])

    def open_game_folder(self) -> None:
        if self.need_game():
            self._open_folder(self.game_dir)

    # ---- global
    def toggle_global(self) -> None:
        if not self.need_game():
            return
        if patch_state(self.game_dir) == ON and not self._modal_call(
                self.dlg.confirm, "Disable ALL mods?", "This un-patches the game database so no mod loads until you turn mods on again.", False):
            return
        self.run_job("toggle mods", "patch", self._job_toggle)

    def _job_toggle(self) -> None:
        try:
            with self.lock:
                r = toggle_mods(self.game_dir, self.registry, confirm_unpatch=lambda: True, runner=self.patch_runner,
                                is_running=self.game_running)
            self.ctl.log(f"{r.message}  MODS: {patch_state(self.game_dir)}", "green" if r.changed else "yellow")
        except PatcherError as e:
            self.ctl.log(f"Patch failed: {e}", "bold red")
        self.ctl.ui(self.refresh_header)

    def launch(self) -> None:
        if self.need_game():
            self.run_job("launch", "launch", self._job_launch)

    def _job_launch(self) -> None:
        from cogitator.launch import LaunchError, launch

        self.ctl.log("Checking the setup before launching...", "dim")
        try:
            with self.lock:
                r = launch(self.game_dir, self.registry, is_running=self.game_running, runner=self.patch_runner)
        except (LaunchError, ModError, OSError) as e:
            self.ctl.log(str(e), "bold red")
            return
        for n in r.notes:
            self.ctl.log(n, "yellow")
        self.ctl.log("Asked Steam to start Darktide.", "green")
        self.ctl.ui(self.refresh_header)

    def doctor(self) -> None:
        if self.need_game():
            self.log_line("Running doctor ...", "dim")
            self.run_job("doctor", "doctor", self._job_doctor)

    def _job_doctor(self) -> None:
        from cogitator.doctor import exit_code, record_history, run_doctor

        checks = run_doctor(str(self.game_dir) if self.game_dir else None, running=self.game_running, offline=False,
                            registry=Registry.load() if self.game_dir else None)
        record_history(checks)
        self.ctl.ui(self._show_report, checks, exit_code(checks))

    def _show_report(self, checks, code: int) -> None:
        self._modal_call(self.dlg.show_text, "Doctor", report_segments(checks), "Close", "820x520", True, self.palette)

    def import_archives(self) -> None:
        if not self.need_game():
            return
        files = self._modal_call(self.dlg.pick_files, self.ctx().downloads_dir())
        if files:
            ctx, ui = self.ctx(), self.ui
            self.run_job("import", "install", lambda: self._job_import(ctx, ui, list(files)))

    def _job_import(self, ctx: actions.Ctx, ui, files: list[str]) -> None:
        for p in files:
            actions.import_file(ctx, ui, str(p), None, self._after_outcome_ui)

    def set_key(self) -> None:
        key = self._modal_call(self.dlg.ask_text, "Nexus personal API key", "paste key (hidden)", KEY_BLURB, False, True)
        if not key:
            return
        ctx = self.ctx() if self.game_dir else None
        if ctx is None:
            self.log_line("Choose the game folder first.", "yellow")
            return
        self.run_job("validate key", "key", lambda: self._job_key(ctx, key))

    def _job_key(self, ctx: actions.Ctx, key: str) -> None:
        r = actions.store_key(ctx, key)
        self.ctl.log(r.message, "green" if r.ok else "bold red")
        if r.ok:
            self.ctl.ui(self.refresh_header)
            self.ctl.ui(lambda: self.start_check(force=True))

    # ---- update check
    def recheck(self) -> None:
        if self.need_game():
            self.start_check(force=True)

    def start_check(self, force: bool = False) -> None:
        if not self.need_game() or self.checking:
            return
        self.checking = True
        self.refresh_header()
        if not self.run_job("update check", "check", lambda: self._job_check(force)):
            self.checking = False
            self.refresh_header()

    def _job_check(self, force: bool) -> None:
        try:
            with self.lock:
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
        self.refresh_header()

    def _check_done(self, b: CheckBundle) -> None:
        self.checking = False
        self.last_bundle = b
        n = sum(1 for r in b.mods.rows if r.status == UPDATE) + sum(1 for u in b.frameworks if u.status == UPDATE)
        if not b.key_set:
            self.log_line("No Nexus API key: updates cannot be checked. Use Nexus > Nexus API key to set one.", "yellow")
        elif b.mods.rate_limited:
            self.log_line(f"Rate limited by Nexus - showing cached results. {b.mods.error}", "bold yellow")
        elif b.mods.error:
            self.log_line(f"Update check: {b.mods.error}", "bold yellow")
        else:
            src = "cache" if not b.live else f"{b.requests} request(s)"
            self.log_line(f"Update check done ({src}): {n} update(s) available.", "green" if not n else "bold yellow")
        self.refresh_header()
        self._update_hint()

    # ---- help, quit
    def show_help(self) -> None:
        self._modal_call(self.dlg.notice, f"Cogitator {__version__}", HELP_BODY.format(log=log_path(), cfg=config_dir()))

    def release(self) -> None:
        self.exiting = True
        self.ctl.close()
        if self.instance_lock:
            self.instance_lock.release()
            self.instance_lock = None

    def quit(self) -> None:
        if self.exiting:
            return
        self.release()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def mainloop(self) -> int:
        self.root.mainloop()
        self.release()
        return self.exit_code


# ================================================================== entry points
class GuiUnavailable(RuntimeError):
    """No usable Tk (no tkinter, or no display)."""


def _make_dpi_aware() -> None:
    """Without this Windows stretches the window as a bitmap on scaled displays and the text is blurry."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def run_gui(game_override: str | None = None, check_updates: bool = True, game_dir: str | None = None) -> int:
    from cogitator.config import SchemaTooNew
    from cogitator.registry import check_data_versions

    try:
        check_data_versions()
    except SchemaTooNew as e:
        _fatal(str(e))
        return 2
    _make_dpi_aware()
    try:
        root = tk.Tk()
    except tk.TclError as e:
        raise GuiUnavailable(str(e)) from e
    app = CogitatorGui(root, game_dir=Path(game_dir) if game_dir else None, game_override=game_override, check_updates=check_updates)
    return app.mainloop()


def _fatal(message: str) -> None:
    """Report a problem when there is no window yet (a windowed exe has no console to print to)."""
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass
    try:
        from tkinter import messagebox

        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(TITLE, message, parent=r)
        r.destroy()
    except Exception:
        pass


USAGE = ("usage: cogitator-gui [--game-dir DIR] [--instance NAME] [--no-update-check] [--version] [--self-check]\n"
         "Opens Cogitator's graphical interface. All other commands: run cogitator in a terminal (cogitator --help).")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-check" in argv:
        from .selfcheck import run

        return run()
    if "--version" in argv:
        print(__version__)
        return 0
    if "-h" in argv or "--help" in argv:
        print(USAGE)
        return 0
    game_dir = None
    for i, a in enumerate(argv):
        if a == "--game-dir" and i + 1 < len(argv):
            game_dir = argv[i + 1]
        elif a.startswith("--game-dir="):
            game_dir = a.split("=", 1)[1]
    from cogitator.cli import _early_instance
    from cogitator.config import setup_logging

    try:
        _early_instance(argv)
        setup_logging()
        return run_gui(game_override=game_dir, check_updates="--no-update-check" not in argv)
    except GuiUnavailable as e:
        _fatal(f"Cogitator could not open its window: {e}\nUse the terminal version instead:  cogitator --tui")
        return 4
    except Exception as e:
        logging.getLogger(__name__).exception("unhandled error")
        _fatal(f"Cogitator stopped because of an unexpected error: {redact(f'{type(e).__name__}: {e}')}\n"
               f"Details are in {log_path()}. Please include that file when you report the problem.")
        return 3

