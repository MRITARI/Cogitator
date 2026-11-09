"""Pre-existing mods: guessed baseline from file dates, name-search candidates, link-all flow."""
import json
import os

import httpx
import pytest
import respx

from cogitator.linking import GUESS_WINDOW_S, apply_guess, choose_baseline, folder_mtime, guess_baseline, link_mod, rank_candidates
from cogitator.nexus import GAME, GRAPHQL, NexusClient, NexusFile, parse_files
from cogitator.registry import Registry
from cogitator.tui.app import installed_text, status_text
from cogitator.glyphs import glyphs_for
from cogitator.updates import PROBABLY_OUTDATED, PROBABLY_UP_TO_DATE, UNKNOWN, UP_TO_DATE, cached_rows, check_mods, decide_mod

from test_tui import B, HDR, SIZE, accepted_notice, fx, game, make_app, settle  # noqa: F401  (fixtures)

DAY = 86400


def files(*uploads):
    return [NexusFile(100 + i, f"1.{i}", "MAIN", up) for i, up in enumerate(uploads)]


def test_guess_picks_first_upload_at_or_after_mtime_within_a_week():
    fs = files(1_000_000, 2_000_000, 2_000_000 + 3 * DAY, 2_000_000 + 30 * DAY)
    assert guess_baseline(1_999_000, fs).file_id == 101        # first upload >= mtime
    assert guess_baseline(2_000_000 + DAY, fs).file_id == 102  # earlier uploads are skipped
    assert guess_baseline(2_000_000 + 3 * DAY - GUESS_WINDOW_S, fs).file_id == 101  # edge: exactly 7 days before


def test_guess_ignored_when_mtime_is_far_from_every_upload():
    fs = files(1_000_000, 2_000_000)
    assert guess_baseline(2_000_000 - GUESS_WINDOW_S - 1, fs) is None  # next upload is more than 7 days away
    assert guess_baseline(2_000_000 + 1, fs) is None                   # newer than every upload
    assert guess_baseline(None, fs) is None


def test_folder_mtime_ignores_load_order_and_dotfiles(tmp_path):
    d = tmp_path / "m"
    (d / ".git").mkdir(parents=True)
    for rel, t in (("a.lua", 1000), ("sub/b.lua", 2000), ("mod_load_order.txt", 9000), (".hidden", 9000), (".git/x", 9000)):
        p = d / rel
        p.parent.mkdir(exist_ok=True)
        p.write_text("x")
        os.utime(p, (t, t))
    assert folder_mtime(d) == 2000
    assert folder_mtime(tmp_path / "missing") is None


def test_guessed_baseline_is_stored_shown_with_tilde_and_never_plain_up_to_date():
    reg = Registry.load()
    fs = files(1_000_000, 2_000_000)
    link_mod(reg, "m", 5, "M", fs, via="manual", baseline="none")
    assert apply_guess(reg, "m", fs, 999_000).file_id == 100
    rec = reg.mod("m")
    assert (rec["baseline"], rec["installed_file_id"]) == ("guessed", 100)
    row = cached_rows(["m"], reg)[0]
    assert row.status == PROBABLY_OUTDATED and row.installed == "~1.0"   # old file vs latest 1.1
    assert installed_text(row, None).plain == "~1.0"
    assert "probably outdated" in status_text(row, glyphs_for("utf-8")).plain
    rec["installed_file_id"], rec["installed_version"] = 101, "1.1"      # guess equals the latest file
    assert decide_mod(rec)[0] == PROBABLY_UP_TO_DATE != UP_TO_DATE


def test_guess_never_overrides_known_baseline_or_unlinked_mod():
    reg = Registry.load()
    fs = files(1_000_000)
    assert apply_guess(reg, "nope", fs, 999_000) is None and reg.mod("nope") is None
    link_mod(reg, "m", 5, "M", fs, via="manual")  # marked-current
    assert apply_guess(reg, "m", fs, 999_000) is None and reg.mod("m")["baseline"] == "marked-current"


def test_confirming_a_guess_makes_it_chosen():
    reg = Registry.load()
    fs = files(1_000_000, 2_000_000)
    link_mod(reg, "m", 5, "M", fs, via="manual", baseline="none")
    apply_guess(reg, "m", fs, 999_000)
    choose_baseline(reg, "m", fs, 101)
    rec = Registry.load().mod("m")
    assert (rec["baseline"], rec["installed_file_id"]) == ("chosen", 101)
    assert decide_mod(rec)[0] == UP_TO_DATE
    with pytest.raises(ValueError):
        choose_baseline(reg, "m", fs, 999)


def test_unknown_date_fallback_and_unknown_status_wording():
    reg = Registry.load()
    link_mod(reg, "m", 5, "M", None, via="manual", baseline="none")
    row = cached_rows(["m"], reg)[0]
    assert row.status == UNKNOWN and row.baseline_unknown
    assert status_text(row, glyphs_for("utf-8")).plain == "version unknown - press L to set it"
    assert installed_text(row, 86400 * 365).plain.startswith("1971")  # unknown version -> folder's last-modified date
    assert installed_text(row, None).plain == "-"
    assert status_text(None, glyphs_for("utf-8")).plain == "not linked"


def test_rank_candidates_exact_then_near_only_and_limited_to_three():
    res = [{"mod_id": 1, "name": "Unrelated Thing"}, {"mod_id": 2, "name": "Cool Mod Extended"},
           {"mod_id": 3, "name": "cool mod"}, {"mod_id": 4, "name": "Cool Mod 2"}, {"mod_id": 5, "name": "Cool Mods"},
           {"mod_id": 6, "name": "Cool_Mod"}]
    got = [c["mod_id"] for c in rank_candidates("Cool_Mod", res)]
    assert got[:2] == [3, 6] and len(got) == 3 and 1 not in got  # exact ignoring case/space/underscore first


@respx.mock
def test_check_mods_guesses_baseline_for_linked_mod_without_one():
    reg = Registry.load()
    link_mod(reg, "modA", 19, "A", None, via="manual", baseline="none")
    F19 = parse_files(fx("files_19.json"))
    f = next(x for x in F19 if x.uploaded and x.file_id)
    respx.get(f"{B}/users/validate.json").respond(200, json=fx("validate.json"), headers=HDR)
    respx.get(f"{B}/games/{GAME}/mods/19/files.json").respond(200, json=fx("files_19.json"), headers=HDR)
    with NexusClient("k" * 12) as nx:
        check_mods(["modA"], reg, nx, mtimes={"modA": f.uploaded - 60})
    rec = Registry.load().mod("modA")
    assert rec["baseline"] == "guessed" and rec["installed_file_id"] is not None


# ------------------------------------------------------------ TUI: link-all flow

def graphql(request):
    q = json.loads(request.content)["variables"]["f"]["name"][0]["value"]
    nodes = [{"modId": 19, "name": "Alpha Overhaul"}] if q == "alpha" else []
    if q == "beta":
        nodes = [{"modId": 8, "name": "Betamax Gizmo"}]  # fuzzy-ish: must never be linked unless the user says yes
    return httpx.Response(200, json={"data": {"mods": {"nodes": nodes}}}, headers=HDR)


async def next_dialog(app, pilot):
    for _ in range(100):
        await pilot.pause(0.05)
        if len(app.screen_stack) > 1:
            return
    raise AssertionError("dialog did not appear")


@respx.mock
async def test_link_all_end_to_end_with_guess_preselected_and_no_auto_link(game, monkeypatch):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k" * 12)
    f = next(x for x in parse_files(fx("files_19.json")) if x.uploaded and x.file_id)
    for p in (game / "mods" / "alpha").iterdir():
        os.utime(p, (f.uploaded - 3600, f.uploaded - 3600))
    guess = guess_baseline(folder_mtime(game / "mods" / "alpha"), parse_files(fx("files_19.json")))
    respx.post(GRAPHQL).mock(side_effect=graphql)
    respx.get(f"{B}/games/{GAME}/mods/19.json").respond(200, json=fx("mod_19.json"), headers=HDR)
    respx.get(f"{B}/games/{GAME}/mods/19/files.json").respond(200, json=fx("files_19.json"), headers=HDR)
    app = make_app(game)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("m")
        # alpha: candidate offered -> Y (first option)
        await next_dialog(app, pilot)
        assert "Link 'alpha'?" in "".join(str(w.render()) for w in app.screen.query("Static"))
        await pilot.press("enter")
        # version picker: guess preselected, guessed baseline already stored
        await next_dialog(app, pilot)
        assert Registry.load().mod("alpha")["baseline"] == "guessed"
        assert Registry.load().mod("alpha")["installed_file_id"] == guess.file_id
        assert app.screen.query_one("OptionList").highlighted is not None
        await pilot.press("enter")  # confirm the preselected guess
        # beta: fuzzy candidate offered; skip it (option "Skip this mod" is 2nd to last: Yes, No, Skip, Stop)
        await next_dialog(app, pilot)
        await pilot.press("down", "down", "enter")
        # remaining unlinked mods (broken, gamma): no candidate -> blank input = skip
        for _ in range(2):
            await next_dialog(app, pilot)
            await pilot.press("enter")
            await pilot.pause(0.2)
        await settle(app, pilot)
        reg = Registry.load()
        assert reg.mod("alpha")["baseline"] == "chosen" and reg.mod("alpha")["nexus_id"] == 19
        assert reg.mod("beta") is None and reg.mod("gamma") is None  # fuzzy match was not auto-linked
        assert "Link all finished: 1 linked" in "\n".join(l.text for l in app.query_one("#log").lines)
