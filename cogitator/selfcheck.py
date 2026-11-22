"""`cogitator --self-check`: proves an installed/frozen build has everything it needs (no game, no network, no secrets).

Used to verify pipx installs and PyInstaller builds, where dynamic imports (pywebview, keyring
backends) and the TLS certificate bundle are the usual things that go missing.
"""
from __future__ import annotations

import importlib
import sys


def run() -> int:
    problems: list[str] = []

    def step(label: str, fn) -> None:
        try:
            detail = fn()
            print(f"ok    {label}" + (f": {detail}" if detail else ""))
        except Exception as e:  # report every failure, not just the first
            problems.append(label)
            print(f"FAIL  {label}: {type(e).__name__}: {e}")

    def core():
        for m in ("config", "gamefinder", "loadorder", "mods", "loader", "frameworks", "fwupdate", "patcher", "nexus",
                  "updates", "modinstall", "doctor", "registry", "archives", "linking", "procs", "selfupdate", "updater",
                  "snapshots", "profiles", "modfile", "modchecks", "integrity", "diag", "launch", "repair", "bisect",
                  "crashlog", "nxm", "i18n"):
            importlib.import_module(f"cogitator.{m}")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # signature checks need it

        Ed25519PublicKey.from_public_bytes(bytes(32))

    def nexus_identity():
        import re

        from cogitator import __version__
        from cogitator.nexus import app_headers

        h = app_headers()
        assert h["Application-Name"] == "Cogitator" and h["Application-Version"] == __version__, h
        assert re.fullmatch(r"Cogitator/\S+ \(Windows; Python \d+\.\d+\)", h["User-Agent"]), h["User-Agent"]
        return f"Cogitator {__version__}"

    def tls():
        import httpx

        httpx.Client(verify=True).close()  # loads the CA bundle (certifi)
        return "CA bundle loads"

    def vdf():
        import vdf as v

        assert v.loads('"a"\n{\n"b" "C:\\\\x"\n}\n')["a"]["b"] == "C:\\x"  # multi-line, like real libraryfolders.vdf
        return "escaped backslashes parse"

    def keyring_backend():
        try:
            import keyring
        except ImportError:
            return "keyring not installed (key will use the 0600 file fallback)"
        get = getattr(keyring, "get_keyring", None)
        return f"backend {type(get()).__name__}" if get else "keyring module present"

    def web():
        import webview  # noqa: F401

        return "pywebview present"

    for label, fn in (("core modules", core), ("pywebview", web), ("TLS", tls), ("Nexus identity", nexus_identity),
                      ("VDF parser", vdf), ("keyring", keyring_backend)):
        step(label, fn)
    print(f"python {sys.version.split()[0]}, frozen={getattr(sys, 'frozen', False)}")
    return 1 if problems else 0
