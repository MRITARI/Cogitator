"""The PATH logic in packaging/cogitator.iss ([Code]: NormEntry / PathHasEntry / PathWithout / AddToPath), ported line by line.
Inno code cannot run outside an installer, so this checks the algorithm; the .iss itself is compiled by packaging/release.py."""
import pytest

D = r"C:\Users\u\AppData\Local\Programs\Cogitator\app"


def norm(s):
    s = s.strip()
    while len(s) > 1 and s[-1] == "\\":
        s = s[:-1]
    return s.upper()


def items(paths):
    while paths:
        i = paths.find(";")
        item, paths = (paths, "") if i < 0 else (paths[:i], paths[i + 1:])
        yield item


def has(paths, d):
    return any(norm(i) == norm(d) for i in items(paths))


def without(paths, d):
    return ";".join(i for i in items(paths) if i and norm(i) != norm(d))


def add(paths, d):
    if has(paths, d):
        return paths
    if paths and paths[-1] != ";":
        paths += ";"
    return paths + d


@pytest.mark.parametrize("before", ["", r"C:\a;%USERPROFILE%\bin", r"C:\a;" + D + r";C:\b", D + r";C:\a", r"C:\a;" + D])
def test_add_then_remove_restores_the_original(before):
    once = add(before, D)
    assert has(once, D) and add(once, D) == once  # reinstall / upgrade adds nothing
    assert without(once, D) == without(before, D)
    assert not has(without(once, D), D)


def test_only_our_entry_and_empty():
    assert add("", D) == D and without(D, D) == "" and without("", D) == ""


def test_matches_whole_entries_ignoring_case_and_trailing_backslash():
    assert has(r"C:\x;" + D.lower() + "\\", D)
    assert not has(D + "2", D) and not has(D + r"\bin", D) and not has(r"C:\Foo\Cogitator2", r"C:\Foo\Cogitator")
    assert without(r"C:\Foo\Cogitator2;" + D + r"\bin;" + D, D) == r"C:\Foo\Cogitator2;" + D + r"\bin"


def test_other_entries_are_kept_verbatim():
    p = r"%USERPROFILE%\bin;C:\Program Files\X;" + D + r";%LOCALAPPDATA%\Y"
    assert without(p, D) == r"%USERPROFILE%\bin;C:\Program Files\X;%LOCALAPPDATA%\Y"
    assert add(r"C:\a;", D) == r"C:\a;" + D
