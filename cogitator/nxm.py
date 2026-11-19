"""nxm:// links: the "Mod Manager Download" button on Nexus Mods hands a link to the registered program. Cogitator validates
the link strictly, queues it, and the interface asks before it downloads and installs anything.

    nxm://warhammer40kdarktide/mods/<mod id>/files/<file id>?key=<token>&expires=<unix time>&user_id=<id>

The key and expiry come from the website and let a FREE account download that one file; they are never logged."""
from __future__ import annotations

import json
import os
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .config import InstanceLock, LockError, Settings, config_dir
from .fsutil import atomic_write_text
from .nexus import GAME

log = logging.getLogger(__name__)

SCHEME = "nxm"
MAX_URL = 600
MAX_AGE_S = 3600  # a queued link older than this is dropped, whatever its own expiry says
_TOKEN = re.compile(r"^[A-Za-z0-9_\-]{1,200}$")
_PATH = re.compile(r"^/mods/(\d{1,9})/files/(\d{1,12})/?$")
HKCU_KEY = r"Software\Classes\nxm"


class NxmError(Exception):
    pass


class NxmExpired(NxmError):
    pass


@dataclass(frozen=True)
class NxmLink:
    mod_id: int
    file_id: int
    key: str
    expires: int
    user_id: int | None = None

    def __repr__(self) -> str:  # the key is a credential: it never appears in logs or tracebacks
        return f"NxmLink(mod_id={self.mod_id}, file_id={self.file_id}, key=***, expires={self.expires})"


def parse(url: str) -> NxmLink:
    """Strict parse. Raises NxmError for anything that is not a Darktide mod-file link with a key and expiry."""
    if not isinstance(url, str) or len(url) > MAX_URL or any(ord(c) < 32 for c in url):
        raise NxmError("Not a valid nxm link.")
    try:
        u = urlsplit(url.strip())
        has_extras = u.username is not None or u.password is not None or u.port is not None or bool(u.fragment)
    except ValueError:
        raise NxmError("Not a valid nxm link.") from None
    if u.scheme.lower() != SCHEME:
        raise NxmError("Not an nxm:// link.")
    if has_extras:
        raise NxmError("Not a valid nxm link.")
    host = (u.hostname or "").lower()
    if host != GAME:
        raise NxmError(f"This nxm link is for another game ('{host[:60]}'); Cogitator only handles {GAME} links. Nothing was done.")
    m = _PATH.match(u.path)
    if not m:
        raise NxmError("Only single mod-file links are supported (not collections).")
    q = parse_qs(u.query, keep_blank_values=True)
    if any(len(q.get(n, [])) > 1 for n in ("key", "expires", "user_id")):
        raise NxmError("The link repeats a parameter.")
    key = (q.get("key") or [""])[0]
    exp = (q.get("expires") or [""])[0]
    uid = (q.get("user_id") or [""])[0]
    if not _TOKEN.match(key):
        raise NxmError("The link has no valid download key.")
    if not exp.isdigit() or len(exp) > 12:
        raise NxmError("The link has no valid expiry.")
    if uid and not (uid.isdigit() and len(uid) <= 12):
        raise NxmError("The link has an invalid user id.")
    return NxmLink(int(m.group(1)), int(m.group(2)), key, int(exp), int(uid) if uid else None)


def redact_url(url: str) -> str:
    return re.sub(r"(?i)((?<![\w-])(?:key|user_id|md5)=)[^&\s]+", r"\1***", url)


def check_expiry(link: NxmLink, now: Callable[[], float] = time.time) -> None:
    if link.expires <= now():
        raise NxmExpired("link expired (click the Mod Manager Download button on the website again)")


# ------------------------------------------------------------------ the queue

def queue_dir() -> Path:
    return config_dir() / "nxm-queue"


def enqueue(url: str, now: Callable[[], float] = time.time) -> Path:
    link = parse(url)  # refuse to queue anything invalid
    check_expiry(link, now)  # ... or already expired
    d = queue_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{int(now() * 1000)}-{link.mod_id}-{link.file_id}.json"
    atomic_write_text(p, json.dumps({"url": url.strip(), "at": now()}))
    return p


def pending(now: Callable[[], float] = time.time, on_expired: Callable[[NxmLink], None] | None = None) -> list[tuple[Path, NxmLink]]:
    """Valid, fresh queued links (oldest first). Invalid, expired or stale files are deleted (on_expired hears about expired ones)."""
    out = []
    d = queue_dir()
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            link = parse(rec["url"])
            fresh = now() - float(rec["at"]) <= MAX_AGE_S and link.expires > now()
            if link.expires <= now() and on_expired:
                on_expired(link)
        except (OSError, ValueError, KeyError, TypeError, NxmError):
            fresh, link = False, None
        if fresh and link is not None:
            out.append((p, link))
        else:
            p.unlink(missing_ok=True)
    return out


def done(path: Path) -> None:
    Path(path).unlink(missing_ok=True)


# ------------------------------------------------------------------ downloading

def download(link: NxmLink, nexus, dest_dir: Path, client=None, now: Callable[[], float] = time.time) -> tuple[Path, object]:
    """Download the linked file with the link's own key. Returns (zip path, NexusFile). Raises NxmError/NexusError.
    The download link is requested fresh every time and never stored."""
    from .loader import Asset, download_asset
    from .nexus import make_download_client

    check_expiry(link, now)
    files = [f for f in nexus.files(link.mod_id) if f.file_id == link.file_id]
    if not files:
        raise NxmError("Nexus does not list that file for the mod (it may have been removed).")
    f = files[0]
    links = nexus.download_links(link.mod_id, link.file_id, key=link.key, expires=link.expires)
    if not links:
        raise NxmError("Nexus returned no download link (the link may have expired; click the button on the website again).")
    own = client is None
    client = client or make_download_client(timeout=60.0)
    try:
        return download_asset(client, Asset(f.file_name or f"{link.mod_id}-{link.file_id}.zip", links[0]["URI"], int(f.size_bytes or 0)),
                              Path(dest_dir)), f
    finally:
        if own:
            client.close()


# ------------------------------------------------------------------ the click: queue it and make sure Cogitator is open

def _spawn_command() -> list[str]:
    """A click with no window open starts the web window when this install has one (cogitator-web.exe), else the terminal UI."""
    if getattr(sys, "frozen", False):
        web = Path(sys.executable).parent / "cogitator-web.exe"
        return [str(web if web.is_file() else web.with_name("cogitator.exe"))]
    return [sys.executable, "-m", "cogitator"]


def interface_running(settings: Settings | None = None) -> bool | None:
    """True when a Cogitator interface holds the lock for the configured game; None when no game is configured yet."""
    gd = (settings or Settings.load()).game_dir
    if not gd:
        return None
    lock = InstanceLock(Path(gd))
    try:
        lock.acquire()
    except LockError:
        return True
    lock.release()
    return False


def _say(msg: str) -> None:
    print(msg, file=sys.stderr)


def _message_box(msg: str) -> None:  # the windowless handler has no console, so a rejected click is shown in a dialog
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, msg, "Cogitator", 0x40)  # type: ignore[attr-defined]
    except Exception:
        pass


def handle_click(url: str, popen=subprocess.Popen, running: Callable[[], bool | None] = interface_running,
                 notify: Callable[[str], None] = _say) -> int:
    """What the registered handler runs: validate, queue, and open Cogitator when it is not already open.
    A link for another game, a malformed one or an expired one is refused with a message and nothing else happens."""
    try:
        enqueue(url)
    except NxmError as e:
        log.warning("ignored nxm link: %s", e)
        notify(f"Cogitator ignored this nxm link: {e}")
        return 2
    if running() is True:
        return 0  # the open interface picks the link up within a couple of seconds
    cmd = _spawn_command()
    # the windowed web program needs no console of its own; the terminal UI does
    flags = 0 if Path(cmd[0]).name.lower() == "cogitator-web.exe" else getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    popen(cmd, creationflags=flags, close_fds=True)
    return 0


def click_main(argv: list[str]) -> int:
    """Entry point of the windowless cogitator-nxm.exe (no console, so a click never flashes a window)."""
    from .config import setup_logging

    setup_logging()
    if len(argv) != 1:
        return 2
    return handle_click(argv[0], notify=_message_box)


# ------------------------------------------------------------------ registering as the nxm: handler (HKCU, no admin)

def _winreg():
    import winreg

    return winreg


def read_handler() -> str | None:
    w = _winreg()
    try:
        with w.OpenKey(w.HKEY_CURRENT_USER, HKCU_KEY + r"\shell\open\command") as k:
            v, _ = w.QueryValueEx(k, "")
            return v if isinstance(v, str) else None
    except FileNotFoundError:
        return None


def _write_handler(command: str) -> None:
    w = _winreg()
    with w.CreateKeyEx(w.HKEY_CURRENT_USER, HKCU_KEY, 0, w.KEY_SET_VALUE | w.KEY_QUERY_VALUE) as k:
        try:
            w.QueryValueEx(k, "URL Protocol")
        except FileNotFoundError:
            w.SetValueEx(k, "", 0, w.REG_SZ, "URL:NXM Protocol")
            w.SetValueEx(k, "URL Protocol", 0, w.REG_SZ, "")
    with w.CreateKeyEx(w.HKEY_CURRENT_USER, HKCU_KEY + r"\shell\open\command", 0, w.KEY_SET_VALUE) as k:
        w.SetValueEx(k, "", 0, w.REG_SZ, command)


def _delete_handler() -> None:
    w = _winreg()
    for sub in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
        try:
            w.DeleteKey(w.HKEY_CURRENT_USER, HKCU_KEY + sub)
        except (FileNotFoundError, OSError):
            pass


def default_command() -> str:
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        nx = exe.parent / "cogitator-nxm.exe"
        return f'"{nx}" "%1"' if nx.is_file() else f'"{exe}" nxm handle "%1"'
    return f'"{exe}" -m cogitator nxm handle "%1"'


def _exe_of(command: str) -> str:
    command = command.strip()
    if command.startswith('"'):
        exe = command[1:].split('"', 1)[0]
    else:
        exe = command.split(None, 1)[0] if command else ""
    return os.path.normcase(os.path.normpath(exe)) if exe else ""


def is_ours(command: str | None) -> bool:
    """True when the registered command launches this Cogitator install (compares the executable path, not a name fragment)."""
    if not command:
        return False
    exe = _exe_of(command)
    return bool(exe) and exe == _exe_of(default_command())


def _previous_path() -> Path:
    return config_dir() / "nxm-previous.json"


def register(command: str | None = None) -> str:
    """Make Cogitator the nxm: handler for this user. Remembers the previous handler (Vortex, MO2) for unregister."""
    command = command or default_command()
    cur = read_handler()
    if cur and not is_ours(cur):
        atomic_write_text(_previous_path(), json.dumps({"command": cur}))
    _write_handler(command)
    return ("Cogitator now handles nxm:// links" + (" (the previous handler is remembered)." if cur and not is_ours(cur) else "."))


def unregister() -> str:
    cur = read_handler()
    if cur is None:
        return "No nxm:// handler is registered."
    if not is_ours(cur):
        return "The nxm:// handler belongs to another program; left unchanged."
    try:
        prev = json.loads(_previous_path().read_text(encoding="utf-8")).get("command")
    except (OSError, ValueError, AttributeError):
        prev = None
    if prev and isinstance(prev, str) and not is_ours(prev):
        _write_handler(prev)
        _previous_path().unlink(missing_ok=True)
        return "The previous nxm:// handler is restored."
    _delete_handler()
    return "Cogitator no longer handles nxm:// links."


def status() -> str:
    cur = read_handler()
    if cur is None:
        return "No program handles nxm:// links."
    return "Cogitator handles nxm:// links." if is_ours(cur) else f"Another program handles nxm:// links: {cur}"
