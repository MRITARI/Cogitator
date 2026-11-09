"""The uninstaller and maintenance-mode window (tkinter). Pages: [Maintenance: Repair / Update / Uninstall] -> What to remove ->
Review -> Progress -> Finish. This file only draws and moves messages; the page rules are in `uninstall_vm` and every
deletion is done by the engine (`cogitator.uninstall_run`)."""
from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable

from cogitator.config import redact
from cogitator.setup_run import ProgressEvent
from cogitator.uninstall_run import STEP_NAMES, UninstallDeps, planned_steps, run_uninstall

from .app import MARK, POLL_MS, Dialogs
from .controller import Controller
from .uninstall_vm import Detected, MaintenanceModel, UPage, UServices, UninstallModel

TITLE = "Cogitator Uninstall"


class UninstallApp:
    def __init__(self, model: UninstallModel | None = None, controller: Controller | None = None, dialogs: Dialogs | None = None,
                 root: tk.Tk | None = None, services: UServices | None = None, deps: UninstallDeps | None = None,
                 start: str = "choose", dry_run: bool = False, run_fn: Callable | None = None):
        from .dryrun import simulated_uninstall

        self.root = root or tk.Tk()
        self.model = model or UninstallModel(services)
        self.model.dry_run = dry_run
        self.ctl = controller or Controller(self.model, deps or UninstallDeps(), run_fn=run_fn or (simulated_uninstall if dry_run else run_uninstall))
        self.dlg = dialogs or Dialogs()
        self.action: str | None = None  # "repair" when the user picked Repair (the caller then opens the Setup wizard)
        self.result = None
        self.step_rows: dict[str, str] = {}
        self.root.title(TITLE + (" - DRY RUN (nothing will be changed)" if dry_run else ""))
        self.root.geometry("760x580")
        self.root.minsize(560, 440)
        self._fonts()
        self._shell()
        self.pages: dict[str, ttk.Frame] = {}
        for build in (self._build_maintenance, self._build_choose, self._build_review, self._build_progress, self._build_finish):
            build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Return>", lambda e: self._on_return())
        self.root.bind("<Escape>", lambda e: self._on_escape())
        for seq, d in (("<Control-plus>", 1), ("<Control-equal>", 1), ("<Control-minus>", -1)):
            self.root.bind(seq, lambda e, d=d: self.scale_text(d))
        self.root.bind("<Control-0>", lambda e: self.scale_text(0))
        self.model.load()
        if start == "maintenance":
            self.maint = MaintenanceModel(self.model.det)
            self._show_maintenance()
        else:
            self._goto(UPage.CHOOSE)
        self.root.after(POLL_MS, self._poll)

    # ---------------------------------------------------------------- shell
    def _fonts(self) -> None:
        self.fonts = [tkfont.nametofont(n) for n in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont")]
        self.base_size = abs(int(self.fonts[0].cget("size"))) or 10
        self.title_font = tkfont.Font(family=self.fonts[0].cget("family"), size=self.base_size + 6, weight="bold")

    def scale_text(self, delta: int) -> None:
        size = self.base_size if delta == 0 else max(8, min(28, abs(int(self.fonts[0].cget("size"))) + delta))
        for f in self.fonts:
            f.configure(size=size)
        self.title_font.configure(size=size + 6)

    def _shell(self) -> None:
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

    def _title(self, parent, text: str) -> ttk.Label:
        lab = ttk.Label(parent, text=text, font=self.title_font)
        lab.grid(row=0, column=0, sticky="w", pady=(0, 10))
        return lab

    def _wrapped(self, parent, text: str = "", row: int = 0, **kw) -> ttk.Label:
        lab = ttk.Label(parent, text=text, justify="left", **kw)
        lab.grid(row=row, column=0, sticky="we", pady=2)
        lab.bind("<Configure>", lambda e, l=lab: l.configure(wraplength=max(200, e.width - 4)))
        return lab

    # ---------------------------------------------------------------- maintenance
    def _build_maintenance(self) -> None:
        f = self._page("maintenance")
        self._title(f, "Cogitator is already installed")
        self.maint_summary = self._wrapped(f, "", row=1)
        row = ttk.Frame(f)
        row.grid(row=2, column=0, sticky="w", pady=14)
        self.btn_repair = ttk.Button(row, text="Repair", command=self.on_repair)
        self.btn_update = ttk.Button(row, text="Update", state="disabled")
        self.btn_uninstall = ttk.Button(row, text="Uninstall...", command=lambda: self._goto(UPage.CHOOSE))
        for b in (self.btn_repair, self.btn_update, self.btn_uninstall):
            b.pack(side="left", padx=(0, 10), ipadx=14, ipady=6)
        self._wrapped(f, "Repair reinstalls what Cogitator put in place and keeps your mods and mod list. Updating Cogitator itself "
                         "arrives in a later version.", row=3, foreground="#555")

    def _show_maintenance(self) -> None:
        self.maint_summary.configure(text=self.maint.summary())
        self.btn_update.configure(state="normal" if self.maint.enabled("update") else "disabled")
        self.nav.pack_forget()
        self.pages["maintenance"].tkraise()

    def on_repair(self) -> None:
        self.action = "repair"
        self.root.destroy()

    # ---------------------------------------------------------------- 1 What to remove
    def _build_choose(self) -> None:
        f = self._page("choose")
        self._title(f, "What do you want to remove?")
        self.v: dict[str, tk.BooleanVar] = {}
        r = 1

        def check(key: str, text: str, attr: str | None = None, cmd=None) -> ttk.Checkbutton:
            nonlocal r
            var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(f, text=text, variable=var, command=cmd or (lambda k=key: self._tick(k)))
            cb.grid(row=r, column=0, sticky="w")
            self.v[key] = var
            r += 1
            return cb

        self.cb_app = check("app", "Cogitator and its shortcuts")
        self.cb_dmf = check("dmf", "Darktide Mod Framework")
        self.cb_dml = check("dml", "Darktide Mod Loader, and switch mods off (the game goes back to normal)")
        self.cb_key = check("key", "My stored Nexus key")
        self.cb_data = check("data", "Cogitator's settings, caches and rollback copies")
        self.data_info = ttk.Label(f, text="", foreground="#555")
        self.data_info.grid(row=r, column=0, sticky="w", padx=24)
        r += 1
        self.cb_mods = check("mods", "My installed mods (this deletes their folders)")
        self.mods_frame = ttk.Frame(f)
        self.mods_frame.grid(row=r, column=0, sticky="we", padx=24)
        r += 1
        self.cb_order = check("order", "Also delete my mod list (mod_load_order.txt)")
        self.running_msg = self._wrapped(f, "", row=r, foreground="#a33")
        ttk.Button(f, text="Retry", command=self.on_retry).grid(row=r + 1, column=0, sticky="w")
        self.mod_vars: dict[str, tk.BooleanVar] = {}

    def _enter_choose(self) -> None:
        d = self.model.det
        self.v["app"].set(self.model.remove_app)
        self.v["dmf"].set(self.model.remove_dmf)
        self.v["dml"].set(self.model.remove_dml)
        self.v["key"].set(self.model.remove_key)
        self.v["data"].set(self.model.remove_data)
        has_app = self.model.svc.app_remover is not None
        self.cb_app.configure(text="Cogitator and its shortcuts" + ("" if has_app else "  (removed by the installed version)"))
        self.cb_app.state(["!disabled"] if has_app else ["disabled"])
        self.cb_dmf.configure(text="Darktide Mod Framework" + ("" if d.dmf != "missing" else "  (not installed)"))
        self.cb_dmf.state(["!disabled"] if d.dmf != "missing" else ["disabled"])
        self.cb_dml.configure(text="Darktide Mod Loader, and switch mods off (the game goes back to normal)" + ("" if d.dml != "missing" else "  (not installed)"))
        self.cb_dml.state(["!disabled"] if d.dml != "missing" else ["disabled"])
        self.cb_mods.state(["!disabled"] if d.user_mods else ["disabled"])
        info = ", ".join(f"{p} ({s})" for p, s in self.model.data_summary())
        self.data_info.configure(text=("Will remove: " + info) if info else "Nothing stored yet.")
        self._render_mods()
        self._refresh_choose()

    def _tick(self, key: str) -> None:
        m, v = self.model, self.v
        if key == "app":
            m.remove_app = v["app"].get()
        elif key == "dmf":
            m.remove_dmf = v["dmf"].get()
        elif key == "dml":
            m.remove_dml = v["dml"].get()
        elif key == "key":
            m.remove_key = v["key"].get()
        elif key == "data":
            m.remove_data = v["data"].get()
        elif key == "mods":
            m.set_remove_user_mods(v["mods"].get())
            self._render_mods()
        elif key == "order":
            m.set_remove_load_order(v["order"].get())
        self._refresh_choose()

    def _render_mods(self) -> None:
        for w in self.mods_frame.winfo_children():
            w.destroy()
        self.mod_vars = {}
        if not self.model.remove_user_mods:
            return
        for name in self.model.det.user_mods:
            var = tk.BooleanVar(value=name in self.model.user_mods_selected)
            self.mod_vars[name] = var
            ttk.Checkbutton(self.mods_frame, text=name, variable=var,
                            command=lambda n=name: (self.model.toggle_user_mod(n, self.mod_vars[n].get()), self._refresh_choose())).pack(anchor="w")

    def _refresh_choose(self) -> None:
        offered = self.model.load_order_offered()
        self.cb_order.state(["!disabled"] if offered else ["disabled"])
        self.v["order"].set(self.model.remove_load_order)
        self.running_msg.configure(text="Darktide is running. Close it, then press Retry." if self.model.game_running else "")
        self._refresh_nav()

    def on_retry(self) -> None:
        self.model.game_running = bool(self.model.svc.is_running())
        self._refresh_choose()

    # ---------------------------------------------------------------- 2 Review
    def _build_review(self) -> None:
        f = self._page("review")
        f.rowconfigure(2, weight=1)
        self._title(f, "Review: this will be removed")
        self._wrapped(f, "Check the list. Nothing is deleted until you press Remove.", row=1)
        body = ttk.Frame(f)
        body.grid(row=2, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.review = tk.Text(body, wrap="word", state="disabled", relief="solid", borderwidth=1)
        sb = ttk.Scrollbar(body, command=self.review.yview)
        self.review.configure(yscrollcommand=sb.set)
        self.review.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.review.tag_configure("h", font=self.title_font)

    def _enter_review(self) -> None:
        t = self.review
        t.configure(state="normal")
        t.delete("1.0", "end")
        for title, lines in self.model.review_text():
            t.insert("end", title + "\n", "h")
            for line in lines:
                t.insert("end", "  " + line + "\n")
            t.insert("end", "\n")
        t.configure(state="disabled")

    # ---------------------------------------------------------------- 3 Progress
    def _build_progress(self) -> None:
        f = self._page("progress")
        self._title(f, "Removing")
        self.bar = ttk.Progressbar(f, maximum=100)
        self.bar.grid(row=1, column=0, sticky="we", pady=6)
        self.steps = ttk.Treeview(f, columns=("state", "what"), show="", height=8, selectmode="none")
        self.steps.column("state", width=48, stretch=False)
        self.steps.column("what", width=520)
        self.steps.grid(row=2, column=0, sticky="we")

    def start_run(self) -> None:
        plan = self.model.build_plan()
        for iid in self.steps.get_children():
            self.steps.delete(iid)
        self.step_rows = {n: self.steps.insert("", "end", values=(MARK["pending"], STEP_NAMES.get(n, n))) for n in planned_steps(plan)}
        self.bar["value"] = 0
        self.model.page = UPage.PROGRESS
        self._goto(UPage.PROGRESS)
        self.ctl.install(plan)

    def _progress(self, ev: ProgressEvent) -> None:
        iid = self.step_rows.get(ev.step)
        if iid:
            state = {"start": "running"}.get(ev.state, ev.state)
            self.steps.item(iid, values=(MARK.get(state, "[?]"), STEP_NAMES.get(ev.step, ev.step)))
        self.bar["value"] = ev.fraction * 100

    # ---------------------------------------------------------------- 4 Finish
    def _build_finish(self) -> None:
        f = self._page("finish")
        self.fin_title = self._title(f, "Done")
        self.fin_lines = [self._wrapped(f, "", row=i) for i in (1, 2, 3)]
        btns = ttk.Frame(f)
        btns.grid(row=4, column=0, sticky="w", pady=14)
        self.btn_copy = ttk.Button(btns, text="Copy details", command=self.on_copy)
        self.btn_copy.pack(side="left")
        self.btn_close = ttk.Button(btns, text="Close", command=self.root.destroy)
        self.btn_close.pack(side="left", padx=8)

    def _show_finish(self, res) -> None:
        self.result = res
        for lab, text in zip(self.fin_lines, self.model.finish_lines(res)):
            lab.configure(text=text)
        self.fin_title.configure(text="Done" if res.ok else ("Uninstall was cancelled" if res.cancelled else "Uninstall could not finish"))
        self._goto(UPage.FINISH)

    def on_copy(self) -> None:
        if self.result:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.result.diagnostics() if hasattr(self.result, "diagnostics") else "")

    # ---------------------------------------------------------------- navigation
    def _goto(self, page: UPage) -> None:
        self.model.page = page
        self.nav.pack(fill="x")
        self.pages[page.value].tkraise()
        getattr(self, f"_enter_{page.value}", lambda: None)()
        self._refresh_nav()

    def _refresh_nav(self) -> None:
        p = self.model.page
        ok, why = self.model.can_next()
        if not ok and self.model.can_ask_confirmation():
            ok, why = True, ""  # Next asks the second confirmation before it goes on
        running = p in (UPage.PROGRESS, UPage.FINISH)
        self.btn_back.configure(state="normal" if p is UPage.REVIEW else "disabled")
        self.btn_next.configure(state="normal" if ok and not running else "disabled", text="Remove" if p is UPage.REVIEW else "Next >")
        self.nav_msg.configure(text=("Press Next to confirm which of your mods will be deleted." if (ok and self.model.can_ask_confirmation()) else "")
                               if (ok or running) else why)
        self.btn_cancel.configure(text="Close" if p is UPage.FINISH else ("Cancelling..." if (p is UPage.PROGRESS and self.ctl.cancel_event.is_set()) else "Cancel"),
                                  state="disabled" if (p is UPage.PROGRESS and self.ctl.cancel_event.is_set()) else "normal")
        if p is UPage.FINISH:
            self.btn_next.pack_forget()
            self.btn_back.pack_forget()

    def on_next(self) -> None:
        m = self.model
        if m.page is UPage.CHOOSE and m.needs_user_mod_confirmation:
            if not self.dlg.ask_yes_no("Delete these mods?", m.user_mod_confirmation_text()):
                return
            m.confirm_user_mods()
        if m.page is UPage.REVIEW:
            if m.can_next()[0]:
                self.start_run()
            return
        if m.next():
            self._goto(m.page)

    def on_back(self) -> None:
        if self.model.back():
            self._goto(self.model.page)

    def on_cancel(self) -> None:
        if self.model.page is UPage.PROGRESS:
            self.ctl.cancel()
            self._refresh_nav()
        else:
            self.root.destroy()

    def on_close(self) -> None:
        if self.ctl.running:
            self.ctl.cancel()  # never abandon a run mid-way
            self._refresh_nav()
            return
        self.root.destroy()

    def _on_return(self) -> None:
        if str(self.btn_next.cget("state")) == "normal" and self.model.page is UPage.CHOOSE:
            self.on_next()

    def _on_escape(self) -> None:
        if self.model.page in (UPage.CHOOSE, UPage.REVIEW):
            self.root.destroy()

    # ---------------------------------------------------------------- message pump
    def _poll(self) -> None:
        try:
            for msg in self.ctl.drain():
                kind = msg[0]
                if kind == "progress":
                    self._progress(msg[1])
                elif kind == "confirm":
                    self.ctl.answer(msg[3], self.dlg.ask_yes_no(msg[1], msg[2]))
                elif kind == "done":
                    self._show_finish(msg[1])
                elif kind == "error":
                    self.dlg.show_error(TITLE, f"Something went wrong ({msg[1]}). {msg[2]}")
        finally:
            try:
                self.root.after(POLL_MS, self._poll)
            except tk.TclError:
                pass

    def mainloop(self) -> None:
        self.root.mainloop()


def run_silent(services: UServices | None = None, deps: UninstallDeps | None = None, out=print) -> int:
    """`--uninstall --silent`: the defaults only (framework, mods off, key, Cogitator). Never user mods, the mod list or data."""
    m = UninstallModel(services)
    m.load()
    if (m.remove_dml or m.remove_dmf) and m.game_running:
        out("Darktide is running. Close it and run the uninstaller again. Nothing was changed.")
        return 2
    res = run_uninstall(m.build_plan(), deps=deps or UninstallDeps())
    for line in m.finish_lines(res):
        if line:
            out(line)
    return 0 if res.ok else 2
