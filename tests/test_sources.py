"""Source resolution/selection: free vs premium defaults, the UNRELEASED label, nothing downloaded until fetch."""
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from cogitator.loader import GITHUB_API, LoaderError, make_client
from cogitator.nexus import GAME, NexusClient, parse_files
from cogitator import sources as src
from cogitator.sources import (GITHUB_PINNED, GITHUB_RELEASE, GITHUB_SNAPSHOT, GITHUB_TAG, LOCAL, NEXUS_PREMIUM, PINNED_DMF, Pin,
                               SNAPSHOT_EXPLANATION, choose_sources, fetch_source, local_option, resolve_github,
                               resolve_head_snapshot, resolve_nexus, resolve_pinned_dmf)

from test_frameworks import DMF_FILES, DML_FILES, mkzip

FX = Path(__file__).parent / "fixtures" / "nexus"
B = "https://api.nexusmods.com/v1"
DML_R, DMF_R = "Darktide-Mod-Framework/Darktide-Mod-Loader", "Darktide-Mod-Framework/Darktide-Mod-Framework"
SHA = "0107475abcdef0123456789abcdef0123456789a"


def api(p):
    return f"{GITHUB_API}/repos/{p}"


def zbytes(files, prefix=""):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(prefix + n, d)
    return b.getvalue()


def mock_github():
    return [respx.get(api(f"{DML_R}/releases/latest")).respond(200, json={"tag_name": "26.06.24", "assets": []}),
            respx.get(api(f"{DMF_R}/releases/latest")).respond(404),
            respx.get(api(f"{DMF_R}/commits/HEAD")).respond(200, json={"sha": SHA, "commit": {"committer": {"date": "2026-10-22T10:00:00Z"}}})]


@respx.mock
def test_resolve_downloads_nothing():
    routes = mock_github()
    with make_client() as c:
        dml, dmf = resolve_github("dml", c), resolve_head_snapshot("dmf", c)
    assert (dml.kind, dml.version, dml.unreleased) == (GITHUB_TAG, "26.06.24", False)
    assert (dmf.kind, dmf.version, dmf.unreleased) == (GITHUB_SNAPSHOT, SHA, True)
    assert SNAPSHOT_EXPLANATION in dmf.explanation and "UNRELEASED SNAPSHOT" in dmf.label.upper() + dmf.explanation.upper()
    assert all("zipball" not in str(r.calls.last.request.url) for r in routes if r.called)  # metadata only, no archive fetched
    assert dmf.url.endswith(f"/zipball/{SHA}") and dml.url.endswith("/zipball/26.06.24")  # pinned refs


@respx.mock
def test_release_with_a_zip_asset_is_preferred():
    respx.get(api(f"{DML_R}/releases/latest")).respond(200, json={"tag_name": "9.9.9", "assets": [
        {"name": "DML.zip", "browser_download_url": "https://dl.example/DML.zip", "size": 1234}]})
    with make_client() as c:
        o = resolve_github("dml", c)
    assert o.kind == GITHUB_RELEASE and o.size == 1234 and not o.unreleased


@respx.mock
def test_github_rate_limit_is_a_plain_message():
    respx.get(api(f"{DMF_R}/releases/latest")).respond(404)
    respx.get(api(f"{DMF_R}/commits/HEAD")).respond(403, headers={"x-ratelimit-remaining": "0"})
    with make_client() as c, pytest.raises(LoaderError, match="rate limit"):
        resolve_head_snapshot("dmf", c)


@respx.mock
def test_resolve_nexus_picks_the_primary_main_file():
    respx.get(f"{B}/games/{GAME}/mods/19/files.json").respond(200, json=json.loads((FX / "files_19.json").read_text(encoding="utf-8")))
    with NexusClient("k" * 12) as nx:
        o = resolve_nexus("dml", nx)
    assert o.kind == NEXUS_PREMIUM and o.nexus_file.file_id == 5781 and o.version == "26.06.24"  # not the "(Update Only)" variant


# ---- choose: defaults are pre-selections only

def opts(premium, *, dmf=True):
    from cogitator.sources import SourceOption
    gh = SourceOption("dmf", GITHUB_SNAPSHOT, "snap", SHA, unreleased=True) if dmf else SourceOption("dml", GITHUB_TAG, "tag", "26.06.24")
    nx = SourceOption("dmf" if dmf else "dml", NEXUS_PREMIUM, "official", "26.10.22")
    return choose_sources("dmf" if dmf else "dml", premium=premium, github=gh, nexus=nx)


def test_premium_defaults_to_the_official_nexus_release():
    o = opts(True)
    assert o[0].kind == NEXUS_PREMIUM and o[0].recommended and [x.kind for x in o] == [NEXUS_PREMIUM, GITHUB_SNAPSHOT]


def test_free_dmf_offers_snapshot_and_manual_but_never_recommends_the_snapshot():
    o = opts(False)
    assert [x.kind for x in o] == [GITHUB_SNAPSHOT, "manual"]
    assert o[0].unreleased and not o[0].recommended and not any(x.kind == NEXUS_PREMIUM for x in o)


def test_free_dml_tag_source_is_recommended():
    o = opts(False, dmf=False)
    assert o[0].kind == GITHUB_TAG and o[0].recommended and o[-1].kind == "manual"


def test_unknown_account_is_treated_like_free_and_offline_leaves_manual_only():
    assert [x.kind for x in opts(None)] == [GITHUB_SNAPSHOT, "manual"]
    only = choose_sources("dmf", premium=False, github=None, nexus=None)
    assert [x.kind for x in only] == ["manual"] and "nexusmods.com" in only[0].url


def test_premium_with_no_official_release_falls_back_to_github():
    from cogitator.sources import SourceOption
    o = choose_sources("dml", premium=True, github=SourceOption("dml", GITHUB_TAG, "t", "1"), nexus=None)
    assert o[0].kind == GITHUB_TAG


def test_review_lines_carry_the_unreleased_warning():
    from cogitator.sources import SourceOption
    text = "\n".join(SourceOption("dmf", GITHUB_SNAPSHOT, "snap", SHA, url="https://x", unreleased=True).review_lines())
    assert "UNRELEASED SNAPSHOT" in text and SHA in text and "https://x" in text


# ---- fetch: exactly the pinned ref

@respx.mock
def test_fetch_downloads_the_pinned_tag_and_sha(tmp_path):
    mock_github()
    z = respx.get(api(f"{DML_R}/zipball/26.06.24")).respond(200, content=zbytes(DML_FILES, "o-r-4bd075a/"))
    z2 = respx.get(api(f"{DMF_R}/zipball/{SHA}")).respond(200, content=zbytes(DMF_FILES, "o-r-0107475/"))
    with make_client() as c:
        dml, dmf = resolve_github("dml", c), resolve_head_snapshot("dmf", c)
        p1, p2 = fetch_source(dml, tmp_path, client=c), fetch_source(dmf, tmp_path, client=c)
    assert z.call_count == 1 and z2.call_count == 1
    assert (p1.component, p1.source.kind, p1.source.version) == ("dml", GITHUB_TAG, "26.06.24")
    assert (p2.source.kind, p2.source.version, p2.source.unreleased) == (GITHUB_SNAPSHOT, SHA, True)
    assert p2.source.sha256 and dmf.sha256 == p2.source.sha256  # the hash becomes known after the fetch


@respx.mock
def test_fetch_refuses_an_archive_of_the_wrong_kind(tmp_path):
    mock_github()
    respx.get(api(f"{DML_R}/zipball/26.06.24")).respond(200, content=zbytes(DMF_FILES, "o-r-1/"))
    with make_client() as c:
        dml = resolve_github("dml", c)
        with pytest.raises(LoaderError, match="looks like DMF"):
            fetch_source(dml, tmp_path, client=c)


@respx.mock
def test_fetch_premium_downloads_through_the_api(tmp_path):
    files = json.loads((FX / "files_19.json").read_text(encoding="utf-8"))
    data = zbytes(DML_FILES)
    for f in files["files"]:
        if f["file_id"] == 5781:
            f["size_in_bytes"], f["file_name"] = len(data), "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z ndQ1md9gG.zip"
    respx.get(f"{B}/games/{GAME}/mods/19/files.json").respond(200, json=files)
    respx.get(f"{B}/games/{GAME}/mods/19/files/5781/download_link.json").respond(200, json=[{"URI": "https://cdn.example/d.zip"}])
    respx.get("https://cdn.example/d.zip").respond(200, content=data)
    with NexusClient("k" * 12) as nx:
        o = resolve_nexus("dml", nx)
        plan = fetch_source(o, tmp_path, nexus=nx)
    assert plan.source.kind == NEXUS_PREMIUM and plan.source.version == "26.06.24" and plan.component == "dml"
    assert plan.source.archive_name.endswith("ndQ1md9gG.zip")  # lets the update check resolve the exact file id


def test_local_option_checks_the_kind_and_carries_the_hash(tmp_path):
    z = mkzip(tmp_path / "Darktide Mod Loader 19 26.06.24 2026-06-24T06-07Z a.zip", DML_FILES)
    o = local_option("dml", z)
    assert o.kind == LOCAL and o.sha256 and o.version == "26.06.24"
    with pytest.raises(LoaderError, match="DML archive, not DMF"):
        local_option("dmf", z)
    assert fetch_source(o, tmp_path).component == "dml"


# ---- the pinned DMF source (Express default for free accounts)

def test_resolving_the_pinned_dmf_needs_no_network():
    with respx.mock(assert_all_called=False):  # any HTTP request at all would raise
        with make_client() as c:
            o = resolve_github("dmf", c)
    assert o.kind == GITHUB_PINNED and o.pin == PINNED_DMF and o.version == PINNED_DMF.sha and not o.unreleased
    text = chr(10).join(o.review_lines())
    assert PINNED_DMF.sha in text and PINNED_DMF.payload_sha256 in text and "26.10.22" in text and "asked before anything is installed" in text


@pytest.mark.skipif(not list(Path(__file__).parent.parent.glob("Darktide Mod Framework*.zip")), reason="Nexus zip not on disk")
def test_the_built_in_pin_still_equals_the_real_nexus_26_10_22_payload():
    from cogitator.frameworks import payload_hash
    z = next(Path(__file__).parent.parent.glob("Darktide Mod Framework*.zip"))
    assert "26.10.22" in z.name
    assert payload_hash(z) == (PINNED_DMF.payload_sha256, PINNED_DMF.files)


def test_payload_hash_ignores_wrappers_and_changes_with_content(tmp_path):
    from cogitator.frameworks import payload_hash
    a = mkzip(tmp_path / "a.zip", DMF_FILES)
    b = mkzip(tmp_path / "b.zip", DMF_FILES, "owner-repo-abc1234/")
    assert payload_hash(a) == payload_hash(b) and payload_hash(a)[1] == len(DMF_FILES)
    edited = mkzip(tmp_path / "c.zip", {**DMF_FILES, "dmf/scripts/mods/dmf/dmf_loader.lua": b"-- changed"})
    renamed = mkzip(tmp_path / "d.zip", {("dmf/scripts/mods/dmf/other.lua" if k.endswith("dmf_loader.lua") else k): v for k, v in DMF_FILES.items()})
    extra = mkzip(tmp_path / "e.zip", {**DMF_FILES, "dmf/extra.lua": b"x"})
    assert len({payload_hash(z)[0] for z in (a, edited, renamed, extra)}) == 4
    with pytest.raises(LoaderError, match="not a DMF"):
        payload_hash(mkzip(tmp_path / "f.zip", DML_FILES), "dmf")


def pin_for(zip_path, **kw):
    from cogitator.frameworks import payload_hash
    h, n = payload_hash(zip_path)
    return Pin(PINNED_DMF.repo, SHA, "26.10.22", kw.get("payload", h), n)


@respx.mock
def test_pinned_fetch_that_matches_is_labelled_verified(tmp_path):
    data = zbytes(DMF_FILES, "o-r-0107475/")
    ref = mkzip(tmp_path / "ref.zip", DMF_FILES)
    pin = pin_for(ref)
    respx.get(api(f"{DMF_R}/zipball/{SHA}")).respond(200, content=data)
    opt = resolve_pinned_dmf(pin)
    with make_client() as c:
        plan = fetch_source(opt, tmp_path / "w", client=c)
    assert plan.source.verified is True and not plan.source.unreleased and plan.source.kind == GITHUB_PINNED
    assert plan.source.description == "matches Nexus release 26.10.22 (verified)" and opt.label == plan.source.description
    assert plan.source.version == "26.10.22" and plan.source.sha256 and "verified" in __import__("cogitator.frameworks", fromlist=["x"]).describe_plan(plan)


@respx.mock
def test_pinned_fetch_with_a_different_payload_falls_back_to_the_unreleased_warning(tmp_path):
    ref = mkzip(tmp_path / "ref.zip", DMF_FILES)
    pin = pin_for(ref)
    drifted = zbytes({**DMF_FILES, "dmf/scripts/mods/dmf/dmf_loader.lua": b"-- not the official file"}, "o-r-1/")
    respx.get(api(f"{DMF_R}/zipball/{SHA}")).respond(200, content=drifted)
    opt = resolve_pinned_dmf(pin)
    with make_client() as c:
        plan = fetch_source(opt, tmp_path / "w", client=c)
    assert plan.source.verified is False and plan.source.unreleased and opt.unreleased
    assert "UNRELEASED SNAPSHOT" in plan.source.description and "does NOT match" in plan.source.description
    assert "does NOT match" in __import__("cogitator.frameworks", fromlist=["x"]).describe_plan(plan)


def test_free_choice_prefers_pinned_and_premium_still_prefers_nexus():
    pinned = resolve_pinned_dmf()
    nx = src.SourceOption("dmf", NEXUS_PREMIUM, "official", "26.10.22")
    free = choose_sources("dmf", premium=False, github=pinned, nexus=None)
    assert [o.kind for o in free] == [GITHUB_PINNED, "manual"] and free[0].recommended and not free[0].unreleased
    prem = choose_sources("dmf", premium=True, github=pinned, nexus=nx)
    assert [o.kind for o in prem] == [NEXUS_PREMIUM, GITHUB_PINNED] and prem[0].recommended and not prem[1].recommended
