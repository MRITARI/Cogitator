"""`cogitator-gui --self-check`: proves a built GUI has tkinter, its modules, the engine and a usable Tk (no game, no network)."""
from __future__ import annotations

import sys


def run() -> int:
    problems: list[str] = []

    def step(label: str, fn) -> None:
        try:
            detail = fn()
            print(f"ok    {label}" + (f": {detail}" if detail else ""))
        except Exception as e:
            problems.append(label)
            print(f"FAIL  {label}: {type(e).__name__}: {e}")

    def tk_runtime():
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.withdraw()
        ttk.Treeview(root).destroy()
        ver = root.tk.call("info", "patchlevel")
        root.destroy()
        return f"Tk {ver}"

    def gui_imports():
        import cogitator_gui.app  # noqa: F401
        import cogitator_gui.controller  # noqa: F401
        import cogitator_gui.dialogs  # noqa: F401
        import cogitator_gui.viewmodel  # noqa: F401

    def engine():
        import contextlib
        import io

        from cogitator import selfcheck

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = selfcheck.run()
        if rc:
            raise RuntimeError("engine self-check failed:\n" + buf.getvalue())
        return "engine self-check passed"

    for label, fn in (("gui modules", gui_imports), ("tkinter runtime", tk_runtime), ("engine", engine)):
        step(label, fn)
    print(f"python {sys.version.split()[0]}, frozen={getattr(sys, 'frozen', False)}")
    return 1 if problems else 0
