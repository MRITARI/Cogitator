import io
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from cogitator import cli, installer
from cogitator.frameworks import (LoaderError, classify_zip, describe_plan, detect_framework,
                                  install_plan, internal_folders_from_zips, plan_github, plan_local,
                                  version_from_filename)
from cogitator.loader import GITHUB_API, make_client
from cogitator.mods import INTERNAL_FOLDERS
from cogitator.registry import Registry, migrate

DML_FILES = {
    "binaries/mod_loader": b"\x7fELF-ish",
    "bundle/9ba626afa44a3aa3.patch_999": b"patch",
    "mods/base/mod_manager.lua": b"-- mm\r\n",
    "mods/base/function/class.lua": b"-- c\r\n",
    "mods/mod_load_order.txt": b"-- default\r\n",
    "tools/dtkit-patch.exe": b"MZ",
    "tools/README.md": b"# t",
    "toggle_darktide_mods.bat": b"@echo off\r\n",
    "README.md": b"readme",
}
DMF_FILES = {
    "dmf/dmf.mod": b"return {}",
    "dmf/scripts/mods/dmf/dmf_loader.lua": b"-- l",
    "dmf/scripts/mods/dmf/modules/a.lua": b"-- a",
}


def mkzip(path: Path, files: dict, prefix: str = "") -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for n, d in files.items():
            z.writestr(prefix + n, d)
    return path


@pytest.fixture
def zips(tmp_path):
    d = tmp_path / "z"
    d.mkdir()
    return (mkzip(d / "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z abc.zip", DML_FILES),
            mkzip(d / "Darktide Mod Framework 8 26.10.22 2026-10-23T02-11Z def.zip", DMF_FILES))


@pytest.fixture
def game(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    return g


NOT_RUNNING = dict(is_running=lambda: False)


# ---- classification / derived lists

def test_classify(tmp_path, zips):
    dml, dmf = zips
    assert classify_zip(dml) == ("dml", "")
    assert classify_zip(dmf) == ("dmf", "")
    assert classify_zip(mkzip(tmp_path / "w.zip", DML_FILES, "owner-repo-4bd075a/")) == ("dml", "owner-repo-4bd075a/")
    assert classify_zip(mkzip(tmp_path / "w2.zip", {"junk.md": b"x", **DMF_FILES}, "o-r-0107475/")) == ("dmf", "o-r-0107475/")
    assert classify_zip(mkzip(tmp_path / "m.zip", {"mods/dmf/dmf.mod": b"x"})) == ("dmf", "mods/")
    with pytest.raises(LoaderError):
        classify_zip(mkzip(tmp_path / "u.zip", {"readme.txt": b"x"}))
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(LoaderError):
        classify_zip(bad)


def test_internal_folders_derived_from_zips(zips):
    assert internal_folders_from_zips(*zips) == INTERNAL_FOLDERS == frozenset({"base", "dmf"})


def test_version_from_filename():
    assert version_from_filename("Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z ndQ1md9gG.zip") == "26.06.24"
    assert version_from_filename("Darktide Mod Framework 8 26.10.22 2026-10-23T02-11Z FQPKZQIuu.zip") == "26.10.22"
    assert version_from_filename("something.zip") is None
    assert version_from_filename("x 1234567.zip") is None


# ---- install

def test_install_dml_and_dmf(game, zips):
    reg = Registry.load()
    dml, dmf = zips
    r = install_plan(plan_local(dml), game, reg, **NOT_RUNNING)
    assert r.written == 8 and r.kept == []  # README.md skipped
    assert not (game / "README.md").exists()
    assert (game / "mods" / "mod_load_order.txt").read_bytes() == b"-- default\r\n"
    assert detect_framework(game, "dml").status == "installed" and detect_framework(game, "dmf").status == "missing"
    install_plan(plan_local(dmf), game, reg, **NOT_RUNNING)
    assert detect_framework(game, "dmf").status == "installed"
    rec = Registry.load().framework("dml")
    assert rec["version"] == "26.06.24" and rec["source_kind"] == "local" and rec["sha256"]


def test_existing_load_order_never_overwritten_and_files_updated(game, zips):
    dml, _ = zips
    install_plan(plan_local(dml), game, None, **NOT_RUNNING)
    lo = game / "mods" / "mod_load_order.txt"
    lo.write_bytes(b"my_mod\r\n--other\r\n")
    (game / "mods" / "base" / "mod_manager.lua").write_bytes(b"old")
    r = install_plan(plan_local(dml), game, None, **NOT_RUNNING)
    assert lo.read_bytes() == b"my_mod\r\n--other\r\n" and r.kept == ["mods/mod_load_order.txt"]
    assert (game / "mods" / "base" / "mod_manager.lua").read_bytes() == b"-- mm\r\n"
    assert any(r.backup_dir.iterdir())  # overwritten file was backed up


def test_dmf_replaces_dir(game, zips):
    _, dmf = zips
    stale = game / "mods" / "dmf" / "stale.lua"
    stale.parent.mkdir(parents=True)
    stale.write_text("x")
    install_plan(plan_local(dmf), game, None, **NOT_RUNNING)
    assert not stale.exists() and (game / "mods" / "dmf" / "dmf.mod").is_file()


def test_rollback_on_failure(game, zips, monkeypatch):
    dml, _ = zips
    install_plan(plan_local(dml), game, None, **NOT_RUNNING)
    (game / "binaries" / "mod_loader").write_bytes(b"ORIGINAL")
    before = {p.relative_to(game): p.read_bytes() for p in game.rglob("*") if p.is_file()}
    real = installer.Transaction.put_file
    n = {"c": 0}

    def flaky(self, src, dest):
        n["c"] += 1
        if n["c"] == 5:
            raise OSError("disk full")
        return real(self, src, dest)

    monkeypatch.setattr(installer.Transaction, "put_file", flaky)
    with pytest.raises(OSError):
        install_plan(plan_local(dml), game, None, **NOT_RUNNING)
    after = {p.relative_to(game): p.read_bytes() for p in game.rglob("*") if p.is_file()}
    assert after == before


def test_rollback_new_install_leaves_nothing(game, zips, monkeypatch):
    dml, _ = zips
    real = installer.Transaction.put_file
    n = {"c": 0}

    def flaky(self, src, dest):
        n["c"] += 1
        if n["c"] == 4:
            raise OSError("boom")
        return real(self, src, dest)

    monkeypatch.setattr(installer.Transaction, "put_file", flaky)
    with pytest.raises(OSError):
        install_plan(plan_local(dml), game, None, **NOT_RUNNING)
    assert [p for p in game.rglob("*") if p.is_file()] == []


def test_refuses_when_running_or_bad_dir(game, zips, tmp_path):
    dml, _ = zips
    with pytest.raises(LoaderError, match="running"):
        install_plan(plan_local(dml), game, None, is_running=lambda: True)
    assert not (game / "mods").exists()
    with pytest.raises(LoaderError, match="Not a Darktide"):
        install_plan(plan_local(dml), tmp_path / "nope", None, **NOT_RUNNING)


def test_malicious_zip_writes_nothing(game, tmp_path):
    z = mkzip(tmp_path / "evil.zip", {**DML_FILES, "../../escape.txt": b"x"})
    with pytest.raises(LoaderError):
        install_plan(plan_local(z), game, None, **NOT_RUNNING)
    assert [p for p in game.rglob("*") if p.is_file()] == [] and not (tmp_path / "escape.txt").exists()


def test_unknown_version_recorded_as_none(game, tmp_path):
    z = mkzip(tmp_path / "dml.zip", DML_FILES)
    reg = Registry.load()
    install_plan(plan_local(z), game, reg, **NOT_RUNNING)
    assert Registry.load().framework("dml")["version"] is None


# ---- GitHub opt-in

def api(path):
    return f"{GITHUB_API}/repos/{path}"


def zip_bytes(files, prefix=""):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(prefix + n, d)
    return b.getvalue()


DML_R, DMF_R = "Darktide-Mod-Framework/Darktide-Mod-Loader", "Darktide-Mod-Framework/Darktide-Mod-Framework"
SHA = "0107475abcdef0123456789abcdef0123456789a"


@respx.mock
def test_github_dml_tag_source_and_dmf_snapshot(tmp_path):
    respx.get(api(f"{DML_R}/releases/latest")).respond(200, json={"tag_name": "26.06.24", "assets": []})
    respx.get(api(f"{DML_R}/zipball/26.06.24")).respond(200, content=zip_bytes(DML_FILES, "o-r-4bd075a/"))
    respx.get(api(f"{DMF_R}/releases/latest")).respond(404)
    respx.get(api(f"{DMF_R}/commits/HEAD")).respond(200, json={"sha": SHA, "commit": {"committer": {"date": "2026-10-22T00:00:00Z"}}})
    respx.get(api(f"{DMF_R}/zipball/{SHA}")).respond(200, content=zip_bytes(DMF_FILES, "o-r-0107475/"))
    with make_client() as c:
        dml = plan_github("dml", c, tmp_path)
        dmf = plan_github("dmf", c, tmp_path)
    assert (dml.component, dml.source.kind, dml.source.version, dml.source.unreleased) == ("dml", "github-tag-source", "26.06.24", False)
    assert (dmf.component, dmf.source.kind, dmf.source.version, dmf.source.unreleased) == ("dmf", "github-snapshot", SHA, True)
    text = describe_plan(dmf)
    assert SHA in text and "UNRELEASED SNAPSHOT" in text and "api.github.com" in text and "sha256" not in text.lower().replace("sha-256", "")
    assert "tag 26.06.24" in describe_plan(dml)


@respx.mock
def test_github_release_asset_preferred(tmp_path):
    respx.get(api(f"{DML_R}/releases/latest")).respond(200, json={"tag_name": "9.9.9", "assets": [
        {"name": "DML.zip", "browser_download_url": "https://dl.example/DML.zip", "size": 0}]})
    respx.get("https://dl.example/DML.zip").respond(200, content=zip_bytes(DML_FILES))
    with make_client() as c:
        p = plan_github("dml", c, tmp_path)
    assert p.source.kind == "github-release" and p.source.version == "9.9.9"


@respx.mock
def test_github_wrong_kind_and_rate_limit(tmp_path):
    respx.get(api(f"{DML_R}/releases/latest")).respond(200, json={"tag_name": "t", "assets": []})
    respx.get(api(f"{DML_R}/zipball/t")).respond(200, content=zip_bytes(DMF_FILES))  # DMF content served for DML
    with make_client() as c:
        with pytest.raises(LoaderError, match="looks like dmf"):
            plan_github("dml", c, tmp_path)
    respx.get(api(f"{DMF_R}/releases/latest")).respond(404)
    respx.get(api(f"{DMF_R}/commits/HEAD")).respond(403, headers={"x-ratelimit-remaining": "0"})
    with make_client() as c, pytest.raises(LoaderError, match="rate limit"):
        plan_github("dmf", c, tmp_path)


# ---- CLI

def test_cli_requires_source(game, capsys):
    assert cli.main(["--game-dir", str(game), "install-framework"]) == 2
    out = capsys.readouterr().out
    assert "--zip" in out and "nexusmods.com/warhammer40kdarktide/mods/19" in out and "--from-github" in out


def test_cli_dry_run_changes_nothing(game, zips, capsys):
    rc = cli.main(["--game-dir", str(game), "install-framework", "--zip", str(zips[0]), "--zip", str(zips[1]), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "local file Darktide Mod Loader" in out and "Dry run" in out
    assert [p for p in game.rglob("*") if p.is_file()] == []


def test_cli_needs_confirmation_then_installs(game, zips, monkeypatch, capsys):
    monkeypatch.setattr("cogitator.frameworks.darktide_running", lambda: False)
    from cogitator.fwupdate import install_framework_plan
    monkeypatch.setattr("cogitator.cli.install_framework_plan",
                        lambda *a, **k: install_framework_plan(*a, **{**k, "is_running": lambda: False}))
    args = ["--game-dir", str(game), "install-framework", "--zip", str(zips[1]), "--zip", str(zips[0])]
    assert cli.main(args) == 2  # pytest stdin is not a tty -> refuses without --yes
    assert [p for p in game.rglob("*") if p.is_file()] == []
    assert cli.main(args + ["--yes"]) == 0
    assert detect_framework(game, "dml").status == "installed" and detect_framework(game, "dmf").status == "installed"


def test_cli_rejects_duplicate_component(game, zips, tmp_path, capsys):
    other = mkzip(tmp_path / "dml2 26.01.01.zip", DML_FILES)
    assert cli.main(["--game-dir", str(game), "install-framework", "--zip", str(zips[0]), "--zip", str(other), "--dry-run"]) == 2


# ---- registry

def test_registry_migrate_and_corrupt(tmp_path):
    assert migrate("garbage")["schema"] == 3
    m = migrate({"frameworks": "bad", "mods": {"1": {}, "2": "junk"}, "extra": 1})
    assert m["frameworks"] == {} and m["mods"] == {"1": {}} and m["extra"] == 1 and m["updates"] == {}
    v1 = migrate({"schema": 1, "frameworks": {"dml": {"version": "26.06.24"}}, "mods": {}, "patch": {"last_patch_at": 5}})
    assert v1["schema"] == 3 and v1["updates"] == {} and v1["manifests"] == {} and v1["frameworks"]["dml"]["version"] == "26.06.24" \
        and v1["patch"] == {"last_patch_at": 5}  # v1 data survives untouched
    p = tmp_path / "registry.json"
    p.write_text("{not json")
    r = Registry.load(p)
    assert r.data["frameworks"] == {} and (tmp_path / "registry.json.corrupt").exists()


# ---- install manifest (drives uninstall)

import hashlib  # noqa: E402


def test_install_records_a_manifest_with_hashes(game, zips):
    dml, dmf = zips
    reg = Registry.load()
    r = install_plan(plan_local(dml), game, reg, **NOT_RUNNING)
    m = reg.manifest("dml")
    assert set(m["files"]) == {"binaries/mod_loader", "bundle/9ba626afa44a3aa3.patch_999", "mods/base/mod_manager.lua",
                               "mods/base/function/class.lua", "tools/dtkit-patch.exe", "tools/README.md",
                               "toggle_darktide_mods.bat"}  # README.md skipped; mod_load_order.txt is the user's
    assert m["files"]["binaries/mod_loader"] == hashlib.sha256(DML_FILES["binaries/mod_loader"]).hexdigest()
    assert m["created_load_order"] is True and r.created_load_order and r.placed == m["files"]
    install_plan(plan_local(dmf), game, reg, **NOT_RUNNING)
    mf = reg.manifest("dmf")
    assert set(mf["files"]) == {"mods/dmf/dmf.mod", "mods/dmf/scripts/mods/dmf/dmf_loader.lua", "mods/dmf/scripts/mods/dmf/modules/a.lua"}
    assert Registry.load().manifest("dml") == m  # persisted


def test_existing_load_order_is_not_claimed_by_the_manifest(game, zips):
    (game / "mods").mkdir()
    (game / "mods" / "mod_load_order.txt").write_bytes(b"mine\r\n")
    reg = Registry.load()
    r = install_plan(plan_local(zips[0]), game, reg, **NOT_RUNNING)
    assert reg.manifest("dml")["created_load_order"] is False and r.kept == ["mods/mod_load_order.txt"]
    assert "mods/mod_load_order.txt" not in reg.manifest("dml")["files"]


def test_a_finished_install_can_be_rolled_back_later_from_its_journal(game, zips):
    from cogitator.installer import Transaction
    (game / "binaries").mkdir(exist_ok=True)
    (game / "binaries" / "mod_loader").write_bytes(b"OLD")
    r = install_plan(plan_local(zips[0]), game, None, **NOT_RUNNING)
    assert (game / "binaries" / "mod_loader").read_bytes() != b"OLD"
    Transaction.from_journal(r.backup_dir).rollback()
    assert (game / "binaries" / "mod_loader").read_bytes() == b"OLD"
    assert [p for p in game.rglob("*") if p.is_file()] == [game / "binaries" / "mod_loader"]  # everything else removed
