"""Static checks over cogitator_web/ui: no unsafe DOM sinks, no remote URLs, a strict CSP, every local reference exists, and the
theme's text/background pairs keep 4.5:1 contrast. Needs neither a window nor `webview`."""
import re
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "cogitator_web" / "ui"
ASSETS = [p for p in UI.rglob("*") if p.is_file()]
TEXT = [p for p in ASSETS if p.suffix in (".html", ".css", ".js")]
JS = [p for p in TEXT if p.suffix == ".js"]


def rel(p: Path) -> str:
    return str(p.relative_to(UI)).replace("\\", "/")


def test_there_are_assets():
    names = {rel(p) for p in ASSETS}
    assert {"index.html", "theme.css", "app.css", "js/main.js", "js/pages/mods.js", "js/pages/settings.js", "js/icons.js"} <= names


def test_no_binary_assets():
    binary = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".otf")
    assert sorted(rel(p) for p in ASSETS if p.suffix.lower() in binary) == ["img/cogitator-bg.svg", "img/logo.png"]
    assert (UI / "img" / "logo.png").stat().st_size < 60 * 1024
    assert (UI / "img" / "cogitator-bg.svg").stat().st_size < 60 * 1024


def test_background_svg_is_inert():
    svg = (UI / "img" / "cogitator-bg.svg").read_text(encoding="utf-8")
    low = svg.lower()
    assert "<script" not in low and "<foreignobject" not in low and "<image" not in low and "<use" not in low
    assert not re.search(r"on\w+\s*=", low), "event handler attribute"
    assert not re.search(r"(xlink:)?href\s*=", low)
    assert not re.search(r"url\(\s*[\"']?(?!#)", low), "url() may only point at in-file ids"
    assert not re.search(r"(?:https?:)?//", svg.replace('xmlns="http://www.w3.org/2000/svg"', "")), "external URL"
    assert "@import" not in low and "javascript:" not in low


def test_local_image_references_exist():
    pat = re.compile(r"""\bsrc\s*[=:]\s*["']([^"']+)["']""")
    for p in TEXT:
        if p.suffix == ".css":
            continue
        for ref in pat.findall(p.read_text(encoding="utf-8")):
            if not ref.endswith((".png", ".ico", ".svg", ".jpg")):
                continue
            assert (UI / ref).is_file(), f"{rel(p)} -> {ref}"


FORBIDDEN = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setAttribute(\"style\"", "javascript:",
             "srcdoc", "importScripts"]


@pytest.mark.parametrize("path", TEXT, ids=rel)
def test_no_unsafe_sinks(path):
    text = path.read_text(encoding="utf-8")
    for bad in FORBIDDEN:
        assert bad not in text, f"{rel(path)} contains {bad}"
    assert not re.search(r"\bFunction\s*\(", text)
    assert not re.search(r"set(Timeout|Interval)\(\s*[\"'`]", text), "string timers are eval in disguise"


@pytest.mark.parametrize("path", TEXT, ids=rel)
def test_no_remote_urls(path):
    assert not re.search(r"https?://|//cdn\.|@import", path.read_text(encoding="utf-8")), rel(path)


@pytest.mark.parametrize("page", ["index.html", "setup.html", "uninstall.html"])
def test_csp_meta_is_strict(page):
    html = (UI / page).read_text(encoding="utf-8")
    m = re.search(r'<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"', html)
    assert m, "no CSP meta tag"
    csp = {d.split()[0]: d.split()[1:] for d in m.group(1).split(";") if d.strip()}
    assert csp["default-src"] == ["'self'"]
    assert csp["script-src"] == ["'self'"] and csp["object-src"] == ["'none'"] and csp["base-uri"] == ["'none'"]
    assert "'unsafe-inline'" not in m.group(1) and "'unsafe-eval'" not in m.group(1)
    assert not re.search(r"https?:|\*", m.group(1))


@pytest.mark.parametrize("page", ["index.html", "setup.html", "uninstall.html"])
def test_html_has_no_inline_script_style_or_handlers(page):
    html = (UI / page).read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"<style", html) and not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+\s*=", html)


def test_local_references_exist():
    html = (UI / "index.html").read_text(encoding="utf-8")
    for ref in re.findall(r'(?:href|src)="([^"]+)"', html):
        assert not re.match(r"[a-z]+:|//", ref), ref
        assert (UI / ref).is_file(), ref
    for p in JS:
        for ref in re.findall(r"""(?:from|import)\s*["']([^"']+)["']""", p.read_text(encoding="utf-8")):
            assert ref.startswith("./") or ref.startswith("../"), f"{rel(p)} imports {ref}"
            assert (p.parent / ref).resolve().is_file(), f"{rel(p)} imports missing {ref}"
    assert "url(" not in (UI / "theme.css").read_text(encoding="utf-8")
    refs = re.findall(r"url\(([^)]*)\)", (UI / "app.css").read_text(encoding="utf-8"))
    assert refs == ['"img/cogitator-bg.svg"'], refs  # the one local background; no other, no remote, no data: in the stylesheet


def test_every_js_module_is_reachable_from_main():
    seen, todo = set(), [UI / "js" / "main.js", UI / "js" / "setup" / "main.js", UI / "js" / "uninstall" / "main.js"]
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        for ref in re.findall(r"""(?:from|import)\s*["']([^"']+)["']""", p.read_text(encoding="utf-8")):
            todo.append((p.parent / ref).resolve())
    assert {p.resolve() for p in JS} <= {p.resolve() for p in seen}


# ---------------------------------------------------------------- theme
def _block(css: str, selector: str) -> dict[str, str]:
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert m, selector
    return dict(re.findall(r"--([\w-]+):\s*([^;/]+?)\s*(?:/\*.*?\*/)?\s*;", m.group(1), re.S))


def _themes():
    css = (UI / "theme.css").read_text(encoding="utf-8")
    dark = _block(css, ":root")
    light = _block(css, ':root[data-theme="light"]')
    system_light = _block(css, ":root:not([data-theme])")
    return css, dark, light, system_light


def _lum(h: str) -> float:
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_COLOURS = ("fg", "muted", "ok-text", "attention-text", "destructive-text")
SURFACES = ("bg", "sidebar", "card", "tile", "secondary", "input")


def test_the_spec_colours_are_there():
    _, d, _, _ = _themes()
    for k, v in {"bg": "#0a0a0a", "card": "#171717", "tile": "#1e1e1e", "input": "#282828", "secondary": "#262626", "border": "#2f2f2f",
                 "primary": "#e5e5e5", "fg": "#fafafa", "muted": "#a3a3a3", "destructive": "#ef4444", "ok": "#4ade80", "attention": "#fbbf24",
                 "neutral": "#71717a", "accent": "#b8892e"}.items():
        assert d[k].lower() == v, k
    assert (d["radius-pill"], d["radius-card"], d["radius-tile"]) == ("999px", "24px", "16px")


@pytest.mark.parametrize("which", ["dark", "light"])
def test_text_contrast_on_every_surface(which):
    _, dark, light, _ = _themes()
    t = dark if which == "dark" else light
    for fg in TEXT_COLOURS:
        for bg in SURFACES:
            assert ratio(t[fg], t[bg]) >= 4.5, f"{which}: {fg} {t[fg]} on {bg} {t[bg]} = {ratio(t[fg], t[bg]):.2f}"
    assert ratio(t["primary-fg"], t["primary"]) >= 4.5  # the primary button
    assert ratio(t["accent-fg"], t["accent"]) >= 4.5  # the accent (Launch) button
    for bg in SURFACES:
        assert ratio(t["ring"], t[bg]) >= 3, f"{which}: focus ring on {bg}"
    assert ratio(t["knob-on"], t["primary"]) >= 3  # switch knob on its track (not text)


def test_light_variants_are_identical_and_complete():
    _, dark, light, system_light = _themes()
    assert {k: v for k, v in light.items() if k != "color-scheme"} == {k: v for k, v in system_light.items() if k != "color-scheme"}
    colour_keys = {k for k, v in dark.items() if v.startswith("#") or v.startswith("rgba")}
    assert colour_keys <= set(light), colour_keys - set(light)


def test_css_uses_only_defined_variables():
    css, dark, _, _ = _themes()
    app = (UI / "app.css").read_text(encoding="utf-8") + (UI / "setup.css").read_text(encoding="utf-8") + (UI / "uninstall.css").read_text(encoding="utf-8")
    used = set(re.findall(r"var\(--([\w-]+)", app))
    assert used <= set(dark), used - set(dark)
    assert "prefers-reduced-motion" in app and "prefers-color-scheme" in css


def test_accessibility_hooks_are_in_the_scripts():
    js = {rel(p): p.read_text(encoding="utf-8") for p in JS}
    assert 'role: "switch"' in js["js/components/widgets.js"] and "aria-checked" in js["js/components/widgets.js"]
    assert "aria-current" in js["js/components/shell.js"]
    assert 'aria-label": "Mods"' in js["js/pages/mods.js"] and 'scope: "col"' in js["js/pages/mods.js"] and "aria-sort" in js["js/pages/mods.js"]
    assert 'role: "dialog"' in js["js/components/modal.js"] and "Escape" in js["js/components/modal.js"]


def test_filter_chips_are_pressable_buttons():
    js = (UI / "js" / "pages" / "mods.js").read_text(encoding="utf-8")
    assert "aria-pressed" in js and all(w in js for w in ('"Updates"', '"Not linked"', '"Favourites"', '"Enabled"'))


def test_toasts_are_a_polite_live_region_and_wired():
    toast = (UI / "js" / "components" / "toast.js").read_text(encoding="utf-8")
    assert '"aria-live": "polite"' in toast and "4000" in toast
    main = (UI / "js" / "main.js").read_text(encoding="utf-8")
    assert "components/toast.js" in main and 'on("log"' in main
    shell = (UI / "js" / "components" / "shell.js").read_text(encoding="utf-8")
    assert "let collapsed = true" in shell and "dot-bad" in shell


def _rgba(v: str):
    nums = [float(x) for x in re.findall(r"[\d.]+", v)]
    return nums if v.startswith("rgba") else [*[int(v[i:i + 2], 16) for i in (1, 3, 5)], 1.0]


def _over(top, under):
    r, g, b, a = top
    return "#" + "".join(f"{round(c * a + u * (1 - a)):02x}" for c, u in zip((r, g, b), under))


def test_text_contrast_on_translucent_surfaces_over_the_worst_custom_image():
    _, d, light, _ = _themes()
    worst = _over(_rgba(d["bg-shade"]), (255, 255, 255))  # a pure white photo under the dark shade
    assert _rgba(d["bg-shade"])[3] >= 0.8
    rgb = lambda h: tuple(int(worst[i:i + 2], 16) for i in (1, 3, 5))  # noqa: E731
    assert ratio(d["muted"], worst) >= 4.5 and ratio(d["fg"], worst) >= 4.5  # text sitting straight on the layer
    for glass in ("sidebar-glass", "card-glass", "menu-glass"):
        surface = _over(_rgba(d[glass]), rgb(worst))
        for fg in TEXT_COLOURS:
            assert ratio(d[fg], surface) >= 4.5, f"{fg} on {glass} over the worst background = {ratio(d[fg], surface):.2f}"
    for k in ("sidebar-glass", "card-glass", "menu-glass"):  # the light theme never shows a layer: its glass is the opaque surface
        assert light[k] == light[k.replace("-glass", "").replace("menu", "card")]


def test_background_is_wired():
    js = {rel(p): p.read_text(encoding="utf-8") for p in JS}
    assert "style.backgroundImage" in js["js/background.js"] and "get_background" in js["js/background.js"]
    assert "applyBackground" in js["js/main.js"] and "pick_background" in js["js/pages/settings.js"] and "reset_background" in js["js/pages/settings.js"]
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert 'id="bg"' in html
    css = (UI / "app.css").read_text(encoding="utf-8")
    assert 'img/cogitator-bg.svg' in css and "blur(" in css and "--bg-shade" in css
