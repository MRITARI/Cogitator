"""Cogitator Setup: the tkinter wizard. Page rules are in `viewmodel` and the work is done by the `cogitator` engine;
this file draws the pages and moves messages between the UI thread and the worker threads."""
from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from cogitator import __version__
from cogitator.config import redact
from cogitator.setup_run import ProgressEvent, SetupDeps, SetupResult

from . import platform_gate
from .controller import Controller
from .viewmodel import KEY_BLURB, NEXUS_API_PAGE, NOTICE, PATCH_SENTENCE, Page, Services, SetupModel

TITLE = "Cogitator Setup"
POLL_MS = 40
STEP_LABELS = {
    "check_game": "Check the game folder", "check_running": "Make sure Darktide is closed", "app": "Install Cogitator",
    "dml": "Install the Darktide Mod Loader", "dmf": "Install the Darktide Mod Framework", "patch": "Switch mods on",
    "register": "Save your settings", "shortcuts": "Create shortcuts", "doctor": "Check everything",
}
MARK = {"pending": "[ ]", "running": "[..]", "done": "[x]", "skipped": "[-]", "failed": "[!]", "rolled_back": "[<]"}


def scroll_dialog(title: str, body: str) -> None:
    """A resizable, scrollable text window (for long reports such as the check-up results). Returns when it is closed."""
    parent = tk._default_root
    top = tk.Toplevel(parent)
    top.title(title)
    top.geometry("760x480")
    top.minsize(420, 260)
    top.columnconfigure(0, weight=1)
    top.rowconfigure(0, weight=1)
    text = tk.Text(top, wrap="word", padx=8, pady=8)
    sb = ttk.Scrollbar(top, command=text.yview)
    text.configure(yscrollcommand=sb.set)
    text.insert("1.0", body)
    text.configure(state="disabled")
    text.grid(row=0, column=0, sticky="nsew")
    sb.grid(row=0, column=1, sticky="ns")
    close = ttk.Button(top, text="Close", command=top.destroy)
    close.grid(row=1, column=0, columnspan=2, pady=8)
    top.bind("<Escape>", lambda e: top.destroy())
    top.bind("<Return>", lambda e: top.destroy())
    if parent is not None:
        top.transient(parent)
    close.focus_set()
    top.grab_set()
    top.wait_window()


def _launch_cogitator() -> None:
    from .launch import launch_cogitator

    launch_cogitator()


# ---------------------------------------------------------------- things tests replace
@dataclass
class Dialogs:
    ask_yes_no: Callable[[str, str], bool] = lambda title, body: messagebox.askyesno(title, body)
    show_error: Callable[[str, str], None] = lambda title, body: messagebox.showerror(title, body)
    show_info: Callable[[str, str], None] = scroll_dialog
    browse_dir: Callable[[], str | None] = lambda: filedialog.askdirectory(title="Choose your Darktide folder") or None
    browse_zip: Callable[[str], str | None] = lambda what: filedialog.askopenfilename(
        title=f"Choose the {what} zip you downloaded", filetypes=[("Zip files", "*.zip")]) or None
    open_url: Callable[[str], object] = webbrowser.open
    launch: Callable[[], object] = lambda: _launch_cogitator()


class SetupApp:
    def __init__(self, model: SetupModel | None = None, controller: Controller | None = None, dialogs: Dialogs | None = None,
                 root: tk.Tk | None = None, services: Services | None = None, deps: SetupDeps | None = None,
                 dry_run: bool = False, repair: bool = False, first_run: bool = False):
        from .dryrun import simulated_setup

        self.root = root or tk.Tk()
        self.model = model or SetupModel(services)
        self.model.repair = repair
        self.model.first_run = first_run
        self.dry_run = dry_run
        self.ctl = controller or Controller(self.model, deps, **({"run_fn": simulated_setup} if dry_run else {}))
        self.dlg = dialogs or (Dialogs(launch=lambda: None) if dry_run else Dialogs())
        self.root.title(f"{TITLE} {self.model.version}" + (" - DRY RUN (nothing will be changed)" if dry_run else ""))
        self.root.geometry("760x560")
        self.root.minsize(560, 420)
        self.step_rows: dict[str, str] = {}
        self.result: SetupResult | None = None
        self.in_wizard = False
        self._build_fonts()
        self._build_shell()
        self._build_pages()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Return>", lambda e: self._on_return())
        self.root.bind("<Escape>", lambda e: self._on_escape())
        for seq, delta in (("<Control-plus>", 1), ("<Control-equal>", 1), ("<Control-minus>", -1)):
            self.root.bind(seq, lambda e, d=delta: self.scale_text(d))
        self.root.bind("<Control-0>", lambda e: self.scale_text(0))
        if first_run:
            self.show_wizard()  # the installer already put the app in place: go straight to the install flow
            self._to_front()
        else:
            self.show_express()
        self.root.after(POLL_MS, self._poll)

    def _to_front(self) -> None:
        """Launched by the installer, the window must not open behind it."""
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(500, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- shell
    def _build_fonts(self) -> None:
        self.fonts = [tkfont.nametofont(n) for n in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont")]
        self.base_size = abs(int(self.fonts[0].cget("size"))) or 10
        self.title_font = tkfont.Font(family=self.fonts[0].cget("family"), size=self.base_size + 6, weight="bold")

    def scale_text(self, delta: int) -> None:
        """Ctrl+Plus / Ctrl+Minus / Ctrl+0: larger or smaller text."""
        size = self.base_size if delta == 0 else max(8, min(28, abs(int(self.fonts[0].cget("size"))) + delta))
        for f in self.fonts:
            f.configure(size=size)
        self.title_font.configure(size=size + 6)

    def _build_shell(self) -> None:
        self.container = ttk.Frame(self.root, padding=16)
        self.container.pack(fill="both", expand=True)
        self.container.rowconfigure(0, weight=1)
        self.container.columnconfigure(0, weight=1)
        self.nav = ttk.Frame(self.root, padding=(16, 0, 16, 12))
        self.nav.pack(fill="x")
        self.btn_cancel = ttk.Button(self.nav, text="Cancel", command=self.on_cancel)
        self.btn_next = ttk.Button(self.nav, text="Next >", command=self.on_next)
        self.btn_back = ttk.Button(self.nav, text="< Back", command=self.on_back)
        self.nav_msg = ttk.Label(self.nav, text="", foreground="#a33", wraplength=380, justify="left")
        self.btn_cancel.pack(side="right", padx=(6, 0))
        self.btn_next.pack(side="right", padx=(6, 0))
        self.btn_back.pack(side="right")
        self.nav_msg.pack(side="left", fill="x", expand=True)

    def _page(self, name: str) -> ttk.Frame:
        f = ttk.Frame(self.container)
        f.grid(row=0, column=0, sticky="nsew")
        f.columnconfigure(0, weight=1)
        self.pages[name] = f
        return f

    def _title(self, parent, text: str, row: int = 0) -> None:
        ttk.Label(parent, text=text, font=self.title_font).grid(row=row, column=0, sticky="w", pady=(0, 10))

    def _wrapped(self, parent, text: str = "", row: int = 0, **kw) -> ttk.Label:
        lab = ttk.Label(parent, text=text, justify="left", **kw)
        lab.grid(row=row, column=0, sticky="we", pady=2)
        lab.bind("<Configure>", lambda e, l=lab: l.configure(wraplength=max(200, e.width - 4)))
        return lab

    def _build_pages(self) -> None:
        self.pages: dict[str, ttk.Frame] = {}
        self._build_express()
        self._build_welcome()
        self._build_game()
        self._build_key()
        self._build_components()
        self._build_review()
        self._build_progress()
        self._build_finish()

    # ---------------------------------------------------------------- Express (first screen)
    def _build_express(self) -> None:
        f = self._page("express")
        f.rowconfigure(3, weight=1)
        self._title(f, "Set up Cogitator")
        self.ex_status = self._wrapped(f, "Looking for Darktide...", row=1)
        self.ex_lines = tk.Text(f, height=7, wrap="word", relief="flat", background=self.root.cget("background"), state="disabled")
        self.ex_lines.grid(row=2, column=0, sticky="we", pady=6)
        self.ex_notice = self._wrapped(f, NOTICE, row=3, foreground="#555")
        self.ex_notice.grid(sticky="nwe")
        bar = ttk.Frame(f)
        bar.grid(row=4, column=0, sticky="we", pady=(10, 0))
        self.ex_install = ttk.Button(bar, text="Install", command=self.on_express_install, state="disabled")
        self.ex_install.pack(side="left", ipadx=40, ipady=8)
        self.ex_retry = ttk.Button(bar, text="Retry", command=self.start_express)
        self.ex_adv = ttk.Button(bar, text="Advanced...", command=self.show_wizard)
        self.ex_adv.pack(side="right")
        self.ex_retry.pack(side="right", padx=8)
        self.ex_hint = self._wrapped(f, "Clicking Install accepts the notice above and the downloads listed.", row=5, foreground="#555")

    def start_express(self) -> None:
        self.ex_install.configure(state="disabled")
        self.ex_status.configure(text="Looking for Darktide and checking where the files will come from...")
        self.ctl.prepare_express()

    def show_express(self) -> None:
        self.in_wizard = False
        self.pages["express"].tkraise()
        self.nav.pack_forget()
        self.start_express()

    def on_express_install(self) -> None:
        if self.model.confirm_install():
            self.start_install()

    def _express_ready(self, ok: bool, message: str) -> None:
        self.model.refresh_running() if self.model.game_dir else None
        self.ex_lines.configure(state="normal")
        self.ex_lines.delete("1.0", "end")
        self.ex_lines.insert("end", "\n".join(self.model.express_lines()) if ok else "")
        self.ex_lines.configure(state="disabled")
        if ok:
            self.ex_status.configure(text=f"Darktide found: {self.model.game_dir}\nThis will be installed:")
            self.ex_install.configure(state="normal")
        else:
            self.ex_status.configure(text=message or "Cogitator could not get ready.")
            self.ex_install.configure(state="disabled")

    # ---------------------------------------------------------------- wizard shell
    def show_wizard(self) -> None:
        self.in_wizard = True
        self.nav.pack(fill="x")
        self.model.page = Page.WELCOME
        self.model.accepted = False
        self._goto(Page.WELCOME)

    def _goto(self, page: Page) -> None:
        self.model.page = page
        self.pages[page.value].tkraise()
        getattr(self, f"_enter_{page.value}", lambda: None)()
        self._refresh_nav()

    def _refresh_nav(self) -> None:
        p = self.model.page
        ok, why = self.model.can_next()
        run_pages = p in (Page.PROGRESS, Page.FINISH)
        self.btn_back.configure(state="disabled" if p is Page.WELCOME or run_pages else "normal")
        self.btn_next.configure(state="normal" if ok and not run_pages else "disabled",
                                text="Install" if p is Page.REVIEW else "Next >")
        self.nav_msg.configure(text="" if ok or run_pages else why)
        self.btn_cancel.configure(text="Close" if p is Page.FINISH else "Cancel",
                                  state="disabled" if (p is Page.PROGRESS and self.ctl.cancel_event.is_set()) else "normal")
        if p is Page.PROGRESS:
            self.btn_cancel.configure(text="Cancelling..." if self.ctl.cancel_event.is_set() else "Cancel")
        if p is Page.FINISH:
            self.btn_next.pack_forget()
            self.btn_back.pack_forget()

    def on_next(self) -> None:
        p = self.model.page
        if p is Page.REVIEW:
            if self.model.confirm_install():
                self.start_install()
            return
        if self.model.next():
            self._goto(self.model.page)

    def on_back(self) -> None:
        if self.model.back():
            self._goto(self.model.page)

    def on_cancel(self) -> None:
        if self.model.page is Page.PROGRESS:
            self.ctl.cancel()
            self._refresh_nav()
        elif self.model.page is Page.FINISH or not self.in_wizard:
            self.root.destroy()
        else:
            self.root.destroy()  # nothing has been changed before the Progress page

    def on_close(self) -> None:
        if self.ctl.running:
            self.ctl.cancel()  # never kill a run mid-way: ask it to stop at a safe boundary
            self._refresh_nav()
            return
        self.root.destroy()

    def _on_return(self) -> None:
        if self.in_wizard and str(self.btn_next.cget("state")) == "normal" and self.model.page is not Page.REVIEW:
            self.on_next()

    def _on_escape(self) -> None:
        if self.in_wizard and self.model.page not in (Page.PROGRESS, Page.FINISH):
            self.on_cancel()

    # ---------------------------------------------------------------- 1 Welcome
    def _build_welcome(self) -> None:
        f = self._page("welcome")
        self._title(f, f"Welcome to Cogitator {self.model.version}")
        self._wrapped(f, "Cogitator installs and updates mods for Warhammer 40,000: Darktide, and switches them on for you.", row=1)
        self._wrapped(f, NOTICE, row=2, foreground="#444")
        self.v_accept = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="I understand", variable=self.v_accept, command=self._accept_changed).grid(row=3, column=0, sticky="w", pady=10)

    def _accept_changed(self) -> None:
        self.model.accepted = self.v_accept.get()
        self._refresh_nav()

    # ---------------------------------------------------------------- 2 Game folder
    def _build_game(self) -> None:
        f = self._page("game")
        self._title(f, "Your Darktide folder")
        self.game_status = self._wrapped(f, "", row=1)
        row = ttk.Frame(f)
        row.grid(row=2, column=0, sticky="we", pady=6)
        row.columnconfigure(0, weight=1)
        self.v_game = tk.StringVar()
        self.e_game = ttk.Entry(row, textvariable=self.v_game)
        self.e_game.grid(row=0, column=0, sticky="we")
        ttk.Button(row, text="Browse...", command=self.on_browse_game).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(row, text="Retry", command=self.on_retry_game).grid(row=0, column=2, padx=(6, 0))
        self.e_game.bind("<FocusOut>", lambda e: self._typed_game())
        self.e_game.bind("<Return>", lambda e: (self._typed_game(), "break")[1])

    def _enter_game(self) -> None:
        self.game_status.configure(text="Looking for Darktide...")
        self.ctl.detect_game()

    def _typed_game(self) -> None:
        text = self.v_game.get().strip().strip('"')
        if text and (self.model.game_dir is None or str(self.model.game_dir) != text):
            self.model.set_game(text)
            self._game_changed()

    def on_browse_game(self) -> None:
        d = self.dlg.browse_dir()
        if d:
            self.v_game.set(str(Path(d)))
            self.model.set_game(d)
            self._game_changed()

    def on_retry_game(self) -> None:
        if self.model.game_dir:
            self.model.refresh_running()
            self._game_changed()
        else:
            self._enter_game()

    def _game_changed(self) -> None:
        if self.model.game_dir:
            self.v_game.set(str(self.model.game_dir))
            self.game_status.configure(text="Darktide is running. Close it, then press Retry." if self.model.game_running
                                       else "Found your Darktide folder.")
        else:
            self.game_status.configure(text=self.model.game_message)
        self._refresh_nav()

    # ---------------------------------------------------------------- 3 Nexus key
    def _build_key(self) -> None:
        f = self._page("key")
        self._title(f, "Nexus Mods key (optional)")
        self._wrapped(f, KEY_BLURB, row=1)
        row = ttk.Frame(f)
        row.grid(row=2, column=0, sticky="we", pady=8)
        row.columnconfigure(0, weight=1)
        self.v_key = tk.StringVar()
        self.e_key = ttk.Entry(row, textvariable=self.v_key, show="*")
        self.e_key.grid(row=0, column=0, sticky="we")
        self.v_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Show", variable=self.v_show, command=self._toggle_show).grid(row=0, column=1, padx=6)
        self.btn_validate = ttk.Button(row, text="Validate", command=self.on_validate_key)
        self.btn_validate.grid(row=0, column=2)
        btns = ttk.Frame(f)
        btns.grid(row=3, column=0, sticky="w")
        ttk.Button(btns, text="Open the Nexus API page", command=lambda: self.dlg.open_url(NEXUS_API_PAGE)).pack(side="left")
        ttk.Button(btns, text="Skip", command=self.on_skip_key).pack(side="left", padx=8)
        self.key_status = self._wrapped(f, "", row=4)
        self.e_key.bind("<KeyRelease>", lambda e: self._key_typed())

    def _toggle_show(self) -> None:
        self.e_key.configure(show="" if self.v_show.get() else "*")

    def _key_typed(self) -> None:
        self.model.set_key(self.v_key.get())
        self.key_status.configure(text="")
        self._refresh_nav()

    def on_validate_key(self) -> None:
        self.model.set_key(self.v_key.get())
        if not self.model.has_key():
            self.key_status.configure(text="Paste your key first, or press Skip.")
            return
        self.key_status.configure(text="Checking with Nexus Mods...")
        self.btn_validate.configure(state="disabled")
        self.ctl.validate_key()

    def _key_checked(self, check) -> None:
        self.btn_validate.configure(state="normal")
        self.key_status.configure(text=check.message + (f"  Quota: {check.quota}." if check.ok and check.quota else ""))
        if check.ok:  # never echo the key back: clear the box (the model keeps it for the install) and reset the Show toggle
            self.v_key.set("")
            self.v_show.set(False)
            self._toggle_show()
        self._refresh_nav()

    def on_skip_key(self) -> None:
        self.v_key.set("")
        self.model.skip_key()
        self.key_status.configure(text="Skipped. You can add the key later with the K key in Cogitator.")
        if self.model.next():
            self._goto(self.model.page)

    # ---------------------------------------------------------------- 4 Components
    def _build_components(self) -> None:
        f = self._page("components")
        self._title(f, "What to install")
        self.comp_vars: dict[str, tk.BooleanVar] = {}
        self.comp_status: dict[str, ttk.Label] = {}
        r = 1
        for kind in ("app", "dml", "dmf"):
            c = self.model.components[kind]
            v = tk.BooleanVar(value=c.selected)
            cb = ttk.Checkbutton(f, text=c.title, variable=v, command=lambda k=kind: self._comp_changed(k))
            if c.required:
                cb.state(["disabled"])
            cb.grid(row=r, column=0, sticky="w")
            st = ttk.Label(f, text=c.description, foreground="#555")
            st.grid(row=r + 1, column=0, sticky="w", padx=24, pady=(0, 6))
            self.comp_vars[kind], self.comp_status[kind] = v, st
            r += 2
        self.v_mods = tk.BooleanVar(value=True)
        self.v_desktop = tk.BooleanVar(value=True)
        self.v_start = tk.BooleanVar(value=True)
        for text, var, attr in (("Switch mods on", self.v_mods, "enable_mods"), ("Desktop icon", self.v_desktop, "desktop_icon"),
                                ("Start Menu entry", self.v_start, "start_menu")):
            ttk.Checkbutton(f, text=text, variable=var, command=lambda a=attr, v=var: setattr(self.model, a, v.get())).grid(row=r, column=0, sticky="w")
            r += 1
        self._wrapped(f, PATCH_SENTENCE, row=r, foreground="#555")

    def _enter_components(self) -> None:
        for kind in ("dml", "dmf"):
            c = self.model.components[kind]
            self.comp_vars[kind].set(c.selected)
            self.comp_status[kind].configure(text=f"{c.description}  Status: {c.status_text}.")

    def _comp_changed(self, kind: str) -> None:
        self.model.toggle(kind, self.comp_vars[kind].get())

    # ---------------------------------------------------------------- 5 Review
    def _build_review(self) -> None:
        f = self._page("review")
        f.rowconfigure(2, weight=1)
        self._title(f, "Review")
        self.review_status = self._wrapped(f, "Working out where each download comes from...", row=1)
        body = ttk.Frame(f)
        body.grid(row=2, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.review_text = tk.Text(body, wrap="word", state="disabled", relief="solid", borderwidth=1)
        sb = ttk.Scrollbar(body, command=self.review_text.yview)
        self.review_text.configure(yscrollcommand=sb.set)
        self.review_text.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.review_text.tag_configure("h", font=self.title_font)
        self.review_text.tag_configure("warn", foreground="#a33")
        self.choice_frame = ttk.Frame(f)
        self.choice_frame.grid(row=3, column=0, sticky="we", pady=6)

    def _enter_review(self) -> None:
        self.review_status.configure(text="Working out where each download comes from...")
        self.btn_next.configure(state="disabled")
        self.ctl.load_options()

    def _render_review(self) -> None:
        for w in self.choice_frame.winfo_children():
            w.destroy()
        for kind in self.model.selected_kinds():
            opts = self.model.options.get(kind) or []
            if len(opts) <= 1 and not (opts and opts[0].kind == "manual"):
                continue
            row = ttk.Frame(self.choice_frame)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=self.model.components[kind].title + ":").pack(side="left")
            cb = ttk.Combobox(row, state="readonly", values=[o.label for o in opts], width=58)
            cb.current(self.model.choice.get(kind, 0))
            cb.pack(side="left", padx=6)
            cb.bind("<<ComboboxSelected>>", lambda e, k=kind, c=cb: self._pick_source(k, c.current()))
            if (self.model.selected_option(kind) or opts[0]).kind == "manual":
                ttk.Button(row, text="Open Nexus page", command=lambda o=self.model.selected_option(kind): self.dlg.open_url(o.url)).pack(side="left")
                ttk.Button(row, text="Choose zip...", command=lambda k=kind: self._choose_zip(k)).pack(side="left", padx=4)
        t = self.review_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        for row in self.model.review_rows():
            t.insert("end", row.label + "\n", "h")
            for line in row.lines:
                t.insert("end", "  " + line + "\n")
            if row.warning:
                t.insert("end", "  " + row.warning + "\n", "warn")
            t.insert("end", "\n")
        t.configure(state="disabled")
        ok, why = self.model.install_ready()
        self.review_status.configure(text="Check what will happen, then press Install." if ok else why)
        self._refresh_nav()

    def _pick_source(self, kind: str, index: int) -> None:
        self.model.select_option(kind, index)
        self._render_review()

    def _choose_zip(self, kind: str) -> None:
        z = self.dlg.browse_zip(self.model.components[kind].title)
        if z:
            try:
                self.model.set_manual_zip(kind, Path(z))
                self.model.install_ready()
            except Exception as e:
                self.dlg.show_error(TITLE, redact(str(e)))
                self.model.manual_zip.pop(kind, None)
            self._render_review()

    # ---------------------------------------------------------------- 6 Progress
    def _build_progress(self) -> None:
        f = self._page("progress")
        f.rowconfigure(3, weight=1)
        self._title(f, "Installing")
        self.bar = ttk.Progressbar(f, maximum=100)
        self.bar.grid(row=1, column=0, sticky="we", pady=6)
        self.steps = ttk.Treeview(f, columns=("state", "what"), show="", height=9, selectmode="none")
        self.steps.column("state", width=48, stretch=False)
        self.steps.column("what", width=520)
        self.steps.grid(row=2, column=0, sticky="we")
        self.v_details = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Show details", variable=self.v_details, command=self._toggle_details).grid(row=4, column=0, sticky="w", pady=4)
        self.log = tk.Text(f, height=7, state="disabled", wrap="word")

    def _toggle_details(self) -> None:
        if self.v_details.get():
            self.log.grid(row=5, column=0, sticky="nsew")
        else:
            self.log.grid_remove()

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", redact(text) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start_install(self) -> None:
        from cogitator.setup_run import planned_steps

        plan = self.model.build_plan()
        self.in_wizard = True
        self.nav.pack(fill="x")
        for iid in self.steps.get_children():
            self.steps.delete(iid)
        self.step_rows = {}
        for name in planned_steps(plan):
            self.step_rows[name] = self.steps.insert("", "end", values=(MARK["pending"], STEP_LABELS.get(name, name)))
        self.bar["value"] = 0
        self._goto(Page.PROGRESS)
        self.ctl.install(plan)

    def _progress(self, ev: ProgressEvent) -> None:
        iid = self.step_rows.get(ev.step)
        if iid:
            state = {"start": "running"}.get(ev.state, ev.state)
            self.steps.item(iid, values=(MARK.get(state, "[?]"), STEP_LABELS.get(ev.step, ev.step) + (f" - {ev.message}" if ev.message and ev.state != "start" else "")))
        self.bar["value"] = ev.fraction * 100
        self._log(f"{ev.step}: {ev.state} {ev.message}".strip())

    # ---------------------------------------------------------------- 7 Finish
    def _build_finish(self) -> None:
        f = self._page("finish")
        self._title(f, "All done")
        self.fin_title = f.grid_slaves(row=0, column=0)[0]
        self.fin_lines = [self._wrapped(f, "", row=i) for i in (1, 2, 3)]
        self.v_doctor = tk.BooleanVar(value=True)
        self.v_launch = tk.BooleanVar(value=True)
        self.fin_doctor = ttk.Checkbutton(f, text="Show the check-up results", variable=self.v_doctor)
        self.fin_launch = ttk.Checkbutton(f, text="Start Cogitator now", variable=self.v_launch, command=self._finish_label)
        self.fin_doctor.grid(row=4, column=0, sticky="w", pady=(12, 0))
        self.fin_launch.grid(row=5, column=0, sticky="w")
        btns = ttk.Frame(f)
        btns.grid(row=6, column=0, sticky="w", pady=10)
        self.btn_copy = ttk.Button(btns, text="Copy details", command=self.on_copy_details)
        self.btn_copy.pack(side="left")
        self.btn_finish = ttk.Button(btns, text="Finish", command=self.on_finish)
        self.btn_finish.pack(side="left", padx=8)

    def _finish_label(self) -> None:
        self.btn_finish.configure(text="Start Cogitator" if self.v_launch.get() else "Finish")

    def _show_finish(self, res: SetupResult) -> None:
        self.result = res
        lines = self.model.finish_lines(res)
        for lab, text in zip(self.fin_lines, lines):
            lab.configure(text=text)
        self.fin_title.configure(text="All done" if res.ok else ("Setup was cancelled" if res.cancelled else "Setup could not finish"))
        if not res.ok:
            self.fin_launch.state(["disabled"])
            self.v_launch.set(False)
        if not res.doctor:
            self.fin_doctor.state(["disabled"])
            self.v_doctor.set(False)
        self._finish_label()
        self._goto(Page.FINISH)

    def on_copy_details(self) -> None:
        if self.result:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.result.diagnostics())  # redacted

    def on_finish(self) -> None:
        res = self.result
        if res and res.ok and self.v_doctor.get() and res.doctor:
            from cogitator.doctor import format_report

            self.dlg.show_info("Check-up results", format_report(res.doctor))
        if res and res.ok and self.v_launch.get():
            try:
                self.dlg.launch()
            except OSError:
                self.dlg.show_error(TITLE, "Cogitator could not be started. Open it from the Start Menu.")
        self.root.destroy()

    # ---------------------------------------------------------------- message pump (UI thread only)
    def _poll(self) -> None:
        try:
            for msg in self.ctl.drain():
                self._handle(msg)
        finally:
            try:
                self.root.after(POLL_MS, self._poll)
            except tk.TclError:  # window already destroyed
                pass

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "express":
            self._express_ready(msg[1], msg[2])
        elif kind == "game":
            self._game_changed()
        elif kind == "key":
            self._key_checked(msg[1])
        elif kind == "options":
            self._render_review()
        elif kind == "progress":
            self._progress(msg[1])
        elif kind == "confirm":
            self.ctl.answer(msg[3], self.dlg.ask_yes_no(msg[1], msg[2]))
        elif kind == "done":
            self._show_finish(msg[1])
        elif kind == "error":
            self.dlg.show_error(TITLE, f"Something went wrong ({msg[1]}). {msg[2]}")

    def mainloop(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------- entry point
def _default_show_error(title: str, body: str) -> None:
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(title, body, parent=r)
        r.destroy()
    except tk.TclError:
        print(f"{title}: {body}", file=sys.stderr)


_DETECT = object()  # "look at the real system"; None means "not Windows"

USAGE = """Cogitator Setup

  cogitator-setup                     install (or, if Cogitator is already installed, repair / uninstall)
  cogitator-setup --wizard            always open the install wizard
  cogitator-setup --uninstall         open the uninstaller
  cogitator-setup --uninstall --silent   uninstall with the defaults only, no window
  cogitator-setup --dry-run           look through every screen; NOTHING is installed, removed or saved
  cogitator-setup --game-dir PATH     use this Darktide folder (for example a scratch copy)
  cogitator-setup --self-check        check this build can run
"""


@dataclass
class Flags:
    uninstall: bool = False
    silent: bool = False
    maintenance: bool = False
    wizard: bool = False
    dry_run: bool = False
    help: bool = False
    game_dir: str | None = None


def parse_flags(argv: list[str]) -> Flags:
    f = Flags(uninstall="--uninstall" in argv, silent="--silent" in argv, maintenance="--maintenance" in argv, wizard="--wizard" in argv,
              dry_run="--dry-run" in argv, help=("--help" in argv or "-h" in argv))
    if "--game-dir" in argv and argv.index("--game-dir") + 1 < len(argv):
        f.game_dir = argv[argv.index("--game-dir") + 1]
    return f


def choose_mode(f: Flags, installed: bool) -> str:
    """setup | maintenance | uninstall | silent-uninstall. Running Setup when Cogitator is already installed opens maintenance."""
    if f.uninstall:
        return "silent-uninstall" if f.silent else "uninstall"
    if f.wizard:
        return "setup"
    return "maintenance" if (f.maintenance or installed) else "setup"


def setup_services(game_dir: str | None) -> Services:
    from cogitator.gamefinder import Resolution

    if not game_dir:
        return Services()
    g = Path(game_dir)
    return Services(find_game=lambda: Resolution(g, "override", [g]))


def uninstall_services(game_dir: str | None):
    from .uninstall_vm import UServices

    return UServices(game_dir=lambda: Path(game_dir)) if game_dir else UServices()


def _default_show_error(title: str, body: str) -> None:
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(title, body, parent=r)
        r.destroy()
    except tk.TclError:
        print(f"{title}: {body}", file=sys.stderr)


def main(argv: list[str] | None = None, *, build=_DETECT, show_error: Callable[[str, str], None] | None = None,
         app_factory: Callable[..., object] | None = None, uninstall_factory: Callable[..., object] | None = None,
         installed: bool | None = None, silent_fn: Callable[..., int] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-check" in argv:
        from .selfcheck import run

        return run()
    flags = parse_flags(argv)
    if flags.help:
        print(USAGE)
        return 0
    b = platform_gate.windows_build() if build is _DETECT else build
    if not platform_gate.is_supported(b):
        (show_error or _default_show_error)(TITLE, platform_gate.MESSAGE)
        return 1  # nothing has been created, read or changed
    from cogitator.config import SchemaTooNew
    from cogitator.registry import check_data_versions

    try:
        check_data_versions()
    except SchemaTooNew as e:
        (show_error or _default_show_error)(TITLE, str(e))
        return 2
    from .uninstall_app import UninstallApp, run_silent
    from .uninstall_vm import detect

    usvc = uninstall_services(flags.game_dir)
    if installed is None:
        installed = detect(usvc).any_installed
    mode = choose_mode(flags, installed)
    make_setup = app_factory or (lambda **kw: SetupApp(services=setup_services(flags.game_dir), dry_run=flags.dry_run, **kw))
    make_uninstall = uninstall_factory or (lambda **kw: UninstallApp(services=usvc, dry_run=flags.dry_run, **kw))
    if mode == "silent-uninstall":
        return (silent_fn or run_silent)(usvc)
    if mode == "uninstall":
        make_uninstall(start="choose").mainloop()
        return 0
    kw: dict = {"first_run": True} if flags.wizard else {}
    if mode == "maintenance":
        win = make_uninstall(start="maintenance")
        win.mainloop()
        if getattr(win, "action", None) != "repair":
            return 0
        kw["repair"] = True
    app = make_setup(**kw)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
