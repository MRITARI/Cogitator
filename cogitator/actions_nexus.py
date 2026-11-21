"""Interface-independent flows that talk to Nexus or GitHub: link, update, framework update, bootstrap from GitHub, nxm links,
search and changelogs. Same contract as `actions`: a flow talks to the person through a `Ui`, runs on a worker thread and
returns an `Outcome` (or reports through `after`) so the caller can refresh. The Nexus key is read here, used for the
calls and never logged."""
from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import nxm
from .actions import DIM, ERR, INFO, OK, WARN, Ctx, Outcome, Ui, import_file
from .config import cache_dir, get_api_key
from .frameworks import describe_plan, plan_github
from .fwupdate import install_framework_plan
from .linking import apply_guess, choose_baseline, folder_mtime, link_mod, rank_candidates
from .loader import LoaderError, make_client
from .modinstall import (ModImportError, NotPremium, apply_update_premium, fetch_premium_download, mod_page_url, open_files_page,
                         wait_for_download)
from .mods import INTERNAL_FOLDERS, mods_dir
from .nexus import InvalidKey, NexusClient, NexusError, RateLimited, latest_main_file, parse_nexus_ref
from .updates import FRAMEWORK_MODS, PROBABLY_OUTDATED, UPDATE, FrameworkUpdate, ModRow

NEED_KEY = "Linking validates through the API - set your Nexus key first."
LINK_CHOICES = ["Link and mark current as installed (only newer uploads will be flagged)",
                "Link, but leave the installed file unknown (status stays 'unknown')", "Cancel"]


def is_premium(ctx: Ctx) -> bool:
    return bool(ctx.registry and ctx.registry.data["updates"].get("premium") and get_api_key())


def _open(ctx: Ctx, mod_id: int) -> str:
    return open_files_page(mod_id, ctx.open_url) if ctx.open_url else open_files_page(mod_id)


# ---------------------------------------------------------------- link

def link_needs_version_only(row: ModRow | None) -> bool:
    """A mod that is already linked only needs its installed version set (when it is unknown or a guess)."""
    return bool(row is not None and row.nexus_id and row.baseline in (None, "guessed"))


def link_refusal(folder: str) -> str | None:
    return f"'{folder}' belongs to the loader/framework." if folder in INTERNAL_FOLDERS else None


def link_one_mod(ctx: Ctx, ui: Ui, folder: str, mod_id: int) -> Outcome | None:
    """Link `folder` to Nexus mod `mod_id` after validating through the API and asking the baseline question."""
    key = get_api_key()
    if not key:
        ui.log(NEED_KEY, WARN)
        return None
    try:
        with NexusClient(key) as nx:
            info, files = nx.mod(mod_id), nx.files(mod_id)
    except NexusError as e:
        ui.log(f"Link failed: {e}", ERR)
        return None
    name = info.get("name") or f"mod {mod_id}"
    latest = latest_main_file(files)
    choice = ui.choose(f"Nexus mod {mod_id}: {name} (latest {latest.version if latest else 'n/a'}). Link '{folder}' to it?", LINK_CHOICES)
    if choice not in (0, 1):
        ui.log("Link cancelled.", DIM)
        return None
    with ctx.lock:
        rec = link_mod(ctx.registry, folder, mod_id, name, files, via="manual", baseline="current" if choice == 0 else "none")
    ui.log(f"Linked '{folder}' to {name}." + ("" if rec["installed_file_id"] else " Baseline unknown."), OK)
    return Outcome(refresh=True, folder=folder)


def version_options(files, guess) -> tuple[list, list[str], int]:
    """(files shown newest first, menu lines with dates, preselected index). The last menu line is 'I do not know'."""
    pool = sorted((f for f in files if f.file_id is not None), key=lambda f: (f.uploaded or 0, f.file_id), reverse=True)[:40]
    opts = [f"{f.version or '?'}   {time.strftime('%Y-%m-%d', time.localtime(f.uploaded)) if f.uploaded else '?'}   "
            f"{f.category or ''} {f.name or f.file_name or ''}".rstrip() + ("   <- guess" if f.file_id == guess else "")
            for f in pool] + ["I do not know"]
    return pool, opts, next((i for i, f in enumerate(pool) if f.file_id == guess), 0)


def pick_version(ctx: Ctx, ui: Ui, folder: str, files) -> None:
    """Version picker with dates; the current guess is preselected. 'I do not know' keeps whatever is stored."""
    guess = (ctx.registry.mod(folder) or {}).get("installed_file_id")
    pool, opts, sel = version_options(files, guess)
    if not pool:
        return
    c = ui.choose(f"Which version of '{folder}' is installed?", opts, selected=sel)
    if c is not None and c < len(pool):
        with ctx.lock:
            choose_baseline(ctx.registry, folder, files, pool[c].file_id)


def set_version(ctx: Ctx, ui: Ui, folder: str, mod_id: int) -> Outcome | None:
    key = get_api_key()
    if not key:
        ui.log("Set your Nexus key first.", WARN)
        return None
    try:
        with NexusClient(key) as nx:
            files = nx.files(mod_id)
    except NexusError as e:
        ui.log(f"Could not load versions: {e}", ERR)
        return None
    with ctx.lock:
        apply_guess(ctx.registry, folder, files, folder_mtime(mods_dir(ctx.game_dir) / folder))
    pick_version(ctx, ui, folder, files)
    return Outcome(refresh=True, folder=folder)


def unlinked_folders(ctx: Ctx, present: list[str]) -> list[str]:
    return [f for f in present if f not in INTERNAL_FOLDERS and not (ctx.registry.mod(f) or {}).get("nexus_id")]


def _link_candidate(ctx: Ctx, ui: Ui, nx: NexusClient, folder: str) -> bool | None:
    """True linked, False skipped, None stop. Never links without the person's confirmation."""
    mod_id = None
    try:
        cands = rank_candidates(folder, nx.search_mods(folder.replace("_", " ")))
    except (InvalidKey, RateLimited):
        raise
    except NexusError as e:
        ui.log(f"Name search unavailable ({e}); paste a URL or id.", WARN)
        cands = []
    if cands:
        opts = [f"Yes - link to: {cands[0]['name']} (mod {cands[0]['mod_id']})"]
        opts += [f"Use instead: {c['name']} (mod {c['mod_id']})" for c in cands[1:]]
        opts += ["No - paste a Nexus URL or id", "Skip this mod", "Stop"]
        c = ui.choose(f"Link '{folder}'?", opts)
        if c is None or c == len(opts) - 1:
            return None
        if c == len(opts) - 2:
            return False
        if c < len(cands):
            mod_id = cands[c]["mod_id"]
    if mod_id is None:
        ref = ui.ask_text(f"Link '{folder}' to its Nexus page", "URL or numeric id (blank = skip)", allow_blank=True)
        if ref is None:
            return None
        if not ref:
            return False
        mod_id, problem = parse_nexus_ref(ref)
        if mod_id is None:
            ui.log(f"{folder}: {problem}", ERR)
            return False
    try:
        info, files = nx.mod(mod_id), nx.files(mod_id)
    except (InvalidKey, RateLimited):
        raise
    except NexusError as e:
        ui.log(f"{folder}: {e}", ERR)
        return False
    with ctx.lock:
        link_mod(ctx.registry, folder, mod_id, info.get("name") or f"mod {mod_id}", files, via="manual", baseline="none")
        apply_guess(ctx.registry, folder, files, folder_mtime(mods_dir(ctx.game_dir) / folder))
    pick_version(ctx, ui, folder, files)
    ui.log(f"Linked '{folder}' to {info.get('name') or mod_id}.", OK)
    return True


def link_all(ctx: Ctx, ui: Ui, present: list[str]) -> Outcome | None:
    key = get_api_key()
    if not key:
        ui.log(NEED_KEY, WARN)
        return None
    todo = unlinked_folders(ctx, present)
    if not todo:
        ui.log("Every mod is already linked.", DIM)
        return None
    done = 0
    try:
        with NexusClient(key) as nx:
            for folder in todo:
                r = _link_candidate(ctx, ui, nx, folder)
                if r is None:
                    break
                done += bool(r)
    except NexusError as e:
        ui.log(f"Link all stopped: {e}", ERR)
    ui.log(f"Link all finished: {done} linked.", OK)
    return Outcome(refresh=True)


# ---------------------------------------------------------------- updates

def update_candidates(urows: dict[str, ModRow], only: list[str] | None = None, include_outdated: bool = False) -> list[str]:
    """Folders whose row says an update exists. A pinned mod never has UPDATE status; PROBABLY_OUTDATED (installed version
    is a guess) only counts for a mod the person selected by hand."""
    want = (UPDATE, PROBABLY_OUTDATED) if include_outdated else (UPDATE,)
    return [f for f in (only if only is not None else list(urows)) if f in urows and urows[f].status in want]


def premium_update(ctx: Ctx, ui: Ui, folders: list[str]) -> Outcome:
    key = get_api_key()
    limits = ctx.settings.extract_limits()
    with NexusClient(key) as nx:
        for f in folders:
            try:
                with ctx.lock:
                    r = apply_update_premium(f, ctx.game_dir, ctx.registry, nx, premium=is_premium(ctx), is_running=ctx.game_running,
                                             max_bytes=limits[0], max_files=limits[1])
                ui.log(f"{f}: updated to {r.version or r.file_id}.", OK)
            except (ModImportError, LoaderError, NexusError, NotPremium) as e:
                ui.log(f"{f}: {e}", ERR)
    return Outcome(refresh=True, recheck=True)


def watch_download(ctx: Ctx, ui: Ui, mod_id: int, latest: dict, label: str) -> Path | None:
    """Free-account path: open the files page, then wait (cancellably) for the finished download to appear."""
    since = time.time()
    url = _open(ctx, mod_id)
    dl = ctx.downloads_dir()
    ui.log(f"Opened {url} - download '{latest.get('file_name')}' for {label}. Watching {dl} ...", INFO)
    found = wait_for_download(dl, mod_id, latest, since=since, timeout_s=ctx.download_wait_s, cancelled=ctx.cancelled)
    if not found:
        ui.log("No matching download seen. When it finishes, import it by hand.", WARN)
    return found


def manual_update(ctx: Ctx, ui: Ui, folder: str, after: Callable[[Outcome], None]) -> None:
    """Free account: open the mod's files page and return. The page's Mod Manager Download button sends an nxm link that
    Cogitator picks up and installs, so nothing waits for a download here."""
    rec = ctx.registry.mod(folder) or {}
    if not rec.get("nexus_id") or not rec.get("latest"):
        ui.log(f"{folder}: not linked or not checked yet.", WARN)
        return
    ui.log(f"Opened {_open(ctx, rec['nexus_id'])} for {folder}. Press Mod Manager Download there; Cogitator installs it.", INFO)


def free_update_all_help(ctx: Ctx, ui: Ui, urows: dict[str, ModRow], targets: list[str]) -> None:
    """Free account: open every outdated mod's files page (one after another, without waiting for the downloads)."""
    ui.log(f"Free account: opening {len(targets)} page(s). Press Mod Manager Download on each; Cogitator installs them as they arrive.", INFO)
    for i, f in enumerate(targets):
        if ctx.cancelled():
            break
        if i:
            time.sleep(0.7)
        ui.log(f"  {f}: {_open(ctx, urows[f].nexus_id)}")


# ---------------------------------------------------------------- frameworks

def pending_framework_updates(fw: list[FrameworkUpdate]) -> list[FrameworkUpdate]:
    return [u for u in fw if u.status == UPDATE and u.latest_file_id is not None]


def framework_update_body(ups: list[FrameworkUpdate], premium: bool) -> str:
    how = ("Cogitator downloads it through your Premium account, then shows the install source for a final confirmation."
           if premium else
           "Cogitator opens the Nexus files page; download the MAIN file and it is imported from your Downloads folder "
           "after a final confirmation.")
    return "\n".join(f"{u.kind.upper()}: {u.installed or 'unknown'} -> {u.latest}" for u in ups) + "\n\n" + how


def framework_update(ctx: Ctx, ui: Ui, ups: list[FrameworkUpdate], after: Callable[[Outcome], None]) -> None:
    key = get_api_key()
    work = cache_dir() / "work"
    for u in sorted(ups, key=lambda x: x.kind != "dml"):  # loader before framework
        mod_id = FRAMEWORK_MODS[u.kind]
        latest = {"file_name": u.latest_file_name, "version": u.latest, "file_id": u.latest_file_id}
        tmp: Path | None = None
        try:
            if is_premium(ctx):
                work.mkdir(parents=True, exist_ok=True)
                tmp = Path(tempfile.mkdtemp(prefix="fw-", dir=work))
                with NexusClient(key) as nx:
                    found = fetch_premium_download(nx, mod_id, u.latest_file_id, u.latest_file_name, u.latest_size, tmp, premium=True)
            else:
                found = watch_download(ctx, ui, mod_id, latest, u.kind.upper())
            if found:
                import_file(ctx, ui, str(found), None, after)  # the existing import flow: shows source, asks, installs, re-patches
        except (ModImportError, LoaderError, NexusError, NotPremium) as e:
            ui.log(f"{u.kind.upper()} update: {e}", ERR)
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)


def install_from_github(ctx: Ctx, ui: Ui, missing: list[str]) -> Outcome:
    limits = ctx.settings.extract_limits()
    with tempfile.TemporaryDirectory(prefix="cogitator-gh-") as wd, make_client(github_only=True) as client:
        for kind in sorted(missing, key=lambda k: k != "dml"):
            plan = plan_github(kind, client, Path(wd))
            if not ui.confirm(f"Install {kind.upper()} from GitHub?", describe_plan(plan)):
                ui.log(f"{kind.upper()} install declined.", WARN)
                continue
            with ctx.lock:
                fi = install_framework_plan(plan, ctx.game_dir, ctx.registry, runner=ctx.patch_runner, is_running=ctx.game_running,
                                            max_bytes=limits[0], max_files=limits[1])
            ui.log(f"{kind.upper()} installed ({fi.result.written} files).", OK)
    return Outcome(refresh=True)


# ---------------------------------------------------------------- nxm links from the website

def nxm_install(ctx: Ctx, ui: Ui, path, link, after: Callable[[Outcome], None]) -> None:
    """One queued nxm:// link: confirm, download, import. The queue file and the work folder are always cleaned up."""
    work = cache_dir() / "nxm"
    try:
        key = get_api_key()
        if not key:
            ui.log("A Nexus link arrived, but no API key is set. Set it, then click the button again.", "bold yellow")
            return
        with NexusClient(key) as nx:
            try:
                name = nx.mod(link.mod_id).get("name") or f"mod {link.mod_id}"
                f = next((x for x in nx.files(link.mod_id) if x.file_id == link.file_id), None)
            except NexusError as e:
                ui.log(f"Nexus link: {e}", ERR)
                return
            what = f"{name}\n{f.file_name or f.name} (v{f.version})" if f else f"{name} (file {link.file_id})"
            if not ui.confirm("Install from Nexus?", what + "\n\nThis came from the website's Mod Manager Download button. "
                              "Only install mods you trust.", default_yes=False):
                ui.log("Nexus link cancelled.", WARN)
                return
            ui.log(f"Downloading {name}...", DIM)
            try:
                zp, _ = nxm.download(link, nx, work)
            except (nxm.NxmError, NexusError, ModImportError, OSError) as e:
                ui.log(f"Download failed: {e}", ERR)
                return
        import_file(ctx, ui, str(zp), link.mod_id, after)
    finally:
        nxm.done(path)
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- search and changelog

def search_nexus(ctx: Ctx, ui: Ui, text: str) -> None:
    key = get_api_key()
    if not key:
        ui.log("Set your Nexus API key first.", WARN)
        return
    try:
        with NexusClient(key) as nx:
            rows = nx.search_mods(text, count=10)
    except NexusError as e:
        ui.log(f"Search: {e}", ERR)
        return
    if not rows:
        ui.log(f"Nothing found for '{text}'.", WARN)
        return
    i = ui.choose(f"Results for '{text}' (OK opens the page)", [f"{r['name']}  (#{r['mod_id']})" for r in rows])
    if i is None:
        return
    url = mod_page_url(rows[i]["mod_id"]).replace("?tab=files", "")
    if ctx.open_url:
        ctx.open_url(url)
    else:
        import webbrowser

        webbrowser.open(url)
    ui.log(f"{rows[i]['name']}: {url}", INFO)
    ui.log("Download it from the page, then import it (or use the website's Mod Manager Download button if Cogitator handles nxm links).", DIM)


@dataclass
class Changelog:
    ok: bool
    lines: list[tuple[str, str]]  # (text, style)


def changelog_for(registry, folder: str) -> Changelog:
    from .versions import newer_versions

    rec = (registry.mod(folder) if registry else None) or {}
    key = get_api_key()
    if not (rec.get("nexus_id") and key):
        return Changelog(False, [("Link the mod to Nexus and set your API key to see its changelog.", WARN)])
    try:
        with NexusClient(key) as nx:
            log = nx.changelogs(int(rec["nexus_id"]))
    except NexusError as e:
        return Changelog(False, [(f"Changelog: {e}", ERR)])
    if not log:
        return Changelog(False, [(f"{folder}: no changelog published on Nexus.", WARN)])
    out: list[tuple[str, str]] = []
    for v in newer_versions(list(log), rec.get("installed_version")):
        out.append((f"{folder} v{v}", "bold"))
        out += [(f"   - {line}", "") for line in log[v][:12]]
    return Changelog(True, out)
