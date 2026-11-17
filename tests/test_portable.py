import sys

from cogitator import config


def test_env_dirs_win_over_portable(monkeypatch, tmp_path):
    monkeypatch.setenv("COGITATOR_PORTABLE", "1")
    assert config.config_dir() == tmp_path / "cfg"
    assert config.cache_dir() == tmp_path / "cache"


def test_portable_env_uses_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("COGITATOR_CONFIG_DIR")
    monkeypatch.delenv("COGITATOR_CACHE_DIR")
    monkeypatch.setenv("COGITATOR_PORTABLE", "1")
    monkeypatch.chdir(tmp_path)
    assert config.is_portable()
    assert config.config_dir() == tmp_path / "data" / "config"
    assert config.cache_dir() == tmp_path / "data" / "cache"


def test_portable_marker_beside_frozen_exe(monkeypatch, tmp_path):
    monkeypatch.delenv("COGITATOR_CONFIG_DIR")
    monkeypatch.delenv("COGITATOR_CACHE_DIR")
    exe = tmp_path / "cogitator.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert not config.is_portable()
    (tmp_path / "portable.txt").write_text("")
    assert config.config_dir() == tmp_path / "data" / "config"


def test_portable_key_never_touches_keyring(monkeypatch, tmp_path):
    monkeypatch.setenv("COGITATOR_PORTABLE", "1")
    monkeypatch.delenv("COGITATOR_NEXUS_KEY", raising=False)
    assert config.set_api_key("abc123") == "file"
    assert config.get_api_key() == "abc123"
    import keyring

    assert not keyring._store
