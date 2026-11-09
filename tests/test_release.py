"""Release script -> updater, end to end and offline: what the script produces is exactly what the app accepts."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cogitator import cli, selfupdate as su
from cogitator.config import Settings
from cogitator.registry import Registry
from cogitator.selfupdate import Layout, SelfUpdater, UpdateError

spec = importlib.util.spec_from_file_location("release", Path(__file__).parents[1] / "packaging" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

DL = "https://github.com/MRITARI/Cogitator/releases/download/v1.2.3"


@pytest.fixture
def built(tmp_path):
    key = Ed25519PrivateKey.generate()
    kp = tmp_path / "outside" / "cogitator-signing.key"
    kp.parent.mkdir()
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    app = tmp_path / "dist" / "cogitator"
    (app / "_internal").mkdir(parents=True)
    (app / "cogitator.exe").write_bytes(b"NEWEXE")
    (app / "_internal" / "lib.dll").write_bytes(b"dll")
    out = tmp_path / "release"
    files = release.build("1.2.3", app, out, kp)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return files, pub, tmp_path


def serve(files, mk=None):
    by = {f.name: f.read_bytes() for f in files}
    listing = [{"tag_name": "v1.2.3", "draft": False, "prerelease": False, "body": "notes", "assets": [
        {"name": n, "browser_download_url": f"{DL}/{n}", "size": len(b)} for n, b in by.items()]}]

    def h(req):
        if req.url.host == "api.github.com":
            return httpx.Response(200, json=listing, headers={"etag": '"e"'})
        return httpx.Response(200, content=by[req.url.path.rsplit("/", 1)[1]])

    return lambda: (mk or su.make_client)(httpx.MockTransport(h))


def test_the_assets_the_script_makes_are_accepted_by_the_updater(built):
    files, pub, tmp = built
    assert [f.name for f in files] == ["Cogitator-1.2.3-win-x64.zip", "Cogitator-1.2.3-win-x64.zip.sig", "SHA256SUMS"]
    root = tmp / "Cogitator"
    (root / "app").mkdir(parents=True)
    (root / "install.json").write_text("{}")
    u = SelfUpdater(Registry.load(), Settings(), current="1.0.0", layout=Layout("installer", root), client_factory=serve(files), public_key=pub)
    rd = u.stage(u.check().latest)
    assert (rd.dir / "cogitator.exe").read_bytes() == b"NEWEXE" and (rd.dir / "_internal" / "lib.dll").read_bytes() == b"dll"


def test_a_different_key_or_a_modified_zip_is_rejected(built):
    files, pub, tmp = built
    other = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    root = tmp / "Cogitator"
    (root / "app").mkdir(parents=True)
    (root / "install.json").write_text("{}")
    for key, mutate in ((other, False), (pub, True)):
        if mutate:
            z = files[0]
            z.write_bytes(z.read_bytes() + b"x")
            files[2].write_text(__import__("hashlib").sha256(z.read_bytes()).hexdigest() + "  " + z.name + "\n")  # even with matching sums
        reg = Registry.load()
        reg.data["selfupdate"] = {}
        u = SelfUpdater(reg, Settings(), current="1.0.0", layout=Layout("installer", root), client_factory=serve(files), public_key=key)
        with pytest.raises(UpdateError):
            u.stage(u.check(force=True).latest)


def test_script_refuses_bad_input(built, tmp_path):
    files, _, tmp = built
    with pytest.raises(SystemExit):
        release.build("v1", tmp / "dist" / "cogitator", tmp / "o", tmp / "outside" / "cogitator-signing.key")
    with pytest.raises(SystemExit):
        release.build("1.0.0", tmp / "nothing", tmp / "o", tmp / "outside" / "cogitator-signing.key")
    with pytest.raises(SystemExit):
        release.main(["--version", "1.0.0"])  # no key given anywhere


def test_the_release_key_is_not_in_the_repo_and_the_default_build_refuses_updates():
    assert su.RELEASE_PUBLIC_KEY_HEX == "" or len(su.RELEASE_PUBLIC_KEY_HEX) == 64
    repo = Path(__file__).parents[1]
    assert "release/" in (repo / ".gitignore").read_text() and "*.key" in (repo / ".gitignore").read_text()


def test_cli_self_update_paths(capsys, built, monkeypatch):
    files, pub, tmp = built
    assert cli.main(["self-update", "--check"]) == 1  # no key built in: refuses, says why
    assert "no release key" in capsys.readouterr().out
    monkeypatch.setattr(su, "RELEASE_PUBLIC_KEY_HEX", pub)
    real = su.make_client
    factory = serve(files, real)
    monkeypatch.setattr(su, "make_client", lambda *a, **k: factory())
    monkeypatch.setattr(su, "__version__", "1.0.0")
    monkeypatch.setattr(su, "detect_layout", lambda: Layout("pipx"))
    assert cli.main(["self-update", "--check"]) == 0
    assert "Update available: v1.2.3" in capsys.readouterr().out
    assert cli.main(["self-update"]) == 0 and "pipx upgrade cogitator" in capsys.readouterr().out
    assert cli.main(["self-update", "--apply"]) == 1 and "pipx upgrade cogitator" in capsys.readouterr().err
    assert cli.main(["self-update", "--rollback"]) == 1 and "pipx upgrade cogitator" in capsys.readouterr().err
    assert cli.main(["self-update", "--channel", "beta", "--check"]) == 0
    assert Settings.load().update_channel == "beta"
