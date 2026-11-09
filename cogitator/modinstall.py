"""Importing / updating ordinary mods from archives (never executes anything from them).

* import_archive: local zip -> mods/<folder>, load-order aware, linked to Nexus via the archive's file name.
* apply_update_premium: Nexus download link (Premium ONLY) -> same pipeline. Free users: browser + import.
* find_matching_download / wait_for_download: watch the Downloads folder for the archive a free user fetched.
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .archives import ParsedArchive, match_archive_to_files, parse_archive_name, _clean
from .config import cache_dir
from .frameworks import _DML_ZIP, _DMF_ZIP, _find_prefix, _names
from .gamefinder import validate_game_dir
from .installer import Transaction
from .linking import _latest_record
from .loader import Asset, LoaderError, download_asset, long_path, make_client, safe_extract
from .loadorder import LoadOrder
from .mods import INTERNAL_FOLDERS, load_order_path, mods_dir
from .nexus import GAME, NexusClient, NexusFile, latest_main_file, parse_nexus_ref
from .procs import darktide_running
from .registry import Registry

log = logging.getLogger(__name__)


class ModImportError(LoaderError):
    pass


class AmbiguousArchive(ModImportError):
    """Cannot proceed without asking the user (and no way to ask)."""


class NotPremium(ModImportError):
    pass


# `choose(question, options) -> index | None` -- interactive disambiguation supplied by CLI/TUI.
Choose = Callable[[str, list[str]], "int | None"]


def mod_page_url(mod_id: int) -> str:
    return f"https://www.nexusmods.com/{GAME}/mods/{mod_id}?tab=files"


@dataclass
class ArchiveInfo:
    prefix: str  # wrapper path inside the zip holding the mod folders ('' | 'x/' | 'mods/' ...)
    folders: list[str]


def inspect_mod_zip(zip_path: Path) -> ArchiveInfo:
    """Find mod folders: directories X containing X.mod. Refuses framework archives and internal folders."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = _names(z)
    except zipfile.BadZipFile as e:
        raise ModImportError(f"{Path(zip_path).name} is not a valid zip: {e}") from e
    if _find_prefix(names, _DML_ZIP) is not None or _find_prefix(names, _DMF_ZIP) is not None:
        raise ModImportError("This is a Mod Loader / Mod Framework archive; use `install-framework`.")
    found: dict[str, set[str]] = {}
    for n in names:
        m = re.match(r"^(?P<pre>(?:[^/]+/)?(?:mods/)?)(?P<f>[^/]+)/(?P<g>[^/]+)\.mod$", n)
        if m and m["g"] == m["f"]:
            found.setdefault(m["pre"], set()).add(m["f"])
    if not found:
        raise ModImportError("No mod folder found (expected <folder>/<folder>.mod). Not a Darktide mod archive?")
    pre = min(found, key=lambda p: (len(p), p))  # shallowest layout wins
    folders = sorted(found[pre])
    bad = [f for f in folders if f in INTERNAL_FOLDERS]
    if bad:
        raise ModImportError(f"Archive would replace loader/framework folder(s): {', '.join(bad)}.")
    return ArchiveInfo(pre, folders)


@dataclass
class ImportResult:
    folders: list[str]
    linked: str | None = None
    file_id: int | None = None
    version: str | None = None
    backup_dir: Path | None = None
    added_to_load_order: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def install_mod_archive(zip_path: Path, game_dir: Path, info: ArchiveInfo, *, max_bytes: int, max_files: int,
                        is_running: Callable[[], bool] = darktide_running) -> tuple[Path, list[str]]:
    """Replace mods/<folder> for each folder (old copy kept in a rollback dir); append unlisted folders to the load
    order as enabled. Existing load-order state and position are untouched. Rolls back everything on error."""
    game_dir = Path(game_dir)
    ok, why = validate_game_dir(game_dir)
    if not ok:
        raise ModImportError(f"Not a Darktide folder: {why}")
    if not (mods_dir(game_dir)).is_dir():
        raise ModImportError("mods/ not found - install the Mod Loader first.")
    if is_running():
        raise ModImportError("Darktide is running. Close the game before installing or updating mods.")
    work = cache_dir() / "work"
    work.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="mod-", dir=work))
    tx = Transaction(cache_dir() / "rollback" / f"{time.strftime('%Y%m%d-%H%M%S')}-mod")
    added: list[str] = []
    try:
        safe_extract(zip_path, tmp / "x", max_bytes, max_files)
        root = long_path(tmp / "x" / info.prefix.rstrip("/")) if info.prefix else long_path(tmp / "x")
        try:
            for f in info.folders:
                if not (root / f / f"{f}.mod").is_file():
                    raise ModImportError(f"{f}/{f}.mod missing after extraction.")
                tx.replace_dir(root / f, mods_dir(game_dir) / f)
            lo_path = load_order_path(game_dir)
            lo = LoadOrder.load(lo_path)
            for f in info.folders:
                if lo.state(f) is None:
                    lo.add(f, True)
                    added.append(f)
            if added:
                lo.save(lo_path)
        except BaseException:
            tx.rollback()
            raise
    finally:
        shutil.rmtree(long_path(tmp), ignore_errors=True)
    return tx.backup_root, added


def _describe(f: NexusFile) -> str:
    when = time.strftime("%Y-%m-%d", time.gmtime(f.uploaded)) if f.uploaded else "?"
    return f"{f.file_name or f.name} (file {f.file_id}, v{f.version}, {f.category or '?'}, uploaded {when})"


def import_archive(zip_path: Path, game_dir: Path, registry: Registry, nexus: NexusClient | None, *,
                   choose: Choose | None = None, mod_id: int | None = None, max_bytes: int = 500 * 1024 * 1024,
                   max_files: int = 20000, is_running: Callable[[], bool] = darktide_running) -> ImportResult:
    """Install a mod zip and link it. Anything ambiguous is put to the user via `choose`; with no `choose`
    (non-interactive) ambiguity raises AmbiguousArchive instead of guessing."""
    zip_path = Path(zip_path)
    info = inspect_mod_zip(zip_path)
    parsed = parse_archive_name(zip_path.name)

    def ask(question: str, options: list[str]) -> int:
        if choose is None:
            raise AmbiguousArchive(f"{question} (options: {'; '.join(options)}). Re-run interactively or pass --mod-id.")
        i = choose(question, options)
        if i is None:
            raise ModImportError("Cancelled.")
        return i

    # 1. which folder gets linked
    link_folder: str | None = info.folders[0] if len(info.folders) == 1 else None
    if link_folder is None:
        opts = info.folders + ["(don't link any)"]
        i = ask(f"{zip_path.name} contains {len(info.folders)} mod folders. Which one is the Nexus mod?", opts)
        link_folder = info.folders[i] if i < len(info.folders) else None

    # 2. which Nexus mod
    nid = mod_id or parsed.mod_id
    if mod_id and parsed.mod_id and mod_id != parsed.mod_id:
        nid = [mod_id, parsed.mod_id][ask(f"The archive name says Nexus mod {parsed.mod_id} but you gave {mod_id}. Which is right?",
                                           [f"{mod_id} (as given)", f"{parsed.mod_id} (from the file name)"])]
    if link_folder and nid is None:
        reg = (registry.mod(link_folder) or {}).get("nexus_id")
        if reg:
            nid = int(reg)
        else:  # nothing to go on: the caller should have asked for a mod id/URL (CLI does, via --mod-id or a prompt)
            ask(f"Cannot tell which Nexus mod '{zip_path.name}' belongs to from its name.", ["install without linking"])
            link_folder = None
    # 3. which file
    file: NexusFile | None = None
    files: list[NexusFile] | None = None
    name: str | None = None
    notes: list[str] = []
    if link_folder and nid is not None and nexus is not None:
        files = nexus.files(nid)
        try:
            name = nexus.mod(nid).get("name")
        except Exception:  # name is cosmetic
            name = None
        m = match_archive_to_files(zip_path.name, files, parsed)
        if m.status == "exact":
            file = m.files[0]
        elif m.status == "inferred":
            if ask(f"No exact file name match; best guess is {_describe(m.files[0])}. Use it?", ["yes", "no, leave baseline unknown"]) == 0:
                file = m.files[0]
        elif m.status == "ambiguous":
            opts = [_describe(f) for f in m.files] + ["none of these (baseline unknown)"]
            i = ask(f"{zip_path.name} matches several Nexus files:", opts)
            file = m.files[i] if i < len(m.files) else None
        else:
            notes.append("Archive name matches no file on Nexus; baseline left unknown.")

    backup, added = install_mod_archive(zip_path, game_dir, info, max_bytes=max_bytes, max_files=max_files,
                                        is_running=is_running)
    res = ImportResult(info.folders, backup_dir=backup, added_to_load_order=added, notes=notes)
    if link_folder and nid is not None:
        rec = dict(nexus_id=nid, name=name, linked_via="import", linked_at=time.time(), last_error=None,
                   installed_file_id=file.file_id if file else None,
                   installed_version=file.version if file else parsed.version,
                   installed_archive=zip_path.name, baseline="import" if file else None)
        if files is not None:
            rec.update(latest=_latest_record(files), checked_at=time.time())
        else:
            rec.update(latest=None, checked_at=None)
        registry.set_mod(link_folder, **rec)
        res.linked, res.file_id, res.version = link_folder, rec["installed_file_id"], rec["installed_version"]
        if file is None:
            res.notes.append("Installed file not identified: status stays 'unknown' until it is resolved.")
    return res


# ---------------------------------------------------------------- updating

def fetch_premium_download(nexus: NexusClient, mod_id: int, file_id: int, file_name: str | None, size_bytes: int | None,
                           dest_dir: Path, *, premium: bool | None, client=None) -> Path:
    """Download one Nexus file through the API into dest_dir. Premium accounts ONLY -- enforced here, before any request."""
    if not premium:
        raise NotPremium("Automatic download needs a Nexus Premium account. Use the files page and import instead.")
    links = nexus.download_links(mod_id, file_id)
    if not links:
        raise ModImportError("Nexus returned no download link.")
    own = client is None
    client = client or make_client(timeout=60.0)
    try:
        asset = Asset(file_name or f"{mod_id}-{file_id}.zip", links[0]["URI"], int(size_bytes or 0))
        return download_asset(client, asset, Path(dest_dir))  # verifies size (when known) and that it is a zip
    finally:
        if own:
            client.close()


def apply_update_premium(folder: str, game_dir: Path, registry: Registry, nexus: NexusClient, *, premium: bool | None,
                         max_bytes: int = 500 * 1024 * 1024, max_files: int = 20000,
                         is_running: Callable[[], bool] = darktide_running, client=None) -> ImportResult:
    """Download the latest MAIN file through the API and install it. Premium accounts only -- enforced here."""
    if not premium:
        raise NotPremium("Automatic download needs a Nexus Premium account. Use the files page and `import` instead.")
    rec = registry.mod(folder)
    latest = (rec or {}).get("latest")
    if not rec or not rec.get("nexus_id") or not latest or latest.get("file_id") is None:
        raise ModImportError(f"{folder}: not linked or not checked yet; run check-updates first.")
    (cache_dir() / "work").mkdir(parents=True, exist_ok=True)
    wd = Path(tempfile.mkdtemp(prefix="dl-", dir=cache_dir() / "work"))
    try:
        zp = fetch_premium_download(nexus, rec["nexus_id"], latest["file_id"], latest.get("file_name"),
                                    latest.get("size_bytes"), wd, premium=premium, client=client)
        info = inspect_mod_zip(zp)
        backup, added = install_mod_archive(zp, game_dir, info, max_bytes=max_bytes, max_files=max_files,
                                            is_running=is_running)
    finally:
        shutil.rmtree(wd, ignore_errors=True)
    registry.set_mod(folder, installed_file_id=latest["file_id"], installed_version=latest.get("version"),
                     installed_archive=latest.get("file_name"), baseline="import", last_error=None)
    return ImportResult(info.folders, linked=folder, file_id=latest["file_id"], version=latest.get("version"),
                        backup_dir=backup, added_to_load_order=added)


def open_files_page(mod_id: int, opener: Callable[[str], bool] = webbrowser.open) -> str:
    url = mod_page_url(mod_id)
    opener(url)
    return url


def find_matching_download(downloads: Path, mod_id: int, latest: dict, since: float) -> Path | None:
    """Newest finished .zip in `downloads`, modified at/after `since`, that is the file we want:
    exact `file_name` match, else same mod id + same version per the archive name."""
    want = _clean(latest.get("file_name") or "")
    best: tuple[float, Path] | None = None
    try:
        entries = list(Path(downloads).glob("*.zip"))
    except OSError:
        return None
    for p in entries:
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if mt < since - 5:
            continue
        pa = parse_archive_name(p.name)
        if (want and _clean(p.name) == want) or (pa.mod_id == mod_id and pa.version and pa.version == latest.get("version")):
            if best is None or mt > best[0]:
                best = (mt, p)
    return best[1] if best else None


def wait_for_download(downloads: Path, mod_id: int, latest: dict, *, since: float, timeout_s: float = 180.0,
                      poll_s: float = 2.0, sleep: Callable[[float], None] = time.sleep,
                      clock: Callable[[], float] = time.time,
                      cancelled: Callable[[], bool] = lambda: False) -> Path | None:
    """Poll until a matching archive appears and its size is stable across two polls (download finished)."""
    deadline = clock() + timeout_s
    last: tuple[Path, int] | None = None
    while not cancelled():
        p = find_matching_download(downloads, mod_id, latest, since)
        if p is not None:
            try:
                size = p.stat().st_size
            except OSError:
                size = -1
            if last and last[0] == p and last[1] == size and size > 0 and zipfile.is_zipfile(p):
                return p
            last = (p, size)
        if clock() >= deadline:
            return None
        sleep(poll_s)
    return None
