import httpx
import pytest
import respx

from cogitator import cli, nexus
from cogitator.archives import parse_archive_name
from cogitator.nexus import (InvalidKey, NexusClient, NexusError, NoApiKey, NotFound, RateLimited, latest_main_file,
                             parse_files)
from cogitator.registry import Registry
from cogitator.updates import FAILED, UNKNOWN, UP_TO_DATE, UPDATE, check_framework, decide

B = nexus.BASE
G = nexus.GAME
HDR = {"x-rl-daily-remaining": "2490", "x-rl-hourly-remaining": "95"}


# ---- archive names

@pytest.mark.parametrize("name,mod,ver,form", [
    ("Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z ndQ1md9gG.zip", 19, "26.06.24", "space"),
    ("Darktide Mod Framework 8 26.10.22 2026-10-23T02-11Z FQPKZQIuu.zip", 8, "26.10.22", "space"),
    ("C:\\Users\\me\\Downloads\\Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z ndQ1md9gG (1).zip", 19, "26.06.24", "space"),
    ("Some Mod 1234 v1.2b 2026-01-02T03-04Z.ZIP", 1234, "1.2b", "space"),
    ("Mod With 2 Numbers 77 3.0 2026-01-02T03-04Z x.zip", 77, "3.0", "space"),
])
def test_space_form(name, mod, ver, form):
    p = parse_archive_name(name)
    assert (p.mod_id, p.version, p.form, p.ambiguous) == (mod, ver, form, False)


def test_space_form_fields():
    p = parse_archive_name("Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z ndQ1md9gG.zip")
    assert p.name == "Darktide Mod Loader" and p.timestamp == "2026-06-24T06-07Z"


@pytest.mark.parametrize("name,mod,ver,ts", [  # real file_name values from live files.json (mods 19 / 8)
    ("Darktide Mod Loader-19-25-12-02-1764720822.zip", 19, "25.12.02", 1764720822),
    ("Darktide Mod Loader-19-23-2-25-1677387771.zip", 19, "23.2.25", 1677387771),
    ("Darktide Mod Framework-8-23-3-04-1677966575.zip", 8, "23.3.04", 1677966575),
])
def test_dash_form_live_names_have_no_file_id(name, mod, ver, ts):
    p = parse_archive_name(name)
    assert (p.form, p.mod_id, p.version, p.timestamp, p.ambiguous) == ("dash", mod, ver, ts, False)


def test_dash_form_classic():
    p = parse_archive_name("Skyrim Thing-30379-2-6-1700000000.zip")
    assert (p.mod_id, p.version, p.ambiguous) == (30379, "2.6", False)


def test_fallbacks_never_raise():
    assert parse_archive_name("whatever 26.06.24.zip").version == "26.06.24"
    for junk in ["", ".zip", "no digits at all.zip", "-----.zip", "a-1-b-2.zip", "x" * 400 + ".zip"]:
        p = parse_archive_name(junk)
        assert p.mod_id is None or isinstance(p.mod_id, int)


# ---- tolerant files.json parsing

def test_parse_files_tolerant():
    payload = {"files": [
        {"file_id": 10, "version": "1.0", "category_name": "MAIN", "uploaded_timestamp": 100, "name": "a"},
        {"id": [11, 4943], "mod_version": "1.1", "category_name": "Main", "uploaded_timestamp": 200},
        {"file_id": "12", "version": "9", "category_id": 3, "uploaded_timestamp": "300"},
        {"file_id": 13, "version": "", "category_name": "OLD_VERSION", "uploaded_timestamp": 400},
        "junk", None, {"nothing": 1},
    ]}
    fs = parse_files(payload)
    assert [f.file_id for f in fs] == [10, 11, 12, 13, None]
    assert fs[2].category == "OPTIONAL" and fs[2].uploaded == 300 and fs[3].version is None
    assert latest_main_file(fs).file_id == 11
    assert parse_files([{"file_id": 1}])[0].file_id == 1  # bare list
    assert parse_files({"files": "oops"}) == [] and parse_files(None) == []
    assert latest_main_file([]) is None
    prim = parse_files({"files": [{"file_id": 5, "is_primary": True, "category_name": "UPDATE", "uploaded_timestamp": 1}]})
    assert latest_main_file(prim).file_id == 5


# ---- client

@respx.mock
def test_headers_and_quota_and_no_key_leak(caplog):
    route = respx.get(f"{B}/users/validate.json").respond(200, json={"name": "u", "is_premium": False}, headers=HDR)
    with NexusClient("SECRET-KEY-123") as c:
        assert c.validate()["name"] == "u"
        assert str(c.quota) == "2490 daily / 95 hourly left"
    h = route.calls[0].request.headers
    assert h["apikey"] == "SECRET-KEY-123" and h["application-name"] == "Cogitator" and h["application-version"]
    assert "SECRET" not in caplog.text


def test_requires_key():
    with pytest.raises(NoApiKey):
        NexusClient(None)


@respx.mock
def test_error_mapping():
    respx.get(f"{B}/users/validate.json").respond(401)
    respx.get(f"{B}/games/{G}/mods/9.json").respond(404)
    respx.get(f"{B}/games/{G}/mods/8.json").respond(200, content=b"not json")
    with NexusClient("k") as c:
        with pytest.raises(InvalidKey):
            c.validate()
        with pytest.raises(NotFound):
            c.mod(9)
        with pytest.raises(NexusError, match="invalid JSON"):
            c.mod(8)


@respx.mock
def test_429_stops_everything():
    r = respx.get(f"{B}/games/{G}/mods/19/files.json").respond(429)
    with NexusClient("k") as c:
        with pytest.raises(RateLimited):
            c.files(19)
        n = r.call_count
        with pytest.raises(RateLimited):
            c.files(8)  # no network attempt after exhaustion
        assert r.call_count == n and c.exhausted


@respx.mock
def test_zero_remaining_header_stops_further_requests():
    respx.get(f"{B}/users/validate.json").respond(200, json={}, headers={"x-rl-daily-remaining": "10", "x-rl-hourly-remaining": "0"})
    other = respx.get(f"{B}/games/{G}/mods/19.json").respond(200, json={})
    with NexusClient("k") as c:
        c.validate()
        with pytest.raises(RateLimited):
            c.mod(19)
    assert other.call_count == 0


@respx.mock
def test_retry_5xx_then_ok():
    sleeps = []
    respx.get(f"{B}/users/validate.json").mock(side_effect=[httpx.Response(503), httpx.ConnectError("x"), httpx.Response(200, json={"a": 1})])
    with NexusClient("k", sleep=sleeps.append) as c:
        assert c.validate() == {"a": 1}
    assert sleeps == [1, 2]


# ---- decisions

def F(fid, ver, ts=1):
    return nexus.NexusFile(fid, ver, "MAIN", ts)


def test_decide():
    assert decide("26.06.24", None, F(2, "26.06.24"))[0] == UP_TO_DATE
    assert decide("v26.06.24 ", None, F(2, "26.06.24"))[0] == UP_TO_DATE
    assert decide("25.12.02", None, F(2, "26.06.24")) == (UPDATE, "25.12.02 -> 26.06.24")
    assert decide(None, None, F(2, "26.06.24"))[0] == UNKNOWN
    assert decide("0107475abc", None, F(2, "26.10.22"), version_comparable=False)[0] == UNKNOWN
    assert decide("x", 5, F(5, "whatever"))[0] == UP_TO_DATE  # file id wins over version text
    assert decide("26.06.24", 4, F(5, "26.06.24"))[0] == UPDATE  # ... even when versions look equal
    assert decide("1", None, None)[0] == UNKNOWN
    assert decide("1", None, F(2, None))[0] == UNKNOWN


@respx.mock
def test_check_framework_uses_mod_ids_19_and_8():
    r19 = respx.get(f"{B}/games/{G}/mods/19/files.json").respond(200, json={"files": [
        {"file_id": 1, "version": "25.12.02", "category_name": "MAIN", "uploaded_timestamp": 1},
        {"file_id": 2, "version": "26.06.24", "category_name": "MAIN", "uploaded_timestamp": 2},
        {"file_id": 3, "version": "99", "category_name": "OPTIONAL", "uploaded_timestamp": 3}]})
    r8 = respx.get(f"{B}/games/{G}/mods/8/files.json").respond(200, json={"files": [
        {"file_id": 7, "version": "26.10.22", "category_name": "MAIN", "uploaded_timestamp": 5}]})
    reg = Registry.load()
    reg.set_framework("dml", version="25.12.02", source_kind="local", source="x")
    reg.set_framework("dmf", version="26.10.22", source_kind="local", source="x")
    with NexusClient("k") as c:
        dml, dmf = check_framework("dml", c, reg), check_framework("dmf", c, reg)
    assert (dml.status, dml.latest, dml.mod_id) == (UPDATE, "26.06.24", 19)
    assert (dmf.status, dmf.mod_id) == (UP_TO_DATE, 8)
    assert r19.call_count == 1 and r8.call_count == 1


@respx.mock
def test_check_framework_snapshot_not_compared_and_failure_reported():
    respx.get(f"{B}/games/{G}/mods/8/files.json").respond(200, json={"files": [
        {"file_id": 7, "version": "26.10.22", "category_name": "MAIN", "uploaded_timestamp": 5}]})
    respx.get(f"{B}/games/{G}/mods/19/files.json").respond(500)
    reg = Registry.load()
    reg.set_framework("dmf", version="0107475abcdef", source_kind="github-snapshot", source="x", unreleased=True)
    reg.set_framework("dml", version="26.06.24", source_kind="local", source="x")
    with NexusClient("k", sleep=lambda s: None) as c:
        assert check_framework("dmf", c, reg).status == UNKNOWN
        assert check_framework("dml", c, reg).status == FAILED


# ---- CLI

@pytest.fixture
def fake_game(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    (g / "mods").mkdir()
    return g


@respx.mock
def test_cli_check_updates(monkeypatch, capsys, fake_game):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k" * 12)
    respx.get(f"{B}/users/validate.json").respond(200, json={"is_premium": True}, headers=HDR)
    respx.get(f"{B}/games/{G}/mods/19/files.json").respond(200, json={"files": [
        {"file_id": 2, "version": "26.06.24", "category_name": "MAIN", "uploaded_timestamp": 2}]})
    Registry.load().set_framework("dml", version="26.06.24", source_kind="local", source="x")
    assert cli.main(["--game-dir", str(fake_game), "check-updates", "--json"]) in (0,)
    out = capsys.readouterr().out
    assert "up to date" in out and "k" * 12 not in out


def test_cli_check_updates_without_key(capsys, fake_game):
    Registry.load().set_framework("dml", version="26.06.24", source_kind="local", source="x")
    assert cli.main(["--game-dir", str(fake_game), "check-updates"]) == 2
    assert "set-key" in capsys.readouterr().err


@respx.mock
def test_redirects_are_never_followed_so_the_key_cannot_leak():
    respx.get(f"{B}/users/validate.json").respond(302, headers={"location": "https://evil.example/x"})
    evil = respx.get("https://evil.example/x").respond(200, json={})
    with NexusClient("SECRET-KEY-123", sleep=lambda s: None) as c:
        with pytest.raises(NexusError):
            c.validate()
    assert not evil.called
