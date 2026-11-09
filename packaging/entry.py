"""PyInstaller entry point (frozen builds cannot use the console-script wrapper)."""
import sys

from cogitator.cli import main

if __name__ == "__main__":
    sys.exit(main())
