"""Tolerant parsing of Nexus archive file names, and matching an archive to its Nexus file.

Two name forms occur; neither carries a file id:
  space form: "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z ndQ1md9gG.zip"  (mod id, version, ISO time, tag)
  dash form:  "Darktide Mod Loader-19-25-12-02-1764720822.zip"                    (mod id, dashed version, upload time)
The file id comes from matching the name against `file_name` in files.json. Browser suffixes like " (1)" and directory
prefixes are ignored; names without a mod id are marked `ambiguous`."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath

_EXT = re.compile(r"\.(zip|7z|rar|tar|gz)$", re.I)
_DUP = re.compile(r"\s*\(\d+\)\s*$")

_SPACE = re.compile(
    r"^(?P<name>.+?)[ _](?P<mod>\d{1,8})[ _](?P<ver>v?\d+(?:[.\-_]\d+)*[A-Za-z]?)[ _]"
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}[-:]\d{2}(?:[-:]\d{2})?Z?)(?:[ _](?P<tag>[A-Za-z0-9_\-]+))?$")
_DASH = re.compile(r"^(?P<name>.+?)-(?P<mod>\d{1,8})-(?P<rest>[0-9A-Za-z]+(?:-[0-9A-Za-z]+)*)-(?P<ts>\d{9,11})$")
_DOTTED = re.compile(r"(?<![\d.])v?(\d+(?:\.\d+){1,3}[A-Za-z]?)(?![\d.])")


@dataclass
class ParsedArchive:
    raw: str
    form: str  # space | dash | fallback | none
    name: str | None = None
    mod_id: int | None = None
    version: str | None = None
    timestamp: str | int | None = None
    ambiguous: bool = False


def parse_archive_name(filename: str) -> ParsedArchive:
    base = PurePath(filename.replace("\\", "/")).name
    stem = _DUP.sub("", _EXT.sub("", base.strip())).strip()
    stem = _EXT.sub("", stem)  # .tar.gz
    out = ParsedArchive(raw=filename, form="none")

    m = _SPACE.match(stem)
    if m:
        out.form, out.name = "space", m["name"].strip()
        out.mod_id, out.version, out.timestamp = int(m["mod"]), m["ver"].lstrip("v"), m["ts"]
        return out

    m = _DASH.match(stem)
    if m:
        # Live data (mods 19 and 8): dash names are <Name>-<mod_id>-<version tokens>-<upload ts>; NO file id.
        out.form, out.name, out.mod_id, out.timestamp = "dash", m["name"].strip(), int(m["mod"]), int(m["ts"])
        out.version = ".".join(m["rest"].split("-"))
        return out

    v = _DOTTED.search(stem)
    if v:
        out.form, out.version = "fallback", v.group(1)
    out.ambiguous = True  # no mod id derivable from the name
    return out


# ---------------------------------------------------------------- matching against Nexus files.json

@dataclass
class FileMatch:
    status: str  # exact | inferred | ambiguous | none
    files: list  # list[NexusFile]


def _clean(name: str) -> str:
    base = PurePath(name.replace("\\", "/")).name.strip()
    return _DUP.sub("", _EXT.sub("", base)).strip().casefold()


def _space_ts_to_epoch(ts: str) -> int | None:
    from datetime import datetime, timezone

    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2})[-:](\d{2})", ts)
    if not m:
        return None
    y, mo, d, h, mi = map(int, m.groups())
    try:
        return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def match_archive_to_files(filename: str, files: list, parsed: ParsedArchive | None = None) -> FileMatch:
    """Map a downloaded archive to its Nexus file. Exact `file_name` equality is authoritative; otherwise
    version + upload-time agreement gives an *inferred* match. Several candidates -> ambiguous (ask the user)."""
    want = _clean(filename)
    exact = [f for f in files if f.file_name and _clean(f.file_name) == want]
    if len(exact) == 1:
        return FileMatch("exact", exact)
    if len(exact) > 1:
        return FileMatch("ambiguous", exact)
    p = parsed or parse_archive_name(filename)
    cands = []
    for f in files:
        if p.version is None or f.version != p.version:
            continue
        if isinstance(p.timestamp, int):
            ok = f.uploaded == p.timestamp
        elif isinstance(p.timestamp, str):
            e = _space_ts_to_epoch(p.timestamp)
            ok = e is not None and f.uploaded is not None and abs(f.uploaded - e) < 120
        else:
            ok = False
        if ok:
            cands.append(f)
    if len(cands) == 1:
        return FileMatch("inferred", cands)
    return FileMatch("ambiguous" if cands else "none", cands)
