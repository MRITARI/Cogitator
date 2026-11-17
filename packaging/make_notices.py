"""Writes THIRD-PARTY-NOTICES.txt: the licence and NOTICE texts of everything bundled into the frozen programs.

    python packaging/make_notices.py --out dist/cogitator/THIRD-PARTY-NOTICES.txt \\
        --pyz build/pyi/cogitator/PYZ-00.toc --pyz "build/pyi-setup/Cogitator Setup/PYZ-00.toc" --app-dir dist/cogitator

Run it with the interpreter the programs were built with (.venv-release): everything is read from that environment's installed
package metadata, never from memory. What is covered: the runtime dependency closure of pyproject.toml, any further
distribution that PyInstaller's PYZ tables (--pyz) show was bundled, the Python runtime, Tcl/Tk, the PyInstaller bootloader
exception, the OpenSSL that the cryptography wheel links in (from the wheel's own SBOM), and certifi's CA file.
If a licence text cannot be found for any of them the script prints the list and exits non-zero without writing anything."""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import importlib.metadata as md
import json
import re
import sys
from pathlib import Path, PurePath

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
TEXT_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")
SKIP = {"cogitator", "pyinstaller", "pyinstaller-hooks-contrib"}  # ours; PyInstaller has its own section below
PYTHON_LICENSE_NAMES = ("LICENSE.txt", "LICENSE_PYTHON.txt")  # python.org / Store build, conda build
SECTION = "=" * 78


class NoticesError(SystemExit):
    pass


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_dependencies() -> list[str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    reqs = [Requirement(d) for d in deps]
    return [q.name for q in reqs if q.marker is None or q.marker.evaluate({"extra": ""})]  # e.g. tomli only below 3.11


def runtime_closure(roots: list[str]) -> tuple[dict[str, md.Distribution], list[str]]:
    """(name -> distribution, names that are required on this platform but not installed)."""
    found: dict[str, md.Distribution] = {}
    absent: list[str] = []
    todo: list[tuple[str, tuple[str, ...]]] = [(r, ()) for r in roots]
    seen: set[tuple[str, tuple[str, ...]]] = set()
    while todo:
        name, extras = todo.pop()
        key = (norm(name), tuple(sorted(extras)))
        if key in seen:
            continue
        seen.add(key)
        try:
            d = md.distribution(name)
        except md.PackageNotFoundError:
            absent.append(name)
            continue
        found[norm(name)] = d
        for r in d.requires or []:
            q = Requirement(r)
            if q.marker is None or any(q.marker.evaluate({"extra": e}) for e in (extras or ("",))):
                todo.append((q.name, tuple(q.extras)))
    return found, absent


def packages_distributions() -> dict[str, list[str]]:
    """md.packages_distributions(), plus owners found from RECORD for wheels without top_level.txt (Python 3.10 skips those)."""
    out = {k: list(v) for k, v in md.packages_distributions().items()}
    for d in md.distributions():
        if d.read_text("top_level.txt"):
            continue
        name = d.metadata["Name"]
        for f in d.files or []:
            parts = PurePath(f).parts
            top = parts[0] if len(parts) > 1 else PurePath(f).stem if PurePath(f).suffix == ".py" else ""
            if top and not top.endswith((".dist-info", ".egg-info", ".data")) and top != "__pycache__" and name not in out.setdefault(top, []):
                out[top].append(name)
    return out


def bundled_from_pyz(tocs: list[Path]) -> set[str]:
    """Distributions behind the top-level modules PyInstaller put into its PYZ archives."""
    pd = packages_distributions()
    out: set[str] = set()
    for t in tocs:
        data = ast.literal_eval(Path(t).read_text(encoding="utf-8"))
        mods = data[1] if isinstance(data, tuple) else data
        for m in mods:
            out.update(pd.get(m[0].split(".")[0], []))
    return {norm(n) for n in out}


def read(p: Path) -> str:
    return p.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n").strip("\n")


def _text_files(d: md.Distribution) -> list[tuple[str, str]]:
    """[(path inside the distribution, text)] for every LICENSE/NOTICE/COPYING file it ships; identical texts are merged."""
    found: dict[str, list[str]] = {}
    for f in d.files or []:
        if PurePath(f).name.upper().startswith(TEXT_NAMES):
            p = Path(d.locate_file(f))
            if p.is_file():
                found.setdefault(read(p), []).append(PurePath(f).as_posix())
    if not found:  # nothing in dist-info: look in the package folders themselves
        for top in (d.read_text("top_level.txt") or "").split():
            base = Path(d.locate_file(top))
            for p in sorted(base.glob("*")) if base.is_dir() else []:
                if p.is_file() and p.name.upper().startswith(TEXT_NAMES):
                    found.setdefault(read(p), []).append(f"{top}/{p.name}")
    return [("; ".join(sorted(paths)), text) for text, paths in found.items() if text.strip()]


def licence_name(d: md.Distribution) -> str:
    m = d.metadata
    lic = (m.get("License-Expression") or "").strip()
    if not lic:
        first = ((m.get("License") or "").strip().splitlines() or [""])[0]
        lic = first if 0 < len(first) <= 60 else ""
    if not lic:
        lic = ", ".join(c.split("::")[-1].strip() for c in (m.get_all("Classifier") or []) if c.startswith("License"))
    return lic or "see the text below"


def pyinstaller_exception() -> str:
    d = md.distribution("pyinstaller")
    for f in d.files or []:
        if PurePath(f).name.upper().startswith("COPYING"):
            text = read(Path(d.locate_file(f)))
            m = re.search(r"(?ms)^Bootloader Exception\n-+\n(.*?)(?=^Bootloader and Related Files)", text)
            if m and "unlimited permission" in m.group(1):
                return "Bootloader Exception\n--------------------\n" + m.group(1).strip()
    raise NoticesError("PyInstaller's COPYING.txt does not contain the Bootloader Exception; not claiming it.")


def python_runtime() -> tuple[str, str]:
    for name in PYTHON_LICENSE_NAMES:
        p = Path(sys.base_prefix) / name
        if p.is_file():
            return f"Python {sys.version.split()[0]} (runtime licence file {p.name})", read(p)
    raise NoticesError(f"Python licence file not found in {sys.base_prefix} (looked for {', '.join(PYTHON_LICENSE_NAMES)}).")


def tcl_tk_terms() -> str | None:
    """The Tcl/Tk licence terms, or None when this Python has no tkinter (so nothing of it is bundled)."""
    try:
        import tkinter

        lib = Path(tkinter.Tcl().eval("info library"))
    except Exception:
        return None
    for p in (lib.parent / "tk8.6" / "license.terms", lib / "license.terms", lib.parent / "tk8.6" / "docs" / "license.terms"):
        if p.is_file():
            return read(p)
    raise NoticesError(f"Tcl/Tk is bundled but its license.terms was not found near {lib}.")


def _record_hash(d: md.Distribution, suffix: str) -> tuple[Path, str] | None:
    for f in d.files or []:
        if PurePath(f).as_posix().endswith(suffix) and f.hash:
            return Path(d.locate_file(f)), f.hash.value
    return None


def certifi_unmodified(extra_copy: Path | None) -> str:
    """Verifies cacert.pem against the hash in certifi's own RECORD (and the frozen copy, when given); returns the sentence to print."""
    d = md.distribution("certifi")
    rec = _record_hash(d, "certifi/cacert.pem")
    if not rec:
        raise NoticesError("certifi: cacert.pem has no hash in RECORD; cannot say it is unmodified.")
    for p in [rec[0]] + ([extra_copy] if extra_copy and extra_copy.is_file() else []):
        got = base64.urlsafe_b64encode(hashlib.sha256(p.read_bytes()).digest()).rstrip(b"=").decode()
        if got != rec[1]:
            raise NoticesError(f"certifi: {p} differs from the hash in certifi's RECORD; it is modified.")
    return "The bundled CA file (certifi/cacert.pem) is unmodified: its SHA-256 matches certifi's own RECORD."


def openssl_in_cryptography(apache_text: str | None) -> str | None:
    d = md.distribution("cryptography")
    for f in d.files or []:
        if PurePath(f).name == "sbom.json":
            try:
                comps = json.loads(Path(d.locate_file(f)).read_text(encoding="utf-8")).get("components", [])
            except (OSError, ValueError):
                continue
            for c in comps:
                if c.get("name") == "openssl":
                    if not apache_text:
                        raise NoticesError("cryptography links OpenSSL but ships no LICENSE.APACHE text to include.")
                    url = next((r["url"] for r in c.get("externalReferences", []) if "url" in r), "https://github.com/openssl/openssl")
                    return (f"The cryptography wheel statically links OpenSSL {c.get('version', '?')} (source: {url}).\n"
                            "OpenSSL is distributed under the Apache License 2.0; the text is the one shipped with cryptography as\n"
                            "LICENSE.APACHE (reproduced under cryptography above). OpenSSL's own LICENSE file is not part of the wheel.")
    return None


def collect(pyz: list[Path] | None = None, app_dir: Path | None = None) -> tuple[list[str], list[str]]:
    """(notice sections, problems). Problems is empty when every bundled component has its text."""
    problems: list[str] = []
    dists, absent = runtime_closure(declared_dependencies())
    problems += [f"{n}: required but not installed in this environment" for n in absent]
    for n in bundled_from_pyz(pyz or []) - SKIP:
        if n not in dists:
            try:
                dists[n] = md.distribution(n)
            except md.PackageNotFoundError:
                problems.append(f"{n}: bundled but not installed")
    dists = {k: v for k, v in dists.items() if k not in SKIP}
    sections: list[str] = []
    index: list[str] = []
    apache = None
    for key in sorted(dists):
        d = dists[key]
        title = f"{d.metadata['Name']} {d.version}"
        texts = _text_files(d)
        if not texts:
            problems.append(f"{title}: no licence text found in its metadata or package folder")
            continue
        if key == "cryptography":
            apache = next((t for p, t in texts if "LICENSE.APACHE" in p), None)
        index.append(f"  {title}  ({licence_name(d)})")
        body = "\n\n".join(f"--- {p} ---\n{t}" for p, t in texts)
        sections.append(f"{SECTION}\n{title}   License: {licence_name(d)}\n{SECTION}\n{body}")
        if key == "certifi":
            try:
                sections[-1] += ("\n\n--- MPL-2.0 notice ---\ncertifi is licensed under the Mozilla Public License 2.0 (text above).\n"
                                 "Source code: https://github.com/certifi/python-certifi\n"
                                 + certifi_unmodified(app_dir / "_internal" / "certifi" / "cacert.pem" if app_dir else None))
            except NoticesError as e:
                problems.append(str(e))
    extra: list[str] = []
    for label, fn in (("Python runtime", python_runtime),):
        try:
            name, text = fn()
            index.append(f"  {name}  (PSF-2.0, plus the components named in the text)")
            extra.append(f"{SECTION}\n{name}\n{SECTION}\n{text}")
        except NoticesError as e:
            problems.append(f"{label}: {e}")
    try:
        tk = tcl_tk_terms()
        if tk:
            index.append("  Tcl/Tk 8.6  (Tcl/Tk licence)")
            extra.append(f"{SECTION}\nTcl/Tk (bundled with the Python runtime for the setup window)\n{SECTION}\n{tk}")
    except NoticesError as e:
        problems.append(f"Tcl/Tk: {e}")
    try:
        index.append("  PyInstaller bootloader  (GPL-2.0-or-later with the Bootloader Exception)")
        extra.append(f"{SECTION}\nPyInstaller bootloader\n{SECTION}\nThe .exe files start with PyInstaller's bootloader "
                     "(https://pyinstaller.org). Its licence, from PyInstaller's COPYING.txt:\n\n" + pyinstaller_exception())
    except (NoticesError, md.PackageNotFoundError) as e:
        index.pop()
        problems.append(f"PyInstaller: {e}")
    try:
        ssl = openssl_in_cryptography(apache)
        if ssl:
            index.append("  OpenSSL (inside the cryptography wheel)  (Apache-2.0)")
            extra.append(f"{SECTION}\nOpenSSL\n{SECTION}\n{ssl}")
    except NoticesError as e:
        problems.append(str(e))
    head = ("THIRD-PARTY NOTICES\n\nCogitator itself is MIT licensed (see LICENSE). The program bundles the software below; each part is\n"
            "used under its own licence, whose full text follows.\n\nComponents:\n" + "\n".join(index) + "\n")
    return [head, *sections, *extra], problems


def generate(pyz: list[Path] | None = None, app_dir: Path | None = None) -> str:
    sections, problems = collect(pyz, app_dir)
    if problems:
        raise NoticesError("THIRD-PARTY-NOTICES.txt was NOT written; fix these first:\n  " + "\n  ".join(problems))
    return "\n\n".join(sections) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="dist/cogitator/THIRD-PARTY-NOTICES.txt")
    p.add_argument("--pyz", action="append", default=[], help="a PyInstaller PYZ-00.toc (repeatable)")
    p.add_argument("--app-dir", help="the frozen app folder, to check its certifi CA file too")
    a = p.parse_args(argv)
    text = generate([Path(x) for x in a.pyz], Path(a.app_dir) if a.app_dir else None)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {out} ({len(text.encode('utf-8')):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
