"""PyInstaller entry point for Cogitator Setup (windowed): the web-style setup, with the tkinter one as fallback or via --classic."""
import sys

from cogitator_web.setup_app import entry

if __name__ == "__main__":
    sys.exit(entry())
