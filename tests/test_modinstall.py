import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from cogitator import cli, installer
from cogitator.linking import link_mod
from cogitator.loadorder import LoadOrder
from cogitator.modinstall import (AmbiguousArchive, ModImportError, NotPremium, apply_update_premium, find_matching_download,
                                  import_archive, inspect_mod_zip, mod_page_url, open_files_page, wait_for_download)
from cogitator.nexus import GAME, NexusClient, parse_files
from cogitator.registry import Registry

FX = Path(__file__).parent / "fixtures" / "nexus"
B = "https://api.nexusmods.com/v1"
HDR = {"x-rl-daily-remaining": "19990", "x-rl-hourly-remaining": "1990"}
REAL_NAME = "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z ndQ1md9gG.zip"
NOT_RUNNING = dict(is_running=lambda: False)


def fx(n):
    return json.loads((FX / n).read_text(encoding="utf-8"))


def mkzip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return Path(path)


def mod_files(folder, extra=None, prefix="", payload="v1"):
    return {f"{prefix}{folder}/{folder}.mod": "return {}", f"{prefix}{folder}/scripts/{folder}.lua": payload, **(extra or {})}


@pytest.fixture
def game(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "mods").mkdir()
    (g / "mods" / "mod_load_order.txt").write_bytes(b"-- header\r\nexisting\r\n--off\r\n")
    return g


@pytest.fixture
def z(tmp_path):
    d = tmp_path / "zips"
    d.mkdir()
    return d


# ---- inspection

def test_inspect_layouts(z):
    assert inspect_mod_zip(mkzip(z / "a.zip", mod_files("foo"))).folders == ["foo"]
    i = inspect_mod_zip(mkzip(z / "b.zip", mod_files("foo", prefix="wrapper/")))
    assert (i.prefix, i.folders) == ("wrapper/", ["foo"])
    i = inspect_mod_zip(mkzip(z / "c.zip", mod_files("foo", prefix="mods/")))
    assert (i.prefix, i.folders) == ("mods/", ["foo"])
    both = {**mod_files("foo"), **mod_files("bar")}
    assert inspect_mod_zip(mkzip(z / "d.zip", both)).folders == ["bar", "foo"]


def test_inspect_refusals(z):
    with pytest.raises(ModImportError, match="No mod folder"):
        inspect_mod_zip(mkzip(z / "a.zip", {"readme.txt": "x"}))
    with pytest.raises(ModImportError, match="loader/framework"):
        inspect_mod_zip(mkzip(z / "b.zip", mod_files("base")))
    with pytest.raises(ModImportError, match="install-framework"):
        inspect_mod_zip(mkzip(z / "b2.zip", mod_files("dmf")))  # dmf/dmf.mod is the DMF marker itself
    with pytest.raises(ModImportError, match="install-framework"):
        inspect_mod_zip(mkzip(z / "c.zip", {"toggle_darktide_mods.bat": "x", "mods/base/mod_manager.lua": "x"}))
    bad = z / "bad.zip"
    bad.write_bytes(b"nope")
    with pytest.raises(ModImportError, match="not a valid zip"):
        inspect_mod_zip(bad)


# ---- import

def nexus_for(mod_id=19):
    respx.get(f"{B}/games/{GAME}/mods/{mod_id}.json").respond(200, json=fx("mod_19.json"), headers=HDR)
    respx.get(f"{B}/games/{GAME}/mods/{mod_id}/files.json").respond(200, json=fx("files_19.json"), headers=HDR)


@respx.mock
def test_import_exact_match_links_and_sets_baseline(game, z):
    nexus_for()
    zp = mkzip(z / REAL_NAME, mod_files("foo"))
    reg = Registry.load()
    with NexusClient("k" * 12) as nx:
        r = import_archive(zp, game, reg, nx, **NOT_RUNNING)
    assert r.folders == ["foo"] and r.linked == "foo" and r.file_id == 5781 and r.added_to_load_order == ["foo"]
    assert (game / "mods" / "foo" / "foo.mod").is_file()
    rec = Registry.load().mod("foo")
    assert (rec["nexus_id"], rec["installed_file_id"], rec["baseline"], rec["linked_via"]) == (19, 5781, "import", "import")
    lo = (game / "mods" / "mod_load_order.txt").read_bytes()
    assert lo == b"-- header\r\nexisting\r\n--off\r\nfoo\r\n"  # appended with the file's own CRLF; nothing else touched


@respx.mock
def test_import_replaces_folder_and_keeps_load_order_state_and_position(game, z):
    (game / "mods" / "existing").mkdir()
    (game / "mods" / "existing" / "stale.lua").write_text("old")
    zp = mkzip(z / "x.zip", mod_files("existing", payload="v2"))
    before = (game / "mods" / "mod_load_order.txt").read_bytes()
    r = import_archive(zp, game, Registry.load(), None, choose=lambda q, o: 0, **NOT_RUNNING)
    assert not (game / "mods" / "existing" / "stale.lua").exists()
    assert (game / "mods" / "existing" / "scripts" / "existing.lua").read_text() == "v2"
    assert (game / "mods" / "mod_load_order.txt").read_bytes() == before and r.added_to_load_order == []
    assert any(Path(r.backup_dir).rglob("stale.lua"))  # previous folder kept for rollback


@respx.mock
def test_disabled_mod_stays_disabled_after_update(game, z):
    zp = mkzip(z / "x.zip", mod_files("off"))
    (game / "mods" / "off").mkdir()
    import_archive(zp, game, Registry.load(), None, choose=lambda q, o: 0, **NOT_RUNNING)
    lo = LoadOrder.load(game / "mods" / "mod_load_order.txt")
    assert lo.state("off") is False and lo.entries().count(("off", False)) == 1


def test_import_without_key_records_archive_for_later_resolution(game, z):
    zp = mkzip(z / REAL_NAME, mod_files("foo"))
    r = import_archive(zp, game, Registry.load(), None, **NOT_RUNNING)
    rec = Registry.load().mod("foo")
    assert r.linked == "foo" and rec["installed_file_id"] is None and rec["installed_archive"] == REAL_NAME
    assert any("unknown" in n for n in r.notes)


@respx.mock
def test_ambiguous_file_match_asks_user_and_never_guesses(game, z):
    nexus_for()
    zp = mkzip(z / "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z renamed.zip", mod_files("foo"))
    with NexusClient("k" * 12) as nx:
        with pytest.raises(AmbiguousArchive, match="matches several"):
            import_archive(zp, game, Registry.load(), nx, **NOT_RUNNING)  # non-interactive
        assert not (game / "mods" / "foo").exists()  # nothing installed before the question was answered
        asked = []

        def choose(q, opts):
            asked.append((q, opts))
            return 1  # the "(Update Only)" variant

        r = import_archive(zp, game, Registry.load(), nx, choose=choose, **NOT_RUNNING)
    assert r.file_id == 5782 and len(asked[0][1]) == 3 and "none of these" in asked[0][1][-1]


@respx.mock
def test_user_can_decline_all_candidates(game, z):
    nexus_for()
    zp = mkzip(z / "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z renamed.zip", mod_files("foo"))
    with NexusClient("k" * 12) as nx:
        r = import_archive(zp, game, Registry.load(), nx, choose=lambda q, o: len(o) - 1, **NOT_RUNNING)
    assert r.file_id is None and Registry.load().mod("foo")["installed_file_id"] is None


def test_unparseable_name_needs_mod_id_or_explicit_no_link(game, z):
    zp = mkzip(z / "mystery.zip", mod_files("foo"))
    with pytest.raises(AmbiguousArchive):
        import_archive(zp, game, Registry.load(), None, **NOT_RUNNING)
    assert not (game / "mods" / "foo").exists()
    r = import_archive(zp, game, Registry.load(), None, choose=lambda q, o: 0, **NOT_RUNNING)  # "install without linking"
    assert r.linked is None and Registry.load().mod("foo") is None


@respx.mock
def test_mod_id_conflict_is_asked(game, z):
    nexus_for()
    zp = mkzip(z / REAL_NAME, mod_files("foo"))
    with NexusClient("k" * 12) as nx:
        with pytest.raises(AmbiguousArchive, match="says Nexus mod 19"):
            import_archive(zp, game, Registry.load(), nx, mod_id=77, **NOT_RUNNING)
        r = import_archive(zp, game, Registry.load(), nx, mod_id=77, choose=lambda q, o: 1, **NOT_RUNNING)
    assert Registry.load().mod("foo")["nexus_id"] == 19 and r.file_id == 5781


def test_multi_folder_archive_asks_which_to_link(game, z):
    zp = mkzip(z / REAL_NAME, {**mod_files("lib"), **mod_files("main")})
    with pytest.raises(AmbiguousArchive, match="2 mod folders"):
        import_archive(zp, game, Registry.load(), None, **NOT_RUNNING)
    r = import_archive(zp, game, Registry.load(), None, choose=lambda q, o: o.index("main"), **NOT_RUNNING)
    assert r.folders == ["lib", "main"] and r.linked == "main"
    assert (game / "mods" / "lib").is_dir() and Registry.load().mod("lib") is None


def test_import_safety(game, z, tmp_path):
    zp = mkzip(z / "a.zip", mod_files("foo"))
    with pytest.raises(ModImportError, match="running"):
        import_archive(zp, game, Registry.load(), None, is_running=lambda: True, choose=lambda q, o: 0)
    evil = mkzip(z / "evil.zip", {**mod_files("foo"), "../../escape.txt": "x"})
    with pytest.raises(Exception):
        import_archive(evil, game, Registry.load(), None, choose=lambda q, o: 0, **NOT_RUNNING)
    assert not (game / "mods" / "foo").exists() and not (tmp_path / "escape.txt").exists()
    with pytest.raises(ModImportError, match="Not a Darktide"):
        import_archive(zp, tmp_path / "nope", Registry.load(), None, choose=lambda q, o: 0, **NOT_RUNNING)


def test_import_rolls_back_when_load_order_save_fails(game, z, monkeypatch):
    (game / "mods" / "foo").mkdir()
    (game / "mods" / "foo" / "keep.lua").write_text("original")
    zp = mkzip(z / "a.zip", mod_files("foo"))
    monkeypatch.setattr(LoadOrder, "save", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        import_archive(zp, game, Registry.load(), None, choose=lambda q, o: 0, **NOT_RUNNING)
    assert (game / "mods" / "foo" / "keep.lua").read_text() == "original" and not (game / "mods" / "foo" / "foo.mod").exists()


# ---- premium-only automatic update

def linked_old(reg):
    files = parse_files(fx("files_19.json"))
    link_mod(reg, "foo", 19, "n", files, via="manual", baseline=4069)
    reg.data["mods"]["foo"]["latest"]["size_bytes"] = None
    reg.save()


def mod_zip_bytes(payload="NEW"):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as zf:
        for n, d in mod_files("foo", payload=payload).items():
            zf.writestr(n, d)
    return b.getvalue()


@respx.mock
def test_free_account_never_touches_download_endpoint(game):
    reg = Registry.load()
    linked_old(reg)
    route = respx.get(f"{B}/games/{GAME}/mods/19/files/5781/download_link.json").respond(200, json=[])
    with NexusClient("k" * 12) as nx:
        for premium in (False, None):
            with pytest.raises(NotPremium):
                apply_update_premium("foo", game, reg, nx, premium=premium, **NOT_RUNNING)
    assert route.call_count == 0


@respx.mock
def test_premium_update_downloads_verifies_installs_and_updates_baseline(game):
    reg = Registry.load()
    linked_old(reg)
    data = mod_zip_bytes()
    reg.data["mods"]["foo"]["latest"]["size_bytes"] = len(data)
    reg.save()
    (game / "mods" / "foo").mkdir()
    (game / "mods" / "foo" / "stale.lua").write_text("old")
    respx.get(f"{B}/games/{GAME}/mods/19/files/5781/download_link.json").respond(200, json=[{"name": "CDN", "URI": "https://cdn.example/foo.zip"}], headers=HDR)
    respx.get("https://cdn.example/foo.zip").respond(200, content=data)
    with NexusClient("k" * 12) as nx:
        r = apply_update_premium("foo", game, reg, nx, premium=True, **NOT_RUNNING)
    assert r.file_id == 5781 and not (game / "mods" / "foo" / "stale.lua").exists()
    assert (game / "mods" / "foo" / "scripts" / "foo.lua").read_text() == "NEW"
    rec = Registry.load().mod("foo")
    assert rec["installed_file_id"] == 5781 and rec["baseline"] == "import"


@respx.mock
def test_premium_update_size_mismatch_installs_nothing(game):
    reg = Registry.load()
    linked_old(reg)
    data = mod_zip_bytes()
    reg.data["mods"]["foo"]["latest"]["size_bytes"] = len(data) + 5
    respx.get(f"{B}/games/{GAME}/mods/19/files/5781/download_link.json").respond(200, json=[{"URI": "https://cdn.example/foo.zip"}])
    respx.get("https://cdn.example/foo.zip").respond(200, content=data)
    with NexusClient("k" * 12) as nx:
        with pytest.raises(Exception, match="Size mismatch"):
            apply_update_premium("foo", game, reg, nx, premium=True, **NOT_RUNNING)
    assert not (game / "mods" / "foo").exists() and Registry.load().mod("foo")["installed_file_id"] == 4069


# ---- free-account flow: browser + Downloads watcher

def test_open_files_page_uses_files_tab():
    seen = []
    assert open_files_page(19, seen.append) == mod_page_url(19) == f"https://www.nexusmods.com/{GAME}/mods/19?tab=files"
    assert seen == [mod_page_url(19)]


LATEST = {"file_id": 5781, "version": "26.06.24", "file_name": REAL_NAME}


def test_find_matching_download(tmp_path):
    import os, time
    d = tmp_path / "Downloads"
    d.mkdir()
    since = time.time()
    old = mkzip(d / REAL_NAME, {"a": "b"})
    os.utime(old, (since - 3600, since - 3600))
    assert find_matching_download(d, 19, LATEST, since) is None  # predates the click
    mkzip(d / "unrelated.zip", {"a": "b"})
    mkzip(d / "Other Mod 55 26.06.24 2026-06-24T06-07Z abc.zip", {"a": "b"})
    assert find_matching_download(d, 19, LATEST, since) is None
    fresh = mkzip(d / REAL_NAME.replace(".zip", " (1).zip"), {"a": "b"})
    assert find_matching_download(d, 19, LATEST, since) == fresh
    (d / "x.crdownload").write_bytes(b"partial")
    assert find_matching_download(d, 19, LATEST, since) == fresh  # partial browser files ignored
    assert find_matching_download(tmp_path / "missing", 19, LATEST, since) is None


def test_wait_for_download_waits_for_stable_size(tmp_path):
    d = tmp_path / "dl"
    d.mkdir()
    clock = {"t": 0.0}
    steps = []

    def sleep(s):
        clock["t"] += s
        steps.append(clock["t"])
        if len(steps) == 2:
            mkzip(d / REAL_NAME, {"a": "b"})  # download "appears" after two polls

    got = wait_for_download(d, 19, LATEST, since=0.0, timeout_s=60, poll_s=2, sleep=sleep, clock=lambda: clock["t"])
    assert got == d / REAL_NAME and len(steps) == 3  # appeared at poll 3, accepted only after a 2nd identical size
    t2 = {"t": 0.0}
    assert wait_for_download(d / "empty", 19, LATEST, since=0.0, timeout_s=5, poll_s=2,
                             sleep=lambda s: t2.__setitem__("t", t2["t"] + s), clock=lambda: t2["t"]) is None


# ---- CLI

@respx.mock
def test_cli_import_needs_confirmation_then_installs(game, z, monkeypatch, capsys):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k" * 12)
    nexus_for()
    zp = mkzip(z / REAL_NAME, mod_files("foo"))
    monkeypatch.setattr("cogitator.modinstall.darktide_running", lambda: False)
    monkeypatch.setattr("cogitator.cli.import_archive",
                        lambda *a, **k: import_archive(*a, **{**k, "is_running": lambda: False}))
    g = ["--game-dir", str(game)]
    assert cli.main(g + ["import", str(zp)]) == 2 and not (game / "mods" / "foo").exists()
    assert cli.main(g + ["import", str(zp), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Linked 'foo'" in out and "file 5781" in out
    assert cli.main(g + ["import", str(z / "missing.zip"), "--yes"]) == 2


def test_cli_import_ambiguous_without_tty_is_an_error_not_a_guess(game, z, monkeypatch, capsys):
    zp = mkzip(z / "mystery.zip", mod_files("foo"))
    monkeypatch.setattr("cogitator.cli.import_archive",
                        lambda *a, **k: import_archive(*a, **{**k, "is_running": lambda: False}))
    assert cli.main(["--game-dir", str(game), "import", str(zp), "--yes"]) == 2
    assert "Cannot tell which Nexus mod" in capsys.readouterr().err
    assert not (game / "mods" / "foo").exists()


@respx.mock
def test_cli_update_free_account_prints_page_and_does_not_download(game, monkeypatch, capsys):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k" * 12)
    (game / "mods" / "foo").mkdir()
    (game / "mods" / "foo" / "foo.mod").write_text("x")
    reg = Registry.load()
    linked_old(reg)
    reg.data["updates"]["premium"] = False
    reg.save()
    opened = []
    monkeypatch.setattr("cogitator.cli.open_files_page", lambda mid: opened.append(mid) or mod_page_url(mid))
    dl = respx.get(url__regex=r".*download_link.*").respond(200, json=[])
    g = ["--game-dir", str(game)]
    assert cli.main(g + ["update", "foo", "--wait", "0"]) == 1
    out = capsys.readouterr().out
    assert "?tab=files" in out and opened == [19] and dl.call_count == 0
    assert cli.main(g + ["update", "--all"]) == 0
    assert "Free account" in capsys.readouterr().out and dl.call_count == 0


def test_dropped_paths_understands_what_terminals_paste(tmp_path):
    from cogitator.modinstall import dropped_paths

    a = tmp_path / "My Mod 1 2.zip"
    b = tmp_path / "other.zip"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    assert dropped_paths(f'"{a}"') == [a]  # Windows Terminal quotes paths with spaces
    assert dropped_paths(f"'{a}'  ") == [a]
    assert dropped_paths(str(a)) == [a]  # unquoted, with spaces
    assert dropped_paths(f'"{a}" {b}\r\n') == [a, b]  # several files at once
    assert dropped_paths("file:///" + str(b).replace("\\", "/").replace(" ", "%20")) == [b]
    assert dropped_paths(str(tmp_path / "missing.zip")) == [] and dropped_paths("hello") == []


def test_newest_download_picks_the_latest_recent_zip(tmp_path):
    import os, time
    from cogitator.modinstall import newest_download

    old, new = tmp_path / "old.zip", tmp_path / "new.zip"
    for p, age in ((old, 7200), (new, 60)):
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("a", "b")
        t = time.time() - age
        os.utime(p, (t, t))
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    assert newest_download(tmp_path) == new
    os.utime(new, (time.time() - 7200,) * 2)
    assert newest_download(tmp_path) is None  # nothing from the last hour
