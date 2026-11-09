"""Paths, settings, API-key storage, logging, instance lock."""
from __future__ import annotations

import hashlib
import logging
import logging.handlers
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import platformdirs

from .fsutil import atomic_write_text

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

APP = "cogitator"


def config_dir() -> Path:
    return Path(os.environ.get("COGITATOR_CONFIG_DIR") or platformdirs.user_config_dir(APP, appauthor=False))


def cache_dir() -> Path:
    return Path(os.environ.get("COGITATOR_CACHE_DIR") or platformdirs.user_cache_dir(APP, appauthor=False))


def log_path() -> Path:
    return cache_dir() / "cogitator.log"


# ---------------------------------------------------------------- settings

CONFIG_SCHEMA = 1
UPDATE_MODES = ("off", "notify", "download", "auto")
UPDATE_CHANNELS = ("stable", "beta")


RECOVERED: list[Path] = []  # unreadable settings files that were set aside in this run (the interface tells the user)


class SchemaTooNew(Exception):
    """A data file was written by a newer Cogitator than this one. Never rewritten, never repaired."""

    def __init__(self, what: str, found: int, supported: int):
        self.what, self.found, self.supported = what, found, supported
        super().__init__(f"Your {what} were written by a newer Cogitator (data version {found}; this version understands up to "
                         f"{supported}). Update Cogitator, or restore the file from a backup. Nothing was changed.")


@dataclass
class Settings:
    schema: int = CONFIG_SCHEMA
    update_mode: str = "notify"  # off | notify | download | auto (auto = apply on the next quit/start). Nothing is downloaded unasked:
    update_consent: bool = False  # download/auto only count when the file also says update_consent = true
    update_channel: str = "stable"  # stable | beta
    update_check_hours: int = 24
    game_dir: str = ""
    check_updates_on_start: bool = True
    update_ttl_minutes: int = 60
    downloads_dir: str = ""
    max_extract_mb: int = 500
    max_extract_files: int = 20000
    notice_accepted: bool = False

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "config.toml"

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or cls.path()
        s = cls()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return s
        except tomllib.TOMLDecodeError as e:
            bad = path.with_name(path.name + ".corrupt")
            logging.getLogger(__name__).warning("config unreadable (%s); kept as %s, using defaults", e, bad)
            try:
                path.replace(bad)  # never overwrite the user's file with defaults without keeping a copy
                RECOVERED.append(bad)
            except OSError:
                pass
            return s
        except OSError as e:
            logging.getLogger(__name__).warning("config unreadable (%s); using defaults", e)
            return s
        found = data.get("schema", 1)
        if isinstance(found, int) and found > CONFIG_SCHEMA:
            raise SchemaTooNew("settings", found, CONFIG_SCHEMA)
        for k, v in data.items():
            if hasattr(s, k) and isinstance(v, type(getattr(s, k))):
                setattr(s, k, v)
        if s.update_mode not in UPDATE_MODES:
            s.update_mode = "notify"
        if s.update_mode in ("download", "auto") and not s.update_consent:
            s.update_mode = "notify"  # older versions wrote "download" by default; downloading needs an explicit yes
        if s.update_channel not in UPDATE_CHANNELS:
            s.update_channel = "stable"
        if s.update_check_hours < 1:
            s.update_check_hours = 24
        return s

    def save(self, path: Path | None = None) -> None:
        self.schema = CONFIG_SCHEMA
        lines = []
        for k, v in asdict(self).items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, int):
                lines.append(f"{k} = {v}")
            else:
                esc = str(v).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{k} = "{esc}"')
        atomic_write_text(path or self.path(), "\n".join(lines) + "\n")


# ---------------------------------------------------------------- API key

def _key_file() -> Path:
    return config_dir() / "nexus_key"


def get_api_key() -> str | None:
    if os.environ.get("COGITATOR_NEXUS_KEY"):
        return os.environ["COGITATOR_NEXUS_KEY"].strip()
    try:
        import keyring  # optional

        v = keyring.get_password(APP, "nexus_api_key")
        if v:
            return v
    except Exception as e:
        logging.getLogger(__name__).debug("credential store unavailable: %s", type(e).__name__)
    try:
        v = _key_file().read_text(encoding="utf-8").strip()
        return v or None
    except OSError:
        return None


def set_api_key(key: str) -> str:
    """Store key. Returns where it went: 'keyring' or 'file' (0600)."""
    key = key.strip()
    _KEY_CACHE.clear()
    try:
        import keyring

        keyring.set_password(APP, "nexus_api_key", key)
        try:
            _key_file().unlink()
        except OSError:
            pass
        return "keyring"
    except Exception:
        atomic_write_text(_key_file(), key + "\n", mode=0o600)
        return "file"


def clear_api_key() -> None:
    """Remove the stored key from the keyring and the fallback file (used by rollback and uninstall)."""
    _KEY_CACHE.clear()
    try:
        import keyring

        keyring.delete_password(APP, "nexus_api_key")
    except Exception as e:  # nothing stored, or no credential store: both fine when clearing
        logging.getLogger(__name__).debug("credential delete skipped: %s", type(e).__name__)
    try:
        _key_file().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- logging

_APIKEY_PAT = re.compile(r"(?i)(apikey['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+")


_KEY_CACHE: list = []  # [key]; looked up once per run, since every log line passes through redact()


def _known_key() -> str | None:
    env = os.environ.get("COGITATOR_NEXUS_KEY")
    if env:
        return env.strip()
    if not _KEY_CACHE:
        _KEY_CACHE.append(get_api_key())
    return _KEY_CACHE[0]


def redact(text: str, *extra_secrets: str) -> str:
    """Strip the stored Nexus key, any `apikey=...` values, and any explicitly supplied secrets from text."""
    text = _APIKEY_PAT.sub(r"\1***", text)
    for s in (_known_key(), *extra_secrets):
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    return text


class RedactFilter(logging.Filter):
    """Redacts the stored Nexus key and any apikey=... looking values."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), None
        return True


def setup_logging(level: int = logging.INFO) -> Path:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_cogitator", False):
            root.removeHandler(h)
            h.close()
    h = logging.handlers.RotatingFileHandler(path, maxBytes=512_000, backupCount=3, encoding="utf-8")
    h._cogitator = True  # type: ignore[attr-defined]
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h.addFilter(RedactFilter())
    root.addHandler(h)
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return path


# ---------------------------------------------------------------- lock

class LockError(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class InstanceLock:
    """One Cogitator per game dir. Lock file lives in Cogitator's cache dir."""

    def __init__(self, game_dir: Path):
        key = os.path.normcase(str(Path(game_dir).resolve()))
        self.path = cache_dir() / f"lock-{hashlib.sha256(key.encode()).hexdigest()[:16]}.pid"
        self._held = False

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid and pid != os.getpid() and _pid_alive(pid):
                    raise LockError(f"Another Cogitator instance (pid {pid}) is using this game folder.")
                try:
                    self.path.unlink()
                except OSError:
                    pass
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            self._held = True
            return self
        raise LockError("Could not acquire instance lock.")

    def release(self) -> None:
        if self._held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self._held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *a):
        self.release()
