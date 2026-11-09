"""Self-update, entirely offline: fake GitHub via httpx.MockTransport, a throwaway Ed25519 key, fake app folders."""
import base64
import hashlib
import io
import json
from pathlib import Path
import zipfile

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cogitator import selfupdate as su
from cogitator.config import Settings, config_dir
from cogitator.registry import Registry
from cogitator.selfupdate import Layout, SelfUpdater, UpdateError, Version, detect_layout, select_release, verify_signature

KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
OTHER_PUB = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
DL = "https://github.com/MRITARI/Cogitator/releases/download"


def make_zip(files=None) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in (files or {"cogitator.exe": b"NEW", "lib/x.dll": b"x"}).items():
            z.writestr(n, d)
    return b.getvalue()


class FakeGitHub:
    def __init__(self, ver="1.1.0", *, pre=False, zip_bytes=None, sign_with=KEY, sig=True, redirect_to=None):
        self.ver, self.pre = ver, pre
        self.zip = zip_bytes if zip_bytes is not None else make_zip()
        self.sig = sign_with.sign(self.zip) if (sig and sign_with) else None
        self.redirect_to = redirect_to
        self.requests: list[httpx.Request] = []
        self.etag = '"abc"'
        self.status = None
        self.signed_zip = self.zip

    def release_list(self):
        name = f"Cogitator-{self.ver}-win-x64.zip"
        assets = [{"name": name, "browser_download_url": f"{DL}/v{self.ver}/{name}", "size": len(self.zip)},
                  {"name": "SHA256SUMS", "browser_download_url": f"{DL}/v{self.ver}/SHA256SUMS", "size": 90}]
        if self.sig is not None:
            assets.append({"name": name + ".sig", "browser_download_url": f"{DL}/v{self.ver}/{name}.sig", "size": 64})
        return [{"tag_name": f"v{self.ver}", "draft": False, "prerelease": self.pre, "body": "Fixes <b>things</b>\x1b[31m", "assets": assets}]

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.status:
            return httpx.Response(self.status, headers={"x-ratelimit-reset": "9999999999"})
        if req.url.host == "api.github.com":
            if req.headers.get("if-none-match") == self.etag:
                return httpx.Response(304)
            return httpx.Response(200, json=self.release_list(), headers={"etag": self.etag})
        path = req.url.path
        if self.redirect_to and path.endswith(".zip"):
            return httpx.Response(302, headers={"location": self.redirect_to})
        name = f"Cogitator-{self.ver}-win-x64.zip"
        if path.endswith(".zip") or req.url.host == "objects.githubusercontent.com":
            return httpx.Response(200, content=self.zip)
        if path.endswith(".sig"):
            return httpx.Response(200, content=self.sig)
        if path.endswith("SHA256SUMS"):
            return httpx.Response(200, text=f"{hashlib.sha256(self.signed_zip).hexdigest()}  {name}\n")
        return httpx.Response(404)

    def factory(self):
        return lambda: su.make_client(httpx.MockTransport(self.handler))


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "Cogitator"
    (r / "app").mkdir(parents=True)
    (r / "app" / "cogitator.exe").write_bytes(b"OLD")
    (r / "install.json").write_text("{}", encoding="utf-8")
    return r


def updater(gh, root=None, *, current="1.0.0", settings=None, key=PUB, now=lambda: 1000.0, popen=None, layout=None):
    reg = Registry.load()
    s = settings or Settings()
    lay = layout or (Layout("installer", root, helper=["HELPER"]) if root else Layout("pip"))
    return SelfUpdater(reg, s, current=current, layout=lay, client_factory=gh.factory(), now=now, public_key=key,
                       popen=popen or (lambda *a, **k: None), pid=lambda: 4242)


# ---- versions
def test_semver_ordering_and_prerelease_rules():
    p = Version.parse
    assert p("v1.2.3") and p("1.2.3") == p("v1.2.3") and p("1.2") is None and p("junk") is None
    assert p("1.0.0") < p("1.0.1") < p("1.1.0") < p("2.0.0") and p("1.0.0") < p("1.0.10")
    assert p("1.0.0-beta.1") < p("1.0.0-beta.2") < p("1.0.0-beta.10") < p("1.0.0") and p("1.0.0-alpha") < p("1.0.0-beta")


# ---- signatures
def test_signature_valid_tampered_wrong_key_and_missing():
    data = b"payload"
    sig = KEY.sign(data)
    verify_signature(data, sig, PUB)
    verify_signature(data, base64.b64encode(sig) + b"\n", PUB)  # base64 form accepted
    for bad in (lambda: verify_signature(data + b"x", sig, PUB), lambda: verify_signature(data, sig, OTHER_PUB),
                lambda: verify_signature(data, None, PUB), lambda: verify_signature(data, b"", PUB),
                lambda: verify_signature(data, b"short", PUB), lambda: verify_signature(data, sig, ""),
                lambda: verify_signature(data, sig, "zz")):
        with pytest.raises(UpdateError):
            bad()


# ---- selection
def rel(tag, pre=False, assets=True, draft=False):
    v = tag.lstrip("v")
    n = f"Cogitator-{v}-win-x64.zip"
    names = [n, n + ".sig", "SHA256SUMS"] if assets else [n]
    return {"tag_name": tag, "prerelease": pre, "draft": draft, "body": "", "assets": [
        {"name": x, "browser_download_url": f"{DL}/{tag}/{x}", "size": 5} for x in names]}


def test_selection_refuses_downgrade_same_prerelease_draft_incomplete_skipped_failed():
    cur = Version.parse("1.2.0")
    assert select_release([rel("v1.1.0"), rel("v1.2.0")], cur) is None  # older and same
    assert select_release([rel("v1.3.0", pre=True), rel("v1.3.0-rc.1")], cur) is None  # prerelease on stable
    assert select_release([rel("v1.3.0", pre=True)], cur, "beta").tag == "v1.3.0"
    assert select_release([rel("v1.3.0", draft=True)], cur) is None
    assert select_release([rel("v1.3.0", assets=False)], cur) is None  # no signature published
    assert select_release([rel("v1.3.0")], cur, skipped="1.3.0") is None
    assert select_release([rel("v1.3.0")], cur, failed=["1.3.0"]) is None
    assert select_release([rel("v1.3.0"), rel("v1.4.0"), rel("v1.3.5")], cur).tag == "v1.4.0"
    assert select_release([rel("nightly")], cur) is None


def test_selection_ignores_assets_on_other_hosts():
    r = rel("v2.0.0")
    r["assets"][0]["browser_download_url"] = "https://evil.example/x.zip"
    assert select_release([r], Version.parse("1.0.0")) is None


def test_host_allowlist():
    for ok in ("https://github.com/a/b", "https://api.github.com/x", "https://objects.githubusercontent.com/y"):
        su.check_host(ok)
    for bad in ("http://github.com/a", "https://github.com.evil.io/a", "https://evil.example/a", "https://user@github.com/a",
                "https://github.com:8443/a", "file:///c:/x", "https://githubusercontent.com/a"):
        with pytest.raises(UpdateError):
            su.check_host(bad)


def test_release_notes_are_plain_inert_text():
    out = su.plain_notes("hi\x1b[31m <b>x</b>\r\nline\x00", limit=100)
    assert "\x1b" not in out and "\x00" not in out and "<b>x</b>" in out and "\r" not in out
    assert len(su.plain_notes("a" * 9999, 50)) <= 53


# ---- checking: etag, throttle, offline, limits
def test_check_queries_the_configured_repo(root, monkeypatch):
    monkeypatch.setattr(su, "UPDATE_REPO", "someone/else")
    gh = FakeGitHub()
    updater(gh, root).check()
    assert gh.requests[0].url.path == "/repos/someone/else/releases"


def test_check_uses_etag_and_throttles(root):
    gh, t = FakeGitHub(), [1000.0]
    u = updater(gh, root, now=lambda: t[0])
    r = u.check()
    assert r.status == "available" and str(r.latest.version) == "1.1.0"
    assert len(gh.requests) == 1 and gh.requests[0].headers["user-agent"].startswith("Cogitator/")
    t[0] += 60
    assert u.check().status == "throttled" and len(gh.requests) == 1  # within 24h: no request
    t[0] += 25 * 3600
    r = u.check()
    assert len(gh.requests) == 2 and gh.requests[1].headers["if-none-match"] == '"abc"'  # conditional
    assert r.status == "available"  # 304 keeps the cached list
    assert Registry.load().data["selfupdate"]["etag"] == '"abc"'


def test_check_modes_and_missing_key(root):
    gh = FakeGitHub()
    assert updater(gh, root, key="").check().status == "disabled" and not gh.requests
    s = Settings(); s.update_mode = "off"
    assert updater(gh, root, settings=s).check().status == "off" and not gh.requests


def test_offline_is_silent_and_rate_limit_backs_off(root):
    def boom(req): raise httpx.ConnectError("no network")
    u = updater(FakeGitHub(), root)
    u._client_factory = lambda: su.make_client(httpx.MockTransport(boom))
    assert u.check().status == "offline"
    (config_dir() / "registry.json").unlink()
    gh = FakeGitHub(); gh.status = 403
    t = [1000.0]
    u = updater(gh, root, now=lambda: t[0])
    assert u.check().status == "rate-limited" and u.check().status == "rate-limited" and len(gh.requests) == 1


def test_up_to_date_and_skip_and_remind(root):
    gh = FakeGitHub("1.0.0")
    assert updater(gh, root).check().status == "up-to-date"
    (config_dir() / "registry.json").unlink()
    gh, t = FakeGitHub("1.1.0"), [1000.0]
    u = updater(gh, root, now=lambda: t[0])
    u.check()
    assert u.announcement().version == Version.parse("1.1.0")
    u.remind_later(1)
    assert u.announcement() is None
    t[0] += 2 * 3600
    assert u.announcement() is not None
    u.skip_version("1.1.0")
    assert u.announcement() is None and updater(gh, root).latest() is None  # persisted


# ---- staging
def test_stage_downloads_verifies_and_extracts(root):
    gh = FakeGitHub()
    u = updater(gh, root)
    info = u.check().latest
    rd = u.stage(info)
    assert (rd.dir / "cogitator.exe").read_bytes() == b"NEW" and rd.dir.parent == root / "staging"
    assert (root / "app" / "cogitator.exe").read_bytes() == b"OLD"  # nothing replaced yet
    assert u.ready().version == "1.1.0"


@pytest.mark.parametrize("case", ["tampered", "wrong_key", "no_sig", "bad_sums", "no_exe", "zip_slip"])
def test_stage_refuses_anything_unverified(root, case):
    gh = FakeGitHub()
    if case == "tampered":
        gh.zip = make_zip({"cogitator.exe": b"EVIL"})  # served bytes differ from what was signed
        gh.signed_zip = gh.zip  # sums match so only the signature can catch it
    elif case == "wrong_key":
        gh = FakeGitHub(sign_with=Ed25519PrivateKey.generate())
    elif case == "no_sig":
        gh = FakeGitHub(sig=False)
    elif case == "bad_sums":
        gh.signed_zip = b"other"
    elif case == "no_exe":
        gh = FakeGitHub(zip_bytes=make_zip({"readme.txt": b"x"}))
    elif case == "zip_slip":
        gh = FakeGitHub(zip_bytes=make_zip({"cogitator.exe": b"x", "../evil.txt": b"x"}))
    u = updater(gh, root)
    info = u.check().latest
    if info is None:
        assert case == "no_sig"  # a release without a signature is never even offered
        return
    with pytest.raises(UpdateError):
        u.stage(info)
    assert u.ready() is None and not (root.parent / "evil.txt").exists()
    assert (root / "app" / "cogitator.exe").read_bytes() == b"OLD"


def test_redirect_to_a_foreign_host_is_refused_but_github_cdn_is_followed(root):
    gh = FakeGitHub(redirect_to="https://evil.example/Cogitator.zip")
    u = updater(gh, root)
    info = u.check().latest
    with pytest.raises(UpdateError):
        u.stage(info)
    assert not any(r.url.host == "evil.example" for r in gh.requests)  # never even contacted
    gh = FakeGitHub(redirect_to="https://objects.githubusercontent.com/blob/zip")
    u = updater(gh, root)
    assert u.stage(u.check().latest).version == "1.1.0"


def test_size_cap_is_enforced(root, monkeypatch):
    monkeypatch.setattr(su, "MAX_ZIP", 10)
    u = updater(FakeGitHub(), root)
    with pytest.raises(UpdateError):
        u.stage(u.check().latest)


def test_downgrade_failed_and_no_key_are_refused_at_stage_time(root):
    gh = FakeGitHub("1.1.0")
    info = updater(gh, root).check().latest
    with pytest.raises(UpdateError):
        updater(gh, root, current="1.1.0").stage(info)
    with pytest.raises(UpdateError):
        updater(gh, root, current="2.0.0").stage(info)
    with pytest.raises(UpdateError):
        updater(gh, root, key="").stage(info)
    u = updater(gh, root)
    u.st["failed"] = ["1.1.0"]
    with pytest.raises(UpdateError):
        u.stage(info)


def test_modes_decide_what_happens_automatically(root):
    for mode, staged in (("notify", False), ("download", True), ("auto", True)):
        s = Settings(); s.update_mode = mode
        u = updater(FakeGitHub(), root, settings=s)
        u.st.clear()
        res = u.run_automatic()
        assert res.latest is not None and (u.ready() is not None) == staged
        assert u.should_apply_automatically() == (mode == "auto")
    s = Settings(); s.update_mode = "off"
    gh = FakeGitHub()
    assert updater(gh, root, settings=s).run_automatic().status == "off" and not gh.requests


# ---- install types
def test_install_type_detection(tmp_path):
    app = tmp_path / "Cogitator" / "app"
    app.mkdir(parents=True)
    exe = str(app / "cogitator.exe")
    assert detect_layout(frozen=True, executable=exe).kind == "portable"  # no marker
    (tmp_path / "Cogitator" / "install.json").write_text("{}")
    lay = detect_layout(frozen=True, executable=exe)
    assert lay.kind == "installer" and lay.in_place and lay.app_dir == app and lay.previous_dir.name == "app.previous"
    assert detect_layout(frozen=False, prefix=r"C:\Users\x\pipx\venvs\cogitator").kind == "pipx"
    assert "pipx upgrade cogitator" in Layout("pipx").guidance()
    src = tmp_path / "repo"
    (src / "cogitator").mkdir(parents=True)
    (src / "pyproject.toml").write_text("")
    assert detect_layout(frozen=False, prefix="C:/py", package_file=str(src / "cogitator" / "x.py")).kind == "source"
    assert detect_layout(frozen=False, prefix="C:/py", package_file=str(tmp_path / "site" / "cogitator" / "x.py")).kind == "pip"


def test_only_installer_installs_are_updated_in_place(tmp_path):
    for kind in ("pipx", "source", "portable", "pip"):
        u = updater(FakeGitHub(), layout=Layout(kind))
        info = u.check().latest
        with pytest.raises(UpdateError):
            u.stage(info)
        with pytest.raises(UpdateError):
            u.apply()
        with pytest.raises(UpdateError):
            u.rollback()


# ---- applying hands over to the helper
def test_apply_spawns_helper_with_the_right_arguments_and_rejects_tampered_staging(root):
    spawned = []
    gh = FakeGitHub()
    u = updater(gh, root, popen=lambda argv, **k: spawned.append(argv))
    u.stage(u.check().latest)
    (u.ready().dir / "cogitator.exe").write_bytes(b"TAMPERED")
    with pytest.raises(UpdateError):
        u.apply()
    assert not spawned and u.ready() is None
    u.stage(u.check().latest)
    rd = u.apply()
    argv = spawned[0]
    assert argv[0] == "HELPER" and argv[argv.index("--staged") + 1] == str(rd.dir)
    assert argv[argv.index("--wait-pid") + 1] == "4242" and argv[argv.index("--app") + 1] == str(root / "app")
    assert json.loads(argv[argv.index("--check") + 1])[1] == "--self-check"


def test_failed_result_is_remembered_so_the_update_is_never_retried(root):
    gh = FakeGitHub()
    u = updater(gh, root)
    u.stage(u.check().latest)
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / su.RESULT_FILE).write_text(json.dumps({"status": "reverted", "version": "1.1.0"}))
    assert u.absorb_result()["status"] == "reverted"
    assert u.failed() == ["1.1.0"] and u.ready() is None and u.latest() is None
    s = Settings(); s.update_mode = "auto"
    assert updater(gh, root, settings=s).run_automatic().latest is None  # no loop
    assert not (config_dir() / su.RESULT_FILE).exists()


def test_update_state_survives_in_registry_and_other_data_is_untouched(root):
    reg = Registry.load()
    reg.data["mods"] = {"m": {"nexus_id": 7}}
    reg.save()
    cfgfile = config_dir() / "config.toml"
    Settings(game_dir="X").save()
    before = cfgfile.read_bytes()
    gh = FakeGitHub()
    u = updater(gh, root)
    u.stage(u.check().latest)
    assert cfgfile.read_bytes() == before
    assert Registry.load().data["mods"] == {"m": {"nexus_id": 7}}


def test_raw_signature_with_whitespace_edge_bytes_is_not_mangled():
    raw = b" " + bytes(range(61)) + b"\n\t"
    assert len(raw) == 64 and su._signature_bytes(raw) == raw


def test_check_command_binds_the_release_version(root):
    spawned = []
    u = updater(FakeGitHub(), root, popen=lambda argv, **k: spawned.append(argv))
    u.stage(u.check().latest)
    u.apply()
    cmd = json.loads(spawned[0][spawned[0].index("--check") + 1])
    assert cmd[1:] == ["--self-check", "--expect-version", "1.1.0"]


def test_replayed_old_build_fails_the_version_check_and_is_recorded(tmp_path):
    from cogitator import cli, updater as up

    # an old signed zip re-attached under a newer tag: the staged build reports its real (older) version
    root = tmp_path / "R"
    (root / "app").mkdir(parents=True)
    (root / "app" / "cogitator.exe").write_bytes(b"CUR")
    (root / "staging" / "v9").mkdir(parents=True)
    (root / "staging" / "v9" / "cogitator.exe").write_bytes(b"OLDSIGNED")
    expect = "99.0.0"  # the tag's version; the "staged build" is really cli's __version__

    def check(cmd):
        assert cmd[-2:] == ["--expect-version", expect]
        return cli.main(["--self-check", "--expect-version", expect]) == 0

    res = tmp_path / "res.json"
    rc = up.run(["--app", str(root / "app"), "--previous", str(root / "app.previous"), "--staged", str(root / "staging" / "v9"),
                 "--version", expect, "--result", str(res), "--check", json.dumps(["x", "--self-check", "--expect-version", expect]),
                 "--relaunch", "[]"], check=check)
    assert rc == 1 and (root / "app" / "cogitator.exe").read_bytes() == b"CUR"
    assert json.loads(res.read_text())["status"] == "reverted"
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / su.RESULT_FILE).write_text(res.read_text())
    u = updater(FakeGitHub(ver=expect), root)
    u.absorb_result()
    assert u.failed() == [expect]


@pytest.mark.parametrize("bad", ["abc", "-5", "1.5"])
def test_bad_content_length_is_an_update_error(root, bad):
    gh = FakeGitHub()
    orig = gh.handler

    def handler(req):
        r = orig(req)
        if req.url.path.endswith(".zip"):
            r.headers["content-length"] = bad
        return r

    gh.handler = handler
    u = updater(gh, root)
    with pytest.raises(UpdateError):
        u.stage(u.check().latest)


def test_helper_temp_folder_is_deleted_on_next_start_only_if_ours(root, tmp_path, monkeypatch):
    import tempfile

    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    app = root / "app"
    (app / "cogitator-updater.exe").write_bytes(b"H")
    u = updater(FakeGitHub(), root, layout=Layout("installer", root))
    u._helper_argv()
    ours = Path(u.st["helper_dir"])
    assert ours.is_dir() and ours.name.startswith("cogitator-update-")
    u.absorb_result()
    assert not ours.exists() and "helper_dir" not in u.st
    other = tmp_path / "precious"
    other.mkdir()
    u.st["helper_dir"] = str(other)  # not a folder this feature created
    u.absorb_result()
    assert other.is_dir()
