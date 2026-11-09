"""Builds the three release assets from dist/cogitator and signs them. Nothing is uploaded, tagged or pushed.

    python packaging/release.py --version 1.2.3 --key C:\\path\\to\\cogitator-signing.key

The private key (PKCS8 PEM) comes from --key or the COGITATOR_SIGNING_KEY_FILE environment variable and is never copied
anywhere. Output goes to release/ (git-ignored):
    Cogitator-<ver>-win-x64.zip, Cogitator-<ver>-win-x64.zip.sig (base64), SHA256SUMS
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import sys
import zipfile
from pathlib import Path


def build(version: str, app_dir: Path, out: Path, key_path: Path) -> list[Path]:
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
    sums = out / "SHA256SUMS"
    sums.write_text(f"{hashlib.sha256(data).hexdigest()}  {name}\n", encoding="utf-8", newline="\n")
    sig = out / (name + ".sig")
    sig.write_bytes(base64.b64encode(key.sign(data)) + b"\n")
    return [zpath, sig, sums]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", required=True)
    p.add_argument("--key", default=os.environ.get("COGITATOR_SIGNING_KEY_FILE"), help="private key file (PKCS8 PEM), kept outside the repo")
    p.add_argument("--app-dir", default="dist/cogitator")
    p.add_argument("--out", default="release")
    a = p.parse_args(argv)
    if not a.key:
        raise SystemExit("give the private key with --key or COGITATOR_SIGNING_KEY_FILE")
    for f in build(a.version, Path(a.app_dir), Path(a.out), Path(a.key)):
        print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
