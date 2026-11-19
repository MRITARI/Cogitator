"""Modal dialogs for the main window. Each call returns when the dialog is closed; closing with the X is "no" / "cancel".

Text is shown as plain text (paths contain brackets, the Nexus key is never echoed). Tests replace this class with a fake."""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .viewmodel import style_options, style_tag

WRAP = 560


class Dialogs:
    def __init__(self, root: tk.Misc):
        self.root = root

    # ---- plumbing
    def _top(self, title: str) -> tk.Toplevel:
        top = tk.Toplevel(self.root)
        top.title(title)
        top.transient(self.root)
        top.resizable(True, True)
        top.columnconfigure(0, weight=1)
        return top

    def _show(self, top: tk.Toplevel, focus: tk.Misc | None = None) -> None:
        top.update_idletasks()
        try:
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - top.winfo_reqwidth()) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - top.winfo_reqheight()) // 3)
            top.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass
        if focus is not None:
            focus.focus_set()
        try:
            top.grab_set()
        except tk.TclError:  # not viewable yet on some setups: the dialog still works, it is just not exclusive
            pass
        top.wait_window()

    @staticmethod
    def _buttons(top: tk.Toplevel, row: int, items: list[tuple[str, object]], default: int = 0) -> list[ttk.Button]:
        bar = ttk.Frame(top)
        bar.grid(row=row, column=0, sticky="e", padx=12, pady=(4, 12))
        out = []
        for i, (label, cmd) in enumerate(items):
            b = ttk.Button(bar, text=label, command=cmd, default="active" if i == default else "normal")
            b.grid(row=0, column=i, padx=(8, 0))
            out.append(b)
        return out

    @staticmethod
    def _heading(top: tk.Toplevel, title: str, body: str = "") -> int:
        f = tkfont.nametofont("TkDefaultFont").copy()
        f.configure(weight="bold")
        ttk.Label(top, text=title, font=f, wraplength=WRAP, justify="left").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))
        top._cog_font = f  # type: ignore[attr-defined]  # keep a reference
        row = 1
        if body:
            ttk.Label(top, text=body, wraplength=WRAP, justify="left").grid(row=1, column=0, sticky="w", padx=12, pady=(0, 6))
            row = 2
        return row

    # ---- questions
    def confirm(self, title: str, body: str = "", default_yes: bool = True) -> bool:
        top = self._top("Cogitator")
        res = [False]

        def done(v: bool) -> None:
            res[0] = v
            top.destroy()

        row = self._heading(top, title, body)
        yes, no = self._buttons(top, row, [("Yes", lambda: done(True)), ("No", lambda: done(False))], default=0 if default_yes else 1)
        top.protocol("WM_DELETE_WINDOW", lambda: done(False))
        top.bind("<Escape>", lambda e: done(False))
        top.bind("<Return>", lambda e: done(default_yes))
        top.bind("<Key-y>", lambda e: done(True))
        top.bind("<Key-n>", lambda e: done(False))
        self._show(top, yes if default_yes else no)
        return res[0]

    def ask_text(self, title: str, hint: str = "", body: str = "", allow_blank: bool = False, password: bool = False,
                 initial: str = "") -> str | None:
        top = self._top("Cogitator")
        res: list[str | None] = [None]
        var = tk.StringVar(value=initial)

        def ok() -> None:
            v = var.get().strip()
            if not v and not allow_blank:
                return
            res[0] = v
            top.destroy()

        row = self._heading(top, title, body)
        entry = ttk.Entry(top, textvariable=var, width=64, show="*" if password else "")
        entry.grid(row=row, column=0, sticky="ew", padx=12)
        if hint:
            ttk.Label(top, text=hint, wraplength=WRAP, foreground="#6b6b6b").grid(row=row + 1, column=0, sticky="w", padx=12, pady=(2, 0))
        self._buttons(top, row + 2, [("OK", ok), ("Cancel", top.destroy)])
        top.bind("<Return>", lambda e: ok())
        top.bind("<Escape>", lambda e: top.destroy())
        entry.icursor("end")
        self._show(top, entry)
        return res[0]

    def choose(self, question: str, options: list[str], selected: int = 0) -> int | None:
        top = self._top("Cogitator")
        res: list[int | None] = [None]
        row = self._heading(top, question)
        frame = ttk.Frame(top)
        frame.grid(row=row, column=0, sticky="nsew", padx=12)
        frame.columnconfigure(0, weight=1)
        top.rowconfigure(row, weight=1)
        lb = tk.Listbox(frame, height=min(14, max(4, len(options))), width=min(110, max(50, max((len(o) for o in options), default=50) + 2)),
                        exportselection=False, activestyle="dotbox")
        sb = ttk.Scrollbar(frame, command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        for o in options:
            lb.insert("end", o)
        if options:
            sel = min(max(selected, 0), len(options) - 1)
            lb.selection_set(sel)
            lb.see(sel)
            lb.activate(sel)
        lb.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        def ok(_e=None) -> None:
            cur = lb.curselection()
            if cur:
                res[0] = int(cur[0])
                top.destroy()

        self._buttons(top, row + 1, [("OK", ok), ("Cancel", top.destroy)])
        lb.bind("<Double-1>", ok)
        top.bind("<Return>", ok)
        top.bind("<Escape>", lambda e: top.destroy())
        self._show(top, lb)
        return res[0]

    # ---- information
    def notice(self, title: str, body: str, button: str = "OK") -> None:
        self.show_text(title, [(body, "")], button=button, size="620x380")

    def show_text(self, title: str, segments: list[tuple[str, str]], button: str = "Close", size: str = "780x500",
                  mono: bool = False, palette: dict[str, str] | None = None) -> None:
        from .viewmodel import PALETTES

        pal = palette or PALETTES["light"]
        top = self._top(title)
        top.geometry(size)
        top.minsize(380, 240)
        top.rowconfigure(0, weight=1)
        frame = ttk.Frame(top)
        frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        base = tkfont.nametofont("TkFixedFont" if mono else "TkDefaultFont")
        bold = base.copy()
        bold.configure(weight="bold")
        text = tk.Text(frame, wrap="word", padx=8, pady=8, font=base, bg=pal["bg"], fg=pal["fg"], relief="flat")
        sb = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=sb.set)
        for seg, style in segments:
            tag = style_tag(style)
            if tag:
                opts = style_options(style, pal)
                kw: dict = {}
                if "foreground" in opts:
                    kw["foreground"] = opts["foreground"]
                if "background" in opts:
                    kw["background"] = opts["background"]
                if opts.get("bold"):
                    kw["font"] = bold
                text.tag_configure(tag, **kw)
                text.insert("end", seg, tag)
            else:
                text.insert("end", seg)
        text.configure(state="disabled")
        text.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        (b,) = self._buttons(top, 1, [(button, top.destroy)])
        top.bind("<Escape>", lambda e: top.destroy())
        top.bind("<Return>", lambda e: top.destroy())
        top._cog_fonts = (bold,)  # type: ignore[attr-defined]
        self._show(top, b)

    def pick_action(self, title: str, heading: str, rows: list[str], buttons: list[tuple[str, str, bool]],
                    selected: int = 0) -> tuple[str, int | None] | None:
        """A list with action buttons. `buttons` is (id, label, needs a selected row). Returns (button id, selected row or None)
        when a button is pressed, None when the window is closed. The caller acts and, if it wants the window back, calls again."""
        top = self._top(title)
        res: list[tuple[str, int | None] | None] = [None]
        row = self._heading(top, heading)
        frame = ttk.Frame(top)
        frame.grid(row=row, column=0, sticky="nsew", padx=12)
        frame.columnconfigure(0, weight=1)
        top.rowconfigure(row, weight=1)
        lb = tk.Listbox(frame, height=min(14, max(6, len(rows))), width=min(110, max(60, max((len(r) for r in rows), default=60) + 2)),
                        exportselection=False, activestyle="dotbox")
        sb = ttk.Scrollbar(frame, command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        for r in rows:
            lb.insert("end", r)
        if rows:
            sel = min(max(selected, 0), len(rows) - 1)
            lb.selection_set(sel)
            lb.see(sel)
            lb.activate(sel)
        lb.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        def press(bid: str, needs_row: bool) -> None:
            cur = lb.curselection()
            if needs_row and not cur:
                return
            res[0] = (bid, int(cur[0]) if cur else None)
            top.destroy()

        items: list[tuple[str, object]] = [(label, lambda b=bid, n=needs: press(b, n)) for bid, label, needs in buttons]
        items.append(("Close", top.destroy))
        self._buttons(top, row + 1, items, default=len(items) - 1)
        top.bind("<Escape>", lambda e: top.destroy())
        self._show(top, lb)
        return res[0]

    def show_code(self, title: str, code: str, note: str = "") -> None:
        """A selectable text (a share code) with a Copy button."""
        top = self._top(title)
        row = self._heading(top, title, note)
        box = tk.Text(top, height=6, width=72, wrap="char")
        box.insert("1.0", code)
        box.grid(row=row, column=0, sticky="nsew", padx=12)

        def copy() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(code)

        self._buttons(top, row + 1, [("Copy to clipboard", copy), ("Close", top.destroy)], default=1)
        top.bind("<Escape>", lambda e: top.destroy())
        self._show(top, box)

    def error(self, title: str, body: str) -> None:
        messagebox.showerror(title, body, parent=self.root)

    # ---- system pickers
    def pick_folder(self) -> str | None:
        return filedialog.askdirectory(parent=self.root, title="Where is Darktide installed? Choose its folder") or None

    def pick_files(self, initial: Path | None = None) -> list[str]:
        names = filedialog.askopenfilenames(
            parent=self.root, title="Choose mod zip file(s) to install",
            initialdir=str(initial) if initial and Path(initial).is_dir() else None,
            filetypes=[("Mod archives", "*.zip"), ("All files", "*.*")])
        return list(names) if names else []
