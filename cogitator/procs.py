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
        log.warning("tasklist failed (%s); asking Windows directly", e)
        found = _running_via_toolhelp()
        if found is None:
            log.warning("could not determine whether Darktide is running")
            return False
        return found


def _running_via_toolhelp() -> bool | None:
    """Fallback that does not need tasklist.exe: walk the process list with the Windows API. None = could not."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class ENTRY(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        k = ctypes.windll.kernel32
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return None
        try:
            e = ENTRY()
            e.dwSize = ctypes.sizeof(ENTRY)
            ok = k.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                if e.szExeFile.lower() == PROCESS_NAME.lower():
                    return True
                ok = k.Process32NextW(snap, ctypes.byref(e))
            return False
        finally:
            k.CloseHandle(snap)
    except Exception:
        return None
