"""Fixture-driven tests (tests/fixtures/nexus = redacted real responses for mods 19 and 8)."""
import json
from pathlib import Path

import httpx
import pytest
import respx

from cogitator import cli
from cogitator.archives import match_archive_to_files, parse_archive_name
from cogitator.linking import link_mod, read_metadata_link
from cogitator.nexus import (GAME, VERIFIED_CATEGORY_IDS, NexusClient, latest_main_file, parse_files, parse_nexus_ref,
                             release_group)
from cogitator.registry import Registry
from cogitator.updates import (FAILED, NOT_LINKED, UNKNOWN, UP_TO_DATE, UPDATE, cached_rows, check_frameworks, check_mods,
                               decide_mod)

FX = Path(__file__).parent / "fixtures" / "nexus"
B = "https://api.nexusmods.com/v1"


def fx(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


F19 = parse_files(fx("files_19.json"))
F8 = parse_files(fx("files_8.json"))
PRIMARY, UPDATE_ONLY = 5781, 5782
REAL_NAME = "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z ndQ1md9gG.zip"


# ------------------------------------------------------------ real-shape parsing

def test_fixture_has_no_secrets():
    text = "".join(p.read_text(encoding="utf-8") for p in FX.glob("*.json"))
    assert "apikey" not in text.lower() and "REDACTED" in text and "@example.invalid" in text


def test_real_files_19_shape():
    assert len(F19) == 19
    cats = {f.category for f in F19}
    assert {"MAIN", "ARCHIVED", "DELETED"} <= cats  # category 6 has category_name null in the live data
    assert all(f.file_id is not None for f in F19)  # `id` is [file_id, game_id]; file_id is a top-level int
    assert {1, 4, 6, 7} == set(VERIFIED_CATEGORY_IDS)
    assert {f.category for f in F8} == {"MAIN", "OLD_VERSION", "ARCHIVED"}


def test_latest_main_prefers_primary_over_newer_update_only_variant():
    mains = {f.file_id: f for f in F19 if f.category == "MAIN"}
    assert set(mains) == {PRIMARY, UPDATE_ONLY}
    assert mains[UPDATE_ONLY].uploaded > mains[PRIMARY].uploaded  # the trap: update-only is newer
    latest = latest_main_file(F19)
    assert latest.file_id == PRIMARY and latest.version == "26.06.24"
    assert release_group(F19, latest) == {PRIMARY, UPDATE_ONLY}
    assert latest_main_file(F8).file_id == 9330


def test_archive_name_matching_against_real_names():
    assert match_archive_to_files(REAL_NAME, F19).files[0].file_id == PRIMARY
    assert match_archive_to_files(REAL_NAME.replace(".zip", " (1).zip"), F19).status == "exact"
    assert match_archive_to_files("C:\\Users\\x\\Downloads\\" + REAL_NAME, F19).status == "exact"
    old = match_archive_to_files("Darktide Mod Loader-19-25-12-02-1764720822.zip", F19)
    assert (old.status, old.files[0].file_id) == ("exact", 4069)
    # renamed random tag: version + upload time match BOTH the primary and the update-only file -> must be ambiguous
    m = match_archive_to_files("Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z zzzzzzzzz.zip", F19)
    assert m.status == "ambiguous" and {f.file_id for f in m.files} == {PRIMARY, UPDATE_ONLY}
    # dash name with right version but wrong timestamp -> none
    assert match_archive_to_files("Darktide Mod Loader-19-25-12-02-1.zip", F19).status == "none"
    only = match_archive_to_files("Darktide Mod Loader-19-25-12-02-1764720822.ZIP.zip".replace(".ZIP", ""), F19)
    assert only.status in ("exact", "inferred")


def test_parse_nexus_ref():
    assert parse_nexus_ref("19") == (19, None)
    assert parse_nexus_ref(f"https://www.nexusmods.com/{GAME}/mods/8?tab=files") == (8, None)
    assert parse_nexus_ref(f"nexusmods.com/{GAME}/mods/123#x")[0] == 123
    assert parse_nexus_ref("https://www.nexusmods.com/skyrim/mods/5")[0] is None
    assert parse_nexus_ref("hello")[0] is None and parse_nexus_ref("")[0] is None


# ------------------------------------------------------------ decisions

LATEST = {"file_id": PRIMARY, "version": "26.06.24", "group": [PRIMARY, UPDATE_ONLY]}


def rec(**kw):
    return {"nexus_id": 19, "latest": dict(LATEST), "installed_file_id": None, "installed_version": None, **kw}


def test_decide_mod_table():
    assert decide_mod(None)[0] == NOT_LINKED and decide_mod({})[0] == NOT_LINKED
    assert decide_mod({"nexus_id": 19, "latest": None})[0] == UNKNOWN
    assert decide_mod({"nexus_id": 19, "latest": None, "last_error": "boom"}) == (FAILED, "boom")
    assert decide_mod(rec(installed_file_id=PRIMARY))[0] == UP_TO_DATE
    assert decide_mod(rec(installed_file_id=UPDATE_ONLY))[0] == UP_TO_DATE  # same release group
    s, why = decide_mod(rec(installed_file_id=4069, installed_version="25.12.02"))
    assert (s, why) == (UPDATE, "25.12.02 -> 26.06.24")
    assert decide_mod({"nexus_id": 19, "latest": {"file_id": None}})[0] == UNKNOWN


def test_unknown_baseline_is_never_up_to_date():
    for version in (None, "26.06.24"):  # even when version strings "match", no baseline -> unknown
        for archive in (None, REAL_NAME):
            s, _ = decide_mod(rec(installed_file_id=None, installed_version=version, installed_archive=archive))
            assert s == UNKNOWN


# ------------------------------------------------------------ linking

def test_link_default_baseline_is_current_primary_file():
    reg = Registry.load()
    r = link_mod(reg, "foo", 19, "Darktide Mod Loader", F19, via="manual")
    assert (r["installed_file_id"], r["baseline"]) == (PRIMARY, "marked-current")  # not the newer update-only file
    assert decide_mod(Registry.load().mod("foo"))[0] == UP_TO_DATE  # persisted


def test_link_without_baseline_and_chosen():
    reg = Registry.load()
    r = link_mod(reg, "foo", 19, "n", F19, via="manual", baseline="none")
    assert r["installed_file_id"] is None and decide_mod(r)[0] == UNKNOWN
    r = link_mod(reg, "foo", 19, "n", F19, via="manual", baseline=4069)
    assert r["installed_version"] == "25.12.02" and decide_mod(r)[0] == UPDATE
    with pytest.raises(ValueError):
        link_mod(reg, "foo", 19, "n", F19, via="manual", baseline=999999)


def test_relink_clears_previous_baseline():
    reg = Registry.load()
    link_mod(reg, "foo", 19, "n", F19, via="manual")
    r = link_mod(reg, "foo", 8, "n2", F8, via="manual", baseline="none")
    assert r["nexus_id"] == 8 and r["installed_file_id"] is None and r["installed_archive"] is None


def test_metadata_link_sources(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    assert read_metadata_link(d) is None
    (d / "a.json").write_text(json.dumps({"nexus_id": 77}))
    assert read_metadata_link(d) == 77
    (d / "a.json").write_text(json.dumps({"links": {"homepage": f"https://www.nexusmods.com/{GAME}/mods/55?tab=files"}}))
    assert read_metadata_link(d) == 55
    (d / "a.json").write_text(json.dumps({"mod_id": 12, "name": "plain"}))  # generic id, no Nexus context -> ignored
    assert read_metadata_link(d) is None
    (d / "a.json").write_text(json.dumps({"mod_id": 12, "source": "nexus"}))
    assert read_metadata_link(d) == 12
    (d / "a.json").write_text(json.dumps({"url": "https://www.nexusmods.com/skyrim/mods/9"}))
    assert read_metadata_link(d) is None  # wrong game
    (d / "a.json").write_text("{not json")
    assert read_metadata_link(d) is None


# ------------------------------------------------------------ request budget

HDR = {"x-rl-daily-remaining": "19990", "x-rl-hourly-remaining": "1990"}
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def mock_nexus(updated=None, mods=(19, 8)):
    respx.get(f"{B}/users/validate.json").respond(200, json=fx("validate.json"), headers=HDR)
    routes = {"updated": respx.get(f"{B}/games/{GAME}/mods/updated.json").respond(200, json=updated if updated is not None else [], headers=HDR)}
    for m in mods:
        routes[m] = respx.get(f"{B}/games/{GAME}/mods/{m}/files.json").respond(200, json=fx(f"files_{m}.json"), headers=HDR)
    return routes


def linked_registry(**kw):
    reg = Registry.load()
    link_mod(reg, "modA", 19, "A", None, via="manual", baseline="none")
    link_mod(reg, "modB", 8, "B", None, via="manual", baseline="none")
    return reg


@respx.mock
def test_first_run_is_full_check_without_updated_json_then_ttl_cache_costs_zero():
    reg, clock = linked_registry(), Clock()
    routes = mock_nexus()
    with NexusClient("k" * 12) as nx:
        r = check_mods(["modA", "modB", "loose"], reg, nx, now=clock)
    assert r.full_check and not r.from_cache
    assert routes["updated"].call_count == 0 and routes[19].call_count == 1 and routes[8].call_count == 1
    assert r.requests == 3 and "19990 daily" in r.quota and r.premium is False
    st = {x.folder: x.status for x in r.rows}
    assert st == {"modA": UNKNOWN, "modB": UNKNOWN, "loose": NOT_LINKED}  # linked without baseline, never "up to date"
    # second launch inside the TTL: zero requests
    clock.t += 600
    with NexusClient("k" * 12) as nx:
        r2 = check_mods(["modA", "modB"], Registry.load(), nx, now=clock)
    assert r2.from_cache and r2.requests == 0 and routes[19].call_count == 1
    assert [x.status for x in r2.rows] == [UNKNOWN, UNKNOWN]


@respx.mock
def test_after_ttl_only_changed_mods_are_refetched():
    reg, clock = linked_registry(), Clock()
    routes = mock_nexus()
    with NexusClient("k" * 12) as nx:
        check_mods(["modA", "modB"], reg, nx, now=clock)
    clock.t += 7200  # TTL (1h) expired, but well under a month
    respx.clear()
    routes = mock_nexus(updated=[{"mod_id": 19, "latest_file_update": clock.t - 10, "latest_mod_activity": clock.t - 10},
                                 {"mod_id": 8, "latest_file_update": T0 - 500, "latest_mod_activity": T0 - 500},
                                 {"mod_id": 999, "latest_file_update": clock.t, "latest_mod_activity": clock.t}])
    with NexusClient("k" * 12) as nx:
        r = check_mods(["modA", "modB"], Registry.load(), nx, now=clock)
    assert not r.full_check
    assert routes["updated"].call_count == 1
    assert routes[19].call_count == 1 and routes[8].call_count == 0  # only the changed one
    assert r.requests == 3  # validate + updated + one files.json


@respx.mock
def test_never_checked_linked_mod_is_fetched_even_if_not_listed_as_updated():
    reg, clock = linked_registry(), Clock()
    mock_nexus()
    with NexusClient("k" * 12) as nx:
        check_mods(["modA"], reg, nx, now=clock)  # modB not installed/checked in run 1
    clock.t += 7200
    respx.clear()
    routes = mock_nexus(updated=[])
    with NexusClient("k" * 12) as nx:
        check_mods(["modA", "modB"], Registry.load(), nx, now=clock)
    assert routes[8].call_count == 1 and routes[19].call_count == 0


@respx.mock
def test_older_than_a_month_forces_full_check():
    reg, clock = linked_registry(), Clock()
    mock_nexus()
    with NexusClient("k" * 12) as nx:
        check_mods(["modA", "modB"], reg, nx, now=clock)
    clock.t += 31 * 86400
    respx.clear()
    routes = mock_nexus(updated=[])
    with NexusClient("k" * 12) as nx:
        r = check_mods(["modA", "modB"], Registry.load(), nx, now=clock)
    assert r.full_check and routes["updated"].call_count == 0 and routes[19].call_count == 1 and routes[8].call_count == 1


@respx.mock
def test_force_bypasses_ttl_but_not_the_budget():
    reg, clock = linked_registry(), Clock()
    mock_nexus()
    with NexusClient("k" * 12) as nx:
        check_mods(["modA", "modB"], reg, nx, now=clock)
    clock.t += 60
    respx.clear()
    routes = mock_nexus(updated=[])
    with NexusClient("k" * 12) as nx:
        r = check_mods(["modA", "modB"], Registry.load(), nx, force=True, now=clock)
    assert not r.from_cache and routes["updated"].call_count == 1 and routes[19].call_count == 0 and routes[8].call_count == 0


@respx.mock
def test_updated_json_failure_falls_back_to_full_check():
    reg, clock = linked_registry(), Clock()
    mock_nexus()
    with NexusClient("k" * 12) as nx:
        check_mods(["modA", "modB"], reg, nx, now=clock)
    clock.t += 7200
    respx.clear()
    routes = mock_nexus()
    routes["updated"].respond(403)
    with NexusClient("k" * 12, sleep=lambda s: None) as nx:
        r = check_mods(["modA", "modB"], Registry.load(), nx, now=clock)
    assert r.full_check and routes[19].call_count == 1 and routes[8].call_count == 1


@respx.mock
def test_rate_limit_mid_run_stops_gracefully_and_keeps_cache():
    reg, clock = linked_registry(), Clock()
    respx.get(f"{B}/users/validate.json").respond(200, json=fx("validate.json"), headers=HDR)
    r19 = respx.get(f"{B}/games/{GAME}/mods/19/files.json").respond(200, json=fx("files_19.json"), headers=HDR)
    r8 = respx.get(f"{B}/games/{GAME}/mods/8/files.json").respond(429)
    with NexusClient("k" * 12) as nx:
        r = check_mods(["modA", "modB"], reg, nx, now=clock)
    assert r.rate_limited and r19.call_count == 1 and r8.call_count == 1
    reg2 = Registry.load()
    assert "last_run" not in reg2.data["updates"]  # not advanced -> next launch retries
    assert reg2.mod("modA")["latest"]["file_id"] == PRIMARY  # progress kept
    assert [x.status for x in r.rows] == [UNKNOWN, UNKNOWN]


@respx.mock
def test_exhausted_quota_header_stops_further_requests():
    reg = linked_registry()
    respx.get(f"{B}/users/validate.json").respond(200, json=fx("validate.json"),
                                                  headers={"x-rl-daily-remaining": "5", "x-rl-hourly-remaining": "0"})
    f = respx.get(f"{B}/games/{GAME}/mods/19/files.json").respond(200, json=fx("files_19.json"))
    with NexusClient("k" * 12) as nx:
        r = check_mods(["modA", "modB"], reg, nx, now=Clock())
    assert r.rate_limited and f.call_count == 0


@respx.mock
def test_baseline_resolved_from_installed_archive_on_fetch():
    reg, clock = Registry.load(), Clock()
    reg.set_mod("modA", nexus_id=19, installed_archive=REAL_NAME, installed_file_id=None, latest=None, checked_at=None)
    mock_nexus()
    with NexusClient("k" * 12) as nx:
        r = check_mods(["modA"], reg, nx, now=clock)
    assert r.rows[0].status == UP_TO_DATE
    assert Registry.load().mod("modA")["installed_file_id"] == PRIMARY


@respx.mock
def test_detects_update_and_reports_per_row_failure():
    reg = Registry.load()
    link_mod(reg, "old", 19, "n", F19, via="manual", baseline=4069)
    link_mod(reg, "gone", 8, "n", None, via="manual", baseline="none")
    respx.get(f"{B}/users/validate.json").respond(200, json=fx("validate.json"), headers=HDR)
    respx.get(f"{B}/games/{GAME}/mods/19/files.json").respond(200, json=fx("files_19.json"), headers=HDR)
    respx.get(f"{B}/games/{GAME}/mods/8/files.json").respond(404, headers=HDR)
    seen = []
    with NexusClient("k" * 12) as nx:
        r = check_mods(["old", "gone"], reg, nx, now=Clock(), on_row=seen.append)
    st = {x.folder: x for x in r.rows}
    assert st["old"].status == UPDATE and st["old"].installed == "25.12.02" and st["old"].latest == "26.06.24"
    assert st["gone"].status == FAILED and "404" in st["gone"].reason
    assert {x.folder for x in seen} == {"old", "gone"}
    assert [x.folder for x in seen[:2]] == ["old", "gone"]  # emitted per mod as each result arrives, then a final sweep
    assert seen[0].status == UPDATE


def test_no_key_uses_cache_only():
    reg = linked_registry()
    r = check_mods(["modA"], reg, None, now=Clock())
    assert r.from_cache and r.error == "no Nexus API key set" and r.rows[0].status == UNKNOWN


@respx.mock
def test_frameworks_cached_within_ttl_and_resolve_file_id_from_archive_name():
    reg, clock = Registry.load(), Clock()
    reg.set_framework("dml", version="26.06.24", source_kind="local", source="x", archive_name=REAL_NAME)
    reg.set_framework("dmf", version="26.10.22", source_kind="local",
                      source="local file Darktide Mod Framework 8 26.10.22 2026-10-23T02-11Z FQPKZQIuu.zip")  # legacy record
    routes = mock_nexus()
    with NexusClient("k" * 12) as nx:
        out = check_frameworks(reg, nx, now=clock)
    assert {u.kind: u.status for u in out} == {"dml": UP_TO_DATE, "dmf": UP_TO_DATE}
    assert reg.framework("dml")["file_id"] == PRIMARY and reg.framework("dmf")["file_id"] == 9330
    clock.t += 60
    with NexusClient("k" * 12) as nx:
        again = check_frameworks(Registry.load(), nx, now=clock)
    assert routes[19].call_count == 1 and routes[8].call_count == 1 and [u.status for u in again] == [UP_TO_DATE] * 2


@respx.mock
def test_framework_older_file_flags_update():
    reg = Registry.load()
    reg.set_framework("dml", version="25.12.02", source_kind="local", source="x",
                      archive_name="Darktide Mod Loader-19-25-12-02-1764720822.zip")
    mock_nexus()
    with NexusClient("k" * 12) as nx:
        (u,) = check_frameworks(reg, nx, now=Clock())
    assert u.status == UPDATE and u.latest == "26.06.24" and u.latest_file_id == PRIMARY


# ------------------------------------------------------------ CLI: link + status

@pytest.fixture
def game(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    for m in ("alpha", "beta"):
        (g / "mods" / m).mkdir(parents=True)
        (g / "mods" / m / f"{m}.mod").write_text("return {}")
    (g / "mods" / "mod_load_order.txt").write_bytes(b"alpha\r\n")
    return g


@respx.mock
def test_cli_link_default_marks_current(game, monkeypatch, capsys):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k" * 12)
    respx.get(f"{B}/games/{GAME}/mods/19.json").respond(200, json=fx("mod_19.json"), headers=HDR)
    respx.get(f"{B}/games/{GAME}/mods/19/files.json").respond(200, json=fx("files_19.json"), headers=HDR)
    g = ["--game-dir", str(game)]
    assert cli.main(g + ["link", "alpha", f"https://www.nexusmods.com/{GAME}/mods/19", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Darktide Mod Loader" in out and "Installed file recorded: 5781" in out
    assert cli.main(g + ["status", "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert {m["folder"]: m["update_status"] for m in st["mods"]} == {"alpha": "up to date", "beta": "not linked"}


@respx.mock
def test_cli_link_no_baseline_and_errors(game, monkeypatch, capsys):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k" * 12)
    respx.get(f"{B}/games/{GAME}/mods/19.json").respond(200, json=fx("mod_19.json"))
    respx.get(f"{B}/games/{GAME}/mods/19/files.json").respond(200, json=fx("files_19.json"))
    g = ["--game-dir", str(game)]
    assert cli.main(g + ["link", "alpha", "19", "--yes", "--no-baseline"]) == 0
    assert "Baseline unknown" in capsys.readouterr().out
    assert cli.main(g + ["status"]) == 0 and "[unknown]" in capsys.readouterr().out
    assert cli.main(g + ["link", "alpha", "https://www.nexusmods.com/skyrim/mods/5", "--yes"]) == 2
    assert cli.main(g + ["link", "nope", "19", "--yes"]) == 2
    assert cli.main(g + ["link", "dmf", "19", "--yes"]) == 2  # internal folder
    assert cli.main(g + ["link", "alpha", "19"]) == 2  # no tty, no --yes -> not confirmed
    monkeypatch.delenv("COGITATOR_NEXUS_KEY")
    assert cli.main(g + ["link", "alpha", "19", "--yes"]) == 2


@respx.mock
def test_cli_check_updates_end_to_end_and_ttl(game, monkeypatch, capsys):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k" * 12)
    reg = Registry.load()
    link_mod(reg, "alpha", 19, "n", F19, via="manual", baseline=4069)
    mock_nexus()
    g = ["--game-dir", str(game)]
    assert cli.main(g + ["check-updates", "--json"]) == 1  # update available -> exit 1
    out = json.loads(capsys.readouterr().out)
    assert out["mods"][0]["status"] == "update available" and out["requests"] == 2 and not out["from_cache"]
    assert "k" * 12 not in json.dumps(out)
    assert cli.main(g + ["check-updates", "--json"]) == 1
    again = json.loads(capsys.readouterr().out)
    assert again["from_cache"] and again["requests"] == 0
