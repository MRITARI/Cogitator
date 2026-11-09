import sys
import uuid

import pytest

from cogitator import cli, userpath

D = r"C:\Users\u\AppData\Local\Programs\Cogitator\app"
BAD = r"C:\Users\u\AppData\Local\Programs\Cogitatorpp"
SZ, EXP = 1, 2


@pytest.fixture
def reg(monkeypatch):
    """In-memory stand-in for HKCU\\Environment\\Path: {'v': value-or-None, 'k': type}. Counts broadcasts."""
    st = {"v": None, "k": None, "bc": 0}
    monkeypatch.setattr(userpath, "read_path", lambda key_path=userpath.ENV_KEY: (st["v"], st["k"]))

    def write(value, kind, key_path):
        st["v"], st["k"] = value, kind

    monkeypatch.setattr(userpath, "_write_path", write)
    monkeypatch.setattr(userpath, "broadcast_environment_change", lambda: st.__setitem__("bc", st["bc"] + 1))
    return st


def put(st, value, kind=EXP):
    st["v"], st["k"] = value, kind


@pytest.mark.parametrize("before,after", [
    (None, D),
    ("", D),
    (";;", D),
    (r"C:\a;%USERPROFILE%\bin", r"C:\a;%USERPROFILE%\bin;" + D),
    (r"C:\a;", r"C:\a;" + D),
    (r"C:\Foo\Cogitator2;" + D + "2", r"C:\Foo\Cogitator2;" + D + "2;" + D),
])
def test_add_appends_once(reg, before, after):
    put(reg, before, EXP if before is not None else None)
    assert userpath.add_entry(D) is True
    assert reg["v"] == after and reg["k"] == EXP and reg["bc"] == 1
    assert userpath.add_entry(D) is False and reg["v"] == after and reg["bc"] == 1  # idempotent, no second broadcast


@pytest.mark.parametrize("before", [D, D + ";C:\\a", r"C:\a;" + D + r";C:\b", r"C:\a;" + D, D.upper() + "\\;" + D])
def test_add_when_present_changes_nothing(reg, before):
    put(reg, before)
    assert userpath.add_entry(D) is False and reg["v"] == before and reg["bc"] == 0


def test_add_heals_the_bad_entry_and_keeps_the_rest(reg):
    put(reg, r"C:\a;" + BAD + ";%USERPROFILE%\\bin;")
    assert userpath.add_entry(D) is True
    assert reg["v"] == r"C:\a;%USERPROFILE%\bin;" + D
    put(reg, BAD + ";" + D)
    assert userpath.add_entry(D) is True and reg["v"] == D
    put(reg, BAD.replace("Cogitatorpp", "Cogitator\x07pp") + r";C:\a")  # what 0.1.6 really wrote
    assert userpath.add_entry(D) is True and reg["v"] == r"C:\a;" + D


def test_add_keeps_reg_sz_type_and_percent_entries(reg):
    put(reg, r"%USERPROFILE%\bin", SZ)
    userpath.add_entry(D)
    assert reg["k"] == SZ and reg["v"].startswith(r"%USERPROFILE%\bin;")


@pytest.mark.parametrize("before,after", [
    (D, ""),
    (D + ";", ""),
    (D + r";C:\a;C:\b", r"C:\a;C:\b"),
    (r"C:\a;" + D + r";C:\b", r"C:\a;C:\b"),
    (r"C:\a;C:\b;" + D, r"C:\a;C:\b"),
    (D + ";" + D.lower() + "\\;C:\\a;" + D, r"C:\a"),  # duplicates all go
    (r"C:\Foo\Cogitator2;" + D + "2;" + D + r"\bin;" + D, r"C:\Foo\Cogitator2;" + D + r"2;" + D + r"\bin"),
    (r"%USERPROFILE%\bin;" + D + r";C:\a;", r"%USERPROFILE%\bin;C:\a"),
])
def test_remove_deletes_exactly_our_entries(reg, before, after):
    put(reg, before)
    assert userpath.remove_entry(D) is True and reg["v"] == after and reg["k"] == EXP and reg["bc"] == 1


@pytest.mark.parametrize("before", [None, "", r"C:\a;C:\b", D + "2", r"C:\Foo\Cogitator2;" + D + r"\bin", BAD])
def test_remove_with_nothing_to_remove_is_quiet(reg, before):
    put(reg, before, EXP if before is not None else None)
    assert userpath.remove_entry(D) is False and reg["v"] == before and reg["bc"] == 0


def test_local_appdata_spelling_equals_the_expanded_folder(reg, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    put(reg, r"%LOCALAPPDATA%\Programs\Cogitator\app;C:\a")
    assert userpath.add_entry(D) is False
    assert userpath.remove_entry(D) is True and reg["v"] == r"C:\a"


def test_very_long_path_survives(reg):
    long = ";".join(rf"C:\dir{i:04d}\sub" for i in range(300))
    assert len(long) > 2048
    put(reg, long)
    userpath.add_entry(D)
    assert reg["v"] == long + ";" + D
    userpath.remove_entry(D)
    assert reg["v"] == long


def test_cli_path_commands(reg, capsys):
    assert cli.main(["path", "status", "--dir", D]) == 1 and "not in" in capsys.readouterr().out
    assert cli.main(["path", "add", "--dir", D]) == 0 and reg["v"] == D
    assert cli.main(["path", "status", "--dir", D]) == 0 and "present in" in capsys.readouterr().out
    assert cli.main(["path", "remove", "--dir", D]) == 0 and reg["v"] == ""
    assert cli.main(["path", "remove", "--dir", D]) == 0  # nothing left: still succeeds
    assert cli.main(["path", "add"]) == 2  # not frozen and no --dir


@pytest.mark.skipif(sys.platform != "win32", reason="winreg")
def test_real_registry_preserves_type_and_percent_entries():
    import winreg

    key = rf"Software\CogitatorTest-{uuid.uuid4().hex}"
    calls = []
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, r"%USERPROFILE%\bin;C:\a")
        assert userpath.add_entry(D, key, lambda: calls.append(1)) is True
        assert userpath.read_path(key) == (r"%USERPROFILE%\bin;C:\a;" + D, winreg.REG_EXPAND_SZ)
        assert userpath.has_entry(D, key)
        assert userpath.remove_entry(D, key, lambda: calls.append(1)) is True
        assert userpath.read_path(key) == (r"%USERPROFILE%\bin;C:\a", winreg.REG_EXPAND_SZ)
        assert len(calls) == 2
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, "Path")
        assert userpath.remove_entry(D, key, lambda: calls.append(1)) is False  # missing Path
        assert userpath.add_entry(D, key, lambda: calls.append(1)) is True  # created as REG_EXPAND_SZ
        assert userpath.read_path(key) == (D, winreg.REG_EXPAND_SZ)
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass
