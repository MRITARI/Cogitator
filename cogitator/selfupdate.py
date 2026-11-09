"""Cogitator's own updates: look for a newer release on GitHub, download it, verify it, stage it, and hand over to the
updater helper. Only Cogitator itself is updated here; the Mod Loader, Framework and mods keep their confirm-gated flows.

Trust rules: an update is installed only if its Ed25519 signature verifies against the public key built into this
build, it is a newer stable release (or beta on the beta channel), and every URL is https on a GitHub host. A hash
published next to a download only detects corruption, so the signature is what protects against a bad repository.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from . import __version__
from .config import Settings, config_dir
from .loader import LoaderError, safe_extract
from .registry import Registry

log = logging.getLogger(__name__)

UPDATE_REPO = os.environ.get("COGITATOR_UPDATE_REPO_FOR_TESTS") or "MRITARI/Cogitator"
RELEASE_PUBLIC_KEY_HEX = ""  # the release signing public key (64 hex characters); empty = updates are disabled in this build
API = "https://api.github.com"
ALLOWED_HOSTS = frozenset({"github.com", "api.github.com", "objects.githubusercontent.com",
                           "github-releases.githubusercontent.com", "release-assets.githubusercontent.com"})
REQUIRED_EXE = "cogitator.exe"
MAX_ZIP = 200 * 1024 * 1024
MAX_UNPACKED = 600 * 1024 * 1024
MAX_FILES = 20000
MAX_SIG = 4096
MAX_SUMS = 65536
MAX_NOTES = 4000
RESULT_FILE = "update_result.json"


class UpdateError(Exception):
    """Something about an update cannot be trusted or done. The message is plain language for the user."""


# ---------------------------------------------------------------- versions

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: tuple = ()

    @classmethod
    def parse(cls, text: str) -> "Version | None":
        m = _SEMVER.match((text or "").strip())
        if not m:
            return None
        pre = tuple(m.group(4).split(".")) if m.group(4) else ()
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)

    def _key(self) -> tuple:
        ids = tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in self.pre)
        return (self.major, self.minor, self.patch, (0, *ids) if self.pre else (1,))

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __le__(self, other: "Version") -> bool:
        return self._key() <= other._key()

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}" + (("-" + ".".join(self.pre)) if self.pre else "")


# ---------------------------------------------------------------- hosts and signatures

def check_host(url: str) -> None:
    u = urlsplit(url)
    if u.scheme != "https" or u.username or u.password or (u.port not in (None, 443)):
        raise UpdateError("Refused a download address that is not plain https.")
    if (u.hostname or "").lower() not in ALLOWED_HOSTS:
        raise UpdateError(f"Refused a download from {u.hostname}: updates only come from GitHub.")


def _signature_bytes(sig: bytes) -> bytes:
    if len(sig) == 64:
        return sig  # raw form: checked before stripping, since a raw signature can start or end with a whitespace byte
    sig = sig.strip()
    for decode in (lambda s: base64.b64decode(s, validate=True), lambda s: binascii.unhexlify(s)):
        try:
            out = decode(sig)
            if len(out) == 64:
                return out
        except (binascii.Error, ValueError):
            continue
    raise UpdateError("The release signature is not in a recognised format.")


def verify_signature(data: bytes, signature: bytes | None, public_key_hex: str) -> None:
    if not public_key_hex:
        raise UpdateError("This build has no release key configured, so updates are turned off.")
    if not signature:
        raise UpdateError("The release has no signature, so it was not installed.")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
    except ValueError as e:
        raise UpdateError("The release key built into this app is invalid.") from e
    try:
        key.verify(_signature_bytes(signature), data)
    except InvalidSignature as e:
        raise UpdateError("The download's signature does not match, so it was not installed.") from e


def plain_notes(text: str | None, limit: int = MAX_NOTES) -> str:
    """Release notes as inert plain text: control characters removed, length capped, never interpreted as markup."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = "".join(c for c in t if c in "\n\t" or (c.isprintable()))
    return t if len(t) <= limit else t[:limit].rstrip() + "..."


# ---------------------------------------------------------------- releases

@dataclass
class ReleaseInfo:
    version: Version
    tag: str
    prerelease: bool
    notes: str
    zip_url: str
    zip_size: int
    sig_url: str
    sums_url: str
    published: str = ""

    @property
    def zip_name(self) -> str:
        return f"Cogitator-{self.version}-win-x64.zip"

    def summary(self) -> dict:
        return {"version": str(self.version), "tag": self.tag, "prerelease": self.prerelease, "notes": self.notes,
                "published": self.published}


def _trim(releases: list) -> list[dict]:
    out = []
    for r in releases[:10]:
        if not isinstance(r, dict):
            continue
        out.append({"tag_name": r.get("tag_name"), "draft": bool(r.get("draft")), "prerelease": bool(r.get("prerelease")),
                    "body": plain_notes(r.get("body")), "published_at": r.get("published_at") or "",
                    "assets": [{"name": a.get("name"), "browser_download_url": a.get("browser_download_url"), "size": a.get("size") or 0}
                               for a in (r.get("assets") or []) if isinstance(a, dict)]})
    return out


def select_release(releases: list[dict], current: Version, channel: str = "stable", *, skipped: str | None = None,
                   failed: list[str] | tuple = ()) -> ReleaseInfo | None:
    """The newest release that may be offered, or None. Never offers: drafts, prereleases (unless channel is beta), the same or an
    older version (no downgrades), a version the user skipped or one that already failed to install, or a release missing
    its zip / signature / SHA256SUMS."""
    best: ReleaseInfo | None = None
    for r in releases:
        if r.get("draft"):
            continue
        tag = r.get("tag_name") or ""
        v = Version.parse(tag)
        if v is None:
            continue
        pre = bool(r.get("prerelease")) or bool(v.pre)
        if pre and channel != "beta":
            continue
        if v <= current:
            continue
        if str(v) in failed or (skipped and str(v) == skipped):
            continue
        assets = {a.get("name"): a for a in (r.get("assets") or []) if isinstance(a, dict)}
        zip_name = f"Cogitator-{v}-win-x64.zip"
        z, s, c = assets.get(zip_name), assets.get(zip_name + ".sig"), assets.get("SHA256SUMS")
        if not (z and s and c):
            log.info("release %s skipped: missing zip, signature or SHA256SUMS", tag)
            continue
        try:
            for a in (z, s, c):
                check_host(a.get("browser_download_url") or "")
        except UpdateError as e:
            log.warning("release %s skipped: %s", tag, e)
            continue
        info = ReleaseInfo(v, tag, pre, plain_notes(r.get("body")), z["browser_download_url"], int(z.get("size") or 0),
                           s["browser_download_url"], c["browser_download_url"], r.get("published_at") or "")
        if best is None or best.version < v:
            best = info
    return best


# ---------------------------------------------------------------- how this copy of Cogitator is installed

@dataclass
class Layout:
    kind: str  # installer | portable | pipx | source | pip
    root: Path | None = None  # installer: <root>/app, <root>/app.previous, <root>/staging, <root>/install.json
    helper: list[str] | None = None  # test seam: argv prefix for the helper instead of copying cogitator-updater.exe

    @property
    def in_place(self) -> bool:
        return self.kind == "installer"

    @property
    def app_dir(self) -> Path | None:
        return self.root / "app" if self.root else None

    @property
    def previous_dir(self) -> Path | None:
        return self.root / "app.previous" if self.root else None

    @property
    def staging_dir(self) -> Path | None:
        return self.root / "staging" if self.root else None

    def guidance(self) -> str:
        return {
            "pipx": "Cogitator was installed with pipx. Update it with:  pipx upgrade cogitator",
            "source": "This is a source checkout; update it with git. Cogitator never changes a source checkout.",
            "portable": "This copy was not set up by the installer. Download the new version and run its installer.",
            "pip": "Cogitator was installed with pip. Update it with:  pip install --upgrade cogitator",
        }.get(self.kind, "")


def detect_layout(*, frozen: bool | None = None, executable: str | None = None, package_file: str | None = None,
                  prefix: str | None = None) -> Layout:
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if frozen:
        app = Path(executable or sys.executable).parent
        root = app.parent
        if app.name == "app" and (root / "install.json").is_file():
            return Layout("installer", root)
        return Layout("portable")
    norm = (prefix or sys.prefix).replace("\\", "/").lower()
    if "/pipx/" in norm and "/venvs/" in norm:
        return Layout("pipx")
    here = Path(package_file or __file__).resolve()
    for parent in list(here.parents)[:3]:
        if (parent / "pyproject.toml").is_file() or (parent / ".git").exists():
            return Layout("source")
    return Layout("pip")


# ---------------------------------------------------------------- the updater

def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """TLS verified, redirects handled by us (so each hop is host-checked), and no identifier beyond the app version."""
    return httpx.Client(timeout=30.0, verify=True, follow_redirects=False, transport=transport,
                        headers={"User-Agent": f"Cogitator/{__version__}", "Accept": "application/vnd.github+json"})


@dataclass
class CheckResult:
    status: str  # disabled | off | throttled | rate-limited | offline | up-to-date | available | error
    latest: ReleaseInfo | None = None
    detail: str = ""


@dataclass
class Ready:
    version: str
    dir: Path
    notes: str = ""


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in Path(root).rglob("*") if x.is_file()):
        h.update(p.relative_to(root).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


class SelfUpdater:
    def __init__(self, registry: Registry, settings: Settings, *, current: str = __version__, layout: Layout | None = None,
                 client_factory: Callable[[], httpx.Client] | None = None, now: Callable[[], float] = time.time,
                 public_key: str | None = None, popen: Callable[..., object] = subprocess.Popen,
                 pid: Callable[[], int] = os.getpid):
        self.reg, self.settings = registry, settings
        self.current = Version.parse(current) or Version(0, 0, 0)
        self.layout = layout or detect_layout()
        self.now, self.popen, self.pid = now, popen, pid
        self.public_key = RELEASE_PUBLIC_KEY_HEX if public_key is None else public_key
        self._client_factory = client_factory or self._default_client
        self.st: dict = registry.data.setdefault("selfupdate", {})

    @staticmethod
    def _default_client() -> httpx.Client:
        return make_client()

    # ---- state
    def _save(self) -> None:
        self.reg.save()

    def failed(self) -> list[str]:
        return list(self.st.get("failed") or [])

    def skip_version(self, version: str) -> None:
        self.st["skipped"] = str(version)
        self._save()

    def remind_later(self, hours: float = 24) -> None:
        self.st["remind_until"] = self.now() + hours * 3600
        self._save()

    def _releases(self) -> list[dict]:
        return list(self.st.get("releases") or [])

    def latest(self) -> ReleaseInfo | None:
        """The newest offerable release according to the cache (no network)."""
        return select_release(self._releases(), self.current, self.settings.update_channel,
                              skipped=self.st.get("skipped"), failed=self.failed())

    def announcement(self) -> ReleaseInfo | None:
        """What the UI should announce right now: a newer release, unless the user chose 'remind me later'."""
        if self.now() < float(self.st.get("remind_until") or 0):
            return None
        return self.latest()

    # ---- checking
    def check(self, *, force: bool = False) -> CheckResult:
        if not self.public_key:
            return CheckResult("disabled", detail="This build has no release key configured, so Cogitator does not look for updates.")
        if not force and self.settings.update_mode == "off":
            return CheckResult("off")
        now = self.now()
        if not force and now < float(self.st.get("backoff_until") or 0):
            return CheckResult("rate-limited", self.latest())
        if not force and now < float(self.st.get("next_check_at") or 0):
            return CheckResult("throttled", self.latest())
        headers = {"If-None-Match": self.st["etag"]} if self.st.get("etag") else {}
        try:
            with self._client_factory() as c:
                r = c.get(f"{API}/repos/{UPDATE_REPO}/releases", params={"per_page": 10}, headers=headers)
        except httpx.HTTPError as e:
            log.debug("update check skipped (offline?): %s", type(e).__name__)
            self.st["next_check_at"] = now + 3600
            self._save()
            return CheckResult("offline", self.latest())
        if r.status_code in (403, 429):
            reset = r.headers.get("x-ratelimit-reset")
            self.st["backoff_until"] = float(reset) if (reset or "").isdigit() else now + 3600
            self._save()
            return CheckResult("rate-limited", self.latest(), "GitHub is limiting requests right now.")
        if r.status_code == 304:
            pass
        elif r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                return CheckResult("error", self.latest(), "GitHub sent something unreadable.")
            if isinstance(data, list):
                self.st["releases"] = _trim(data)
                self.st["etag"] = r.headers.get("etag") or ""
        elif r.status_code == 404:
            self.st["releases"] = []  # no such repository or no releases yet
        else:
            self.st["next_check_at"] = now + 3600
            self._save()
            return CheckResult("error", self.latest(), f"GitHub answered HTTP {r.status_code}.")
        self.st["checked_at"] = now
        self.st["next_check_at"] = now + self.settings.update_check_hours * 3600
        self.st.pop("backoff_until", None)
        self._save()
        latest = self.latest()
        return CheckResult("available" if latest else "up-to-date", latest)

    # ---- downloading, verifying, staging
    def _fetch(self, c: httpx.Client, url: str, limit: int) -> bytes:
        for _ in range(6):
            check_host(url)
            with c.stream("GET", url, headers={"Accept": "*/*"}) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, r.headers.get("location", ""))  # every hop is checked again at the top of the loop
                    continue
                if r.status_code != 200:
                    raise UpdateError(f"The download failed (HTTP {r.status_code}).")
                declared = int(r.headers.get("content-length") or 0)
                if declared > limit:
                    raise UpdateError("The download is larger than allowed, so it was refused.")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > limit:
                        raise UpdateError("The download is larger than allowed, so it was refused.")
                return bytes(buf)
        raise UpdateError("Too many redirects.")

    def stage(self, info: ReleaseInfo) -> Ready:
        if not self.layout.in_place:
            raise UpdateError(self.layout.guidance() or "This installation cannot be updated in place.")
        if not self.public_key:
            raise UpdateError("This build has no release key configured, so updates are turned off.")
        if str(info.version) in self.failed():
            raise UpdateError(f"Version {info.version} already failed to start on this computer, so it will not be tried again.")
        if not (self.current < info.version):
            raise UpdateError("That is not newer than the version you have.")
        if info.zip_size > MAX_ZIP:
            raise UpdateError("The download is larger than allowed, so it was refused.")
        try:
            with self._client_factory() as c:
                zip_bytes = self._fetch(c, info.zip_url, MAX_ZIP)
                sig = self._fetch(c, info.sig_url, MAX_SIG)
                sums = self._fetch(c, info.sums_url, MAX_SUMS).decode("utf-8", "replace")
        except httpx.HTTPError as e:
            raise UpdateError("Could not reach GitHub to download the update.") from e
        digest = hashlib.sha256(zip_bytes).hexdigest()
        listed = {parts[1].lstrip("*"): parts[0].lower() for parts in (ln.split() for ln in sums.splitlines()) if len(parts) == 2}
        if listed.get(info.zip_name) != digest:
            raise UpdateError("The download does not match its checksum list, so it was not installed.")
        verify_signature(zip_bytes, sig, self.public_key)
        dest = self.layout.staging_dir / f"v{info.version}"
        shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="dl-", dir=dest.parent))
        try:
            zp = tmp / "update.zip"
            zp.write_bytes(zip_bytes)
            try:
                safe_extract(zp, dest, MAX_UNPACKED, MAX_FILES)
            except LoaderError as e:
                shutil.rmtree(dest, ignore_errors=True)
                raise UpdateError(f"The update archive was rejected as unsafe: {e}") from e
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not (dest / REQUIRED_EXE).is_file():
            shutil.rmtree(dest, ignore_errors=True)
            raise UpdateError("The update does not contain the program, so it was not installed.")
        self.st["ready"] = {"version": str(info.version), "dir": str(dest), "tree": tree_hash(dest), "notes": info.notes,
                            "zip_sha256": digest, "staged_at": self.now()}
        self._save()
        return Ready(str(info.version), dest, info.notes)

    def ready(self) -> Ready | None:
        r = self.st.get("ready")
        if not r:
            return None
        v = Version.parse(r.get("version", ""))
        d = Path(r.get("dir", ""))
        if v is None or not (self.current < v) or not d.is_dir():
            self.st.pop("ready", None)  # we already run it, or it was removed
            return None
        return Ready(r["version"], d, r.get("notes", ""))

    # ---- automatic behaviour per mode
    def run_automatic(self) -> CheckResult:
        """Startup behaviour: off = nothing; notify = look; download/auto = look, then download + verify + stage a newer release."""
        mode = self.settings.update_mode
        if mode == "off":
            return CheckResult("off")
        res = self.check()
        if res.latest and mode in ("download", "auto") and self.layout.in_place:
            rd = self.ready()
            if not rd or rd.version != str(res.latest.version):
                try:
                    self.stage(res.latest)
                except UpdateError as e:
                    log.info("update not staged: %s", e)
                    res.detail = str(e)
        return res

    def should_apply_automatically(self) -> bool:
        return self.settings.update_mode == "auto" and self.layout.in_place and self.ready() is not None

    # ---- applying and rolling back (hand over to the helper, then the caller exits)
    def _helper_argv(self) -> list[str]:
        if self.layout.helper:
            return list(self.layout.helper)
        src = self.layout.app_dir / "cogitator-updater.exe"
        if not src.is_file():
            raise UpdateError("The updater program is missing from this installation.")
        tmp = Path(tempfile.mkdtemp(prefix="cogitator-update-"))
        dst = tmp / "cogitator-updater.exe"
        shutil.copy2(src, dst)  # run from outside the folder that is about to be replaced
        return [str(dst)]

    def _spawn(self, extra: list[str]) -> None:
        argv = self._helper_argv() + extra
        flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        self.popen(argv, creationflags=flags, close_fds=True, shell=False)

    def _common_args(self, version: str) -> list[str]:
        app = self.layout.app_dir
        exe = str(app / REQUIRED_EXE)
        return ["--app", str(app), "--previous", str(self.layout.previous_dir), "--version", version,
                "--wait-pid", str(self.pid()), "--result", str(config_dir() / RESULT_FILE),
                "--check", json.dumps([exe, "--self-check"]), "--relaunch", json.dumps([exe])]

    def apply(self) -> Ready:
        """Start the helper that swaps in the staged build. The caller must exit right after this returns."""
        if not self.layout.in_place:
            raise UpdateError(self.layout.guidance() or "This installation cannot be updated in place.")
        rd = self.ready()
        if rd is None:
            raise UpdateError("There is no downloaded update waiting.")
        if tree_hash(rd.dir) != self.st["ready"].get("tree"):
            shutil.rmtree(rd.dir, ignore_errors=True)
            self.st.pop("ready", None)
            self._save()
            raise UpdateError("The downloaded update changed on disk after it was verified, so it was discarded.")
        self.st["applying"] = {"version": rd.version, "at": self.now()}
        self._save()
        self._spawn(["--staged", str(rd.dir)] + self._common_args(rd.version))
        return rd

    def rollback(self) -> None:
        if not self.layout.in_place:
            raise UpdateError(self.layout.guidance() or "This installation cannot be rolled back in place.")
        prev = self.layout.previous_dir
        if not (prev and prev.is_dir()):
            raise UpdateError("There is no previous version to go back to.")
        self._spawn(["--rollback"] + self._common_args("previous"))

    # ---- the helper's verdict, read at the next start
    def absorb_result(self) -> dict | None:
        p = config_dir() / RESULT_FILE
        try:
            res = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        status, version = res.get("status"), str(res.get("version") or "")
        if status == "reverted" and version and version not in self.failed():
            self.st["failed"] = self.failed() + [version]  # never offered or staged again: no retry loop
            self.st.pop("ready", None)
        if status == "ok":
            self.st.pop("ready", None)
        self.st.pop("applying", None)
        self.st["last_result"] = res
        self._save()
        try:
            p.unlink()
        except OSError:
            pass
        return res
