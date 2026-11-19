"""`cogitator` with no command opens the graphical interface; the terminal one stays behind `cogitator --tui`."""
from __future__ import annotations

import sys


def run_default_gui(game_override: str | None, check_updates: bool) -> int | None:
    """The GUI's exit code, or None when no window can be opened here (no tkinter or no display): the caller then falls back to
    the terminal interface, so cogitator over SSH or in a stripped-down Python still works."""
    try:
        from cogitator_gui.app import GuiUnavailable, run_gui
    except ImportError as e:  # tkinter or the GUI package is not installed
        print(f"Graphical interface not available ({e}); opening the terminal interface.", file=sys.stderr)
        return None
    try:
        return run_gui(game_override=game_override, check_updates=check_updates)
    except GuiUnavailable as e:
        print(f"Graphical interface not available ({e}); opening the terminal interface.", file=sys.stderr)
        return None
