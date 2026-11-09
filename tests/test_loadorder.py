import pytest

from cogitator import loadorder
from cogitator.loadorder import LoadOrder
from cogitator.mods import ModError, UNLISTED, DISABLED, ENABLED, scan_mods, set_mod_enabled, toggle_mod
from cogitator import cli


@pytest.fixture(autouse=True)
def fresh_backup_state():
    loadorder._backed_up.clear()


SAMPLES = [
    b"",
    b"\n",
    b"a\nb\nc\n",
    b"a\nb\nc",  # no trailing newline
    b"a\r\nb\r\n--c\r\n",  # CRLF
    b"a\r\nb\n--c\r\nd",  # mixed endings, no trailing nl
    b"\xef\xbb\xbfa\r\n",  # BOM
    b"-- header\n\n  a  \n\n\n--b\n# note\n--- sep ---\n",
    b"a\n\n\n",  # trailing blank lines
    b"a\rb\r",  # lone CR
    "r\xe9sum\xe9\n".encode("latin-1"),  # invalid utf-8 must survive
]


@pytest.mark.parametrize("data", SAMPLES)
def test_roundtrip_exact(data):
    assert LoadOrder.parse(data).render() == data


def test_classification():
    lo = LoadOrder.parse(b"-- comment\n--- sep\n--\n# hash\n\n--off\non\n  --off2  \n  on2 -- trailing\n")
    assert lo.entries() == [("off", False), ("on", True), ("off2", False), ("on2", True)]


def test_toggle_touches_only_one_line_crlf():
    src = b"-- keep me\r\n\r\nalpha\r\n--beta\r\n  gamma  \r\n\r\n"
    lo = LoadOrder.parse(src)
    assert lo.set_enabled("alpha", False)
    assert lo.set_enabled("beta", True)
    assert lo.set_enabled("gamma", False)
    assert lo.render() == b"-- keep me\r\n\r\n--alpha\r\nbeta\r\n  --gamma  \r\n\r\n"


def test_toggle_noop_and_unlisted():
    lo = LoadOrder.parse(b"a\n")
    assert not lo.set_enabled("a", True)
    assert not lo.set_enabled("zzz", False)
    assert lo.state("zzz") is None and lo.state("a") is True


def test_suffix_preserved():
    lo = LoadOrder.parse(b"a  -- why\n")
    lo.set_enabled("a", False)
    assert lo.render() == b"--a  -- why\n"
    lo.set_enabled("a", True)
    assert lo.render() == b"a  -- why\n"


def test_duplicates_all_toggled():
    lo = LoadOrder.parse(b"a\nb\na\n")
    lo.set_enabled("a", False)
    assert lo.render() == b"--a\nb\n--a\n"


def test_add_matches_eol_and_adds_missing_newline():
    lo = LoadOrder.parse(b"a\r\nb")
    lo.add("c")
    assert lo.render() == b"a\r\nb\r\nc\r\n"
    empty = LoadOrder.parse(b"")
    empty.add("x")
    assert empty.render() == b"x\n"


def test_save_backs_up_once_per_run(tmp_path):
    p = tmp_path / "mod_load_order.txt"
    p.write_bytes(b"a\n")
    lo = LoadOrder.load(p)
    lo.set_enabled("a", False)
    lo.save()
    bak = tmp_path / "mod_load_order.txt.cogitator.bak"
    assert bak.read_bytes() == b"a\n"
    lo.set_enabled("a", True)
    lo.add("b")
    lo.save()
    assert bak.read_bytes() == b"a\n"  # not overwritten by 2nd write
    assert p.read_bytes() == b"a\nb\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_file_loads_empty_and_creates(tmp_path):
    p = tmp_path / "mods" / "mod_load_order.txt"
    lo = LoadOrder.load(p)
    lo.add("x")
    lo.save()
    assert p.read_bytes() == b"x\n"


# ---- mods.py with a fake game dir

@pytest.fixture
def game(tmp_path):
    g = tmp_path / "Warhammer 40,000 DARKTIDE"
    md = g / "mods"
    (g / "bundle").mkdir(parents=True)
    (g / "binaries").mkdir()
    for name, with_mod in [("base", False), ("dmf", False), ("alpha", True), ("beta", True), ("gamma", False), ("delta", True)]:
        (md / name).mkdir(parents=True)
        if with_mod:
            (md / name / f"{name}.mod").write_text("return {}")
    (md / "mod_load_order.txt").write_bytes(b"-- my mods\r\nalpha\r\n--beta\r\nghost\r\n")
    return g


def test_scan(game):
    m = {x.folder: x for x in scan_mods(game)}
    assert set(m) == {"alpha", "beta", "ghost", "gamma", "delta"}  # base/dmf excluded
    assert m["alpha"].state == ENABLED and m["beta"].state == DISABLED
    assert m["gamma"].state == UNLISTED and not m["gamma"].has_mod_file
    assert not m["ghost"].present
    assert [x.folder for x in scan_mods(game)][:3] == ["alpha", "beta", "ghost"]  # load order first


def test_enable_disable_roundtrip_bytes(game):
    p = game / "mods" / "mod_load_order.txt"
    orig = p.read_bytes()
    set_mod_enabled(game, "alpha", False)
    assert p.read_bytes() == b"-- my mods\r\n--alpha\r\n--beta\r\nghost\r\n"
    set_mod_enabled(game, "alpha", True)
    assert p.read_bytes() == orig


def test_enable_unlisted_appends_disable_unlisted_noop(game):
    p = game / "mods" / "mod_load_order.txt"
    assert "not in load order" in set_mod_enabled(game, "delta", False)
    assert b"delta" not in p.read_bytes()
    set_mod_enabled(game, "delta", True)
    assert p.read_bytes().endswith(b"ghost\r\ndelta\r\n")


def test_toggle_and_errors(game):
    assert "disabled" in toggle_mod(game, "alpha")
    assert "enabled" in toggle_mod(game, "alpha")
    with pytest.raises(ModError):
        set_mod_enabled(game, "nope", True)
    with pytest.raises(ModError):
        set_mod_enabled(game, "dmf", True)  # internal
    with pytest.raises(ModError):
        set_mod_enabled(game, "..", True)


def test_cli_mod(game, capsys):
    assert cli.main(["--game-dir", str(game), "mod", "disable", "alpha"]) == 0
    assert cli.main(["--game-dir", str(game), "mod", "enable", "nope"]) == 2
    assert cli.main(["--game-dir", str(game), "status"]) == 0
    assert "disabled  alpha" in capsys.readouterr().out


# ------------------------------------------------------------ moving mods in the load order

def _lo(g):
    return (g / "mods" / "mod_load_order.txt")


def test_move_swaps_neighbours_and_keeps_comments_blank_lines_endings_and_internal_entries(tmp_path):
    from cogitator.mods import move_mod

    (tmp_path / "mods").mkdir()
    for m in ("a", "b", "c"):
        (tmp_path / "mods" / m).mkdir()
    _lo(tmp_path).write_bytes(b"dmf\r\n-- my list\r\na\r\n\r\n--b extra\r\nc")
    move_mod(tmp_path, "a", 1)
    assert _lo(tmp_path).read_bytes() == b"dmf\r\n-- my list\r\n--b extra\r\n\r\na\r\nc"
    move_mod(tmp_path, "a", 1)
    assert _lo(tmp_path).read_bytes() == b"dmf\r\n-- my list\r\n--b extra\r\n\r\nc\r\na"
    move_mod(tmp_path, "a", -1)
    move_mod(tmp_path, "a", -1)
    assert _lo(tmp_path).read_bytes() == b"dmf\r\n-- my list\r\na\r\n\r\n--b extra\r\nc"  # dmf never moved


def test_move_at_the_ends_and_unlisted_mods(tmp_path):
    from cogitator.mods import move_mod

    (tmp_path / "mods" / "a").mkdir(parents=True)
    (tmp_path / "mods" / "z").mkdir()
    _lo(tmp_path).write_bytes(b"dmf\na\n")
    before = _lo(tmp_path).read_bytes()
    assert "already first" in move_mod(tmp_path, "a", -1) and "already last" in move_mod(tmp_path, "a", 1)
    assert _lo(tmp_path).read_bytes() == before
    with pytest.raises(ModError, match="Press Space"):
        move_mod(tmp_path, "z", -1)
    with pytest.raises(ModError):
        move_mod(tmp_path, "dmf", 1)  # the Framework is not movable
