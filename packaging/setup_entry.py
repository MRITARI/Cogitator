"""PyInstaller entry point for Cogitator Setup (windowed): the setup wizard window."""
import sys

from cogitator_web.setup_app import entry

if __name__ == "__main__":
    sys.exit(entry())
