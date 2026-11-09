"""Is Darktide running? (Windows tasklist / Linux ps incl. Proton). Fixed arg lists, no shell."""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)
PROCESS_NAME = "Darktide.exe"  # present in <game>/binaries on the inspected install


def darktide_running(run=subprocess.run) -> bool:
    try:
        if sys.platform == "win32":
            r = run(["tasklist", "/FI", f"IMAGENAME eq {PROCESS_NAME}", "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=15)
            return PROCESS_NAME.lower() in (r.stdout or "").lower()
        # Proton: the Windows exe shows up inside wine/proton command lines
        r = run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=15)
        return PROCESS_NAME.lower() in (r.stdout or "").lower()
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("could not determine whether Darktide is running: %s", e)
        return False
