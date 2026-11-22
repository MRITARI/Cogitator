"""The user's own window background: validated by magic bytes and size, copied into the config folder, served to the page as a data: URI.
Never an SVG (it can carry script); the format is decided by the file's bytes, not its name."""
from __future__ import annotations

import base64
import shutil
from pathlib import Path

from cogitator.config import config_dir
from cogitator.fsutil import atomic_write_bytes

MAX_BYTES = 4 * 1024 * 1024
EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
STEM = "background"


class BackgroundError(ValueError):
    pass


def sniff(head: bytes) -> tuple[str, str] | None:
    """(mime, extension) from the first bytes, or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def read_valid(src: Path) -> tuple[bytes, str, str]:
    """The file's bytes with its mime and extension; raises BackgroundError with a user-facing text."""
    if src.suffix.lower() not in EXTENSIONS:
        raise BackgroundError("Choose a PNG, JPG or WEBP image (SVG is not allowed).")
    try:
        size = src.stat().st_size
        if size > MAX_BYTES:
            raise BackgroundError(f"That image is larger than {MAX_BYTES // (1024 * 1024)} MB.")
        data = src.read_bytes()
    except OSError as e:
        raise BackgroundError(f"Cannot read that file: {e.strerror or e}") from None
    kind = sniff(data[:16])
    if kind is None or len(data) > MAX_BYTES:
        raise BackgroundError("That file is not a PNG, JPG or WEBP image.")
    return data, kind[0], kind[1]


def stored() -> Path | None:
    for ext in (".png", ".jpg", ".webp"):
        p = config_dir() / f"{STEM}{ext}"
        if p.is_file():
            return p
    return None


def reset() -> None:
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        try:
            (config_dir() / f"{STEM}{ext}").unlink()
        except OSError:
            pass


def store(src: Path) -> None:
    """Validate `src` and make it the background (replacing an earlier one)."""
    data, _, ext = read_valid(src)
    reset()
    config_dir().mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(config_dir() / f"{STEM}{ext}", data)


def data_uri() -> str | None:
    """The stored image as a data: URI (re-validated: the file in the config folder is not trusted either)."""
    p = stored()
    if p is None:
        return None
    try:
        data, mime, _ = read_valid(p)
    except BackgroundError:
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
