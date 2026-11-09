"""Headless CLI. Subcommands are added as features land."""
from __future__ import annotations

import argparse
import json
import sys

import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .config import Settings, get_api_key, set_api_key, setup_logging
from .doctor import exit_code as doctor_exit_code, format_report, run_doctor, to_json as doctor_json
from .frameworks import Plan, describe_plan, detect_framework, plan_github, plan_local
from .fwupdate import install_framework_plan
from .uninstall import UninstallPlan, uninstall_framework
from .gamefinder import Resolution, resolve_game_dir
from .loader import NEXUS_PAGES, LoaderError, make_client
from .mods import ModError, scan_mods, set_mod_enabled
from .nexus import NexusClient, NexusError
from .patcher import ON, PatcherError, banner, patch_state
from .patcher import toggle as toggle_mods
from .registry import Registry
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
    p.add_argument("--no-update-check", action="store_true", help="TUI: do not check for updates on start")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)  # verifies a pipx/PyInstaller build
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
    up = sub.add_parser("update", help="update a linked mod (Premium: automatic; free: browser + import)")
    up.add_argument("folder", nargs="?")
    up.add_argument("--all", action="store_true", help="update every mod with an update available")
    up.add_argument("--wait", type=int, default=180, metavar="SECONDS", help="free accounts: how long to watch Downloads")
    up.add_argument("--no-browser", action="store_true")
    up.add_argument("--yes", "-y", action="store_true")
    dr = sub.add_parser("doctor", help="diagnose the install and print fixes (exit 0 ok, 1 warnings, 2 failures)")
    dr.add_argument("--json", action="store_true")
    dr.add_argument("--offline", action="store_true", help="skip the Nexus and GitHub network checks")
    uf = sub.add_parser("uninstall-framework", help="remove DML/DMF and switch mods off (your mods and load order are kept)")
    uf.add_argument("--only", choices=["dml", "dmf"], help="remove just one of them")
    uf.add_argument("--yes", "-y", action="store_true", help="skip the confirmation")
    ul = sub.add_parser("unlink", help="forget the Nexus link of a mod folder (does not touch the mod itself)")
    ul.add_argument("folder")
    mod = sub.add_parser("mod", help="enable/disable a mod")
    mod.add_argument("action", choices=["enable", "disable"])
    mod.add_argument("folder")
    return p


def _resolve(args) -> Resolution:
    s = Settings.load()
    res = resolve_game_dir(args.game_dir, s.game_dir)
    if res.game_dir and res.source in ("steam", "override") and str(res.game_dir) != s.game_dir:
        s.game_dir = str(res.game_dir)
        s.save()
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


def cmd_mod(args) -> int:
    res = _resolve(args)
    if not res.game_dir:
        print(res.message or "Game not found.", file=sys.stderr)
        return 2
    try:
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


def cmd_doctor(args) -> int:
    checks = run_doctor(args.game_dir, offline=args.offline)
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
    print(f"Key valid ({'premium' if who.get('is_premium') else 'free'}); stored in {where}.")
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
            with make_client() as client:
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


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    if args.self_check:
        from .selfcheck import run as self_check

        return self_check()
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
    if args.cmd == "mod":
        return cmd_mod(args)
    if args.cmd is None:
        from .tui.app import run_tui  # imported lazily: headless commands must not pay for Textual

        return run_tui(game_override=args.game_dir, check_updates=not args.no_update_check)
    return 2


if __name__ == "__main__":
    sys.exit(main())
