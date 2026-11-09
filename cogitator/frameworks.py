"""Detect, plan and install the Darktide Mod Loader (DML) and Mod Framework (DMF) from a local zip or GitHub."""
from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .archives import parse_archive_name
from .config import cache_dir
from .gamefinder import validate_game_dir
from .installer import Transaction
from .loader import (GITHUB_API, REPOS, Asset, LoaderError, ReleaseNotFound, _get, download_asset,
                     fetch_latest_release, long_path, pick_asset, safe_extract)
from .procs import darktide_running
from .registry import Registry

log = logging.getLogger(__name__)

# Files that must exist for a component to count as installed (relative to game dir).
DML_REQUIRED = ("binaries/mod_loader", "bundle/9ba626afa44a3aa3.patch_999", "mods/base/mod_manager.lua")
DML_OPTIONAL = ("toggle_darktide_mods.bat", "tools/dtkit-patch.exe")  # tools/ needed for patching (Windows build)
DMF_REQUIRED = ("mods/dmf/dmf.mod", "mods/dmf/scripts/mods/dmf/dmf_loader.lua")

# Archive markers (relative to an optional single wrapper dir).
_DML_ZIP = ("toggle_darktide_mods.bat", "mods/base/mod_manager.lua")
_DMF_ZIP = ("dmf/dmf.mod",)
_SKIP = {"README.md"}  # top-level readme in the DML zip is not part of the install
_PRESERVE = {"mods/mod_load_order.txt"}  # never overwritten if present


@dataclass
class FrameworkState:
    kind: str
    status: str  # installed | incomplete | missing
    missing: list[str] = field(default_factory=list)


def detect_framework(game_dir: Path, kind: str) -> FrameworkState:
    req = DML_REQUIRED if kind == "dml" else DMF_REQUIRED
    missing = [r for r in req if not (Path(game_dir) / r).is_file()]
    if not missing:
        return FrameworkState(kind, "installed")
    return FrameworkState(kind, "missing" if len(missing) == len(req) else "incomplete", missing)


@dataclass
class Source:
    kind: str  # local | github-release | github-tag-source | github-snapshot
    description: str
    version: str | None  # tag, commit SHA, or None (unknown, never guessed)
    url: str | None = None
    unreleased: bool = False
    sha256: str | None = None
    archive_name: str | None = None  # original file name; matched to Nexus `file_name` to learn the file id
    verified: bool | None = None  # pinned sources: payload matched the known official release (None = not applicable)


@dataclass
class Plan:
    component: str  # dml | dmf
    zip_path: Path
    prefix: str  # wrapper dir inside the zip ('' or 'name/')
    source: Source


def describe_plan(p: Plan) -> str:
    s = p.source
    name = {"dml": "Darktide Mod Loader (DML)", "dmf": "Darktide Mod Framework (DMF)"}[p.component]
    lines = [f"Component : {name}", f"Source    : {s.description}"]
    if s.url:
        lines.append(f"URL       : {s.url}")
    ver = s.version or "unknown (will be recorded as unknown)"
    lines.append(f"Version   : {ver}" + ("  [UNRELEASED SNAPSHOT of the default branch, not a published release]" if s.unreleased else ""))
    if s.sha256:
        lines.append(f"SHA-256   : {s.sha256}")
    if s.verified is not None:
        lines.append("Check     : " + ("payload matches the official Nexus release (verified)" if s.verified
                                      else "payload does NOT match the official Nexus release"))
    lines.append("Installs  : " + ("loader files into the game folder (an existing mods/mod_load_order.txt is kept)"
                                   if p.component == "dml" else "mods/dmf (replaces any existing mods/dmf)"))
    return "\n".join(lines)


def _names(z: zipfile.ZipFile) -> list[str]:
    return [i.filename.replace("\\", "/") for i in z.infolist() if not i.is_dir()]


def _find_prefix(names: list[str], markers: tuple[str, ...]) -> str | None:
    """Prefix ('' or one wrapper dir) under which ALL markers exist."""
    s = set(names)
    cands = {""} | {n.split("/", 1)[0] + "/" for n in names if "/" in n}
    for pre in sorted(cands, key=len):
        if all(pre + m in s for m in markers):
            return pre
    return None


def classify_zip(zip_path: Path) -> tuple[str, str]:
    """('dml'|'dmf', wrapper_prefix). Raises LoaderError when neither."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = _names(z)
    except zipfile.BadZipFile as e:
        raise LoaderError(f"{Path(zip_path).name} is not a valid zip: {e}") from e
    pre = _find_prefix(names, _DML_ZIP)
    if pre is not None:
        return "dml", pre
    pre = _find_prefix(names, _DMF_ZIP)
    if pre is not None:
        return "dmf", pre
    pre = _find_prefix(names, ("mods/dmf/dmf.mod",))  # a zip of the whole mods/ folder
    if pre is not None:
        return "dmf", pre + "mods/"
    raise LoaderError(f"{Path(zip_path).name} does not look like DML or DMF (no known marker files).")


def internal_folders_from_zips(*zip_paths: Path) -> frozenset[str]:
    """Folders the framework zips place under mods/ -> these are not user mods."""
    out: set[str] = set()
    for zp in zip_paths:
        kind, pre = classify_zip(zp)
        if kind == "dmf":
            out.add("dmf")
            continue
        with zipfile.ZipFile(zp) as z:
            names = _names(z)
        base = pre + "mods/"
        out |= {n[len(base):].split("/", 1)[0] for n in names if n.startswith(base) and "/" in n[len(base):]}
    return frozenset(out)


def version_from_filename(name: str) -> str | None:
    """Version via the tolerant archive-name parser; ambiguous dash-form names yield None (never guessed)."""
    p = parse_archive_name(name)
    return None if p.ambiguous else p.version


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def payload_hash(zip_path: Path, component: str = "dmf") -> tuple[str, int]:
    """SHA-256 over sorted "relpath NUL sha256(file) LF" for every file of the installable payload (DMF: the dmf/ folder).

    Independent of zip wrapper folders, timestamps and compression, so a GitHub snapshot and a Nexus zip with the same
    files give the same value. Returns (hash, file count).
    """
    kind, pre = classify_zip(zip_path)
    if kind != component:
        raise LoaderError(f"{Path(zip_path).name} is not a {component.upper()} archive.")
    base = pre + "dmf/"
    files: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as z:
        for i in z.infolist():
            n = i.filename.replace("\\", "/")
            if not i.is_dir() and n.startswith(base):
                files[n[len(base):]] = hashlib.sha256(z.read(i)).hexdigest()
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{files[rel]}\n".encode())
    return h.hexdigest(), len(files)


def plan_local(zip_path: Path) -> Plan:
    zip_path = Path(zip_path)
    kind, pre = classify_zip(zip_path)
    return Plan(kind, zip_path, pre, Source(
        "local", f"local file {zip_path.name}", version_from_filename(zip_path.name), sha256=_sha256(zip_path),
        archive_name=zip_path.name))


def _zipball(client, repo: str, ref: str, dest: Path, label: str, sleep) -> Path:
    return download_asset(client, Asset(f"{label}.zip", f"{GITHUB_API}/repos/{repo}/zipball/{ref}"), dest, sleep=sleep)


def plan_github(component: str, client: httpx.Client, workdir: Path, sleep=time.sleep) -> Plan:
    """Opt-in network source: release zip asset -> (DML) tag source archive -> (DMF) default-branch snapshot.

    The caller MUST show describe_plan() and obtain explicit confirmation before install_plan().
    """
    repo = REPOS[component]
    dest = Path(workdir) / component
    try:
        rel = fetch_latest_release(repo, client, sleep=sleep)
    except ReleaseNotFound:
        rel = None
    src: Source | None = None
    zp: Path | None = None
    if rel:
        asset = pick_asset(rel, component)
        if asset:
            zp = download_asset(client, asset, dest, sleep=sleep)
            src = Source("github-release", f"GitHub release asset {asset.name}", rel.tag, url=asset.url)
        elif component == "dml":
            zp = _zipball(client, repo, rel.tag, dest, f"dml-{rel.tag}", sleep)
            src = Source("github-tag-source", f"GitHub source archive of tag {rel.tag} (release has no zip asset)",
                         rel.tag, url=f"{GITHUB_API}/repos/{repo}/zipball/{rel.tag}")
    if src is None or zp is None:
        if component != "dmf":
            raise LoaderError(f"No usable GitHub source for {component}.")
        r = _get(client, f"{GITHUB_API}/repos/{repo}/commits/HEAD", sleep=sleep)
        if r.status_code in (403, 429):
            raise LoaderError("GitHub API rate limit reached (60/hour unauthenticated).")
        try:
            sha, date = r.json()["sha"], r.json()["commit"]["committer"]["date"]
        except (ValueError, KeyError, TypeError) as e:
            raise LoaderError(f"Could not resolve DMF default-branch commit (HTTP {r.status_code}).") from e
        zp = _zipball(client, repo, sha, dest, f"dmf-{sha[:7]}", sleep)
        src = Source("github-snapshot", f"GitHub default-branch snapshot of {repo} @ commit {sha} ({date})",
                     sha, url=f"{GITHUB_API}/repos/{repo}/zipball/{sha}", unreleased=True)
    kind, pre = classify_zip(zp)
    if kind != component:
        raise LoaderError(f"Downloaded archive for {component} looks like {kind}; refusing to install.")
    src.sha256 = _sha256(zp)
    return Plan(component, zp, pre, src)


@dataclass
class InstallResult:
    component: str
    written: int
    kept: list[str]
    backup_dir: Path  # holds journal.json: Transaction.from_journal(backup_dir).rollback() undoes this install later
    placed: dict = field(default_factory=dict)  # game-relative path -> SHA-256 of what was written (the manifest)
    created_load_order: bool = False


def install_plan(plan: Plan, game_dir: Path, registry: Registry | None = None, *,
                 max_bytes: int = 500 * 1024 * 1024, max_files: int = 20000,
                 is_running: Callable[[], bool] = darktide_running) -> InstallResult:
    """Extract to a private temp dir, verify, then place files transactionally (rolled back on error)."""
    game_dir = Path(game_dir)
    ok, why = validate_game_dir(game_dir)
    if not ok:
        raise LoaderError(f"Not a Darktide folder: {why}")
    if is_running():
        raise LoaderError("Darktide is running. Close the game before installing or updating mods.")
    work = cache_dir() / "work"
    work.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"{plan.component}-", dir=work))
    tx = Transaction(cache_dir() / "rollback" / f"{time.strftime('%Y%m%d-%H%M%S')}-{plan.component}")
    kept: list[str] = []
    placed: dict[str, str] = {}
    created_lo = False
    written = 0
    try:
        safe_extract(plan.zip_path, tmp / "x", max_bytes, max_files)
        root = long_path(tmp / "x" / plan.prefix.rstrip("/"))
        try:
            if plan.component == "dml":
                for f in sorted(p for p in root.rglob("*") if p.is_file()):
                    rel = f.relative_to(root).as_posix()
                    if rel in _SKIP:
                        continue
                    dest = game_dir / rel
                    if rel in _PRESERVE and dest.exists():
                        kept.append(rel)
                        continue
                    if rel in _PRESERVE:
                        created_lo = True  # we are creating it: remembered so uninstall can offer to remove the default file
                    tx.put_file(f, dest)
                    written += 1
                    if rel not in _PRESERVE:  # mod_load_order.txt belongs to the user from the moment it exists
                        placed[rel] = _sha256(f)
            else:
                src = root / "dmf"
                if not (src / "dmf.mod").is_file():
                    raise LoaderError("Archive has no dmf/dmf.mod after extraction.")
                tx.replace_dir(src, game_dir / "mods" / "dmf")
                for p in sorted(x for x in src.rglob("*") if x.is_file()):
                    placed["mods/dmf/" + p.relative_to(src).as_posix()] = _sha256(p)
                written = len(placed)
            state = detect_framework(game_dir, plan.component)
            if state.status != "installed":
                raise LoaderError(f"Post-install check failed; missing: {', '.join(state.missing)}")
        except BaseException:
            tx.rollback()
            raise
    finally:
        shutil.rmtree(long_path(tmp), ignore_errors=True)
    s = plan.source
    if registry is not None:
        registry.set_framework(plan.component, version=s.version, source_kind=s.kind,
                               source=s.url or s.description, unreleased=s.unreleased, sha256=s.sha256,
                               archive_name=s.archive_name)
        registry.set_manifest(plan.component, placed, created_load_order=created_lo, version=s.version)
    log.info("installed %s from %s (%d files)", plan.component, s.description, written)
    return InstallResult(plan.component, written, kept, tx.backup_root, placed, created_lo)

