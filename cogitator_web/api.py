"""The object the page calls (`window.pywebview.api.*`). Small methods; every argument is validated here before it reaches the
engine. Return values are plain JSON dicts: {"ok": True, ...} or {"ok": False, "error": "..."}.

pywebview exposes the public methods of this object, so everything that is not a page call starts with an underscore. The page
never passes a path: files and folders come only from the native dialogs (session seams) or from known locations; mods are named
by folder and must be existing mod folders of the current game."""
from __future__ import annotations

import functools
import logging
from typing import Callable

from cogitator import profiles
from cogitator.config import redact
from cogitator.mods import ModError

from . import background
from .bridge import Bridge
from .session import BULK_IDS, ROW_ACTIONS, Session, valid_folder_name

log = logging.getLogger("cogitator.web")

SORTABLE = ("mod", "status", "installed", "latest", "en")
KEY_MAX = 200


def _call(fn: Callable) -> Callable:
    """A page call never raises into pywebview: errors become {"ok": False, "error": redacted text}."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            out = fn(self, *args, **kwargs)
            return {"ok": True} if out is None else out
        except _Reject as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("api call %s failed", fn.__name__)
            return {"ok": False, "error": redact(f"{type(e).__name__}: {e}")}

    return wrapper


class _Reject(Exception):
    pass


class Api:
    def __init__(self, session: Session, on_ready: Callable[[], None] | None = None) -> None:
        self._s = session
        self._bridge: Bridge = session.bridge
        self._on_ready = on_ready or (lambda: None)

    # ---- validation helpers (private)
    def _mod(self, folder: object, present: bool = False) -> str:
        if not self._s.mod_known(folder, present):
            raise _Reject("Unknown mod folder.")
        return folder  # type: ignore[return-value]

    @staticmethod
    def _text(value: object, limit: int, what: str) -> str:
        if not isinstance(value, str) or len(value) > limit:
            raise _Reject(f"{what} must be text of at most {limit} characters.")
        return value

    # ---- boot
    @_call
    def boot(self):
        """First call of the page: what it needs to paint the first frame (theme) before the window is shown."""
        return {"ok": True, "settings": self._s.settings_payload()}

    @_call
    def ready(self):
        """The page has rendered: the app attaches the window and shows it, then the start-up chain runs."""
        self._on_ready()
        self._s.start()

    @_call
    def answer(self, qid, value):
        if not self._bridge.answer(qid, value):
            raise _Reject("That answer does not match an open question.")

    # ---- table
    @_call
    def set_filter(self, text):
        self._s.set_filter(self._text(text, 200, "The filter"))

    @_call
    def set_sort(self, col):
        if col is not None and col not in SORTABLE:
            raise _Reject("Unknown column.")
        self._s.set_sort(col)

    @_call
    def cycle_sort(self):
        self._s.cycle_sort()

    @_call
    def row_menu(self, folder):
        return {"ok": True, "items": self._s.row_menu(self._mod(folder))}

    @_call
    def row_action(self, folder, action):
        if not isinstance(action, str) or action not in ROW_ACTIONS:
            raise _Reject("Unknown action.")
        self._s.row_action(self._mod(folder, present=action in ("toggle", "up", "down", "open_folder", "link", "update")), action)

    @_call
    def toggle(self, folder):
        self._s.toggle(self._mod(folder, present=True))

    @_call
    def move(self, folder, delta):
        if delta not in (-1, 1) or isinstance(delta, bool):
            raise _Reject("delta must be -1 or 1.")
        self._s.move(self._mod(folder, present=True), delta)

    @_call
    def move_to(self, folder, target, after):
        if not isinstance(after, bool):
            raise _Reject("after must be true or false.")
        self._s.move_to(self._mod(folder, present=True), self._mod(target, present=True), after)

    @_call
    def mark(self, folder):
        self._s.mark(self._mod(folder))

    @_call
    def fav(self, folder):
        self._s.fav(self._mod(folder))

    @_call
    def edit_note(self, folder):
        self._s.edit_note(self._mod(folder))

    # ---- profiles, snapshots, bulk
    def _profile(self, name: object) -> str:
        if not self._s.profile_known(name):
            raise _Reject("Unknown profile.")
        return name  # type: ignore[return-value]

    @_call
    def profiles_list(self):
        return {"ok": True, "items": self._s.profiles_list()}

    @_call
    def profile_switch(self, name):
        self._s.profile_switch(self._profile(name))

    @_call
    def profile_save(self, name):
        name = self._text(name, 60, "Profile name")
        try:
            profiles.check_name(name)
        except ModError as e:
            raise _Reject(str(e))
        self._s.profile_save(name.strip())

    @_call
    def profile_delete(self, name):
        self._s.profile_delete(self._profile(name))

    @_call
    def profile_share(self):
        code = self._s.profile_share()
        return {"ok": code is not None, "code": code or "", **({} if code else {"error": "Could not build a setup code."})}

    @_call
    def profile_import_code(self, code):
        code = self._text(code, 50000, "Setup code").strip()
        if not code:
            raise _Reject("Paste a setup code first.")
        self._s.profile_import_code(code)

    @_call
    def snapshots_list(self):
        return {"ok": True, "items": self._s.snapshots_list(), "keep": max(1, self._s.settings.snapshot_keep)}

    @_call
    def snapshot_create(self, reason="manual"):
        self._s.snapshot_create(self._text(reason, 200, "Reason").strip() or "manual")

    @_call
    def snapshot_restore(self, name):
        if not self._s.snapshot_known(name):
            raise _Reject("Unknown snapshot.")
        self._s.snapshot_restore(name)

    @_call
    def snapshot_prune(self, keep):
        if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep <= 1000:
            raise _Reject("Keep must be a whole number from 1 to 1000.")
        self._s.snapshot_prune(keep)

    @_call
    def bulk_menu(self):
        return {"ok": True, "items": self._s.bulk_menu()}

    @_call
    def bulk(self, action):
        if not isinstance(action, str) or action not in BULK_IDS:
            raise _Reject("Unknown action.")
        self._s.bulk(action)

    # ---- global actions
    @_call
    def check_updates(self):
        self._s.recheck()

    @_call
    def update_all(self):
        self._s.update_all()

    @_call
    def link_all(self):
        self._s.link_all()

    @_call
    def framework_update(self):
        self._s.framework_update()

    @_call
    def import_zips(self):
        self._s.import_zips()

    @_call
    def launch(self):
        self._s.launch()

    @_call
    def doctor(self):
        self._s.doctor()

    @_call
    def cogitator_update(self):
        self._s.cogitator_update()

    @_call
    def bisect(self):
        self._s.bisect()

    @_call
    def crashlog(self):
        self._s.crashlog()

    @_call
    def search_nexus(self, text):
        text = self._text(text, 100, "Search text").strip()
        if not text:
            raise _Reject("Enter part of a mod's name.")
        self._s.search_nexus(text)

    @_call
    def toggle_global(self):
        self._s.toggle_global()

    @_call
    def open_game_folder(self):
        self._s.open_folder(None)

    @_call
    def help(self):
        return {"ok": True, **self._s.help_text()}

    # ---- settings
    @_call
    def get_settings(self):
        return {"ok": True, "settings": self._s.settings_payload()}

    @_call
    def set_setting(self, name, value):
        if not isinstance(name, str):
            raise _Reject("Bad setting name.")
        err = self._s.set_setting(name, value)
        if err:
            raise _Reject(err)
        return {"ok": True, "settings": self._s.settings_payload()}

    @_call
    def pick_downloads(self):
        """Browse...: the native folder dialog; the chosen folder is saved if it exists."""
        chosen = self._s.pick_folder()
        if not chosen:
            return {"ok": True, "cancelled": True}
        err = self._s.set_setting("downloads_dir", str(chosen))
        if err:
            raise _Reject(err)
        return {"ok": True, "settings": self._s.settings_payload()}

    @_call
    def pick_background(self):
        """Choose image...: native dialog (png/jpg/webp), validated and copied to the config folder, then made the background."""
        err = self._s.choose_background()
        if err:
            raise _Reject(err)
        return {"ok": True, "settings": self._s.settings_payload()}

    @_call
    def reset_background(self):
        self._s.reset_background()
        return {"ok": True, "settings": self._s.settings_payload()}

    @_call
    def get_background(self):
        """The custom image as a data: URI (None when there is none)."""
        return {"ok": True, "data": background.data_uri() if self._s.settings.gui_background == "custom" else None}

    @_call
    def validate_key(self, key):
        """The Nexus key: one method, validated in a worker. It is never echoed, stored by the page or logged."""
        if not isinstance(key, str):
            raise _Reject("The key must be text.")
        key = key.strip()
        if not (8 <= len(key) <= KEY_MAX) or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise _Reject("That does not look like a Nexus API key.")
        if not self._s.validate_key(key):
            raise _Reject("Could not start the key check (game folder not chosen yet, or a check is already running).")
        return {"ok": True, "started": True}

    # ---- window
    @_call
    def quit(self):
        self._s.quit()


__all__ = ["Api", "valid_folder_name"]
