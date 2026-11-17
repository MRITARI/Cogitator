"""PyInstaller entry point of cogitator-nxm.exe: a windowless program Windows runs for nxm:// links, so a click on the
Nexus website's "Mod Manager Download" button never flashes a console window."""
import sys

from cogitator.nxm import click_main

if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:  # the build script runs this; the program has no window, so the exit code is the answer
        import cogitator.nxm  # noqa: F401

        sys.exit(0)
    sys.exit(click_main(sys.argv[1:]))
