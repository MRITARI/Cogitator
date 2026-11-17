import pytest


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """No test may touch real config/cache/keyring/Steam."""
    monkeypatch.setenv("COGITATOR_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("COGITATOR_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("COGITATOR_NEXUS_KEY", raising=False)
    import cogitator.gamefinder as gf

    monkeypatch.setattr(gf, "steam_roots", lambda: [])
    import cogitator.tui.app as tui_app

    monkeypatch.setattr(tui_app, "pick_files_available", lambda: False)  # never open a real file dialog from a test
    # keyring must never be hit by tests
    import sys, types

    fake = types.ModuleType("keyring")
    store = {}
    fake.get_password = lambda a, b: store.get((a, b))
    fake.set_password = lambda a, b, v: store.__setitem__((a, b), v)
    fake.delete_password = lambda a, b: store.pop((a, b), None)
    fake._store = store
    monkeypatch.setitem(sys.modules, "keyring", fake)


@pytest.fixture
def make_steam(tmp_path):
    """Builds a fake Steam root with libraries; returns (root, [game_dirs])."""

    def _make(libs=1, installdir="Warhammer 40,000 DARKTIDE", game_in=(0,), old_format=False, loc="config"):
        root = tmp_path / "Steam"
        (root / "steamapps").mkdir(parents=True, exist_ok=True)
        lib_paths = [root] + [tmp_path / f"Lib{i}" for i in range(1, libs)]
        entries = []
        for i, lp in enumerate(lib_paths):
            (lp / "steamapps" / "common").mkdir(parents=True, exist_ok=True)
            esc = str(lp).replace("\\", "\\\\")
            if old_format:
                entries.append(f'\t"{i}"\t\t"{esc}"\n')
            else:
                entries.append(f'\t"{i}"\n\t{{\n\t\t"path"\t\t"{esc}"\n\t\t"apps"\n\t\t{{\n\t\t}}\n\t}}\n')
        text = '"libraryfolders"\n{\n' + "".join(entries) + "}\n"
        d = root / ("config" if loc == "config" else "steamapps")
        d.mkdir(parents=True, exist_ok=True)
        (d / "libraryfolders.vdf").write_text(text, encoding="utf-8")
        games = []
        for gi in game_in:
            lp = lib_paths[gi]
            (lp / "steamapps" / "appmanifest_1361210.acf").write_text(
                f'"AppState"\n{{\n\t"appid"\t\t"1361210"\n\t"installdir"\t\t"{installdir}"\n}}\n', encoding="utf-8"
            )
            g = lp / "steamapps" / "common" / installdir
            (g / "bundle").mkdir(parents=True, exist_ok=True)
            (g / "binaries").mkdir(parents=True, exist_ok=True)
            games.append(g)
        return root, games

    return _make
