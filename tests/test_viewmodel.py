"""Wizard page logic with no widgets (so it runs headless). Includes the key-never-leaks assertion for the key page."""
from pathlib import Path

import httpx
import pytest
import respx

from cogitator.gamefinder import Resolution
from cogitator.nexus import NexusClient
from cogitator.setup_run import KeyCheck, SetupResult, validate_key
from cogitator.sources import GITHUB_SNAPSHOT, GITHUB_TAG, NEXUS_PREMIUM, SourceOption, choose_sources
from cogitator_setup.viewmodel import KEY_BLURB, NOTICE, PATCH_SENTENCE, Page, Services, SetupModel

from test_frameworks import DML_FILES, DMF_FILES, mkzip

SECRET = "ViewModelSecretKey-abc123xyz789"
SHA = "0107475abcdef0123456789abcdef0123456789a"


@pytest.fixture
def game(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    return g


def resolver(premium_nexus=True):
    def resolve(kind, premium, key):
        if kind == "dml":
            gh = SourceOption("dml", GITHUB_TAG, "GitHub source archive of tag 26.06.24", "26.06.24", "https://api.github.com/x", ref="26.06.24")
        else:
            gh = SourceOption("dmf", GITHUB_SNAPSHOT, f"GitHub main-branch snapshot @ {SHA[:10]}", SHA, "https://api.github.com/y",
                              unreleased=True, ref=SHA)
        nx = SourceOption(kind, NEXUS_PREMIUM, "Official Nexus release", "26.x") if (premium and premium_nexus) else None
        return choose_sources(kind, premium=premium, github=gh, nexus=nx), ""
    return resolve


def svc(game, *, state=None, running=False, check=None, resolve=None):
    state = state or {}
    found = Resolution(game, "steam", [game]) if game else Resolution(None, "none", message="Darktide not found via Steam.")
    return Services(find_game=lambda: found, is_running=lambda: running,
                    installed_state=lambda g, k: state.get(k, ("missing", None)),
                    resolve_options=resolve or resolver(),
                    check_key=check or (lambda k: KeyCheck(True, False, "20000 daily / 2000 hourly", "Key ok (free account).")))


def to_review(m):
    m.accepted = True
    m.next()  # -> game
    assert m.autodetect_game()
    m.next()  # -> key
    m.skip_key()
    m.next()  # -> components
    m.next()  # -> review
    m.load_options()
    assert m.page is Page.REVIEW


# ------------------------------------------------------------ navigation and page rules

def test_welcome_requires_the_notice_to_be_accepted(game):
    m = SetupModel(svc(game))
    assert not m.can_next()[0] and "understand" in m.can_next()[1] and not m.next()
    assert "not supported by Fatshark" in NOTICE and "malware" in NOTICE and "permissions" in NOTICE
    m.accepted = True
    assert m.next() and m.page is Page.GAME


def test_game_page_autodetect_browse_and_errors(game, tmp_path):
    m = SetupModel(svc(None))
    m.accepted = True
    m.next()
    assert not m.autodetect_game() and "Browse" in m.game_message and not m.can_next()[0]
    assert not m.set_game(tmp_path / "nope") and "does not look like Darktide" in m.game_message
    assert m.set_game(game) and m.can_next() == (True, "")


def test_game_running_blocks_next_until_retry_succeeds(game):
    running = {"v": True}
    m = SetupModel(Services(find_game=lambda: Resolution(game, "steam", [game]), is_running=lambda: running["v"],
                            installed_state=lambda g, k: ("missing", None), resolve_options=resolver()))
    m.accepted = True
    m.next()
    m.autodetect_game()
    ok, why = m.can_next()
    assert not ok and "running" in why and "Retry" in why
    running["v"] = False
    assert m.refresh_running() is False
    assert m.can_next()[0]


def test_back_is_disabled_once_the_run_starts(game):
    m = SetupModel(svc(game))
    to_review(m)
    assert m.back() and m.page is Page.COMPONENTS
    m.page = Page.PROGRESS
    assert not m.back() and not m.next()
    m.page = Page.FINISH
    assert not m.back()


# ------------------------------------------------------------ key page

def test_key_page_validate_then_next_and_skip(game):
    m = SetupModel(svc(game))
    m.accepted = True
    m.next()
    m.autodetect_game()
    m.next()
    assert m.page is Page.KEY and m.can_next()[0]  # optional: Next works with no key at all
    m.set_key("  " + SECRET + " ")
    ok, why = m.can_next()
    assert not ok and "Validate" in why and "Skip" in why  # a typed-but-unchecked key cannot slip through
    assert m.key_for_plan() is None
    assert m.validate_key().ok and m.premium is False and m.can_next()[0] and m.key_for_plan() == SECRET
    m.skip_key()
    assert m.key_for_plan() is None and not m.has_key() and m.can_next()[0] and m.key_skipped


def test_invalid_key_never_reaches_the_plan(game):
    bad = lambda k: KeyCheck(False, message="Nexus did not accept this key. Check that you copied all of it, or press Skip.")
    m = SetupModel(svc(game, check=bad))
    m.set_key(SECRET)
    assert not m.validate_key().ok and m.key_for_plan() is None and not m.premium
    assert "Skip" in m.key_check.message and SECRET not in m.key_check.message


def test_key_copy():
    assert "Only needed for mod update checks" in KEY_BLURB and "K key" in KEY_BLURB and "never shared" in KEY_BLURB


def test_the_key_appears_in_no_user_visible_or_loggable_text(game):
    m = SetupModel(svc(game))
    to_review(m)
    m.page = Page.KEY
    m.set_key(SECRET)
    m.validate_key()
    blobs = [repr(m), str(m.key_check), "\n".join(m.express_lines())]
    for r in m.review_rows():
        blobs += [r.label, *r.lines, r.warning]
    assert m.confirm_install()
    plan = m.build_plan()
    assert plan.api_key == SECRET  # the plan carries it to the engine, which stores it via the key store
    res = SetupResult(ok=True, mods_on=True)
    blobs += m.finish_lines(res) + [res.diagnostics()]
    assert not any(SECRET in b for b in blobs)
    assert "Will be stored in Windows" in "\n".join(blobs)


def test_validate_key_messages_for_each_failure_mode_never_echo_the_key():
    B = "https://api.nexusmods.com/v1"
    with respx.mock:
        respx.get(f"{B}/users/validate.json").respond(200, json={"is_premium": True},
                                                      headers={"x-rl-daily-remaining": "19", "x-rl-hourly-remaining": "9"})
        ok = validate_key(SECRET)
        assert ok.ok and ok.premium and "premium" in ok.message and "19 daily" in ok.quota
        respx.get(f"{B}/users/validate.json").respond(401)
        bad = validate_key(SECRET)
        respx.get(f"{B}/users/validate.json").respond(429)
        limited = validate_key(SECRET)
        respx.get(f"{B}/users/validate.json").mock(side_effect=httpx.ConnectError("down"))
        down = validate_key(SECRET, lambda k: NexusClient(k, sleep=lambda s: None))
    assert not bad.ok and "copied all of it" in bad.message
    assert not limited.ok and "limiting" in limited.message
    assert not down.ok and "Could not reach Nexus" in down.message and "Skip" in down.message
    assert not validate_key("   ").ok
    assert not any(SECRET in x.message for x in (ok, bad, limited, down))


# ------------------------------------------------------------ components page

@pytest.mark.parametrize("state,selected,text", [
    (("missing", None), True, "not installed"),
    (("installed", "26.06.24"), False, "already installed (26.06.24)"),
    (("incomplete", None), True, "needs repair"),
    (("update", "25.12.02"), True, "update available (installed 25.12.02)"),
])
def test_detected_state_sets_defaults_and_text(game, state, selected, text):
    m = SetupModel(svc(game, state={"dml": state}))
    m.set_game(game)
    c = m.components["dml"]
    assert (c.selected, c.status_text) == (selected, text)
    assert m.components["app"].required and m.components["app"].selected
    m.toggle("app", False)
    assert m.components["app"].selected  # the app itself cannot be unticked


def test_defaults_for_the_rest(game):
    m = SetupModel(svc(game))
    assert m.enable_mods and m.desktop_icon and m.start_menu


# ------------------------------------------------------------ review page and sources

def test_free_review_shows_sources_and_the_unreleased_label(game):
    m = SetupModel(svc(game))
    to_review(m)
    rows = {r.label: r for r in m.review_rows()}
    dmf = rows["Darktide Mod Framework - UNRELEASED SNAPSHOT"]
    assert "UNRELEASED SNAPSHOT" in dmf.warning and any(SHA in l for l in dmf.lines)
    assert any("26.06.24" in l for l in rows["Darktide Mod Loader"].lines)
    assert PATCH_SENTENCE in rows["Mods"].lines and "back on" in PATCH_SENTENCE
    assert rows["Shortcuts"].lines == ["Desktop icon", "Start Menu entry"]
    assert rows["Nexus key"].lines == ["Not set. Update checks stay off until you add it (K key)."]


def test_premium_default_is_the_official_nexus_release(game):
    m = SetupModel(svc(game, check=lambda k: KeyCheck(True, True, "q", "Key ok (premium account).")))
    m.accepted = True
    m.next()
    m.autodetect_game()
    m.next()
    m.set_key(SECRET)
    m.validate_key()
    m.next()
    m.next()
    m.load_options()
    assert m.premium is True
    assert m.selected_option("dmf").kind == NEXUS_PREMIUM and m.selected_option("dmf").recommended
    assert m.selected_option("dml").kind == NEXUS_PREMIUM
    assert not any(r.warning for r in m.review_rows())  # no unreleased snapshot in the premium default


def test_a_default_is_never_a_silent_choice(game):
    m = SetupModel(svc(game))
    to_review(m)
    assert not m.confirmed
    with pytest.raises(RuntimeError, match="before the user confirmed"):
        m.build_plan()
    assert m.confirm_install() and m.confirmed
    m.select_option("dmf", 1)  # any change after reviewing withdraws the confirmation
    assert not m.confirmed
    with pytest.raises(RuntimeError):
        m.build_plan()
    m.confirm_install()
    m.back()
    assert not m.confirmed


def test_manual_choice_needs_a_zip_before_install_is_possible(game, tmp_path):
    m = SetupModel(svc(game))
    to_review(m)
    manual = [i for i, o in enumerate(m.options["dmf"]) if o.kind == "manual"][0]
    m.select_option("dmf", manual)
    ok, why = m.install_ready()
    assert not ok and "zip you downloaded" in why and not m.confirm_install()
    z = mkzip(tmp_path / "Darktide Mod Framework 8 26.10.22 2026-10-23T02-11Z b.zip", DMF_FILES)
    m.set_manual_zip("dmf", z)
    assert m.install_ready()[0] and m.confirm_install()
    plan = m.build_plan()
    dmf = [c for c in plan.components if c.option.component == "dmf"][0]
    assert dmf.option.kind == "local" and dmf.confirmed and dmf.option.version == "26.10.22"
    wrong = mkzip(tmp_path / "x 19 26.06.24 2026-06-24T06-07Z a.zip", DML_FILES)
    m.set_manual_zip("dmf", wrong)
    with pytest.raises(Exception, match="DML archive, not DMF"):
        m.install_ready()


def test_offline_leaves_only_the_manual_route_with_a_plain_reason(game):
    def offline(kind, premium, key):
        return choose_sources(kind, premium=premium, github=None, nexus=None), "Could not reach GitHub. You can still choose a zip you downloaded."

    m = SetupModel(svc(game, resolve=offline))
    to_review(m)
    assert [o.kind for o in m.options["dml"]] == ["manual"]
    ok, why = m.install_ready()
    assert not ok and "zip you downloaded" in why


def test_unticked_components_are_left_alone_and_the_plan_reflects_that(game):
    m = SetupModel(svc(game, state={"dml": ("installed", "26.06.24")}))
    to_review(m)
    assert m.selected_kinds() == ["dmf"]
    m.enable_mods, m.desktop_icon = False, False
    m.confirm_install()
    plan = m.build_plan()
    assert [c.option.component for c in plan.components] == ["dmf"] and not plan.enable_mods and not plan.desktop_icon
    rows = {r.label: r for r in m.review_rows()}
    assert "already installed (26.06.24) - left as it is." in rows["Darktide Mod Loader"].lines
    assert rows["Mods"].lines[0].startswith("Left switched off")


# ------------------------------------------------------------ express path and finish

def test_express_shows_the_sources_next_to_the_button(game):
    m = SetupModel(svc(game))
    ok, msg = m.express_prepare()
    assert ok
    lines = "\n".join(m.express_lines())
    assert "[UNRELEASED SNAPSHOT]" in lines and "Switch mods on" in lines and "desktop icon" in lines
    assert m.confirm_install() and m.build_plan().components


def test_express_failures_are_plain(game):
    ok, msg = SetupModel(svc(None)).express_prepare()
    assert not ok and "Browse" in msg
    ok, msg = SetupModel(svc(game, running=True)).express_prepare()
    assert not ok and "Close it" in msg


def test_finish_summary_is_at_most_three_short_plain_lines(game):
    m = SetupModel(svc(game))
    m.set_game(game)
    good = m.finish_lines(SetupResult(ok=True, mods_on=True))
    assert len(good) == 3 and "icon" in good[1] and "press T" in good[2] and "Mods are on" in good[0]
    bad = m.finish_lines(SetupResult(ok=False, what="Darktide is running.", why="It can corrupt files.",
                                     fix="Close Darktide, then press Retry."))
    assert bad == ["Darktide is running.", "It can corrupt files.", "Next step: Close Darktide, then press Retry."]
    assert "nothing was changed" in m.finish_lines(SetupResult(cancelled=True))[0]
    for line in good + bad:
        for jargon in ("unpatch", "patch_999", "bundle_database", "dtkit"):
            assert jargon not in line.lower()


def test_mods_cannot_be_switched_on_without_a_mod_loader(game):
    m = SetupModel(svc(game))
    to_review(m)
    m.toggle("dml", False)
    ok, why = m.install_ready()
    assert not ok and "Mod Loader is installed" in why and not m.confirm_install()
    m.enable_mods = False
    assert m.install_ready()[0]
    m.enable_mods = True
    m.components["dml"].status = "installed"  # already installed: unticking it is fine
    assert m.install_ready()[0]
