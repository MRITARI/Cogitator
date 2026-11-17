import importlib.metadata as md
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("make_notices", Path(__file__).parents[1] / "packaging" / "make_notices.py")
notices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notices)


def test_output_names_every_declared_dependency_and_has_no_missing_marker():
    text = notices.generate()
    low = text.lower().replace("_", "-")
    for name in notices.declared_dependencies():
        assert notices.norm(name) in low, f"{name} is not in THIRD-PARTY-NOTICES.txt"
    assert "MISSING" not in text
    assert "Bootloader Exception" in text and "unlimited permission" in text  # quoted from PyInstaller's own COPYING.txt
    assert "Mozilla Public License" in text and "https://github.com/certifi/python-certifi" in text and "unmodified" in text
    assert "Python " in text and "Permission is hereby granted" in text  # a runtime licence and an MIT text


def test_a_package_without_a_licence_text_fails_loudly_and_names_it(monkeypatch):
    real = notices._text_files
    monkeypatch.setattr(notices, "_text_files", lambda d: [] if notices.norm(d.metadata["Name"]) == "vdf" else real(d))
    with pytest.raises(SystemExit) as e:
        notices.generate()
    assert "vdf" in str(e.value) and "NOT written" in str(e.value)


def test_main_writes_nothing_when_something_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(notices, "_text_files", lambda d: [])
    with pytest.raises(SystemExit):
        notices.main(["--out", str(tmp_path / "n.txt")])
    assert not (tmp_path / "n.txt").exists()


def test_modified_ca_file_is_not_called_unmodified(monkeypatch, tmp_path):
    bad = tmp_path / "cacert.pem"
    bad.write_bytes(b"not the real file")
    with pytest.raises(SystemExit, match="modified"):
        notices.certifi_unmodified(bad)


def test_bundled_distributions_come_from_the_pyz_table(tmp_path):
    toc = tmp_path / "PYZ-00.toc"
    toc.write_text(repr(("PYZ", [("rich.text", "x", "PYMODULE"), ("setuptools", "x", "PYMODULE")])), encoding="utf-8")
    assert {"rich", "setuptools"} <= notices.bundled_from_pyz([toc])
    assert md.version("rich")
