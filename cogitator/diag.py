"""`cogitator diag`: one zip with what a bug report needs and nothing private. Written to the cache folder, never uploaded;
the user attaches it themselves. The home folder and user name are replaced, secrets go through `redact`, and only mod
folder NAMES are included (no mod files, no load-order comments, no API key)."""
from __future__ import annotations

import json
import os
import platform
import sys
import time
import zipfile
from pathlib import Path

from . import __version__
from .config import Settings, cache_dir, log_path, redact
from .doctor import run_doctor, to_json
from .loadorder import LoadOrder
from .mods import load_order_path, scan_mods

LOG_TAIL_BYTES = 200 * 1024


def scrub(text: str) -> str:
    """Remove the account's home folder and user name from `text`, then any secrets."""
    out = text
    home = str(Path.home())
    for h in {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}:
        if h:
            out = out.replace(h, "~")
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if len(user) >= 3:
        out = out.replace(user, "<user>")
    return redact(out)


def _versions() -> str:
    import importlib.metadata as md

    lines = [f"cogitator {__version__}", f"python {sys.version.split()[0]} ({platform.platform()})",
             f"frozen {bool(getattr(sys, 'frozen', False))}"]
    for pkg in ("webview", "httpx", "keyring", "cryptography"):
        try:
            lines.append(f"{pkg} {md.version(pkg)}")
        except md.PackageNotFoundError:
            lines.append(f"{pkg} not installed")
    return "\n".join(lines) + "\n"


def _log_tail() -> str:
    try:
        with open(log_path(), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return "(no log file)\n"


def collect(game_dir: Path | None, registry=None) -> dict[str, str]:
    """name -> text for every file in the bundle (already scrubbed)."""
    files = {"versions.txt": _versions()}
    checks = run_doctor(str(game_dir) if game_dir else None, offline=True)
    files["doctor.json"] = json.dumps(to_json(checks), indent=2)
    files["log_tail.txt"] = _log_tail()
    files["settings.txt"] = "\n".join(f"{k} = {v}" for k, v in vars(Settings.load()).items() if k not in ("game_dir", "downloads_dir")) + "\n"
    if game_dir:
        lo = LoadOrder.load(load_order_path(game_dir))
        files["load_order.txt"] = "\n".join(f"{'on ' if e else 'off'} {n}" for n, e in lo.entries()) + "\n"
        rows = []
        for m in scan_mods(game_dir):
            rec = (registry.mod(m.folder) if registry is not None else None) or {}
            rows.append(f"{m.state:<9} {m.folder}  nexus={rec.get('nexus_id') or '-'}  version={rec.get('installed_version') or '-'}"
                        + ("" if m.present else "  (folder missing)"))
        files["mods.txt"] = "\n".join(rows) + "\n"
    return {k: scrub(v) for k, v in files.items()}


def build(game_dir: Path | None, registry=None, out_dir: Path | None = None) -> tuple[Path, dict[str, int]]:
    files = collect(game_dir, registry)
    out_dir = out_dir or cache_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"cogitator-diag-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    return path, {k: len(v.encode("utf-8")) for k, v in files.items()}
