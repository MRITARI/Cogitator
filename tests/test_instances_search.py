
import pytest
import respx

from cogitator import cli, config, nexus
from make_demo_game import build


@pytest.fixture(autouse=True)
def clean_instance(monkeypatch):
    monkeypatch.setenv("COGITATOR_INSTANCE", "")  # main() sets it for the process; monkeypatch puts it back afterwards


# ---------------------------------------------------------------- instances

@pytest.mark.parametrize("name,ok", [("test", True), ("My_Test-2", True), ("", False), ("../x", False), ("a b", False), ("x" * 31, False)])
def test_instance_names_are_validated(monkeypatch, name, ok):
    monkeypatch.setenv("COGITATOR_INSTANCE", name)
    assert (config.instance_name() == name) is ok
    if not ok:
        assert config.config_dir() == config.config_dir().parent / config.config_dir().name  # unchanged default folder


def test_instance_gets_its_own_folders(monkeypatch, tmp_path):
    base_cfg, base_cache = config.config_dir(), config.cache_dir()
    monkeypatch.setenv("COGITATOR_INSTANCE", "lab")
    assert config.config_dir() == base_cfg / "instances" / "lab"
    assert config.cache_dir() == base_cache / "instances" / "lab"
    assert config.log_path().parent == config.cache_dir()


def test_instances_do_not_share_profiles_registry_or_settings(tmp_path, capsys, monkeypatch):
    game = build(tmp_path)
    lab = lambda *a: cli.main(["--instance", "lab", "--game-dir", str(game), *a])  # noqa: E731
    default = lambda *a: cli.main(["--game-dir", str(game), *a])  # noqa: E731
    assert default("profile", "save", "default-setup") == 0
    capsys.readouterr()
    assert lab("profile", "list") == 0
    assert "No profiles yet" in capsys.readouterr().out  # the default setup's profile is not visible here
    assert lab("profile", "save", "lab-setup") == 0
    assert (config.config_dir() / "profiles" / "lab-setup.json").is_file() and "instances" in config.config_dir().parts
    capsys.readouterr()
    lab("profile", "list")
    out = capsys.readouterr().out
    assert "lab-setup" in out and "default-setup" not in out
    monkeypatch.setenv("COGITATOR_INSTANCE", "")
    default("profile", "list")
    out = capsys.readouterr().out
    assert "default-setup" in out and "lab-setup" not in out


def test_instance_equals_form_is_understood(tmp_path):
    game = build(tmp_path)
    assert cli.main(["--instance=eq", "--game-dir", str(game), "profile", "save", "p"]) == 0
    assert "eq" in config.config_dir().parts


# ---------------------------------------------------------------- search

@respx.mock
def test_search_cli(monkeypatch, capsys):
    monkeypatch.setenv("COGITATOR_NEXUS_KEY", "k")
    respx.post(nexus.GRAPHQL).respond(200, json={"data": {"mods": {"nodes": [{"modId": 7, "name": "Cool HUD"}, {"modId": "x", "name": "bad"}]}}})
    assert cli.main(["search", "cool"]) == 0
    out = capsys.readouterr().out
    assert "Cool HUD" in out and "/mods/7" in out and "bad" not in out


@respx.mock
def test_search_cli_without_key_or_results(monkeypatch, capsys):
    monkeypatch.delenv("COGITATOR_NEXUS_KEY", raising=False)
    monkeypatch.setattr("cogitator.cli.get_api_key", lambda: None)
    assert cli.main(["search", "x"]) == 2
    monkeypatch.setattr("cogitator.cli.get_api_key", lambda: "k")
    respx.post(nexus.GRAPHQL).respond(200, json={"data": {"mods": {"nodes": []}}})
    assert cli.main(["search", "zzz"]) == 0 and "No mods found" in capsys.readouterr().out
    respx.post(nexus.GRAPHQL).respond(401)
    assert cli.main(["search", "zzz"]) == 2
