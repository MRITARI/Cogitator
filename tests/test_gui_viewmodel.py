"""The main window's table rules, with no display: filter, sort, marks, row text and colours, drag steps, style mapping."""
from cogitator import actions
from cogitator.mods import DISABLED, ENABLED, UNLISTED, Mod
from cogitator.updates import FAILED, NOT_LINKED, PROBABLY_OUTDATED, UNKNOWN, UP_TO_DATE, UPDATE, ModRow
from cogitator_gui.viewmodel import (COLUMNS, PALETTES, TAG_BAD, TAG_DIM, TAG_OK, TAG_WARN, ModListModel, move_steps,
                                     style_options, style_tag)


def mods():
    return [Mod("alpha", ENABLED, order=0), Mod("Beta", DISABLED, order=1), Mod("gamma", UNLISTED),
            Mod("ghost", ENABLED, present=False, order=2), Mod("nomod", ENABLED, has_mod_file=False, order=3)]


def row(folder, status, installed=None, latest=None, reason="r", **kw):
    return ModRow(folder, 1, status, installed, latest, reason, **kw)


def model(meta=None):
    meta = meta or {}
    m = ModListModel(meta=lambda f: meta.get(f, {}), mtime=lambda f: None)
    m.set_mods(mods())
    return m


def folders(m, keep=None):
    return [r.folder for r in m.rows(keep)]


def test_columns_match_the_terminal_ui():
    assert [c[1] for c in COLUMNS] == ["On/Off", "Mod", "Installed", "Latest", "Status"]


def test_rows_in_load_order_have_text_and_stripes():
    rows = model().rows()
    assert [r.folder for r in rows] == ["alpha", "Beta", "gamma", "ghost", "nomod"]
    assert rows[0].cells == ("On", "alpha", "-", "-", "not linked")
    assert rows[1].cells[0] == "Off" and rows[2].cells[0] == "-"
    assert "(not in load order)" in rows[2].cells[1]
    assert "(folder missing)" in rows[3].cells[1] and rows[3].cells[4] == "folder missing"
    assert "(no .mod file!)" in rows[4].cells[1]
    assert [r.tags[-1] for r in rows] == ["even", "odd", "even", "odd", "even"]


def test_status_text_and_colours():
    m = model()
    m.set_rows([row("alpha", UPDATE, "1.0", "1.2"), row("Beta", UP_TO_DATE, "2.0", "2.0"), row("gamma", FAILED),
                row("nomod", UNKNOWN, baseline_unknown=True)])
    by = {r.folder: r for r in m.rows()}
    assert by["alpha"].cells[2:] == ("1.0", "1.2", f"{UPDATE} (1.0 → 1.2)") and by["alpha"].tags[0] == TAG_WARN
    assert by["Beta"].tags[0] == TAG_DIM  # up to date but switched off: greyed, not green
    assert by["gamma"].tags[0] == TAG_BAD and by["ghost"].tags[0] == TAG_BAD
    assert "version unknown" in by["nomod"].cells[4]
    m.apply_row(row("Beta", PROBABLY_OUTDATED, "2.0", "2.5"))
    assert [r for r in m.rows() if r.folder == "Beta"][0].cells[4].startswith(PROBABLY_OUTDATED)


def test_enabled_up_to_date_is_green():
    m = model()
    m.set_rows([row("alpha", UP_TO_DATE, "1", "1")])
    assert m.rows()[0].tags[0] == TAG_OK
    m.apply_row(row("alpha", NOT_LINKED))
    assert m.rows()[0].tags[0] == ""


def test_filter_matches_name_tags_note_fav_pinned_like_the_tui():
    meta = {"alpha": {"tags": ["ui", "hud"], "notes": "Crash on map", "fav": True}, "Beta": {"pinned_version": "2.0"}}
    m = model(meta)

    def shown(q):
        m.filter_text = q
        return folders(m)

    assert shown("") == ["alpha", "Beta", "gamma", "ghost", "nomod"]
    assert shown("ALPHA") == ["alpha"] and shown("hud") == ["alpha"]
    assert shown("crash map") == ["alpha"]  # every word must match, in any order
    assert shown("fav") == ["alpha"] and shown("pinned") == ["Beta"]
    assert shown("zzz") == []
    assert folders(m, keep="gamma") == ["gamma"]  # the selected row survives a filter that hides it


def test_marks_favourites_and_pins_show_in_the_name_cell():
    m = model({"alpha": {"fav": True, "pinned_version": "1.0"}})
    assert m.toggle_mark("alpha") is True
    assert m.rows()[0].cells[1] == "+ * alpha  [pinned 1.0]"
    assert m.toggle_mark("alpha") is False
    m.toggle_mark("Beta")
    m.set_mods([Mod("alpha", ENABLED)])  # a removed mod loses its mark
    assert m.marked == set()


def test_sort_by_header_toggles_direction_and_cycle_matches_the_tui():
    m = model()
    m.set_rows([row("alpha", UPDATE, "1.0", "1.2")])
    m.set_sort("mod")
    assert folders(m) == ["alpha", "Beta", "gamma", "ghost", "nomod"]  # case-insensitive
    m.set_sort("mod")
    assert m.sort_desc and folders(m)[0] == "nomod"
    m.set_sort("status")
    assert not m.sort_desc and m.sort_col == "status"
    m.set_sort(None)
    assert m.sort_col is None and folders(m)[0] == "alpha"
    seen = [m.cycle_sort() for _ in range(6)]
    assert seen == ["mod", "status", "installed", "latest", "en", "mod"]


def test_hint_and_summary():
    m = model({"alpha": {"tags": ["ui"], "notes": "n", "pinned_version": "1"}})
    assert m.hint(None).startswith("No mods installed")
    assert "Not linked to Nexus" in m.hint("Beta") and "Not in mod_load_order" in m.hint("gamma")
    assert m.hint("alpha").endswith("pinned to 1  |  tags: ui  |  note: n")
    assert "folder is missing" in m.hint("ghost")
    m.set_rows([row("alpha", UPDATE, "1", "2")])
    m.toggle_mark("alpha")
    m.filter_text = "alpha"
    assert m.summary() == "5 mod(s), 1 shown, 1 update(s), 1 marked"


def test_drag_steps_count_load_order_positions():
    listed = ["a", "b", "c", "d"]
    assert move_steps(listed, "a", "c") == 2 and move_steps(listed, "d", "b") == -2 and move_steps(listed, "b", "b") == 0
    assert move_steps(listed, "a", "unlisted") == 3  # dropping on an unlisted row moves to the end, like the TUI
    assert move_steps(listed, "unlisted", "a") is None
    assert model().listed() == ["alpha", "Beta", "ghost", "nomod"]


def test_style_strings_map_to_tag_options():
    p = PALETTES["light"]
    assert style_options("bold green", p) == {"bold": True, "foreground": p["green"]}
    assert style_options("bold black on yellow", p) == {"bold": True, "foreground": p["black"], "background": p["yellow"]}
    assert style_options("dim", p) == {"foreground": p["dim"]}
    assert style_options("", p) == {} and style_options("underline sparkle", p) == {}
    assert style_tag("bold red") == "s_bold_red" and style_tag("") == ""


def test_note_parsing_shared_with_the_tui():
    assert actions.parse_note("#UI #hud  needs   dmf") == (["ui", "hud"], "needs dmf")
    assert actions.parse_note("# alone") == ([], "# alone")
    assert actions.parse_note("") == ([], "")


def test_gui_wording_has_no_terminal_key_hints():
    from cogitator.viewtext import gui_words, hint_for
    from cogitator.mods import Mod
    from cogitator.updates import NOT_LINKED, ModRow

    assert "press" not in gui_words("press L to link this mod; Game updated - press T to enable mods.")
    m = Mod(folder="a", state="enabled", present=True, has_mod_file=True)
    row = ModRow(folder="a", nexus_id=None, installed=None, latest=None, status=NOT_LINKED, reason="press L to link this mod to its Nexus page")
    assert "press" not in hint_for(m, row, gui=True) and "Press L" not in hint_for(m, None, gui=True)
    assert "Press L" in hint_for(m, row)  # the terminal UI keeps its own wording
    assert "press I" not in hint_for(None, None, gui=True) and "Press I" in hint_for(None, None)
