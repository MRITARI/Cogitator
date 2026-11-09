"""`cogitator-setup --self-check`: proves a built installer has tkinter, its engine, and a usable Tk (no game, no network)."""
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
        ttk.Progressbar(root).destroy()
        ver = root.tk.call("info", "patchlevel")
        root.destroy()
        return f"Tk {ver}"

    def wizard_imports():
        import cogitator_setup.app  # noqa: F401
        import cogitator_setup.controller  # noqa: F401
        import cogitator_setup.viewmodel  # noqa: F401

    def engine():
        from cogitator import selfcheck

        import io, contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = selfcheck.run()
        if rc:
            raise RuntimeError("engine self-check failed:\n" + buf.getvalue())
        return "engine self-check passed"

    def windows():
        from . import platform_gate

        b = platform_gate.windows_build()
        return f"Windows build {b} ({'supported' if platform_gate.is_supported(b) else 'NOT Windows 11'})"

    for label, fn in (("wizard modules", wizard_imports), ("tkinter runtime", tk_runtime), ("engine", engine), ("platform", windows)):
        step(label, fn)
    print(f"python {sys.version.split()[0]}, frozen={getattr(sys, 'frozen', False)}")
    return 1 if problems else 0
