"""Where DML and DMF come from, resolved before anything is downloaded.

resolve_* returns metadata only (a SourceOption); fetch_source downloads exactly the pinned ref that was shown;
choose_sources orders the options. A default is only a pre-selection: the install refuses to run until the chosen
option is confirmed. Commit SHAs are immutable pins; a force-moved tag would not be noticed."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .frameworks import Plan, Source, _get, _sha256, _zipball, classify_zip, payload_hash, plan_local
from .loader import (GITHUB_API, REPOS, Asset, LoaderError, ReleaseNotFound, download_asset, fetch_latest_release,
                     pick_asset)
from .modinstall import fetch_premium_download
from .nexus import NexusClient, NexusFile, latest_main_file
from .updates import FRAMEWORK_MODS

log = logging.getLogger(__name__)

GITHUB_RELEASE, GITHUB_TAG, GITHUB_SNAPSHOT, GITHUB_PINNED, NEXUS_PREMIUM, LOCAL = (
    "github-release", "github-tag-source", "github-snapshot", "github-pinned", "nexus-premium", "local")


@dataclass(frozen=True)
class Pin:
    """A GitHub commit whose payload is known to equal an official Nexus release. Updated by Cogitator releases."""

    repo: str
    sha: str
    nexus_version: str
    payload_sha256: str
    files: int


# DMF has no GitHub release. This commit's dmf/ folder is byte-identical (58 files) to the Nexus 26.10.22 zip, as
# measured by `frameworks.payload_hash`.
PINNED_DMF = Pin("Darktide-Mod-Framework/Darktide-Mod-Framework", "0107475ee51f07af2957b72fdf09b83e0b114791", "26.10.22",
                 "8375830536e08dd9fab79bd975fae0d8010670976a93372ab60b6cf7f70ad0c5", 58)

SNAPSHOT_EXPLANATION = ("UNRELEASED SNAPSHOT: this is the newest code on the project's main branch, not a published release. "
                        "It matched the official Nexus release when last checked, but it can change at any time.")


@dataclass
class SourceOption:
    component: str  # dml | dmf
    kind: str
    label: str  # one line for a list
    version: str | None  # tag, commit SHA, Nexus version; None = unknown
    url: str | None = None
    unreleased: bool = False
    size: int | None = None
    sha256: str | None = None  # known only after a fetch (or for local files)
    recommended: bool = False
    explanation: str = ""
    # fetch details
    ref: str | None = None
    asset: Asset | None = None
    local_path: Path | None = None
    nexus_file: NexusFile | None = None
    pin: Pin | None = None

    @property
    def id(self) -> str:
        return f"{self.component}:{self.kind}:{self.version or '?'}"

    def review_lines(self) -> list[str]:
        out = [f"{self.label}"]
        if self.url:
            out.append(f"  from   {self.url}")
        if self.pin:
            out.append(f"  pinned to commit {self.pin.sha}")
            out.append(f"  will be checked against the official Nexus release {self.pin.nexus_version} while installing "
                       f"({self.pin.files} files, payload SHA-256 {self.pin.payload_sha256})")
            out.append("  if it does not match, you are asked before anything is installed")
            return out
        out.append(f"  version {self.version or 'unknown'}" + (f", {self.size / 1024:.0f} KB" if self.size else ""))
        if self.sha256:
            out.append(f"  SHA-256 {self.sha256}")
        if self.unreleased:
            out.append("  " + SNAPSHOT_EXPLANATION)
        return out


# ---------------------------------------------------------------- resolve (no download)

def resolve_github(component: str, client: httpx.Client, sleep: Callable[[float], None] = time.sleep) -> SourceOption:
    if component == "dmf":
        return resolve_pinned_dmf()  # DMF has no GitHub release; the pinned commit is verified at install time
    repo = REPOS[component]
    try:
        rel = fetch_latest_release(repo, client, sleep=sleep)
    except ReleaseNotFound:
        rel = None
    if rel:
        asset = pick_asset(rel, component)
        if asset:
            return SourceOption(component, GITHUB_RELEASE, f"GitHub release {rel.tag}", rel.tag, asset.url, size=asset.size or None,
                                asset=asset, ref=rel.tag)
        if component == "dml":
            return SourceOption(component, GITHUB_TAG, f"GitHub source archive of tag {rel.tag}", rel.tag,
                                f"{GITHUB_API}/repos/{repo}/zipball/{rel.tag}", ref=rel.tag,
                                explanation="The release has no zip asset; the tagged source has the same layout as the Nexus zip.")
    raise LoaderError(f"No usable GitHub source for {component}.")


def resolve_pinned_dmf(pin: Pin | None = None) -> SourceOption:
    pin = pin or PINNED_DMF
    return SourceOption("dmf", GITHUB_PINNED, f"DMF {pin.nexus_version} from GitHub (pinned commit {pin.sha[:7]})", pin.sha,
                        f"{GITHUB_API}/repos/{pin.repo}/zipball/{pin.sha}", ref=pin.sha, pin=pin, recommended=True,
                        explanation=(f"This exact commit matched the official Nexus release {pin.nexus_version}. Its contents are checked "
                                     "against that release while installing; if they differ you are asked before anything is installed."))


def resolve_head_snapshot(component: str, client: httpx.Client, sleep: Callable[[float], None] = time.sleep) -> SourceOption:
    """The newest commit on the default branch. Always UNRELEASED; no longer offered by the wizard (kept for the CLI)."""
    repo = REPOS[component]
    r = _get(client, f"{GITHUB_API}/repos/{repo}/commits/HEAD", sleep=sleep)
    if r.status_code in (403, 429):
        raise LoaderError("GitHub API rate limit reached (60/hour without signing in). Try again in a while, or use a Nexus zip.")
    try:
        sha, date = r.json()["sha"], r.json()["commit"]["committer"]["date"]
    except (ValueError, KeyError, TypeError) as e:
        raise LoaderError(f"Could not read the newest DMF commit from GitHub (HTTP {r.status_code}).") from e
    return SourceOption(component, GITHUB_SNAPSHOT, f"GitHub main-branch snapshot @ {sha[:10]} ({date[:10]})", sha,
                        f"{GITHUB_API}/repos/{repo}/zipball/{sha}", unreleased=True, ref=sha, explanation=SNAPSHOT_EXPLANATION)


def resolve_nexus(component: str, nexus: NexusClient) -> SourceOption:
    """The official Nexus release (latest MAIN, preferring the primary file). Needs a Premium key to *download*."""
    files = nexus.files(FRAMEWORK_MODS[component])
    f = latest_main_file(files)
    if f is None or f.file_id is None:
        raise LoaderError(f"Nexus lists no MAIN file for {component.upper()}.")
    return SourceOption(component, NEXUS_PREMIUM, f"Official Nexus release {f.version or f.file_name}", f.version,
                        f"https://www.nexusmods.com/warhammer40kdarktide/mods/{FRAMEWORK_MODS[component]}?tab=files",
                        size=f.size_bytes, nexus_file=f, explanation="Downloaded through your Premium account.")


def local_option(component_hint: str | None, zip_path: Path) -> SourceOption:
    plan = plan_local(zip_path)
    if component_hint and plan.component != component_hint:
        raise LoaderError(f"{Path(zip_path).name} is a {plan.component.upper()} archive, not {component_hint.upper()}.")
    return SourceOption(plan.component, LOCAL, f"File you downloaded: {Path(zip_path).name}", plan.source.version,
                        size=Path(zip_path).stat().st_size, sha256=plan.source.sha256, local_path=Path(zip_path))


# ---------------------------------------------------------------- choose

def choose_sources(component: str, *, premium: bool | None, github: SourceOption | None, nexus: SourceOption | None,
                   local: SourceOption | None = None) -> list[SourceOption]:
    """Ordered options; options[0] is pre-selected and is never applied without confirmation.

        Premium with an official release: the Nexus release first. Otherwise a published GitHub source. An unreleased
        snapshot is never recommended, and "I'll download it myself" is always offered."""
    opts: list[SourceOption] = []
    if premium and nexus is not None:
        nexus.recommended = True
        opts.append(nexus)
    if local is not None:
        opts.append(local)
    if github is not None:
        github.recommended = not github.unreleased and not opts
        opts.append(github)
    if not premium:
        opts.append(SourceOption(component, "manual", "I'll download the Nexus zip myself",
                                 None, f"https://www.nexusmods.com/warhammer40kdarktide/mods/{FRAMEWORK_MODS[component]}?tab=files",
                                 explanation="Cogitator opens the Nexus files page; pick the downloaded zip afterwards."))
    return opts


# ---------------------------------------------------------------- fetch (downloads the pinned ref)

def fetch_source(opt: SourceOption, workdir: Path, *, client: httpx.Client | None = None, nexus: NexusClient | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> Plan:
    """Download exactly what `opt` describes and return an install Plan. Raises LoaderError if it is not what was shown."""
    dest = Path(workdir) / opt.component
    if opt.kind == LOCAL:
        plan = plan_local(opt.local_path)
        return _check(opt, plan)
    if opt.kind == GITHUB_PINNED:
        if client is None:
            raise LoaderError("No network client available.")
        zp = _zipball(client, opt.pin.repo, opt.pin.sha, dest, f"dmf-{opt.pin.sha[:10]}", sleep)
        got, count = payload_hash(zp, "dmf")
        ok = got == opt.pin.payload_sha256
        _, pre = classify_zip(zp)
        label = (f"matches Nexus release {opt.pin.nexus_version} (verified)" if ok else
                 f"UNRELEASED SNAPSHOT: commit {opt.pin.sha[:7]} does NOT match Nexus release {opt.pin.nexus_version}")
        src = Source(GITHUB_PINNED, label, opt.pin.nexus_version if ok else opt.pin.sha, url=opt.url, unreleased=not ok,
                     sha256=_sha256(zp), verified=ok)  # verified -> recorded as the Nexus version so update checks can compare
        opt.sha256 = src.sha256
        opt.unreleased = not ok
        opt.label = label
        return _check(opt, Plan("dmf", zp, pre, src), "dmf")
    if opt.kind in (GITHUB_RELEASE, GITHUB_TAG, GITHUB_SNAPSHOT):
        if client is None:
            raise LoaderError("No network client available.")
        repo = REPOS[opt.component]
        if opt.kind == GITHUB_RELEASE:
            zp = download_asset(client, opt.asset, dest, sleep=sleep)
        else:
            zp = _zipball(client, repo, opt.ref, dest, f"{opt.component}-{str(opt.ref)[:10]}", sleep)
        kind, pre = classify_zip(zp)
        src = Source(opt.kind, opt.label, opt.version, url=opt.url, unreleased=opt.unreleased, sha256=_sha256(zp))
        return _check(opt, Plan(opt.component, zp, pre, src), kind)
    if opt.kind == NEXUS_PREMIUM:
        if nexus is None:
            raise LoaderError("A Nexus API key is needed for the official download.")
        f = opt.nexus_file
        zp = fetch_premium_download(nexus, FRAMEWORK_MODS[opt.component], f.file_id, f.file_name, f.size_bytes, dest, premium=True)
        plan = plan_local(zp)
        plan.source.kind, plan.source.description = NEXUS_PREMIUM, f"official Nexus release {f.file_name}"
        plan.source.version = f.version or plan.source.version
        return _check(opt, plan)
    raise LoaderError(f"Source '{opt.kind}' cannot be downloaded automatically.")


def _check(opt: SourceOption, plan: Plan, kind: str | None = None) -> Plan:
    if (kind or plan.component) != opt.component:
        raise LoaderError(f"The downloaded archive for {opt.component.upper()} looks like {(kind or plan.component).upper()}; refusing to install it.")
    opt.sha256 = plan.source.sha256
    return plan
