"""Data-format versions: a newer app upgrades older files; an older app refuses newer ones and never rewrites them."""
import json

import pytest

from cogitator import cli
from cogitator.config import SchemaTooNew, Settings, config_dir
from cogitator.registry import SCHEMA, Registry, check_data_versions, migrate


def test_a_v3_registry_is_upgraded_and_keeps_everything_it_had():
    v3 = {"schema": 3, "frameworks": {"dml": {"version": "26.06.24"}}, "mods": {"a": {"nexus_id": 1}}, "updates": {"premium": False},
          "manifests": {"dml": {"files": {"x": "y"}}}, "patch": {"last_patch_at": 5}, "future_unknown_key": [1, 2]}
    out = migrate(v3)
    assert out["schema"] == SCHEMA == 4 and out["selfupdate"] == {}
    for k in ("frameworks", "mods", "updates", "manifests", "patch", "future_unknown_key"):
        assert out[k] == v3[k]


def test_an_old_registry_on_disk_loads_and_is_written_back_at_the_current_version(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({"schema": 1, "frameworks": {"dml": {"version": "1"}}, "mods": {}}), encoding="utf-8")
    r = Registry.load(p)
    assert r.framework("dml")["version"] == "1" and r.data["schema"] == SCHEMA
    r.save()
    assert json.loads(p.read_text(encoding="utf-8"))["schema"] == SCHEMA


def test_a_registry_from_a_newer_app_is_refused_and_left_untouched(tmp_path):
    p = tmp_path / "registry.json"
    body = json.dumps({"schema": SCHEMA + 1, "frameworks": {}, "brand_new": {"x": 1}})
    p.write_text(body, encoding="utf-8")
    with pytest.raises(SchemaTooNew) as e:
        Registry.load(p)
    msg = str(e.value)
    assert "newer Cogitator" in msg and str(SCHEMA + 1) in msg and "Nothing was changed" in msg
    assert p.read_text(encoding="utf-8") == body  # not rewritten, not moved aside as 'corrupt'
    assert not p.with_name("registry.json.corrupt").exists()


def test_a_corrupt_registry_is_still_set_aside_not_mistaken_for_a_newer_one(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text("{not json", encoding="utf-8")
    assert Registry.load(p).data["schema"] == SCHEMA and p.with_name("registry.json.corrupt").exists()


def test_settings_without_a_version_are_treated_as_version_one_and_saved_with_one():
    Settings.path().parent.mkdir(parents=True, exist_ok=True)
    Settings.path().write_text('game_dir = "X"\n', encoding="utf-8")
    s = Settings.load()
    assert s.game_dir == "X" and s.schema == 1
    s.save()
    assert "schema = 1" in Settings.path().read_text(encoding="utf-8")


def test_settings_from_a_newer_app_are_refused_and_left_untouched():
    Settings.path().parent.mkdir(parents=True, exist_ok=True)
    body = 'schema = 99\ngame_dir = "X"\nsomething_new = true\n'
    Settings.path().write_text(body, encoding="utf-8")
    with pytest.raises(SchemaTooNew) as e:
        Settings.load()
    assert "settings" in str(e.value).lower() and "99" in str(e.value)
    assert Settings.path().read_text(encoding="utf-8") == body


def test_update_settings_have_safe_defaults_and_bad_values_fall_back():
    s = Settings()
    assert (s.update_mode, s.update_channel, s.update_check_hours) == ("download", "stable", 24)
    Settings.path().parent.mkdir(parents=True, exist_ok=True)
    Settings.path().write_text('update_mode = "yolo"\nupdate_channel = "nightly"\nupdate_check_hours = 0\n', encoding="utf-8")
    t = Settings.load()
    assert (t.update_mode, t.update_channel, t.update_check_hours) == ("download", "stable", 24)
    Settings.path().write_text('update_mode = "auto"\nupdate_channel = "beta"\nupdate_check_hours = 6\n', encoding="utf-8")
    t = Settings.load()
    assert (t.update_mode, t.update_channel, t.update_check_hours) == ("auto", "beta", 6)


def test_the_preflight_checks_both_files():
    check_data_versions()  # nothing there yet: fine
    Registry.load().save()
    Settings().save()
    check_data_versions()
    p = config_dir() / "registry.json"
    p.write_text(json.dumps({"schema": 999}), encoding="utf-8")
    with pytest.raises(SchemaTooNew):
        check_data_versions()


def test_every_entry_point_refuses_cleanly_instead_of_crashing(capsys):
    (config_dir()).mkdir(parents=True, exist_ok=True)
    (config_dir() / "registry.json").write_text(json.dumps({"schema": 999}), encoding="utf-8")
    assert cli.main(["status"]) == 2
    err = capsys.readouterr().err
    assert "newer Cogitator" in err and "Traceback" not in err
    from cogitator.tui.app import run_tui
    assert run_tui() == 2 and "newer Cogitator" in capsys.readouterr().err
    from cogitator_setup.app import main as setup_main
    shown = []
    assert setup_main([], build=22000, show_error=lambda t, b: shown.append(b)) == 2 and "newer Cogitator" in shown[0]
