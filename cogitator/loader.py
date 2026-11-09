"""GitHub release lookup, downloads, and safe zip extraction."""
from __future__ import annotations

import logging
import posixpath
import re
import stat
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from . import __version__

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
REPOS = {
    "dml": "Darktide-Mod-Framework/Darktide-Mod-Loader",
    "dmf": "Darktide-Mod-Framework/Darktide-Mod-Framework",
}
NEXUS_PAGES = {
    "dml": "https://www.nexusmods.com/warhammer40kdarktide/mods/19",
    "dmf": "https://www.nexusmods.com/warhammer40kdarktide/mods/8",
}
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024


class LoaderError(Exception):
    pass


class UnsafeArchive(LoaderError):
    pass


class ReleaseNotFound(LoaderError):
    pass


@dataclass
class Asset:
    name: str
    url: str
    size: int = 0
    content_type: str = ""


@dataclass
class Release:
    repo: str
    tag: str
    name: str = ""
    html_url: str = ""
    published_at: str = ""
    assets: list[Asset] = field(default_factory=list)


def make_client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        verify=True,
        headers={"User-Agent": f"Cogitator/{__version__}", "Accept": "application/vnd.github+json"},
    )


def _get(client: httpx.Client, url: str, retries: int = 3, sleep: Callable[[float], None] = time.sleep, **kw) -> httpx.Response:
    """GET with backoff on network errors / 5xx. 4xx is returned to the caller untouched."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = client.get(url, **kw)
            if r.status_code < 500:
                return r
            last = LoaderError(f"HTTP {r.status_code} from {url}")
        except httpx.HTTPError as e:
            last = e
        if attempt < retries - 1:
            sleep(2 ** attempt)
    raise LoaderError(f"Request failed after {retries} attempts: {url}: {last}")


def fetch_latest_release(repo: str, client: httpx.Client, sleep=time.sleep) -> Release:
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    r = _get(client, url, sleep=sleep)
    if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
        reset = r.headers.get("x-ratelimit-reset", "")
        raise LoaderError(f"GitHub API rate limit reached (60/hour unauthenticated); resets at epoch {reset}.")
    if r.status_code == 404:
        raise ReleaseNotFound(f"No published release found for {repo}.")
    if r.status_code != 200:
        raise LoaderError(f"GitHub returned HTTP {r.status_code} for {repo}.")
    try:
        d = r.json()
        assets = [
            Asset(a["name"], a["browser_download_url"], int(a.get("size") or 0), a.get("content_type") or "")
            for a in d.get("assets", [])
            if isinstance(a, dict) and a.get("name") and a.get("browser_download_url")
        ]
        return Release(repo, str(d["tag_name"]), d.get("name") or "", d.get("html_url") or "",
                       d.get("published_at") or "", assets)
    except (ValueError, KeyError, TypeError) as e:
        raise LoaderError(f"Unexpected GitHub response for {repo}: {e}") from e


def pick_asset(release: Release, kind: str, update_only: bool = False) -> Asset | None:
    """Pick the zip asset for a component. Returns None when no unambiguous zip exists."""
    zips = [a for a in release.assets if a.name.lower().endswith(".zip")]
    if kind == "dml":
        upd = [a for a in zips if "update" in a.name.lower()]
        full = [a for a in zips if "update" not in a.name.lower()]
        pool = upd if update_only else full
    else:
        pool = zips
    if len(pool) == 1:
        return pool[0]
    if len(pool) > 1:
        hint = "dmf" if kind == "dmf" else "dml"
        hinted = [a for a in pool if hint in a.name.lower() or ("loader" if kind == "dml" else "framework") in a.name.lower()]
        if len(hinted) == 1:
            return hinted[0]
    return None


def download_asset(client: httpx.Client, asset: Asset, dest_dir: Path, max_bytes: int = MAX_DOWNLOAD_BYTES,
                   sleep=time.sleep) -> Path:
    """Stream to dest_dir/<name>; verifies size and that the result is a zip."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.\-]", "_", asset.name) or "download.zip"
    dest = dest_dir / name
    last: Exception | None = None
    for attempt in range(3):
        try:
            with client.stream("GET", asset.url, headers={"Accept": "*/*"}) as r:
                if r.status_code != 200:
                    if r.status_code < 500:
                        raise LoaderError(f"Download failed: HTTP {r.status_code} for {asset.name}")
                    raise httpx.HTTPError(f"HTTP {r.status_code}")
                total = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise LoaderError(f"Download exceeds {max_bytes} bytes; aborted.")
                        f.write(chunk)
            break
        except httpx.HTTPError as e:
            last = e
            if attempt == 2:
                raise LoaderError(f"Download failed: {e}") from e
            sleep(2 ** attempt)
    if asset.size and dest.stat().st_size != asset.size:
        raise LoaderError(f"Size mismatch for {asset.name}: expected {asset.size}, got {dest.stat().st_size}.")
    if not zipfile.is_zipfile(dest):
        raise LoaderError(f"{asset.name} is not a zip file.")
    return dest


# ------------------------------------------------------------ safe extraction

def _check_member(name: str) -> str:
    n = name.replace("\\", "/")
    if not n or "\x00" in n:
        raise UnsafeArchive(f"Bad entry name: {name!r}")
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n):
        raise UnsafeArchive(f"Absolute path in archive: {name!r}")
    if any(p == ".." for p in n.split("/")):
        raise UnsafeArchive(f"Path traversal in archive: {name!r}")
    return posixpath.normpath(n) if n.rstrip("/") else n


def long_path(p: Path) -> Path:
    """Windows: extended-length prefix so paths >260 chars (deep DMF trees) work."""
    import os

    if os.name != "nt":
        return p
    s = str(Path(p).resolve())
    if s.startswith("\\\\?\\"):
        return Path(s)
    return Path("\\\\?\\UNC\\" + s[2:]) if s.startswith("\\\\") else Path("\\\\?\\" + s)


def safe_extract(zip_path: Path, dest: Path, max_bytes: int = 500 * 1024 * 1024, max_files: int = 20000) -> list[str]:
    """Extract with zip-slip / symlink / size / count protection. Returns extracted file paths (posix, relative)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    out: list[str] = []
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        if len(infos) > max_files:
            raise UnsafeArchive(f"Archive has {len(infos)} entries (limit {max_files}).")
        if sum(i.file_size for i in infos) > max_bytes:
            raise UnsafeArchive(f"Archive would extract to more than {max_bytes} bytes.")
        plan = []
        for i in infos:  # validate everything before writing anything
            mode = i.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise UnsafeArchive(f"Symlink in archive: {i.filename!r}")
            rel = _check_member(i.filename)
            target = (root / rel).resolve()
            if target != root and root not in target.parents:
                raise UnsafeArchive(f"Entry escapes destination: {i.filename!r}")
            plan.append((i, rel, target))
        written = 0
        for i, rel, target in plan:
            if i.is_dir():
                long_path(target).mkdir(parents=True, exist_ok=True)
                continue
            long_path(target).parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as src, open(long_path(target), "wb") as dst:
                while chunk := src.read(65536):
                    written += len(chunk)
                    if written > max_bytes:  # headers can lie; enforce on actual bytes
                        raise UnsafeArchive("Extracted size exceeded limit.")
                    dst.write(chunk)
            out.append(rel)
    return out


def layout(root: Path, max_depth: int = 3) -> str:
    """Printable tree of an extracted directory (dirs shown to max_depth, with file counts)."""
    root = long_path(Path(root))
    lines = [f"{root.name}/"]

    def walk(d: Path, depth: int, prefix: str):
        entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        for e in entries:
            if e.is_dir():
                n = sum(1 for _ in e.rglob("*") if _.is_file())
                lines.append(f"{prefix}{e.name}/  ({n} files)")
                if depth < max_depth:
                    walk(e, depth + 1, prefix + "  ")
            else:
                lines.append(f"{prefix}{e.name}  ({e.stat().st_size} B)")

    walk(root, 1, "  ")
    return "\n".join(lines)
