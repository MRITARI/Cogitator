"""Workflow features for the interface: launch, game-update repair and notifications, bisect, crash-log leads, marks and
bulk actions, notes/tags/favourites, filter, pinning and changelogs."""
from __future__ import annotations

from rich.text import Text

from .. import bisect, crashlog, gamefinder, nxm, repair
from ..config import cache_dir, get_api_key
from ..doctor import console_log_dirs
from ..loadorder import LoadOrder
from ..mods import ModError, load_order_path, set_mod_enabled
from ..nexus import NexusClient, NexusError
from ..patcher import OFF, PatcherError, patch, patch_state
from ..updates import UPDATE
from .screens import ChoiceScreen, ConfirmScreen, InputScreen
from .view import mod_cell


class WorkflowMixin:
    """Methods of CogitatorApp; they use its attributes, so it is only meant to be mixed into it."""

    # ------------------------------------------------------------ row display, marks, filter
    def _meta(self, folder: str) -> dict:
        return (self.registry.mod(folder) if self.registry else None) or {}

    def _mod_cell(self, m) -> Text:
        t = mod_cell(m)
        meta = self._meta(m.folder)
        prefix = ("+ " if m.folder in self.marked else "") + ("* " if meta.get("fav") else "")
        return Text(prefix) + t if prefix else t

    def _visible(self, m) -> bool:
        q = self.filter_text.strip().casefold()
        if not q:
            return True
        meta = self._meta(m.folder)
        hay = " ".join([m.folder, " ".join(meta.get("tags") or []), meta.get("notes") or "", "fav" if meta.get("fav") else "",
                        "pinned" if meta.get("pinned_version") else ""]).casefold()
        return all(w in hay for w in q.split())

    def _meta_hint(self, folder: str) -> str:
        meta = self._meta(folder)
        bits = []
        if meta.get("pinned_version"):
            bits.append(f"pinned to {meta['pinned_version']}")
        if meta.get("tags"):
            bits.append("tags: " + ", ".join(meta["tags"]))
        if meta.get("notes"):
            bits.append("note: " + meta["notes"])
        return "  |  " + "  |  ".join(bits) if bits else ""

    def action_toggle_mark(self) -> None:
        f = self.selected()
        if not (self._need_game() and f):
            return
        self.marked.symmetric_difference_update({f})
        self._refresh_mod_row(f)
        self.log_line(f"{len(self.marked)} mod(s) marked. Press B to act on them.", "dim")

    def action_filter(self) -> None:
        if self._need_game():
            self.push_screen(InputScreen("Filter mods", "name, tag, note, 'fav' or 'pinned' (empty = show all)", allow_blank=True),
                             self._filter_entered)

    def _filter_entered(self, text: str | None) -> None:
        if text is None:
            return
        self.filter_text = text
        self.refresh_all()
        shown = sum(1 for m in self.mods if self._visible(m))
        self.log_line(f"Filter '{text}': showing {shown} of {len(self.mods)} mod(s)." if text else "Filter cleared.", "dim")

    # ------------------------------------------------------------ notes, tags, favourites, pinning
    def action_edit_note(self) -> None:
        f = self.selected()
        if self._need_game() and f:
            self.push_screen(InputScreen(f"Tags and note for {f}", "#ui #hud  free text note (empty = clear)", allow_blank=True),
                             lambda t, f=f: self._note_entered(f, t))

    def _note_entered(self, folder: str, text: str | None) -> None:
        if text is None:
            return
        words = text.split()
        tags = [w[1:].lower()[:30] for w in words if w.startswith("#") and len(w) > 1]
        note = " ".join(w for w in words if not (w.startswith("#") and len(w) > 1))
        self.registry.set_mod(folder, tags=tags, notes=note[:500])
        self._refresh_mod_row(folder)
        self.log_line(f"{folder}: " + (f"tags {', '.join(tags) or '(none)'}; note saved." if text else "tags and note cleared."), "green")

    def action_toggle_fav(self) -> None:
        f = self.selected()
        if not (self._need_game() and f):
            return
        fav = not self._meta(f).get("fav")
        self.registry.set_mod(f, fav=fav)
        self._refresh_mod_row(f)
        self.log_line(f"{f}: {'favourite' if fav else 'not a favourite'}.", "green")

    def _toggle_pin(self, folder: str) -> None:
        meta = self._meta(folder)
        if meta.get("pinned_version"):
            self.registry.set_mod(folder, pinned_version=None)
            self.log_line(f"{folder}: unpinned.", "green")
        else:
            v = str(meta.get("installed_version") or "the installed version")
            self.registry.set_mod(folder, pinned_version=v)
            self.log_line(f"{folder}: pinned to {v}; its updates are skipped until you unpin it.", "green")
        self._after_registry_change(folder)

    def _show_changelog(self, folder: str) -> None:
        self._bg(self._job_changelog, folder, name="changelog", group="net")

    def _job_changelog(self, folder: str) -> None:
        from ..cli import _newer

        rec = self._meta(folder)
        key = get_api_key()
        if not (rec.get("nexus_id") and key):
            self._ui(self.log_line, "Link the mod to Nexus and set your API key (K) to see its changelog.", "yellow")
            return
        try:
            with NexusClient(key) as nx:
                log = nx.changelogs(int(rec["nexus_id"]))
        except NexusError as e:
            self._ui(self.log_line, f"Changelog: {e}", "bold red")
            return
        if not log:
            self._ui(self.log_line, f"{folder}: no changelog published on Nexus.", "yellow")
            return
        for v in _newer(list(log), rec.get("installed_version")):
            self._ui(self.log_line, f"{folder} v{v}", "bold")
            for line in log[v][:12]:
                self._ui(self.log_line, f"   - {line}")

    # ------------------------------------------------------------ bulk actions on marked mods
    def action_bulk(self) -> None:
        if not self._need_game():
            return
        if not self.marked:
            self.log_line("Mark mods with X first (shown with a +), then press B.", "yellow")
            return
        opts = [f"Enable {len(self.marked)} mod(s)", f"Disable {len(self.marked)} mod(s)", "Update them (Premium)",
                "Add a tag...", "Remove them...", "Clear marks"]
        self.push_screen(ChoiceScreen(f"{len(self.marked)} marked mod(s)", opts), self._bulk_choice)

    def _bulk_choice(self, i: int | None) -> None:
        marked = sorted(self.marked)
        if i is None or not marked:
            return
        if i in (0, 1):
            from .. import snapshots

            snapshots.auto(self.game_dir, "bulk", registry=self.registry, once=True)
            done = 0
            for f in marked:
                try:
                    set_mod_enabled(self.game_dir, f, i == 0)
                    done += 1
                except ModError as e:
                    self.log_line(str(e), "bold red")
            self.log_line(f"{'Enabled' if i == 0 else 'Disabled'} {done} mod(s).", "green")
            self.refresh_all()
        elif i == 2:
            todo = [f for f in marked if self.urows.get(f) is not None and self.urows[f].status == UPDATE]
            if not self._premium():
                self.log_line("Free account: updates need a download for each mod. Select one and press U.", "yellow")
            elif not todo:
                self.log_line("None of the marked mods has an update available.", "yellow")
            else:
                self._bg(self._job_premium, todo, name="update marked", group="install", exclusive=True)
        elif i == 3:
            self.push_screen(InputScreen("Tag the marked mods", "tag names separated by spaces"), self._bulk_tag)
        elif i == 4:
            self.push_screen(ConfirmScreen(f"Remove {len(marked)} mod(s)?", ", ".join(marked) + "\nTheir folders are moved (not deleted) so you can restore them.",
                                           default_yes=False),
                             lambda ok, m=marked: ok and self._bg(self._job_remove_many, m, name="remove marked", group="install", exclusive=True))
        else:
            self.marked.clear()
            self.refresh_all()

    def _bulk_tag(self, text: str | None) -> None:
        tags = [t.lstrip("#").lower()[:30] for t in (text or "").split() if t.lstrip("#")]
        if not tags:
            return
        for f in sorted(self.marked):
            cur = [t for t in self._meta(f).get("tags") or [] if isinstance(t, str)]
            self.registry.set_mod(f, tags=cur + [t for t in tags if t not in cur])
        self.log_line(f"Tagged {len(self.marked)} mod(s): {', '.join(tags)}", "green")
        self.refresh_all()

    def _job_remove_many(self, folders: list[str]) -> None:
        for f in folders:
            self._job_remove(f)
            self.marked.discard(f)
        self._ui(self.refresh_all)

    # ------------------------------------------------------------ launch
    def action_launch(self) -> None:
        if self._need_game():
            self._bg(self._job_launch, name="launch", group="launch", exclusive=True)

    def _job_launch(self) -> None:
        from ..launch import LaunchError, launch

        self._ui(self.log_line, "Checking the setup before launching...", "dim")
        try:
            with self._lock:
                r = launch(self.game_dir, self.registry, is_running=self.game_running, runner=self.patch_runner)
        except (LaunchError, ModError, OSError) as e:
            self._ui(self.log_line, str(e), "bold red")
            return
        for n in r.notes:
            self._ui(self.log_line, n, "yellow")
        self._ui(self.log_line, "Asked Steam to start Darktide.", "green")
        self._ui(self.refresh_header)

    # ------------------------------------------------------------ game-update watcher
    def _watch(self) -> None:
        """Every few seconds: notice the game being updated (mods switched off) and, per the auto_repair setting, tell the user
        or patch again."""
        if not (self.game_dir and self.registry) or len(self.screen_stack) > 1:
            return
        try:
            st = patch_state(self.game_dir)
        except Exception:
            return
        if st == self._last_patch_state:
            return
        first = self._last_patch_state is None
        self._last_patch_state = st
        if not first:
            self.refresh_header()
        if st != OFF or not repair.needs_repair(self.game_dir, self.registry):
            return
        act = repair.decide(self.settings.auto_repair, bool(self.game_running()))
        if act == "patch":
            self._bg(self._job_repair, name="repair", group="patch", exclusive=True)
        elif act == "notify":
            self.notify("The game was updated and your mods are switched off. Press T to turn them on again.",
                        title="Game updated", severity="warning", timeout=15)

    def _job_repair(self) -> None:
        try:
            with self._lock:
                r = patch(self.game_dir, self.registry, runner=self.patch_runner, is_running=self.game_running)
            self._ui(self.log_line, "Game was updated: " + r.message, "green" if r.changed else "yellow")
        except PatcherError as e:
            self._ui(self.log_line, f"Could not re-enable mods after the game update: {e}", "bold red")
        self._ui(self.refresh_header)

    # ------------------------------------------------------------ nxm:// links from the Nexus website
    def _nxm_tick(self) -> None:
        """Pick up links queued by the nxm handler. One at a time, and never while a dialog is open."""
        if self._nxm_busy or not (self.game_dir and self.registry) or len(self.screen_stack) > 1:
            return
        items = nxm.pending()
        if items:
            self._nxm_busy = True
            path, link = items[0]
            self._bg(self._job_nxm, path, link, name="nxm link", group="install", exclusive=True)

    def _job_nxm(self, path, link) -> None:
        import shutil

        from ..modinstall import ModImportError

        work = cache_dir() / "nxm"
        try:
            key = get_api_key()
            if not key:
                self._ui(self.log_line, "A Nexus link arrived, but no API key is set. Press K to set it, then click the button again.", "bold yellow")
                return
            with NexusClient(key) as nx:
                try:
                    name = nx.mod(link.mod_id).get("name") or f"mod {link.mod_id}"
                    f = next((x for x in nx.files(link.mod_id) if x.file_id == link.file_id), None)
                except NexusError as e:
                    self._ui(self.log_line, f"Nexus link: {e}", "bold red")
                    return
                what = f"{name}\n{f.file_name or f.name} (v{f.version})" if f else f"{name} (file {link.file_id})"
                ok = self.ask(ConfirmScreen("Install from Nexus?", what + "\n\nThis came from the website's Mod Manager Download button. "
                                            "Only install mods you trust.", default_yes=False))
                if not ok:
                    self._ui(self.log_line, "Nexus link cancelled.", "yellow")
                    return
                self._ui(self.log_line, f"Downloading {name}...", "dim")
                try:
                    zp, _ = nxm.download(link, nx, work)
                except (nxm.NxmError, NexusError, ModImportError, OSError) as e:
                    self._ui(self.log_line, f"Download failed: {e}", "bold red")
                    return
            self._job_import(str(zp), link.mod_id)
        finally:
            nxm.done(path)
            shutil.rmtree(work, ignore_errors=True)
            self._nxm_busy = False

    # ------------------------------------------------------------ crash log leads
    def action_crashlog(self) -> None:
        if self._need_game():
            self._bg(self._job_crashlog, name="crash log", group="net")

    def _job_crashlog(self) -> None:
        log = crashlog.newest_log(console_log_dirs(gamefinder.steam_roots()))
        rep = crashlog.analyze(log, {m.folder for m in self.mods}) if log else crashlog.CrashReport()
        lo = LoadOrder.load(load_order_path(self.game_dir)).entries()
        for line in crashlog.format_report(rep, lo):
            self._ui(self.log_line, line, "yellow" if line.startswith("  ") else "")

    # ------------------------------------------------------------ bisect
    def action_bisect(self) -> None:
        if not self._need_game():
            return
        s = bisect.load()
        if s is None:
            self.push_screen(ConfirmScreen("Find a crashing mod?",
                                           "Cogitator will switch half of your enabled mods off, you start the game and see whether the "
                                           "problem is still there, and it narrows the list down. A snapshot is taken first and you can "
                                           "restore your load order at any time. Make sure the problem happens with all mods on.",
                                           default_yes=False),
                             lambda ok: ok and self._bisect_step("start"))
            return
        head = bisect.describe(s).splitlines()[0]
        opts = ["The problem still happens", "The problem is gone", "Stop and restore my load order", "Close"]
        self.push_screen(ChoiceScreen(head, opts), lambda i: self._bisect_step(("bad", "good", "reset")[i]) if i in (0, 1, 2) else None)

    def _bisect_step(self, what: str) -> None:
        try:
            if what == "start":
                s = bisect.start(self.game_dir, self.registry)
            elif what == "reset":
                for line in bisect.reset(self.game_dir, self.registry):
                    self.log_line(line, "green")
                self.refresh_all()
                return
            else:
                s = bisect.answer(self.game_dir, still_broken=what == "bad")
        except ModError as e:
            self.log_line(str(e), "bold red")
            return
        for line in bisect.describe(s).splitlines():
            self.log_line(line, "bold" if s.get("culprit") else "")
        self.refresh_all()
