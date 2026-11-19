"""PyInstaller entry point for cogitator-web.exe (windowed)."""
import sys

from cogitator_web.app import main

if __name__ == "__main__":
    sys.exit(main())
