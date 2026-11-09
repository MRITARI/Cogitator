import io
import stat
import zipfile

import httpx
import pytest
import respx

from cogitator import loader
from cogitator.loader import (Asset, LoaderError, Release, UnsafeArchive, download_asset, fetch_latest_release,
                              long_path, pick_asset, safe_extract)


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            if isinstance(data, zipfile.ZipInfo):
                z.writestr(data, "target")
            else:
                z.writestr(name, data)
    return path


def test_extract_ok(tmp_path):
    zp = make_zip(tmp_path / "a.zip", {"root/mods/base/x.lua": "1", "root/tools/": "", "root/a.txt": "2"})
    files = safe_extract(zp, tmp_path / "out")
    assert sorted(files) == ["root/a.txt", "root/mods/base/x.lua"]
    assert (tmp_path / "out" / "root" / "mods" / "base" / "x.lua").read_text() == "1"


@pytest.mark.parametrize("bad", ["../evil.txt", "a/../../evil.txt", "/abs.txt", "C:/abs.txt", "..\\evil.txt", "a\\..\\..\\e"])
def test_zip_slip_rejected_and_nothing_written(tmp_path, bad):
    zp = make_zip(tmp_path / "a.zip", {"good.txt": "ok", bad: "x"})
    out = tmp_path / "out"
    with pytest.raises(UnsafeArchive):
        safe_extract(zp, out)
    assert not (out / "good.txt").exists()  # validated before any write
    assert not (tmp_path / "evil.txt").exists()


def test_symlink_rejected(tmp_path):
    zi = zipfile.ZipInfo("link")
    zi.external_attr = (stat.S_IFLNK | 0o777) << 16
    zp = make_zip(tmp_path / "a.zip", {"link": zi})
    with pytest.raises(UnsafeArchive):
        safe_extract(zp, tmp_path / "out")


def test_limits(tmp_path):
    zp = make_zip(tmp_path / "a.zip", {"a": "x" * 1000, "b": "y" * 1000})
    with pytest.raises(UnsafeArchive):
        safe_extract(zp, tmp_path / "o1", max_bytes=1500)
    with pytest.raises(UnsafeArchive):
        safe_extract(zp, tmp_path / "o2", max_files=1)


def test_long_path_extract(tmp_path):
    deep = "/".join(["d" * 40] * 8) + "/file.lua"  # >260 chars once under tmp_path
    zp = make_zip(tmp_path / "a.zip", {deep: "x"})
    assert len(str(tmp_path / "out" / deep)) > 260
    assert safe_extract(zp, tmp_path / "out") == [deep]
    assert long_path(tmp_path / "out" / deep).is_file()


def test_layout_lists_top_level(tmp_path):
    make_zip(tmp_path / "a.zip", {"mods/base/x.lua": "1", "toggle.bat": "z"})
    safe_extract(tmp_path / "a.zip", tmp_path / "o")
    text = loader.layout(tmp_path / "o")
    assert "mods/  (1 files)" in text and "toggle.bat" in text


# ---- pick_asset

def rel(*names):
    return Release("r/r", "v1", assets=[Asset(n, f"https://x/{n}") for n in names])


def test_pick_asset():
    assert pick_asset(rel("DML.zip"), "dml").name == "DML.zip"
    assert pick_asset(rel("DML.zip", "DML-update.zip"), "dml").name == "DML.zip"
    assert pick_asset(rel("DML.zip", "DML-update.zip"), "dml", update_only=True).name == "DML-update.zip"
    assert pick_asset(rel("mod_load_order.txt"), "dml") is None  # real DML 24.11.27 case
    assert pick_asset(rel("a.zip", "b.zip"), "dmf") is None  # ambiguous -> None, never guess
    assert pick_asset(rel("x.zip", "dmf.zip"), "dmf").name == "dmf.zip"
    assert pick_asset(rel(), "dmf") is None


# ---- network (respx only)

API = "https://api.github.com/repos/o/r/releases/latest"


@respx.mock
def test_release_parse():
    respx.get(API).respond(200, json={"tag_name": "26.06.24", "name": "n", "assets": [
        {"name": "a.zip", "browser_download_url": "https://dl/a.zip", "size": 5}, {"junk": 1}]})
    with loader.make_client() as c:
        r = fetch_latest_release("o/r", c)
    assert r.tag == "26.06.24" and [a.name for a in r.assets] == ["a.zip"]


@respx.mock
def test_release_rate_limit_and_404_and_garbage():
    with loader.make_client() as c:
        respx.get(API).respond(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "123"})
        with pytest.raises(LoaderError, match="rate limit"):
            fetch_latest_release("o/r", c)
        respx.get(API).respond(404)
        with pytest.raises(LoaderError, match="No published release"):
            fetch_latest_release("o/r", c)
        respx.get(API).respond(200, json={"nope": 1})
        with pytest.raises(LoaderError, match="Unexpected"):
            fetch_latest_release("o/r", c)


@respx.mock
def test_retry_on_5xx_then_success_and_exhaustion():
    sleeps = []
    route = respx.get(API).mock(side_effect=[httpx.Response(502), httpx.ConnectError("x"),
                                             httpx.Response(200, json={"tag_name": "t"})])
    with loader.make_client() as c:
        assert fetch_latest_release("o/r", c, sleep=sleeps.append).tag == "t"
    assert sleeps == [1, 2] and route.call_count == 3
    respx.get(API).respond(500)
    with loader.make_client() as c, pytest.raises(LoaderError, match="after 3 attempts"):
        fetch_latest_release("o/r", c, sleep=lambda s: None)


@respx.mock
def test_download_verifies(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a", "b")
    data = buf.getvalue()
    respx.get("https://dl/ok.zip").respond(200, content=data)
    respx.get("https://dl/html.zip").respond(200, content=b"<html>not a zip</html>")
    respx.get("https://dl/gone.zip").respond(404)
    with loader.make_client() as c:
        p = download_asset(c, Asset("ok.zip", "https://dl/ok.zip", len(data)), tmp_path)
        assert zipfile.is_zipfile(p)
        with pytest.raises(LoaderError, match="Size mismatch"):
            download_asset(c, Asset("ok.zip", "https://dl/ok.zip", len(data) + 1), tmp_path)
        with pytest.raises(LoaderError, match="not a zip"):
            download_asset(c, Asset("html.zip", "https://dl/html.zip"), tmp_path)
        with pytest.raises(LoaderError, match="HTTP 404"):
            download_asset(c, Asset("gone.zip", "https://dl/gone.zip"), tmp_path)
        with pytest.raises(LoaderError, match="exceeds"):
            download_asset(c, Asset("ok.zip", "https://dl/ok.zip"), tmp_path, max_bytes=10)
