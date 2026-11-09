"""Update decisions and the request-budgeted check (frameworks via mods 19/8, and linked mods).

Budget per run (see check_mods): validate once -> updated.json once -> files.json only for linked mods that
changed since their last check or were never checked; everything cached in registry.json with a TTL.
Detection compares *file ids* (never free-form version strings) for mods. An unknown baseline is never "up to date".
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .archives import match_archive_to_files
from .linking import _latest_record, apply_guess
from .nexus import (InvalidKey, NexusClient, NexusError, NexusFile, RateLimited, latest_main_file, release_group)
from .registry import Registry

log = logging.getLogger(__name__)

FRAMEWORK_MODS = {"dml": 19, "dmf": 8}
UP_TO_DATE, UPDATE, UNKNOWN, FAILED, NOT_LINKED = "up to date", "update available", "unknown", "check failed", "not linked"
PROBABLY_UP_TO_DATE, PROBABLY_OUTDATED = "probably up to date", "probably outdated"  # baseline only guessed
FULL_CHECK_AFTER_S = 30 * 86400


def _norm(v: str | None) -> str | None:
    return v.strip().lstrip("vV") if isinstance(v, str) and v.strip() else None


def decide(installed_version: str | None, installed_file_id: int | None, latest: NexusFile | None, *,
           group: set[int] | None = None, version_comparable: bool = True) -> tuple[str, str]:
    """Frameworks: file id wins; version strings are only a fallback (and never for a GitHub commit SHA)."""
    if latest is None:
        return UNKNOWN, "no MAIN file found on Nexus"
    ids = set(group or ()) | ({latest.file_id} if latest.file_id is not None else set())
    if installed_file_id is not None and ids:
        if installed_file_id in ids:
            return UP_TO_DATE, "file id matches the current release"
        return UPDATE, f"file {installed_file_id} -> {latest.file_id}"
    iv, lv = _norm(installed_version), _norm(latest.version)
    if not version_comparable or iv is None:
        return UNKNOWN, "installed version unknown or not comparable to Nexus versions"
    if lv is None:
        return UNKNOWN, "Nexus latest file has no version"
    return (UP_TO_DATE, "version matches latest MAIN file") if iv == lv else (UPDATE, f"{iv} -> {lv}")


def decide_mod(rec: dict | None) -> tuple[str, str]:
    """Status for one mod from its registry record + cached latest. Pure; no network."""
    if not rec or not rec.get("nexus_id"):
        return NOT_LINKED, "press L to link this mod to its Nexus page"
    latest = rec.get("latest")
    if not latest:
        if rec.get("last_error"):
            return FAILED, rec["last_error"]
        return UNKNOWN, "not checked yet"
    if latest.get("file_id") is None:
        return UNKNOWN, "no MAIN file found on Nexus"
    inst = rec.get("installed_file_id")
    if inst is None:  # the rule: never "up to date" without a known baseline
        return UNKNOWN, "version unknown - press L to set it"
    if inst == latest["file_id"] or inst in (latest.get("group") or []):
        if rec.get("baseline") == "guessed":
            return PROBABLY_UP_TO_DATE, "installed version is a guess from file dates; press L to confirm it"
        return UP_TO_DATE, "installed file is the current release"
    old = rec.get("installed_version") or f"file {inst}"
    if rec.get("baseline") == "guessed":
        return PROBABLY_OUTDATED, f"~{old} -> {latest.get('version') or latest['file_id']} (installed version is a guess; press L to confirm it)"
    return UPDATE, f"{old} -> {latest.get('version') or latest['file_id']}"


# ---------------------------------------------------------------- frameworks

@dataclass
class FrameworkUpdate:
    kind: str
    mod_id: int
    status: str
    installed: str | None
    latest: str | None
    latest_file_id: int | None
    reason: str
    latest_file_name: str | None = None  # what the Downloads watcher looks for
    latest_size: int | None = None


def check_framework(kind: str, nexus: NexusClient, registry: Registry) -> FrameworkUpdate:
    mod_id = FRAMEWORK_MODS[kind]
    rec = registry.framework(kind) or {}
    installed = rec.get("version")
    try:
        files = nexus.files(mod_id)
    except NexusError as e:
        return FrameworkUpdate(kind, mod_id, FAILED, installed, None, None, str(e))
    latest = latest_main_file(files)
    file_id = rec.get("file_id")
    archive = rec.get("archive_name")
    if not archive and rec.get("source_kind") == "local" and str(rec.get("source", "")).startswith("local file "):
        archive = str(rec["source"])[len("local file "):]  # records written before archive_name existed
    if file_id is None and archive:  # resolve the exact installed file from the imported archive name
        m = match_archive_to_files(archive, files)
        if m.status in ("exact", "inferred") and len(m.files) == 1:
            file_id = rec["file_id"] = m.files[0].file_id
            registry.save()
    comparable = rec.get("source_kind") in ("local", "github-release", "github-tag-source", "github-pinned") and not rec.get("unreleased")
    status, reason = decide(installed, file_id, latest, group=release_group(files, latest), version_comparable=comparable)
    return FrameworkUpdate(kind, mod_id, status, installed, latest.version if latest else None,
                           latest.file_id if latest else None, reason,
                           latest.file_name if latest else None, latest.size_bytes if latest else None)


def cached_frameworks(registry: Registry) -> list[FrameworkUpdate]:
    """Zero-network framework statuses from the cache (UNKNOWN where nothing is cached yet)."""
    cache = registry.data["updates"].get("frameworks", {})
    return [FrameworkUpdate(**cache[k]) if k in cache else
            FrameworkUpdate(k, FRAMEWORK_MODS[k], UNKNOWN, (registry.framework(k) or {}).get("version"), None, None,
                            "not checked yet")
            for k in ("dml", "dmf") if registry.framework(k)]


def check_frameworks(registry: Registry, nexus: NexusClient | None, *, ttl_s: float = 3600.0, force: bool = False,
                     now: Callable[[], float] = time.time,
                     on_result: Callable[[FrameworkUpdate], None] | None = None) -> list[FrameworkUpdate]:
    """DML/DMF check with the same TTL cache as mods (2 files.json calls only when stale)."""
    from dataclasses import asdict

    upd = registry.data["updates"]
    cache = upd.setdefault("frameworks", {})
    kinds = [k for k in ("dml", "dmf") if registry.framework(k)]
    t = now()
    fresh = (not force) and isinstance(upd.get("fw_last_run"), (int, float)) and 0 <= t - upd["fw_last_run"] < ttl_s \
        and all(k in cache for k in kinds)
    if fresh or nexus is None:
        out = cached_frameworks(registry)
        if nexus is None:
            for u in out:
                if u.reason == "not checked yet":
                    u.reason = "not checked (no Nexus API key)"
        for u in out:
            if on_result:
                on_result(u)
        return out
    out = []
    for k in kinds:
        u = check_framework(k, nexus, registry)
        out.append(u)
        if on_result:
            on_result(u)
        if u.status != FAILED:
            cache[k] = asdict(u)
    if all(u.status != FAILED for u in out):
        upd["fw_last_run"] = t
    registry.save()
    return out


# ---------------------------------------------------------------- linked mods

@dataclass
class ModRow:
    folder: str
    nexus_id: int | None
    status: str
    installed: str | None
    latest: str | None
    reason: str
    baseline: str | None = None  # chosen | guessed | import | marked-current | None (unknown)
    baseline_unknown: bool = False  # linked, but no installed file recorded


@dataclass
class CheckResult:
    rows: list[ModRow]
    requests: int = 0
    from_cache: bool = False
    rate_limited: bool = False
    full_check: bool = False
    premium: bool | None = None
    quota: str = ""
    error: str = ""


def _row(folder: str, rec: dict | None) -> ModRow:
    status, reason = decide_mod(rec)
    rec = rec or {}
    latest = rec.get("latest") or {}
    inst = rec.get("installed_version")
    if inst and rec.get("baseline") == "guessed":
        inst = f"~{inst}"
    return ModRow(folder, rec.get("nexus_id"), status, inst, latest.get("version"), reason, rec.get("baseline"),
                  bool(rec.get("nexus_id")) and rec.get("installed_file_id") is None)


def cached_rows(folders: list[str], registry: Registry) -> list[ModRow]:
    """Zero-network statuses from whatever is cached (used for instant first paint)."""
    return [_row(f, registry.mod(f)) for f in folders]


def check_mods(folders: list[str], registry: Registry, nexus: NexusClient | None, *, ttl_s: float = 3600.0,
               force: bool = False, now: Callable[[], float] = time.time,
               on_row: Callable[[ModRow], None] | None = None,
               mtimes: dict[str, float | None] | None = None) -> CheckResult:
    """Run the budgeted update check for the installed `folders`. Never raises for network/quota problems."""
    t = now()
    upd = registry.data["updates"]
    emit = on_row or (lambda r: None)
    linked = [f for f in folders if (registry.mod(f) or {}).get("nexus_id")]
    res = CheckResult(rows=[], premium=upd.get("premium"))
    last_run = upd.get("last_run")
    fresh = (not force) and isinstance(last_run, (int, float)) and 0 <= t - last_run < ttl_s

    def finish() -> CheckResult:
        res.rows = cached_rows(folders, registry)
        for r in res.rows:
            emit(r)
        if nexus is not None:
            res.requests, res.quota = nexus.requests, str(nexus.quota)
        registry.save()
        return res

    if fresh or not linked or nexus is None:
        res.from_cache = True
        res.error = "" if nexus is not None or not linked else "no Nexus API key set"
        return finish()

    try:
        who = nexus.validate()  # request 1: key check + premium flag
        upd["premium"], upd["premium_checked"] = bool(who.get("is_premium")), t
        res.premium = upd["premium"]
        full = not isinstance(upd.get("last_full"), (int, float)) or (t - upd["last_full"]) > FULL_CHECK_AFTER_S
        changed: dict[int, float] | None = None
        if not full:
            try:  # request 2: one listing of everything updated in the last month
                changed = {int(u["mod_id"]): float(u.get("latest_file_update") or 0)
                           for u in nexus.updated("1m") if "mod_id" in u}
            except RateLimited:
                raise
            except (NexusError, ValueError, TypeError) as e:
                log.warning("updated.json unusable (%s); falling back to a full check", e)
                full = True
        res.full_check = full
        for folder in linked:
            rec = registry.mod(folder)
            checked = rec.get("checked_at")
            stale = (full or changed is None or not rec.get("latest") or checked is None
                     or changed.get(rec["nexus_id"], 0) > checked)
            if not stale:
                continue
            try:
                files = nexus.files(rec["nexus_id"])  # request 3..n: only changed / never-checked mods
            except (RateLimited, InvalidKey):
                raise
            except NexusError as e:
                rec["last_error"] = str(e)  # keep any older cached `latest`; decide_mod reports FAILED only if none
                emit(_row(folder, rec))
                continue
            rec["latest"], rec["checked_at"], rec["last_error"] = _latest_record(files), t, None
            if rec.get("installed_file_id") is None and rec.get("installed_archive"):
                m = match_archive_to_files(rec["installed_archive"], files)
                if m.status in ("exact", "inferred") and len(m.files) == 1:
                    f = m.files[0]
                    rec.update(installed_file_id=f.file_id, installed_version=f.version, baseline="import")
            if rec.get("installed_file_id") is None and mtimes:  # pre-existing mod: guess from file dates
                apply_guess(registry, folder, files, mtimes.get(folder), save=False)
            emit(_row(folder, rec))  # rows appear in the UI as each mod's result arrives
        upd["last_run"] = t
        if full:
            upd["last_full"] = t
    except RateLimited as e:
        res.rate_limited, res.error = True, str(e)  # stop gracefully; last_run is NOT advanced
    except InvalidKey as e:
        res.error = str(e)
    except NexusError as e:
        res.error = str(e)
    return finish()


# ---------------------------------------------------------------- one pipeline for CLI and TUI

def quota_text(registry: Registry) -> str | None:
    """Last known quota with the time it was observed, e.g. '19992 daily / 1992 hourly left (as of 14:22)'."""
    q = registry.data["updates"].get("quota") or {}
    if not q.get("text"):
        return None
    when = time.strftime("%H:%M", time.localtime(q["at"])) if isinstance(q.get("at"), (int, float)) else "?"
    return f"{q['text']} (as of {when})"


@dataclass
class CheckBundle:
    frameworks: list
    mods: CheckResult
    live: bool
    requests: int
    premium: bool | None
    key_set: bool
    quota: str | None


def run_checks(folders: list[str], registry: Registry, key: str | None, *, ttl_s: float = 3600.0, force: bool = False,
               on_row: Callable[[ModRow], None] | None = None,
               on_framework: Callable[[FrameworkUpdate], None] | None = None,
               mtimes: dict[str, float | None] | None = None,
               now: Callable[[], float] = time.time) -> CheckBundle:
    """Frameworks + linked mods in one budgeted pass. Validates at most once; records quota and account type."""
    nx = NexusClient(key) if key else None
    t_start = now()
    try:
        fw = check_frameworks(registry, nx, ttl_s=ttl_s, force=force, now=now, on_result=on_framework)
        r = check_mods(folders, registry, nx, ttl_s=ttl_s, force=force, now=now, on_row=on_row, mtimes=mtimes)
        upd = registry.data["updates"]
        if nx is not None and nx.requests > 0 and not nx.exhausted and (upd.get("premium_checked") or 0) < t_start:
            try:  # a live run happened but check_mods did not validate (frameworks only): validate exactly once
                upd["premium"], upd["premium_checked"] = bool(nx.validate().get("is_premium")), now()
            except NexusError:
                pass
        live = nx is not None and nx.requests > 0
        if live:
            upd["quota"] = {"text": str(nx.quota), "at": now()}
        registry.save()
        return CheckBundle(fw, r, live, nx.requests if nx else 0, upd.get("premium"), bool(key), quota_text(registry))
    finally:
        if nx:
            nx.close()
