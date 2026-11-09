"""Builds the three release assets from dist/cogitator and signs them. Nothing is uploaded, tagged or pushed.

    python packaging/release.py --version 1.2.3 --key C:\\path\\to\\cogitator-signing.key

The private key (PKCS8 PEM) comes from --key or the COGITATOR_SIGNING_KEY_FILE environment variable and is never copied
anywhere. Output goes to release/ (git-ignored):
    Cogitator-<ver>-win-x64.zip, Cogitator-<ver>-win-x64.zip.sig (base64),
    Cogitator-<ver>-win-x64-installer.exe, SHA256SUMS (covers the zip and the installer), SHA256SUMS.sig (signature of that list)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def find_iscc() -> str | None:
    found = shutil.which("ISCC")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base and (Path(base) / "Inno Setup 6" / "ISCC.exe").is_file():
            return str(Path(base) / "Inno Setup 6" / "ISCC.exe")
    return None


def build_installer(version: str, out: Path) -> Path:
    iscc = find_iscc()
    if not iscc:
        raise SystemExit("Inno Setup 6 (ISCC.exe) not found; install it or pass --no-installer")
    subprocess.run([iscc, "/Qp", f"/DAppVersion={version}", f"/O{out.resolve()}", str(Path(__file__).with_name("cogitator.iss"))], check=True)
    exe = out / f"Cogitator-{version}-win-x64-installer.exe"
    if not exe.is_file():
        raise SystemExit(f"installer was not produced: {exe}")
    return exe


def build(version: str, app_dir: Path, out: Path, key_path: Path, installer: bool = True) -> list[Path]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if not re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?", version):
        raise SystemExit("version must look like 1.2.3 (or 1.2.3-beta.1)")
    if not (app_dir / "cogitator.exe").is_file():
        raise SystemExit(f"{app_dir} does not contain cogitator.exe; build it first (packaging\\build_exe.ps1)")
    key = serialization.load_pem_private_key(key_path.read_bytes(), None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("the signing key is not an Ed25519 key")
    out.mkdir(parents=True, exist_ok=True)
    name = f"Cogitator-{version}-win-x64.zip"
    zpath = out / name
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(x for x in app_dir.rglob("*") if x.is_file()):
            z.write(p, p.relative_to(app_dir).as_posix())  # the app folder's contents sit at the zip root
    data = zpath.read_bytes()
    sig = out / (name + ".sig")
    sig.write_bytes(base64.b64encode(key.sign(data)) + b"\n")
    assets = [zpath]
    if installer:
        assets.append(build_installer(version, out))
    sums = out / "SHA256SUMS"
    sums.write_text("".join(f"{hashlib.sha256(a.read_bytes()).hexdigest()}  {a.name}\n" for a in assets), encoding="utf-8", newline="\n")
    sums_sig = out / "SHA256SUMS.sig"  # lets anyone check the installer too: cogitator verify-download SHA256SUMS <file>
    sums_sig.write_bytes(base64.b64encode(key.sign(sums.read_bytes())) + b"\n")
    return [*assets, sig, sums, sums_sig]


def check_versions(version: str) -> None:
    """The version baked into the app must equal the release version, or the updater would offer the release forever."""
    root = Path(__file__).resolve().parents[1]
    found = {"pyproject.toml": re.search(r'(?m)^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8")),
             "cogitator/__init__.py": re.search(r'__version__\s*=\s*"([^"]+)"', (root / "cogitator" / "__init__.py").read_text(encoding="utf-8"))}
    bad = [f"{n} says {m.group(1) if m else '?'}" for n, m in found.items() if not m or m.group(1) != version]
    if bad:
        raise SystemExit(f"--version {version} disagrees with the source: " + "; ".join(bad) + ". Bump them first.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", required=True)
    p.add_argument("--key", default=os.environ.get("COGITATOR_SIGNING_KEY_FILE"), help="private key file (PKCS8 PEM), kept outside the repo")
    p.add_argument("--app-dir", default="dist/cogitator")
    p.add_argument("--out", default="release")
    p.add_argument("--no-build", action="store_true", help="reuse the existing dist/ instead of running PyInstaller")
    p.add_argument("--no-installer", action="store_true", help="skip the Inno Setup installer")
    a = p.parse_args(argv)
    if not a.key:
        raise SystemExit("give the private key with --key or COGITATOR_SIGNING_KEY_FILE")
    if not Path(a.key).is_file():
        raise SystemExit("signing key file not found")
    check_versions(a.version)
    if not a.no_build:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(Path(__file__).with_name("build_exe.ps1"))], check=True)
    for f in build(a.version, Path(a.app_dir), Path(a.out), Path(a.key), installer=not a.no_installer):
        print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
