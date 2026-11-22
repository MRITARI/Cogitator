"""Interface-independent flows for setups and housekeeping: profiles, snapshots, bulk actions on selected mods, bisect, crash-log
leads, game-update repair and Cogitator's own update texts. Same contract as `actions`: talk to the person through a `Ui`, run on
a worker thread, return an `Outcome`. Messages that are only text come back as (text, rich-style) pairs so any interface can show
them."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import bisect, crashlog, gamefinder, profiles, snapshots
from .actions import DIM, ERR, OK, WARN, Ctx, Outcome, Ui, remove_one
from .config import config_dir
from .doctor import console_log_dirs
from .loadorder import LoadOrder
from .modinstall import mod_page_url
from .mods import ModError, load_order_path, set_mod_enabled
from .patcher import PatcherError, patch

Lines = list[tuple[str, str]]


# ---------------------------------------------------------------- profiles

def profile_labels(rows: list[dict]) -> list[str]:
    return [f"{r['name']}  ({r['enabled']}/{r['mods']} on)" for r in rows]


def switch_body(plan: dict) -> str:
    body = f"{len(plan['use'])} mod(s) are set as the profile lists them."
    if plan["will_disable"]:
        body += "\nDisabled (not deleted): " + ", ".join(plan["will_disable"])
    if plan["missing"]:
        body += "\nNot installed: " + ", ".join(plan["missing"])
    return body + "\nA snapshot is taken first."


def prepare_switch(ctx: Ctx, name: str) -> tuple[dict, str]:
    """(profile, confirmation text). Raises ModError / OSError for a missing or unreadable profile."""
    prof = profiles.load(name)
    return prof, switch_body(profiles.plan_switch(ctx.game_dir, prof))


def switch_profile(ctx: Ctx, ui: Ui, prof: dict) -> Outcome:
    try:
        with ctx.lock:
            plan = profiles.switch(ctx.game_dir, prof, ctx.registry)
        ui.log(f"Switched to profile '{prof['name']}'.", OK)
        for n in plan["missing"]:
            ui.log(f"  not installed: {n}", WARN)
    except (ModError, OSError) as e:
        ui.log(f"Could not switch profile: {e}", ERR)
    return Outcome(refresh=True)


def profile_exists(name: str) -> bool:
    try:
        return profiles._path(name).exists()
    except ModError:
        return False


def save_profile(ctx: Ctx, name: str) -> Lines:
    try:
        prof = profiles.capture(ctx.game_dir, ctx.registry, name)
        profiles.save(prof)
        return [(f"Saved profile '{prof['name']}' ({len(prof['entries'])} mods).", OK)]
    except (ModError, OSError) as e:
        return [(str(e), ERR)]


def share_code(ctx: Ctx) -> str:
    """The cog1: code for the current setup. Raises ModError / OSError."""
    return profiles.encode(profiles.capture(ctx.game_dir, ctx.registry, "shared"))


def import_code(ctx: Ctx, code: str) -> Lines:
    try:
        prof = profiles.decode(code)
        profiles.save(prof)
        out: Lines = [(f"Imported profile '{prof['name']}' ({len(prof['entries'])} mods). Open Profiles to switch to it.", OK)]
        for n in profiles.plan_switch(ctx.game_dir, prof)["missing"]:
            m = prof["mods"].get(n, {})
            out.append((f"  not installed: {n}" + (f"  {mod_page_url(m['nexus_id'])}" if m.get("nexus_id") else ""), WARN))
        return out
    except (ModError, OSError) as e:
        return [(str(e), ERR)]


def delete_profile(name: str) -> Lines:
    try:
        profiles.delete(name)
        return [(f"Deleted profile '{name}'.", OK)]
    except (ModError, OSError) as e:
        return [(str(e), ERR)]


# ---------------------------------------------------------------- snapshots

def snapshot_label(r: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created"] or 0))
    return f"{when}  {r['reason']}  ({r['mods']} mods)"


def create_snapshot(ctx: Ctx, reason: str = "manual") -> Lines:
    try:
        with ctx.lock:
            return [(f"Snapshot saved: {snapshots.create(ctx.game_dir, reason or 'manual', registry=ctx.registry).name}", OK)]
    except (ModError, OSError) as e:
        return [(f"Could not save a snapshot: {e}", ERR)]


def restore_snapshot(ctx: Ctx, ui: Ui, name: str) -> Outcome | None:
    """Same rules as `cogitator snapshot restore`: the game must be closed, the changes are shown first, and the current setup
    is snapshotted before anything is replaced."""
    if ctx.game_running():
        ui.log("Darktide is running. Close the game first.", "bold yellow")
        return None
    try:
        m = snapshots.load_manifest(name)
        diff = snapshots.format_diff(snapshots.diff_states(snapshots.current_state(ctx.game_dir, ctx.registry), m))
    except (ModError, OSError, ValueError) as e:
        ui.log(f"Could not read snapshot: {e}", ERR)
        return None
    if not ui.confirm("Restore this snapshot?", "Changes the restore would make:\n  " + "\n  ".join(diff)
                      + "\n\nYour current setup is snapshotted first.", default_yes=False):
        return None
    try:
        with ctx.lock:
            msgs = snapshots.restore(ctx.game_dir, name, ctx.registry)
    except (ModError, OSError) as e:
        ui.log(f"Restore failed: {e}", ERR)
        return Outcome(refresh=True)
    ui.log("; ".join(msgs) or "Nothing to restore.", OK)
    return Outcome(refresh=True)


def prune_snapshots(keep: int) -> Lines:
    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
        return [("Keep must be a whole number of at least 1.", ERR)]
    return [(f"Removed {snapshots.prune(keep)} old snapshot(s); keeping the newest {keep}.", OK)]


# ---------------------------------------------------------------- bulk actions on selected mods

def bulk_set_enabled(ctx: Ctx, folders: list[str], enable: bool) -> Lines:
    snapshots.auto(ctx.game_dir, "bulk", registry=ctx.registry, once=True)
    out: Lines = []
    done = 0
    for f in folders:
        try:
            set_mod_enabled(ctx.game_dir, f, enable)
            done += 1
        except (ModError, OSError) as e:
            out.append((str(e), ERR))
    out.append((f"{'Enabled' if enable else 'Disabled'} {done} mod(s).", OK))
    return out


def remove_many(ctx: Ctx, ui: Ui, folders: list[str]) -> Outcome:
    for f in folders:
        remove_one(ctx, ui, f)
    return Outcome(refresh=True)


# ---------------------------------------------------------------- bisect

BISECT_INTRO = ("Cogitator will switch half of your enabled mods off, you start the game and see whether the problem is still "
                "there, and it narrows the list down. A snapshot is taken first and you can restore your load order at any time. "
                "Make sure the problem happens with all mods on.")
BISECT_OPTIONS = ["The problem still happens", "The problem is gone", "Stop and restore my load order", "Close"]
BISECT_WHAT = ("bad", "good", "reset")


@dataclass
class BisectResult:
    lines: Lines = field(default_factory=list)
    refresh: bool = False
    ok: bool = True


def bisect_step(ctx: Ctx, what: str) -> BisectResult:
    """what: start | bad | good | reset."""
    try:
        with ctx.lock:
            if what == "start":
                s = bisect.start(ctx.game_dir, ctx.registry)
            elif what == "reset":
                return BisectResult([(line, OK) for line in bisect.reset(ctx.game_dir, ctx.registry)], refresh=True)
            else:
                s = bisect.answer(ctx.game_dir, still_broken=what == "bad")
    except (ModError, OSError) as e:
        return BisectResult([(str(e), ERR)], ok=False)
    return BisectResult([(line, "bold" if s.get("culprit") else "") for line in bisect.describe(s).splitlines()], refresh=True)


def bisect_flow(ctx: Ctx, ui: Ui) -> Outcome | None:
    """One turn of the wizard (the state lives in a file, so the person plays the game between turns): start a session, or
    answer the pending question, or stop and restore."""
    s = bisect.load()
    if s is None:
        if not ui.confirm("Find a crashing mod?", BISECT_INTRO, default_yes=False):
            return None
        what = "start"
    else:
        i = ui.choose(bisect.describe(s).splitlines()[0], BISECT_OPTIONS)
        if i not in (0, 1, 2):
            return None
        what = BISECT_WHAT[i]
    res = bisect_step(ctx, what)
    for text, style in res.lines:
        ui.log(text, style)
    return Outcome(refresh=res.refresh)


# ---------------------------------------------------------------- crash-log leads

def crashlog_lines(game_dir, folders: set[str]) -> Lines:
    log = crashlog.newest_log(console_log_dirs(gamefinder.steam_roots()))
    rep = crashlog.analyze(log, folders) if log else crashlog.CrashReport()
    lo = LoadOrder.load(load_order_path(game_dir)).entries()
    return [(line, WARN if line.startswith("  ") else "") for line in crashlog.format_report(rep, lo)]


# ---------------------------------------------------------------- game-update repair

def repair_patch(ctx: Ctx) -> Lines:
    try:
        with ctx.lock:
            r = patch(ctx.game_dir, ctx.registry, runner=ctx.patch_runner, is_running=ctx.game_running)
        return [("Game was updated: " + r.message, OK if r.changed else WARN)]
    except PatcherError as e:
        return [(f"Could not re-enable mods after the game update: {e}", ERR)]


REPAIR_NOTICE = "The game was updated and your mods are switched off."


# ---------------------------------------------------------------- Cogitator's own updates (texts and decisions)

OFFER_CHOICES = ["Download it from GitHub now", "Cancel"]
READY_CHOICES = ["Install now and restart", "Remind me later", "Skip this version", "Cancel"]
INSTALL, REMIND, SKIP = 0, 1, 2


def update_banner_text(u, ready, gui: bool = False) -> str:
    """The one-line banner. `u` is a SelfUpdater (or None); `ready` its staged update (or None)."""
    if u is None or u.settings.update_mode == "off":
        return ""
    a = u.announcement()
    if not a:
        return ""
    if ready:
        return f"Update available v{a.version} - downloaded and verified. " + ("Use Help > Update Cogitator to install it."
                                                                               if gui else "Press A to install.")
    if u.layout.in_place:
        return f"Update available v{a.version}. " + ("Use Help > Update Cogitator to download and install it." if gui
                                                      else "Press A to download and install it.")
    return f"Update available v{a.version}. {u.layout.guidance()}"


def offer_question(offer) -> str:
    mb = f" (about {offer.zip_size // (1024 * 1024)} MB)" if offer.zip_size else ""
    return f"Cogitator v{offer.version} is available{mb}.\n\n{offer.notes or '(no release notes)'}"


def ready_question(u, rd) -> str:
    a = u.latest() or u.announcement()
    return f"Cogitator v{rd.version}\n\n{(a.notes if a else rd.notes) or '(no release notes)'}"


def update_result_lines(r: dict) -> Lines:
    """What the update helper did last time, in plain words (it ran after Cogitator had closed)."""
    status, ver, detail = r.get("status"), r.get("version") or "", r.get("detail") or ""
    if status == "ok":
        return [(f"Updated to v{ver}.", OK)]
    if status == "rolled-back":
        return [("Went back to the previous version.", OK)]
    why = {"aborted": "The update did not start; your current version is unchanged.",
           "reverted": "The new version failed its check, so your previous version was restored.",
           "broken": "The update failed and the old version could not be restored."}.get(status, "The update did not finish.")
    out: Lines = [(f"Update to v{ver} failed. {why}", ERR)]
    if detail:
        out.append((f"Reason: {detail}", "red"))
    out.append((f"Details: {config_dir() / 'update.log'}. If it keeps failing, run the latest installer from the releases page; "
                "your mods and settings are kept.", WARN))
    return out


def download_update(ctx: Ctx, ui: Ui, u, info) -> bool:
    """Stage a release (download + verify). True when it is ready to install."""
    from .selfupdate import UpdateError

    ui.log(f"Downloading Cogitator v{info.version} ...", DIM)
    try:
        with ctx.lock:
            u.stage(info)
    except UpdateError as e:
        ui.log(str(e), ERR)
        return False
    return True


def remember_choice(ctx: Ctx, u, i: int, version: str) -> None:
    """'Remind me later' / 'Skip this version'. Takes the lock, so it belongs on a worker thread, never the UI thread."""
    with ctx.lock:
        if i == REMIND:
            u.remind_later()
        elif i == SKIP:
            u.skip_version(version)


def apply_update(u, ui: Ui) -> bool:
    """Hand over to the update helper. True: the caller must quit right now."""
    from .selfupdate import UpdateError

    try:
        rd = u.apply()
    except UpdateError as e:
        ui.log(str(e), ERR)
        return False
    ui.log(f"Installing v{rd.version}; Cogitator will restart.", "bold")
    return True

