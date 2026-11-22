import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("make_notices", Path(__file__).parents[1] / "packaging" / "make_notices.py")
notices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notices)


@pytest.mark.skipif(importlib.util.find_spec("PyInstaller") is None, reason="needs the release build environment (PyInstaller installed)")
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


def _toc(tmp_path, *mods):
    toc = tmp_path / "PYZ-00.toc"
    toc.write_text(repr(("PYZ", [(m, "x", "PYMODULE") for m in mods])), encoding="utf-8")
    return toc


def test_bundled_distributions_come_from_the_pyz_table(tmp_path, monkeypatch):
    monkeypatch.setattr(notices.md, "packages_distributions", lambda: {"rich": ["rich"], "setuptools": ["setuptools"]})
    monkeypatch.setattr(notices.md, "distributions", lambda: [])
    assert notices.bundled_from_pyz([_toc(tmp_path, "rich.text", "setuptools")]) == {"rich", "setuptools"}


def test_distribution_without_top_level_txt_is_found_from_its_record(tmp_path, monkeypatch):
    class Fake:  # a wheel like rich: no top_level.txt; Python 3.10's packages_distributions() does not list it
        metadata = {"Name": "Fancy-Pkg"}
        files = ["fancy/__init__.py", "fancy/x.py", "single.py", "fancy_pkg-1.dist-info/RECORD", "__pycache__/a.pyc"]

        def read_text(self, name):
            return None

    monkeypatch.setattr(notices.md, "packages_distributions", lambda: {})
    monkeypatch.setattr(notices.md, "distributions", lambda: [Fake()])
    assert notices.bundled_from_pyz([_toc(tmp_path, "fancy.x", "single")]) == {"fancy-pkg"}


class _FakeDist:
    metadata = {"Name": "Foo_Bar", "License-Expression": "MIT"}
    version = "1.2"


def _fake_build(monkeypatch, texts):
    monkeypatch.setattr(notices, "declared_dependencies", lambda: ["foo-bar"])
    monkeypatch.setattr(notices, "runtime_closure", lambda roots: ({"foo-bar": _FakeDist()}, []))
    monkeypatch.setattr(notices, "_text_files", lambda d: texts)
    monkeypatch.setattr(notices, "python_runtime", lambda: ("Python 3.x", "Python licence text"))
    monkeypatch.setattr(notices, "pyinstaller_exception", lambda: "Bootloader Exception unlimited permission")
    monkeypatch.setattr(notices, "openssl_in_cryptography", lambda apache: None)
    monkeypatch.setattr(notices, "certifi_unmodified", lambda p: "unmodified")


def test_generate_with_fake_distributions_names_everything(monkeypatch):
    _fake_build(monkeypatch, [("LICENSE", "Permission is hereby granted")])
    text = notices.generate()
    assert "Foo_Bar 1.2" in text and "Permission is hereby granted" in text
    assert "Bootloader Exception" in text and "Python licence text" in text and "MISSING" not in text


def test_generate_with_fake_distributions_fails_when_a_text_is_missing(monkeypatch):
    _fake_build(monkeypatch, [])
    with pytest.raises(SystemExit) as e:
        notices.generate()
    assert "Foo_Bar 1.2" in str(e.value) and "NOT written" in str(e.value)
