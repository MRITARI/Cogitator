"""Theme choice, fonts, glyph table and palette readability. Widget-free: no display needed."""
import pytest

from cogitator_gui import theme
from cogitator_gui.viewmodel import PALETTES, badgify, style_options


def test_normalize_choice_defaults_to_system():
    assert [theme.normalize_choice(v) for v in ("system", "light", "dark")] == ["system", "light", "dark"]
    assert theme.normalize_choice("tokyo-night") == "system" and theme.normalize_choice(None) == "system"


def test_resolve_theme_system_light_dark():
    assert theme.resolve_theme("light", lambda: False) == "light"
    assert theme.resolve_theme("dark", lambda: True) == "dark"
    assert theme.resolve_theme("system", lambda: True) == "light"
    assert theme.resolve_theme("system", lambda: False) == "dark"
    assert theme.resolve_theme("junk", lambda: False) == "dark"  # junk = system


def test_system_theme_reads_the_registry_value_and_falls_back_to_light():
    assert theme.system_uses_light(lambda: 1) is True and theme.system_uses_light(lambda: 0) is False

    def boom():
        raise OSError("no such key")

    assert theme.system_uses_light(boom) is True
    assert theme.system_uses_light(lambda: "x") is True  # not a number


def test_system_theme_off_windows_is_light(monkeypatch):
    monkeypatch.setattr(theme.sys, "platform", "linux")
    assert theme.system_uses_light() is True


def test_titlebar_call_does_nothing_off_windows(monkeypatch):
    monkeypatch.setattr(theme.sys, "platform", "linux")
    assert theme.set_titlebar_dark(object(), True) is False and theme.is_dark() is True
    theme.set_titlebar_dark(object(), False)


def test_fonts_prefer_the_modern_ones_and_fall_back():
    assert theme.ui_family(["Arial", "Segoe UI", "Segoe UI Variable"]) == "Segoe UI Variable"
    assert theme.ui_family(["Arial"]) == "Segoe UI"
    assert theme.mono_family(["Consolas", "Cascadia Mono"]) == "Cascadia Mono" and theme.mono_family([]) == "Consolas"
    assert theme.icon_family(["Segoe MDL2 Assets"]) == "Segoe MDL2 Assets" and theme.icon_family(["Arial"]) is None
    assert theme.icon_family(["Segoe MDL2 Assets", "Segoe Fluent Icons"]) == "Segoe Fluent Icons"
    assert theme.heading_family(["Segoe UI Semibold"]) == "Segoe UI Semibold" and theme.heading_family([]) is None


def test_scaling_and_button_text():
    assert theme.scaled(32, 96) == 32 and theme.scaled(32, 144) == 48
    assert theme.button_text("launch", "Launch game", False) == "Launch game"
    assert theme.button_text("launch", "Launch game", True).endswith("Launch game") and theme.button_text("launch", "Launch game", True) != "Launch game"
    assert set(theme.ACCENT_BUTTONS) <= set(theme.TOOLBAR_ICONS)


def _lum(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("name", ["light", "dark"])
def test_palette_text_is_readable_on_rows_and_badges(name):
    p = PALETTES[name]
    for bg in (p["bg"], p["stripe"]):
        for key in ("fg", "dim", "green", "red", "yellow"):
            assert contrast(p[key], bg) >= 4.5, (name, key, bg)
    for kind in ("ok", "warn", "bad"):
        assert contrast(p[kind + "_fg"], p[kind + "_bg"]) >= 4.5, (name, kind)
    assert p["stripe"] != p["bg"]


def test_palettes_match_the_sun_valley_backgrounds():
    assert PALETTES["light"]["bg"] == "#fafafa" and PALETTES["dark"]["bg"] == "#1c1c1c"


def test_badges_keep_the_text_and_use_palette_colours():
    segs = [("DML ", "bold"), ("missing", "bold red"), ("   ", ""), (" MODS: UNKNOWN ", "bold black on yellow"), ("key ok (free)", "green")]
    out = badgify(segs)
    assert "".join(t for t, _ in out).replace(" ", "") == "".join(t for t, _ in segs).replace(" ", "")
    assert [s for _, s in out] == ["bold", "badge_bad", "", "badge_warn", "badge_ok"]
    o = style_options("badge_warn", PALETTES["dark"])
    assert o["foreground"] == PALETTES["dark"]["warn_fg"] and o["background"] == PALETTES["dark"]["warn_bg"]


# ---------------------------------------------------------------- the console window behind the GUI
def test_own_console_is_hidden_but_a_terminal_is_left_alone():
    from cogitator_gui.app import hide_own_console

    calls = []
    assert hide_own_console(platform="win32", process_count=lambda: 1, hide=lambda: calls.append(1)) is True and calls == [1]
    assert hide_own_console(platform="win32", process_count=lambda: 2, hide=lambda: calls.append(2)) is False  # started from a terminal
    assert hide_own_console(platform="win32", process_count=lambda: 0, hide=lambda: calls.append(3)) is False  # no console
    assert hide_own_console(platform="linux", process_count=lambda: 1, hide=lambda: calls.append(4)) is False
    assert calls == [1]

    def boom():
        raise OSError

    assert hide_own_console(platform="win32", process_count=boom) is False
