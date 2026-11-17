"""Headless CLI. Subcommands are added as features land."""
from __future__ import annotations

import argparse
import json
import os
import sys

import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__, profiles, snapshots
from .config import InstanceLock, LockError, SchemaTooNew, Settings, get_api_key, set_api_key, setup_logging
from .doctor import exit_code as doctor_exit_code, format_report, run_doctor, to_json as doctor_json
from .frameworks import Plan, describe_plan, detect_framework, plan_github, plan_local
from .fwupdate import install_framework_plan
from .uninstall import UninstallPlan, uninstall_framework
from .gamefinder import Resolution, resolve_game_dir
from .loader import NEXUS_PAGES, LoaderError, make_client
from .mods import ModError, move_mod, scan_mods, set_mod_enabled
from .nexus import NexusClient, NexusError
from .patcher import ON, PatcherError, banner, patch_state
from .patcher import toggle as toggle_mods
from .registry import Registry, check_data_versions
from .archives import parse_archive_name
from .linking import link_mod
from .loader import LoaderError
from .modinstall import (ModImportError, apply_update_premium, import_archive, inspect_mod_zip, mod_page_url,
                         open_files_page, wait_for_download)
from .mods import INTERNAL_FOLDERS, mods_dir
from .nexus import latest_main_file, parse_nexus_ref
from .updates import cached_rows, run_checks


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cogitator", description="Darktide mod manager")
    p.add_argument("--game-dir", help="Darktide install folder (overrides config/auto-detect)")
    p.add_argument("--instance", metavar="NAME", help="use a separate named setup (own settings, registry, snapshots and profiles)")
    p.add_argument("--no-update-check", action="store_true", help="TUI: do not check for updates on start")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)  # verifies a pipx/PyInstaller build
    p.add_argument("--expect-version", help=argparse.SUPPRESS)  # with --self-check: fail unless this is the build's version
    sub = p.add_subparsers(dest="cmd")
    st = sub.add_parser("status", help="show game path and mod state")
    st.add_argument("--json", action="store_true")
    inst = sub.add_parser("install-framework", help="install DML + DMF from local zips (or opt-in GitHub)")
    inst.add_argument("--zip", action="append", default=[], metavar="ZIP", help="DML or DMF archive (repeatable)")
    inst.add_argument("--from-github", action="store_true", help="opt in: fetch from GitHub (source is shown first)")
    inst.add_argument("--only", choices=["dml", "dmf"], help="with --from-github: just this component")
    inst.add_argument("--update", action="store_true", help="with --from-github: also reinstall installed components")
    inst.add_argument("--dry-run", action="store_true", help="show what would be installed; change nothing")
    inst.add_argument("--yes", "-y", action="store_true", help="explicit confirmation (the source is still printed)")
    tg = sub.add_parser("toggle", help="global mods ON/OFF (patch / unpatch the game database)")
    tg.add_argument("--yes", "-y", action="store_true", help="confirm un-patching without prompting")
    cu = sub.add_parser("check-updates", help="check DML/DMF and linked mods for updates via the Nexus API")
    cu.add_argument("--json", action="store_true")
    cu.add_argument("--force", action="store_true", help="ignore the TTL cache (still request-budgeted)")
    sub.add_parser("set-key", help="store your Nexus API key (read from a hidden prompt)")
    ln = sub.add_parser("link", help="link an installed mod folder to its Nexus page")
    ln.add_argument("folder")
    ln.add_argument("ref", help="Nexus mod URL or numeric id")
    ln.add_argument("--no-baseline", action="store_true", help="do not mark the current file as installed (status stays unknown)")
    ln.add_argument("--installed-file", type=int, metavar="FILE_ID", help="record this Nexus file id as the installed one")
    ln.add_argument("--yes", "-y", action="store_true", help="accept the mod name and the default baseline")
    im = sub.add_parser("import", help="install a mod archive (.zip) and link it")
    im.add_argument("archive")
    im.add_argument("--mod-id", type=int, help="Nexus mod id (when the file name does not say)")
    im.add_argument("--yes", "-y", action="store_true", help="confirm the install (ambiguity is still never guessed)")
    im.add_argument("--dry-run", action="store_true", help="list what would be added or overwritten; change nothing")
    up = sub.add_parser("update", help="update a linked mod (Premium: automatic; free: browser + import)")
    up.add_argument("folder", nargs="?")
    up.add_argument("--all", action="store_true", help="update every mod with an update available")
    up.add_argument("--wait", type=int, default=180, metavar="SECONDS", help="free accounts: how long to watch Downloads")
    up.add_argument("--no-browser", action="store_true")
    up.add_argument("--yes", "-y", action="store_true")
    up.add_argument("--dry-run", action="store_true", help="show which updates would be installed; download nothing")
    sn = sub.add_parser("snapshot", help="snapshots of your mod setup: create, list, restore, diff, prune")
    sn.add_argument("action", choices=["create", "list", "restore", "diff", "prune"])
    sn.add_argument("name", nargs="?", help="restore/diff: snapshot name; diff compares it with the current setup or with 'other'")
    sn.add_argument("other", nargs="?")
    sn.add_argument("--json", action="store_true")
    sn.add_argument("--yes", "-y", action="store_true")
    sn.add_argument("--keep", type=int, help="prune: how many to keep (default: the snapshot_keep setting)")
    pr = sub.add_parser("profile", help="named mod setups: save, switch, list, show, diff, delete, export, import")
    pr.add_argument("action", choices=["save", "list", "show", "switch", "delete", "diff", "export", "import"])
    pr.add_argument("name", nargs="?", help="profile name (import: a setup code, or a .json file)")
    pr.add_argument("other", nargs="?", help="diff: second profile (default: the current setup)")
    pr.add_argument("--as", dest="as_name", help="import: save under this name")
    pr.add_argument("--file", help="export: write the profile to this .json file instead of printing a code")
    pr.add_argument("--json", action="store_true")
    pr.add_argument("--yes", "-y", action="store_true")
    pr.add_argument("--dry-run", action="store_true", help="switch: show what would change; change nothing")
    su_ = sub.add_parser("self-update", help="update Cogitator itself from its signed GitHub releases")
    su_.add_argument("--check", action="store_true", help="only look for a newer version")
    su_.add_argument("--apply", action="store_true", help="install the downloaded update and restart")
    su_.add_argument("--rollback", action="store_true", help="go back to the previous version")
    su_.add_argument("--channel", choices=["stable", "beta"], help="release channel (remembered)")
    dr = sub.add_parser("doctor", help="diagnose the install and print fixes (exit 0 ok, 1 warnings, 2 failures)")
    dr.add_argument("--json", action="store_true")
    dr.add_argument("--offline", action="store_true", help="skip the Nexus and GitHub network checks")
    dr.add_argument("--history", nargs="?", type=int, const=20, metavar="N", help="show the last N doctor results instead of running it")
    la = sub.add_parser("launch", help="check, then start Darktide through Steam")
    la.add_argument("--profile", help="switch to this profile first")
    la.add_argument("--vanilla", action="store_true", help="turn mods off for this launch (press T / run toggle to restore)")
    la.add_argument("--force", action="store_true", help="launch even if the doctor reports failures")
    bs = sub.add_parser("bisect", help="find the mod causing a crash by halving the enabled mods")
    bs.add_argument("action", choices=["start", "good", "bad", "status", "reset"])
    cr = sub.add_parser("crashlog", help="read the newest game console log and rank the mods most likely at fault")
    cr.add_argument("--json", action="store_true")
    ch = sub.add_parser("changelog", help="show what changed in a linked mod on Nexus")
    ch.add_argument("folder")
    ch.add_argument("--all", action="store_true", help="every version, not just newer than yours")
    nx_ = sub.add_parser("nxm", help="Nexus 'Mod Manager Download' links: register this program as the nxm:// handler")
    nx_.add_argument("action", choices=["register", "unregister", "status", "handle"])
    nx_.add_argument("url", nargs="?", help="handle: the nxm:// link (what the browser passes)")
    se = sub.add_parser("search", help="search Nexus Mods for Darktide mods by name")
    se.add_argument("text", nargs="+")
    sub.add_parser("diag", help="write a redacted diagnostics zip to attach to a bug report (nothing is uploaded)")
    uf = sub.add_parser("uninstall-framework", help="remove DML/DMF and switch mods off (your mods and load order are kept)")
    uf.add_argument("--only", choices=["dml", "dmf"], help="remove just one of them")
    uf.add_argument("--yes", "-y", action="store_true", help="skip the confirmation")
    ul = sub.add_parser("unlink", help="forget the Nexus link of a mod folder (does not touch the mod itself)")
    ul.add_argument("folder")
    sub.add_parser("restore-database", help="undo a mod patch that was cut off half-way (restores the saved original game database)")
    vd = sub.add_parser("verify-download", help="check a downloaded SHA256SUMS file (and optionally a download listed in it) against the release key")
    vd.add_argument("sums", help="path to SHA256SUMS (the SHA256SUMS.sig file must sit next to it)")
    vd.add_argument("file", nargs="?", help="a downloaded release file to check against the list")
    pa = sub.add_parser("path", help="add/remove the folder of this cogitator.exe in your user PATH (no admin)")
    pa.add_argument("action", choices=["add", "remove", "status"])
    pa.add_argument("--dir", help="folder to use instead of the one containing the running cogitator.exe")
    mod = sub.add_parser("mod", help="list, enable, disable, reorder or remove a mod")
    mod.add_argument("action", choices=["list", "enable", "disable", "up", "down", "remove", "rollback", "rebaseline", "pin", "unpin", "note", "tag", "untag", "fav", "unfav", "link-dev"])
    mod.add_argument("folder", nargs="?", help="mod folder (link-dev: the folder you develop in)")
    mod.add_argument("rest", nargs="*", help="pin: version; note: the text; tag/untag: tags")
    mod.add_argument("--name", help="link-dev: the mod folder name to create (default: the source folder's name)")
    mod.add_argument("--json", action="store_true", help="machine-readable output (list)")
    mod.add_argument("--all", action="store_true", help="rebaseline: every installed mod")
    mod.add_argument("--dry-run", action="store_true", help="remove: show the files that would go; change nothing")
    mod.add_argument("--yes", "-y", action="store_true", help="remove: confirm without prompting")
    return p


_MUTATING = {"restore-database", "toggle", "mod", "snapshot", "profile", "launch", "bisect", "link", "unlink", "import", "update", "install-framework", "uninstall-framework"}
_LOCKS: list = []  # held until the process exits


def _resolve(args) -> Resolution:
    s = Settings.load()
    res = resolve_game_dir(args.game_dir, s.game_dir)
    if res.game_dir and res.source in ("steam", "override") and str(res.game_dir) != s.game_dir:
        s.game_dir = str(res.game_dir)
        s.save()
    if res.game_dir and getattr(args, "cmd", None) in _MUTATING and getattr(args, "action", None) not in ("list", "diff", "show", "export", "status"):
        try:  # the interface holds the same lock: two writers on registry.json / the load order would lose changes
            _LOCKS.append(InstanceLock(res.game_dir).acquire())
        except LockError as e:
            return Resolution(None, "locked", message=f"{e} Close it first, or use it instead of the command line.")
    return res


def cmd_status(args) -> int:
    res = _resolve(args)
    mods = scan_mods(res.game_dir) if res.game_dir else []
    reg = Registry.load()
    urows = {r.folder: r for r in cached_rows([m.folder for m in mods if m.present], reg)}  # cache only, no network
    fw = {}
    state = None
    if res.game_dir:
        for k in ("dml", "dmf"):
            st = detect_framework(res.game_dir, k)
            rec = reg.framework(k) or {}
            fw[k] = {"status": st.status, "missing": st.missing, "version": rec.get("version"),
                     "source_kind": rec.get("source_kind")}
        state = patch_state(res.game_dir)
    data = {
        "game_dir": str(res.game_dir) if res.game_dir else None,
        "source": res.source,
        "candidates": [str(c) for c in res.candidates],
        "message": res.message,
        "frameworks": fw,
        "mods_state": state,
        "mods": [{**asdict(m), "update_status": urows[m.folder].status if m.folder in urows else None} for m in mods],
    }
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"Game:   {data['game_dir'] or '-'}  ({res.source})")
        if res.message:
            print(res.message)
        for c in res.candidates:
            print(f"  candidate: {c}")
        for k, v in fw.items():
            ver = v["version"] or ("unknown" if v["status"] == "installed" else "-")
            print(f"{k.upper():<7} {v['status']}  version: {ver}" + (f"  missing: {', '.join(v['missing'])}" if v["missing"] else ""))
        if state:
            print(f"MODS:   {state}")
            b = banner(res.game_dir)
            if b:
                print(f"!! {b}")
        for m in mods:
            flags = ("" if m.present else " [folder missing]") + ("" if m.has_mod_file else " [no .mod file]")
            u = urows.get(m.folder)
            print(f"  {m.state:<9} {m.folder}{flags}" + (f"   [{u.status}]" if u else ""))
    return 0 if res.game_dir else 2


def cmd_restore_database(args) -> int:
    from .patcher import PatcherError, restore_database

    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    try:
        print(restore_database(res.game_dir, Registry.load()))
    except PatcherError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_path(args) -> int:
    from . import userpath

    directory = args.dir or userpath.default_dir()
    if not directory:
        print("Not running from cogitator.exe: pass --dir <folder>.", file=sys.stderr)
        return 2
    directory = str(Path(directory).absolute())
    try:
        if args.action == "status":
            present = userpath.has_entry(directory)
            print(f"{directory}: {'present in' if present else 'not in'} your user PATH")
            return 0 if present else 1
        adding = args.action == "add"
        changed = (userpath.add_entry if adding else userpath.remove_entry)(directory)
    except OSError as e:
        print(f"Could not change your user PATH: {e}", file=sys.stderr)
        return 1
    if changed:
        print(f"{'Added' if adding else 'Removed'} {directory} {'to' if adding else 'from'} your user PATH.")
    return 0


def cmd_verify_download(args) -> int:
    import hashlib

    from .selfupdate import RELEASE_PUBLIC_KEY_HEX, UpdateError, verify_signature

    sums = Path(args.sums)
    try:
        data = sums.read_bytes()
        sig = sums.with_name(sums.name + ".sig").read_bytes()
        verify_signature(data, sig, RELEASE_PUBLIC_KEY_HEX)
    except OSError as e:
        print(f"Could not read the file: {e}", file=sys.stderr)
        return 2
    except UpdateError as e:
        print(f"NOT VALID: {e}", file=sys.stderr)
        return 1
    print("The checksum list is signed by the Cogitator release key.")
    if args.file:
        target = Path(args.file)
        lines = data.decode("utf-8", "replace").splitlines()
        listed = {p[1].lstrip("*"): p[0].lower() for p in (ln.split() for ln in lines) if len(p) == 2}
        want = listed.get(target.name)
        if want is None:
            print(f"{target.name} is not in the list.", file=sys.stderr)
            return 1
        h = hashlib.sha256()
        with open(target, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            print(f"NOT VALID: {target.name} does not match the signed list. Do not run it.", file=sys.stderr)
            return 1
        print(f"{target.name} matches the signed list.")
    return 0


def _mod_list(game_dir: Path, as_json: bool) -> int:
    reg = Registry.load()
    rows = [{"folder": m.folder, "state": m.state, "present": m.present, "order": m.order,
             "nexus_id": (reg.mod(m.folder) or {}).get("nexus_id"),
             "version": (reg.mod(m.folder) or {}).get("installed_version"),
             "pinned": (reg.mod(m.folder) or {}).get("pinned_version"), "fav": bool((reg.mod(m.folder) or {}).get("fav")),
             "tags": (reg.mod(m.folder) or {}).get("tags") or [], "notes": (reg.mod(m.folder) or {}).get("notes") or "",
             "dev": bool((reg.mod(m.folder) or {}).get("dev"))} for m in scan_mods(game_dir)]
    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            extra = "".join([" *" if r["fav"] else "", f" [pinned {r['pinned']}]" if r["pinned"] else "", " [dev link]" if r["dev"] else "",
                             f" #{' #'.join(r['tags'])}" if r["tags"] else ""])
            print(f"{r['state']:<9} {r['folder']}{'' if r['present'] else '  (folder missing)'}{extra}")
    return 0


def _mod_files(game_dir: Path, folder: str) -> list[str]:
    root = mods_dir(game_dir) / folder
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.is_dir() else []


def _mod_remove(game_dir: Path, args) -> int:
    from .config import cache_dir
    from .mods import remove_mod
    from .procs import darktide_running

    from .fsutil import is_link

    dev = is_link(mods_dir(game_dir) / args.folder)
    files = [] if dev else _mod_files(game_dir, args.folder)
    if dev:
        print(f"'{args.folder}' is a dev link: only the link is removed; your source folder is not touched.")
    else:
        print(f"Remove '{args.folder}' ({len(files)} file(s)):")
    for f in files[:50]:
        print(f"  {f}")
    if len(files) > 50:
        print(f"  ... and {len(files) - 50} more")
    if args.dry_run:
        print("Dry run: nothing changed.")
        return 0
    if darktide_running():
        print("Darktide is running. Close the game first.", file=sys.stderr)
        return 2
    if not _confirm("The folder is moved to Cogitator's removed/ folder (not deleted). Continue?", args.yes):
        print("Cancelled.")
        return 2
    reg = Registry.load()
    snapshots.auto(game_dir, "remove", folders=[args.folder], registry=reg)
    dest = remove_mod(game_dir, args.folder, cache_dir() / "removed")
    from . import integrity

    integrity.forget(args.folder)
    reg.forget_mod(args.folder)
    print("Link removed." if dev else f"Removed. Moved to {dest}")
    return 0


def _mod_meta(game_dir: Path, args) -> int:
    """pin / unpin / note / tag / untag / fav / unfav: small per-mod records kept in the registry."""
    if not args.folder:
        print(f"'mod {args.action}' needs a folder name.", file=sys.stderr)
        return 2
    if not (mods_dir(game_dir) / args.folder).is_dir():
        print(f"No folder '{args.folder}' in {mods_dir(game_dir)}.", file=sys.stderr)
        return 2
    reg = Registry.load()
    rec = reg.mod(args.folder) or {}
    a, rest = args.action, [r for r in (args.rest or []) if r.strip()]
    if a == "pin":
        v = rest[0] if rest else (rec.get("installed_version") or "the installed version")
        reg.set_mod(args.folder, pinned_version=str(v)[:40])
        print(f"{args.folder}: pinned to {v}. Updates are skipped until you run 'mod unpin {args.folder}'.")
    elif a == "unpin":
        reg.set_mod(args.folder, pinned_version=None)
        print(f"{args.folder}: unpinned.")
    elif a == "note":
        reg.set_mod(args.folder, notes=" ".join(rest)[:500])
        print(f"{args.folder}: note " + ("saved." if rest else "cleared."))
    elif a in ("tag", "untag"):
        tags = [t for t in rec.get("tags") or [] if isinstance(t, str)]
        for t in rest:
            t = t.strip().lower()[:30]
            if a == "tag" and t not in tags:
                tags.append(t)
            if a == "untag" and t in tags:
                tags.remove(t)
        reg.set_mod(args.folder, tags=tags)
        print(f"{args.folder}: tags = {', '.join(tags) or '(none)'}")
    else:
        reg.set_mod(args.folder, fav=(a == "fav"))
        print(f"{args.folder}: {'marked as a favourite' if a == 'fav' else 'no longer a favourite'}.")
    return 0


def _mod_rollback(game_dir: Path, folder: str) -> int:
    from .modinstall import rollback_mod

    try:
        print(rollback_mod(folder, game_dir, Registry.load()))
    except ModImportError as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


def _profile_missing_report(profile: dict, missing: list[str]) -> None:
    if not missing:
        return
    print(f"Not installed ({len(missing)}):")
    for n in missing:
        m = profile["mods"].get(n, {})
        link = mod_page_url(m["nexus_id"]) if m.get("nexus_id") else "no Nexus link recorded"
        print(f"  {n}" + (f" v{m['version']}" if m.get("version") else "") + f"  {link}")


def cmd_profile(args) -> int:
    res = _resolve(args)
    a = args.action
    try:
        if a == "list":
            rows = profiles.list_profiles()
            if args.json:
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    print(f"{r['name']}  ({r['enabled']}/{r['mods']} mods enabled)")
                if not rows:
                    print("No profiles yet. Save the current setup with: profile save <name>")
            return 0
        if a == "import":
            if not args.name:
                print("Give a setup code (cog1:...) or a .json file.", file=sys.stderr)
                return 2
            src = Path(args.name)
            try:
                is_file = src.is_file()
            except OSError:
                is_file = False
            prof = profiles.validate(json.loads(src.read_text(encoding="utf-8"))) if is_file else profiles.decode(args.name)
            if args.as_name:
                prof["name"] = profiles.check_name(args.as_name)
            if profiles._path(prof["name"]).exists() and not _confirm(f"A profile named '{prof['name']}' exists. Replace it?", args.yes):
                print("Cancelled.")
                return 2
            profiles.save(prof)
            print(f"Imported profile '{prof['name']}' ({len(prof['entries'])} mods).")
            if res.game_dir:
                _profile_missing_report(prof, profiles.plan_switch(res.game_dir, prof)["missing"])
            return 0
        if a in ("show", "export", "delete") or (a in ("switch", "diff") and args.name):
            prof = profiles.load(args.name) if args.name else None
        if a == "show":
            print(json.dumps(prof, indent=2) if args.json else "\n".join(f"{'on ' if e else 'off'} {n}" for n, e in prof["entries"]))
            return 0
        if a == "export":
            if args.file:
                Path(args.file).write_text(json.dumps(prof, indent=1), encoding="utf-8")
                print(f"Wrote {args.file}")
            else:
                print(profiles.encode(prof))
            return 0
        if a == "delete":
            if not _confirm(f"Delete profile '{args.name}'?", args.yes):
                print("Cancelled.")
                return 2
            profiles.delete(args.name)
            print("Deleted.")
            return 0
        if not res.game_dir:
            print(res.message or "Game not found.", file=sys.stderr)
            return 2
        reg = Registry.load()
        if a == "save":
            if not args.name:
                print("Give a profile name.", file=sys.stderr)
                return 2
            if profiles._path(args.name).exists() and not _confirm(f"A profile named '{args.name}' exists. Replace it?", args.yes):
                print("Cancelled.")
                return 2
            prof = profiles.capture(res.game_dir, reg, args.name)
            profiles.save(prof)
            print(f"Saved profile '{prof['name']}' ({len(prof['entries'])} mods).")
            return 0
        if a == "diff":
            if not args.name:
                print("Give a profile name.", file=sys.stderr)
                return 2
            other = profiles.load(args.other) if args.other else profiles.capture(res.game_dir, reg, "current")
            d = snapshots.diff_states(profiles.as_state(prof), profiles.as_state(other))
            print(json.dumps(d, indent=2) if args.json else "\n".join(snapshots.format_diff(d)))
            return 0 if snapshots.diff_empty(d) else 1
        if a == "switch":
            if not args.name:
                print("Give a profile name.", file=sys.stderr)
                return 2
            plan = profiles.plan_switch(res.game_dir, prof)
            print(f"Profile '{prof['name']}': {len(plan['use'])} mod(s) will be set as listed.")
            if plan["will_disable"]:
                print("These enabled mods are not in the profile and will be disabled (not deleted): " + ", ".join(plan["will_disable"]))
            _profile_missing_report(prof, plan["missing"])
            if args.dry_run:
                print("Dry run: nothing changed.")
                return 0
            if not _confirm("Switch to this profile (a snapshot is taken first)?", args.yes):
                print("Cancelled.")
                return 2
            profiles.switch(res.game_dir, prof, reg)
            print("Switched.")
            return 0
    except (ModError, OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


def cmd_snapshot(args) -> int:
    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    reg = Registry.load()
    try:
        if args.action == "create":
            print(f"Snapshot saved: {snapshots.create(res.game_dir, args.name or 'manual', registry=reg).name}")
        elif args.action == "list":
            rows = snapshots.list_snapshots()
            if args.json:
                print(json.dumps(rows, indent=2))
            for r in ([] if args.json else rows):
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created"] or 0))
                print(f"{r['name']}  {when}  {r['reason']}  ({r['mods']} mods)")
            if not rows and not args.json:
                print("No snapshots yet.")
        elif args.action == "diff":
            if not args.name:
                print("Give a snapshot name (see 'snapshot list').", file=sys.stderr)
                return 2
            a = snapshots.load_manifest(args.name)
            b = snapshots.load_manifest(args.other) if args.other else snapshots.current_state(res.game_dir, reg)
            d = snapshots.diff_states(a, b)
            print(json.dumps(d, indent=2) if args.json else "\n".join(snapshots.format_diff(d)))
            return 0 if snapshots.diff_empty(d) else 1
        elif args.action == "restore":
            if not args.name:
                print("Give a snapshot name (see 'snapshot list').", file=sys.stderr)
                return 2
            from .procs import darktide_running

            if darktide_running():
                print("Darktide is running. Close the game first.", file=sys.stderr)
                return 2
            m = snapshots.load_manifest(args.name)
            print("Changes the restore would make:\n  " + "\n  ".join(snapshots.format_diff(
                snapshots.diff_states(snapshots.current_state(res.game_dir, reg), m))))
            if not _confirm("Restore this snapshot (your current setup is snapshotted first)?", args.yes):
                print("Cancelled.")
                return 2
            print("\n".join(snapshots.restore(res.game_dir, args.name, reg)) or "Nothing to restore.")
        elif args.action == "prune":
            keep = args.keep if args.keep is not None else max(1, Settings.load().snapshot_keep)
            print(f"Removed {snapshots.prune(keep)} old snapshot(s); keeping the newest {keep}.")
    except ModError as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


def cmd_mod(args) -> int:
    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    if args.action == "list":
        return _mod_list(res.game_dir, args.json)
    if args.action == "link-dev":
        from .mods import link_dev_mod

        if not args.folder:
            print("Give the folder you develop in.", file=sys.stderr)
            return 2
        try:
            name = link_dev_mod(res.game_dir, Path(args.folder), args.name)
        except ModError as e:
            print(str(e), file=sys.stderr)
            return 2
        Registry.load().set_mod(name, dev=True, dev_source=str(Path(args.folder).resolve()))
        print(f"{name}: linked to {Path(args.folder).resolve()} and enabled. Removing it later only removes the link.")
        return 0
    if args.action in ("pin", "unpin", "note", "tag", "untag", "fav", "unfav"):
        return _mod_meta(res.game_dir, args)
    if args.action == "rebaseline":
        from . import integrity

        folders = [m.folder for m in scan_mods(res.game_dir) if m.present] if args.all else ([args.folder] if args.folder else [])
        if not folders:
            print("Give a mod folder or --all.", file=sys.stderr)
            return 2
        integrity.record(res.game_dir, folders, Registry.load())
        print(f"Baseline updated for {len(folders)} mod(s): their current files are now the trusted state.")
        return 0
    if not args.folder:
        print(f"'mod {args.action}' needs a folder name.", file=sys.stderr)
        return 2
    try:
        if args.action == "remove":
            return _mod_remove(res.game_dir, args)
        if args.action == "rollback":
            return _mod_rollback(res.game_dir, args.folder)
        if args.action in ("up", "down"):
            snapshots.auto(res.game_dir, "reorder", once=True, registry=Registry.load())
            print(move_mod(res.game_dir, args.folder, -1 if args.action == "up" else 1))
        else:
            print(set_mod_enabled(res.game_dir, args.folder, args.action == "enable"))
    except ModError as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


def cmd_toggle(args) -> int:
    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    reg = Registry.load()
    try:
        r = toggle_mods(res.game_dir, reg, confirm_unpatch=lambda: _confirm("Disable all mods (un-patch the game)?", args.yes))
    except PatcherError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(f"{r.message}  MODS: {patch_state(res.game_dir)}")
    return 0 if r.changed or r.state == ON else 1


def _installed_folders(game_dir: Path) -> list[str]:
    return [m.folder for m in scan_mods(game_dir) if m.present]


def cmd_check_updates(args) -> int:
    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    s = Settings.load()
    reg = Registry.load()
    key = get_api_key()
    b = run_checks(_installed_folders(res.game_dir), reg, key, ttl_s=s.update_ttl_minutes * 60.0, force=args.force)
    fw, r, live, premium, quota = b.frameworks, b.mods, b.live, b.premium, b.quota
    out = {
        "premium": premium, "quota": quota, "requests": b.requests,
        "from_cache": not live, "rate_limited": r.rate_limited, "error": r.error or None,
        "frameworks": [{"kind": u.kind, "status": u.status, "installed": u.installed, "latest": u.latest, "reason": u.reason} for u in fw],
        "mods": [{"folder": x.folder, "nexus_id": x.nexus_id, "status": x.status, "installed": x.installed,
                  "latest": x.latest, "reason": x.reason} for x in r.rows],
    }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        acct = "unknown" if premium is None else ("premium" if premium else "free")
        src = f"live, {out['requests']} request(s)" if live else "cache (TTL)"
        print(f"Nexus: {'key set' if key else 'NO KEY (run set-key)'}, account: {acct}, {src}" + (f", quota: {quota}" if quota else ""))
        if r.rate_limited:
            print(f"!! Rate limited - showing cached results. {r.error}")
        elif r.error:
            print(f"!! {r.error}")
        for u in fw:
            print(f"  {u.kind.upper():<4} {u.status}  installed={u.installed or 'unknown'}  latest={u.latest or '-'}  ({u.reason})")
        for x in r.rows:
            print(f"  {x.folder:<28} {x.status:<16} {x.installed or '-'} -> {x.latest or '-'}  ({x.reason})")
    bad = any(u.status == "check failed" for u in fw) or any(x.status == "check failed" for x in r.rows)
    upd = any(u.status == "update available" for u in fw) or any(x.status == "update available" for x in r.rows)
    if not key and (fw or any(x.nexus_id for x in r.rows)):
        print("No Nexus API key set. Run `cogitator set-key` (or set COGITATOR_NEXUS_KEY).", file=sys.stderr)
        return 2
    return 1 if (bad or upd or r.rate_limited) else 0


def _version_key(v: str) -> tuple:
    import re

    return tuple(int(x) if x.isdigit() else x for x in re.split(r"[.\-_ ]+", v.strip().lstrip("vV")) if x)


def _newer(versions: list[str], installed: str | None) -> list[str]:
    ordered = sorted(versions, key=_version_key, reverse=True)
    if not installed:
        return ordered[:3]
    try:
        return [v for v in ordered if _version_key(v) > _version_key(installed)] or ordered[:1]
    except TypeError:  # mixed number/text parts that cannot be compared
        return ordered[:3]


def _print_changelog(nx: NexusClient, mod_id: int, installed: str | None, everything: bool = False) -> bool:
    log = nx.changelogs(mod_id)
    if not log:
        print("(No changelog published on Nexus for this mod.)")
        return False
    for v in (sorted(log, key=_version_key, reverse=True) if everything else _newer(list(log), installed)):
        print(f"  v{v}")
        for line in log[v]:
            print(f"    - {line}")
    return True


def cmd_changelog(args) -> int:
    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    rec = Registry.load().mod(args.folder) or {}
    if not rec.get("nexus_id"):
        print(f"'{args.folder}' is not linked to Nexus (see 'link').", file=sys.stderr)
        return 2
    key = get_api_key()
    if not key:
        print("A Nexus API key is needed (run 'set-key').", file=sys.stderr)
        return 2
    try:
        with NexusClient(key) as nx:
            print(f"Changelog for {rec.get('name') or args.folder} (installed: {rec.get('installed_version') or 'unknown'})")
            _print_changelog(nx, int(rec["nexus_id"]), rec.get("installed_version"), args.all)
    except NexusError as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


def cmd_launch(args) -> int:
    from .launch import LaunchError, launch

    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    try:
        r = launch(res.game_dir, Registry.load(), profile=args.profile, vanilla=args.vanilla, force=args.force)
    except (LaunchError, ModError) as e:
        print(str(e), file=sys.stderr)
        return 2
    for n in r.notes:
        print(n)
    print("Asked Steam to start Darktide.")
    return 0


def cmd_bisect(args) -> int:
    from . import bisect

    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    try:
        if args.action == "start":
            print("Make sure the problem happens with all your enabled mods on. A snapshot is taken; 'bisect reset' restores it.")
            s = bisect.start(res.game_dir, Registry.load())
        elif args.action in ("good", "bad"):
            s = bisect.answer(res.game_dir, still_broken=args.action == "bad")
        elif args.action == "status":
            s = bisect.load()
            if not s:
                print("No bisect session is running.")
                return 0
        else:
            print("\n".join(bisect.reset(res.game_dir, Registry.load())))
            return 0
    except ModError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(bisect.describe(s))
    return 0


def cmd_crashlog(args) -> int:
    from . import crashlog, gamefinder
    from .doctor import console_log_dirs
    from .loadorder import LoadOrder
    from .mods import load_order_path

    res = _resolve(args)
    log = crashlog.newest_log(console_log_dirs(gamefinder.steam_roots()))
    installed = {m.folder for m in scan_mods(res.game_dir)} if res.game_dir else None
    rep = crashlog.analyze(log, installed) if log else crashlog.CrashReport()
    if args.json:
        print(json.dumps({"log": str(rep.log) if rep.log else None, "suspects": rep.suspects, "error_lines": rep.error_lines,
                          "last_loaded": rep.last_loaded}, indent=2))
        return 0
    lo = LoadOrder.load(load_order_path(res.game_dir)).entries() if res.game_dir else []
    print("\n".join(crashlog.format_report(rep, lo)))
    return 0


def cmd_nxm(args) -> int:
    from . import nxm

    if args.action == "handle":
        return nxm.handle_click(args.url or "")
    if os.name != "nt":
        print("nxm:// registration is only available on Windows.", file=sys.stderr)
        return 2
    print({"register": nxm.register, "unregister": nxm.unregister, "status": nxm.status}[args.action]())
    return 0


def cmd_search(args) -> int:
    key = get_api_key()
    if not key:
        print("A Nexus API key is needed (run 'set-key').", file=sys.stderr)
        return 2
    try:
        with NexusClient(key) as nx:
            rows = nx.search_mods(" ".join(args.text), count=10)
    except NexusError as e:
        print(str(e), file=sys.stderr)
        return 2
    for r in rows:
        print(f"{r['mod_id']:>7}  {r['name']}  https://www.nexusmods.com/warhammer40kdarktide/mods/{r['mod_id']}")
    if not rows:
        print("No mods found.")
    elif len(rows) == 10:
        print("(first 10 results; refine the search to narrow it)")
    return 0


def cmd_diag(args) -> int:
    from . import diag

    res = _resolve(args)
    path, sizes = diag.build(res.game_dir, Registry.load())
    print("Diagnostics bundle (home folder and user name replaced, secrets removed; nothing was uploaded):")
    for name, size in sizes.items():
        print(f"  {name}  ({size:,} bytes)")
    print(f"\nWritten to: {path}\nAttach it to your bug report if you are comfortable with its contents.")
    return 0


def cmd_doctor(args) -> int:
    if args.history is not None:
        from .doctor import read_history

        for r in read_history(args.history):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("t", 0)))
            probs = ", ".join(f"{k}={v}" for k, v in (r.get("problems") or {}).items()) or "all ok"
            print(f"{when}  exit {r.get('exit')}  {probs}")
        return 0
    checks = run_doctor(args.game_dir, offline=args.offline)
    from .doctor import record_history

    record_history(checks)
    if args.json:
        print(json.dumps({"exit_code": doctor_exit_code(checks), "checks": doctor_json(checks)}, indent=2))
    else:
        print(format_report(checks))
    return doctor_exit_code(checks)


def cmd_uninstall_framework(args) -> int:
    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    reg = Registry.load()
    plan = UninstallPlan(res.game_dir, remove_dml=args.only in (None, "dml"), remove_dmf=args.only in (None, "dmf"))
    names = [n for n, on in (("Darktide Mod Loader", plan.remove_dml), ("Darktide Mod Framework", plan.remove_dmf)) if on]
    print(f"This will remove: {', '.join(names)}.")
    if plan.remove_dml:
        print("Mods will be switched off first (the game is restored to its unmodded state) and checked before anything is deleted.")
    print("Your mods and mod_load_order.txt are not touched.")
    if not _confirm("Continue?", args.yes):
        print("Cancelled.")
        return 2
    r = uninstall_framework(plan, registry=reg)
    if not r.ok:
        print("\n".join([r.what, r.why, f"Next step: {r.fix}"]), file=sys.stderr)
        return 2
    print(f"Removed {len(r.removed)} item(s)." + (" Mods are switched off." if r.unpatched or plan.remove_dml else ""))
    for w in r.warnings:
        print(f"Note: {w}")
    print(f"Your mods and mod_load_order.txt were not touched. Rollback copies: {r.rollback_dir}")
    return 0


def cmd_unlink(args) -> int:
    reg = Registry.load()
    if reg.mod(args.folder) is None:
        print(f"'{args.folder}' has no link in registry.json.", file=sys.stderr)
        return 2
    reg.forget_mod(args.folder)
    print(f"Unlinked '{args.folder}'. The mod folder itself was not touched.")
    return 0


def _ask_yn(prompt: str, default: bool) -> bool:
    if not sys.stdin.isatty():
        return default
    a = input(f"{prompt} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not a else a in ("y", "yes")


def _choose(question: str, options: list[str]) -> int | None:
    print(question)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    while True:
        a = input("Choose [number, blank = cancel]: ").strip()
        if not a:
            return None
        if a.isdigit() and 1 <= int(a) <= len(options):
            return int(a) - 1


def cmd_link(args) -> int:
    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    if args.folder in INTERNAL_FOLDERS or not (mods_dir(res.game_dir) / args.folder).is_dir():
        print(f"No manageable mod folder '{args.folder}' in {mods_dir(res.game_dir)}.", file=sys.stderr)
        return 2
    mod_id, problem = parse_nexus_ref(args.ref)
    if mod_id is None:
        print(problem, file=sys.stderr)
        return 2
    key = get_api_key()
    if not key:
        print("Linking validates the mod through the API: run `cogitator set-key` first.", file=sys.stderr)
        return 2
    reg = Registry.load()
    try:
        with NexusClient(key) as nx:
            info = nx.mod(mod_id)
            files = nx.files(mod_id)
    except NexusError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    name = info.get("name") or f"mod {mod_id}"
    latest = latest_main_file(files)
    print(f"Nexus mod {mod_id}: {name}  (latest: {latest.version if latest else 'n/a'})")
    if not (args.yes or _ask_yn(f"Link '{args.folder}' to this mod?", False)):
        print("Cancelled.")
        return 2
    if args.installed_file is not None:
        baseline: str | int = args.installed_file
    elif args.no_baseline or latest is None:
        baseline = "none"
    elif args.yes or _ask_yn(f"Mark current ({latest.version}, file {latest.file_id}) as installed? Only newer uploads will be flagged", True):
        baseline = "current"
    else:
        baseline = "none"
    try:
        rec = link_mod(reg, args.folder, mod_id, name, files, via="manual", baseline=baseline)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if rec["installed_file_id"] is None:
        print(f"Linked. Baseline unknown: '{args.folder}' will show 'unknown', never 'up to date', until you set it.")
    else:
        print(f"Linked. Installed file recorded: {rec['installed_file_id']} (v{rec['installed_version']}, {rec['baseline']}).")
    return 0


def cmd_import(args) -> int:
    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    zp = Path(args.archive)
    if not zp.is_file():
        print(f"No such file: {zp}", file=sys.stderr)
        return 2
    s = Settings.load()
    reg = Registry.load()
    try:
        info = inspect_mod_zip(zp)
    except ModImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    parsed = parse_archive_name(zp.name)
    interactive = sys.stdin.isatty()
    mod_id = args.mod_id
    if mod_id is None and parsed.mod_id is None and interactive and len(info.folders) >= 1:
        ref = input(f"'{zp.name}' does not say which Nexus mod it is. Nexus URL or id (blank = install without linking): ").strip()
        if ref:
            mod_id, problem = parse_nexus_ref(ref)
            if mod_id is None:
                print(problem, file=sys.stderr)
                return 2
    print(f"Archive : {zp.name}\nFolders : {', '.join(info.folders)}\nTarget  : {mods_dir(res.game_dir)}")
    if args.dry_run:
        for f in info.folders:
            have = _mod_files(res.game_dir, f)
            old = (reg.mod(f) or {}).get("installed_version")
            print(f"  {f}: " + (f"OVERWRITE existing folder ({len(have)} files" + (f", installed v{old}" if old else "") + ")" if have else "new folder"))
        print("Dry run: nothing changed.")
        return 0
    if not _confirm("Install this mod?", args.yes):
        print("Cancelled.")
        return 2
    key = get_api_key()
    nx = NexusClient(key) if key else None
    if nx is None:
        print("(No Nexus API key: the installed file cannot be identified yet; status will stay 'unknown'.)")
    try:
        r = import_archive(zp, res.game_dir, reg, nx, choose=_choose if interactive else None, mod_id=mod_id,
                           max_bytes=s.max_extract_mb * 1024 * 1024, max_files=s.max_extract_files)
    except (ModImportError, LoaderError, NexusError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    finally:
        if nx:
            nx.close()
    print(f"Installed: {', '.join(r.folders)}" + (f"; added to load order: {', '.join(r.added_to_load_order)}" if r.added_to_load_order else "")
          + f"\nRollback copies: {r.backup_dir}")
    if r.linked:
        print(f"Linked '{r.linked}' to Nexus mod; file {r.file_id or 'unidentified'}" + (f" (v{r.version})" if r.version else ""))
    for n in r.notes:
        print(f"Note: {n}")
    return 0


def cmd_update(args) -> int:
    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    s = Settings.load()
    reg = Registry.load()
    folders = _installed_folders(res.game_dir)
    rows = {r.folder: r for r in cached_rows(folders, reg)}
    if args.all:
        targets = [f for f, r in rows.items() if r.status == "update available"]
    elif args.folder:
        if args.folder not in rows:
            print(f"No installed mod '{args.folder}'.", file=sys.stderr)
            return 2
        targets = [args.folder]
    else:
        print("Give a mod folder or --all.", file=sys.stderr)
        return 2
    if not targets:
        print("No mods with an update available (run `check-updates` first).")
        return 0
    if args.dry_run:
        for f in targets:
            lat = (reg.mod(f) or {}).get("latest") or {}
            print(f"{f}: {rows[f].status} - would install {lat.get('file_name') or 'the latest main file'}"
                  f" (v{lat.get('version') or '?'}), replacing v{(reg.mod(f) or {}).get('installed_version') or '?'}")
        print("Dry run: nothing downloaded or changed.")
        return 0
    premium = bool(reg.data["updates"].get("premium"))
    key = get_api_key()
    rc = 0
    if premium and key:
        with NexusClient(key) as nx:
            for f in targets:
                if rows[f].status != "update available":
                    print(f"{f}: status is '{rows[f].status}' - nothing to download.")
                    continue
                if not _confirm(f"Download and install the update for {f} ({rows[f].reason})?", args.yes):
                    continue
                try:
                    r = apply_update_premium(f, res.game_dir, reg, nx, premium=True,
                                             max_bytes=s.max_extract_mb * 1024 * 1024, max_files=s.max_extract_files)
                    print(f"{f}: updated to {r.version or r.file_id}. Rollback copies: {r.backup_dir}")
                except (ModImportError, LoaderError, NexusError) as e:
                    print(f"{f}: {e}", file=sys.stderr)
                    rc = 2
        return rc
    # free account: guided manual download
    if args.all:
        print("Free account: open each page, download the file, then `cogitator import <zip>`:")
        for f in targets:
            print(f"  {f}: {mod_page_url(rows[f].nexus_id)}")
        return 0
    f = targets[0]
    rec = reg.mod(f) or {}
    latest = rec.get("latest") or {}
    if not rec.get("nexus_id") or not latest:
        print(f"{f}: not linked or not checked yet; run check-updates.", file=sys.stderr)
        return 2
    if reg.data["updates"].get("premium") is None:
        print("(Account type unknown; run check-updates once. Treating as free.)")
    url = mod_page_url(rec["nexus_id"])
    dl = Path(s.downloads_dir) if s.downloads_dir else Path.home() / "Downloads"
    since = time.time()
    if not args.no_browser:
        open_files_page(rec["nexus_id"])
    print(f"Files page: {url}\nDownload '{latest.get('file_name')}' (MAIN file). Watching {dl} for up to {args.wait}s ... (Ctrl-C to stop)")
    try:
        found = wait_for_download(dl, rec["nexus_id"], latest, since=since, timeout_s=args.wait) if args.wait > 0 else None
    except KeyboardInterrupt:
        found = None
    if not found:
        print(f"No matching download seen. When it finishes: cogitator import \"<path to zip>\"")
        return 1
    print(f"Found: {found}")
    ns = argparse.Namespace(**{**vars(args), "archive": str(found), "mod_id": rec["nexus_id"]})
    return cmd_import(ns)


def _where(where: str) -> str:
    return ("in the Windows credential store" if where == "keyring"
            else "in a plain-text file in your settings folder (the Windows credential store was not available)")


def cmd_set_key(args) -> int:
    import getpass

    key = getpass.getpass("Nexus API key (hidden): ").strip()
    if not key:
        print("No key entered.", file=sys.stderr)
        return 2
    try:
        with NexusClient(key) as nx:
            who = nx.validate()
    except NexusError as e:
        print(f"Key not saved: {e}", file=sys.stderr)
        return 2
    where = set_api_key(key)
    print(f"Key valid ({'premium' if who.get('is_premium') else 'free'}); stored {_where(where)}.")
    return 0


def _confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print("Confirmation required: re-run interactively or pass --yes.", file=sys.stderr)
        return False
    return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")


def cmd_install_framework(args) -> int:
    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    game = res.game_dir
    if not args.zip and not args.from_github:
        print("Provide the Nexus archives:  cogitator install-framework --zip <DML.zip> --zip <DMF.zip>")
        for k, u in NEXUS_PAGES.items():
            print(f"  {k.upper()} download page: {u}")
        print("Or opt in to GitHub (unreleased/source archives; the source is shown before anything is installed):")
        print("  cogitator install-framework --from-github")
        return 2
    s = Settings.load()
    reg = Registry.load()
    wd_ctx = tempfile.TemporaryDirectory(prefix="cogitator-gh-")
    try:
        plans: list[Plan] = []
        for z in args.zip:
            plans.append(plan_local(Path(z)))
        if len({p.component for p in plans}) != len(plans):
            print("Two archives for the same component were given.", file=sys.stderr)
            return 2
        if args.from_github:
            comps = [args.only] if args.only else ["dml", "dmf"]
            have = {p.component for p in plans}
            comps = [c for c in comps if c not in have and (args.update or detect_framework(game, c).status != "installed")]
            with make_client(github_only=True) as client:
                for c in comps:
                    plans.append(plan_github(c, client, Path(wd_ctx.name)))
        if not plans:
            print("Nothing to install (already installed; use --update to reinstall).")
            return 0
        plans.sort(key=lambda p: p.component != "dml")  # DML first
        for p in plans:
            print(describe_plan(p) + "\n")
        if args.dry_run:
            print(f"Dry run: nothing changed. Target would be: {game}")
            return 0
        print(f"Target game folder: {game}")
        for p in plans:
            if not _confirm(f"Install {p.component.upper()} from the source above?", args.yes):
                print("Cancelled.")
                return 2
            fi = install_framework_plan(p, game, reg, max_bytes=s.max_extract_mb * 1024 * 1024, max_files=s.max_extract_files)
            r = fi.result
            kept = f"; kept existing {', '.join(r.kept)}" if r.kept else ""
            print(f"{p.component.upper()} installed: {r.written} files{kept}. Rollback copies: {r.backup_dir}")
            for n in fi.notes:
                print(n)
        if patch_state(game) != ON:
            print("Mods are not active until the game is patched (cogitator toggle).")
        return 0
    except (LoaderError, PatcherError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    finally:
        wd_ctx.cleanup()


def cmd_self_update(args) -> int:
    from .config import Settings
    from .registry import Registry
    from .selfupdate import SelfUpdater, UpdateError

    settings = Settings.load()
    if args.channel and args.channel != settings.update_channel:
        settings.update_channel = args.channel
        settings.save()
    u = SelfUpdater(Registry.load(), settings)
    try:
        if args.rollback:
            u.rollback()
            print("Going back to the previous version; Cogitator will restart.")
            return 0
        if args.apply:
            rd = u.apply()
            print(f"Installing {rd.version}; Cogitator will restart.")
            return 0
        res = u.check(force=True)
        if res.status == "disabled":
            print(res.detail)
            return 1
        if res.status in ("offline", "rate-limited", "error"):
            print(res.detail or "Could not check for updates right now.")
            return 1
        if not res.latest:
            print("Cogitator is up to date.")
            return 0
        print(f"Update available: v{res.latest.version}")
        if res.latest.notes:
            print(res.latest.notes)
        if args.check:
            return 0
        if not u.layout.in_place:
            print(u.layout.guidance())
            return 0
        rd = u.stage(res.latest)
        print(f"Downloaded and verified v{rd.version}. Run  cogitator self-update --apply  to install it.")
        return 0
    except UpdateError as e:
        print(str(e), file=sys.stderr)
        return 1


def _pause_if_console(argv) -> None:
    """Started from a shortcut the console closes with the process: keep it open so the message can be read."""
    try:
        if argv is None and len(sys.argv) == 1 and sys.stdin and sys.stdin.isatty() and sys.stdout.isatty():
            input("Press Enter to close this window. ")
    except (EOFError, OSError):
        pass


def _early_instance(argv: list[str] | None) -> None:
    """--instance must be known before logging starts (the log file lives in the instance's cache folder)."""
    a = list(sys.argv[1:] if argv is None else argv)
    for i, x in enumerate(a):
        if x == "--instance" and i + 1 < len(a):
            os.environ["COGITATOR_INSTANCE"] = a[i + 1]
        elif x.startswith("--instance="):
            os.environ["COGITATOR_INSTANCE"] = x.split("=", 1)[1]


def main(argv: list[str] | None = None) -> int:
    try:
        _early_instance(argv)
        setup_logging()
        args = build_parser().parse_args(argv)
        if args.self_check:
            if args.expect_version and args.expect_version != __version__:
                print(f"FAIL  version: this build is {__version__}, expected {args.expect_version}", file=sys.stderr)
                return 1
            from .selfcheck import run as self_check

            return self_check()
        check_data_versions()  # an older app must not touch files written by a newer one
        return _dispatch(args)
    except SchemaTooNew as e:
        print(str(e), file=sys.stderr)
        _pause_if_console(argv)
        return 2
    except Exception as e:  # last resort: say what happened and where the log is, instead of a vanishing window
        import logging

        from .config import log_path, redact

        logging.getLogger(__name__).exception("unhandled error")
        print(f"Cogitator stopped because of an unexpected error: {redact(f'{type(e).__name__}: {e}')}\n"
              f"Details are in {log_path()}. Please include that file when you report the problem.", file=sys.stderr)
        _pause_if_console(argv)
        return 3


def _dispatch(args) -> int:
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "unlink":
        return cmd_unlink(args)
    if args.cmd == "uninstall-framework":
        return cmd_uninstall_framework(args)
    if args.cmd == "toggle":
        return cmd_toggle(args)
    if args.cmd == "check-updates":
        return cmd_check_updates(args)
    if args.cmd == "set-key":
        return cmd_set_key(args)
    if args.cmd == "link":
        return cmd_link(args)
    if args.cmd == "import":
        return cmd_import(args)
    if args.cmd == "update":
        return cmd_update(args)
    if args.cmd == "install-framework":
        return cmd_install_framework(args)
    if args.cmd == "profile":
        return cmd_profile(args)
    if args.cmd == "launch":
        return cmd_launch(args)
    if args.cmd == "bisect":
        return cmd_bisect(args)
    if args.cmd == "crashlog":
        return cmd_crashlog(args)
    if args.cmd == "changelog":
        return cmd_changelog(args)
    if args.cmd == "nxm":
        return cmd_nxm(args)
    if args.cmd == "search":
        return cmd_search(args)
    if args.cmd == "diag":
        return cmd_diag(args)
    if args.cmd == "snapshot":
        return cmd_snapshot(args)
    if args.cmd == "mod":
        return cmd_mod(args)
    if args.cmd == "restore-database":
        return cmd_restore_database(args)
    if args.cmd == "verify-download":
        return cmd_verify_download(args)
    if args.cmd == "self-update":
        return cmd_self_update(args)
    if args.cmd == "path":
        return cmd_path(args)
    if args.cmd is None:
        from .tui.app import run_tui  # imported lazily: headless commands must not pay for Textual

        return run_tui(game_override=args.game_dir, check_updates=not args.no_update_check)
    return 2


if __name__ == "__main__":
    sys.exit(main())
