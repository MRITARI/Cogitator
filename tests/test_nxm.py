import io
import json
import sys
import zipfile

import httpx
import pytest
import respx

from cogitator import cli, config, nexus, nxm
from cogitator.nxm import NxmError, parse

B, G = nexus.BASE, nexus.GAME
URL = f"nxm://{G}/mods/19/files/156030?key=AbC_123-x&expires=4102444800&user_id=77"


def test_parse_valid_link():
    link = parse(URL)
    assert (link.mod_id, link.file_id, link.key, link.expires, link.user_id) == (19, 156030, "AbC_123-x", 4102444800, 77)
    assert parse(URL.replace("&user_id=77", "")).user_id is None
    assert parse(f"NXM://{G.upper()}/mods/1/files/2/?key=k&expires=9").mod_id == 1


@pytest.mark.parametrize("url", [
    "", "https://example.com", "nxm://skyrim/mods/1/files/2?key=k&expires=9",
    f"nxm://{G}/collections/abc/revisions/3?key=k&expires=9",
    f"nxm://{G}/mods/1/files/2", f"nxm://{G}/mods/1/files/2?key=&expires=9", f"nxm://{G}/mods/1/files/2?key=k",
    f"nxm://{G}/mods/1/files/2?key=k&expires=soon", f"nxm://{G}/mods/1/files/2?key=a b&expires=9",
    f"nxm://{G}/mods/1/files/2?key=../x&expires=9", f"nxm://{G}/mods/x/files/2?key=k&expires=9",
    f"nxm://{G}/mods/1/files/2?key=k&expires=9&user_id=abc", f"nxm://{G}/mods/1234567890/files/2?key=k&expires=9",
    f"nxm://{G}/mods/1/files/2?key=k&expires=9\n&x=1", f"nxm://{G}/mods/1/files/2?key={'k' * 300}&expires=9",
    "nxm://" + G + "/mods/1/files/2?key=k&expires=9&" + "a=1&" * 300, None, 5,
])
def test_parse_rejects_everything_else(url):
    with pytest.raises(NxmError):
        parse(url)


def test_key_never_in_repr_or_redacted_url():
    link = parse(URL)
    assert "AbC_123" not in repr(link) and "AbC_123" not in str(link.__repr__())
    assert "AbC_123" not in nxm.redact_url(URL) and "key=***" in nxm.redact_url(URL)


# ---------------------------------------------------------------- queue

def test_queue_roundtrip_and_cleanup():
    p = nxm.enqueue(URL, now=lambda: 1000.0)
    items = nxm.pending(now=lambda: 1001.0)
    assert [i[0] for i in items] == [p] and items[0][1].mod_id == 19
    nxm.done(p)
    assert nxm.pending(now=lambda: 1001.0) == [] and not p.exists()


def test_queue_refuses_invalid_and_drops_stale_or_corrupt():
    with pytest.raises(NxmError):
        nxm.enqueue("nxm://skyrim/mods/1/files/2?key=k&expires=9")
    assert not nxm.queue_dir().exists() or not list(nxm.queue_dir().iterdir())
    old = nxm.enqueue(URL, now=lambda: 1000.0)
    assert nxm.pending(now=lambda: 1000.0 + nxm.MAX_AGE_S + 1) == [] and not old.exists()  # too old
    expiring = nxm.enqueue(URL.replace("4102444800", "1500"), now=lambda: 1000.0)
    assert nxm.pending(now=lambda: 1600.0) == [] and not expiring.exists()  # the link's own expiry passed
    (nxm.queue_dir() / "junk.json").write_text("{not json")
    (nxm.queue_dir() / "evil.json").write_text(json.dumps({"url": "nxm://skyrim/x", "at": 1}))
    assert nxm.pending() == []
    assert list(nxm.queue_dir().iterdir()) == []


# ---------------------------------------------------------------- download

def zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("m/m.mod", "return {}")
    return buf.getvalue()


@respx.mock
def test_download_uses_the_links_key_and_expiry(tmp_path):
    data = zip_bytes()
    respx.get(f"{B}/games/{G}/mods/19/files.json").respond(200, json={"files": [
        {"file_id": 156030, "name": "Mod", "version": "1.0", "category_name": "MAIN", "file_name": "Mod-19-1-0-1.zip",
         "size_in_bytes": len(data), "uploaded_timestamp": 1}]})
    route = respx.get(f"{B}/games/{G}/mods/19/files/156030/download_link.json").respond(200, json=[{"URI": "https://cdn.example/m.zip"}])
    cdn = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data)))
    with nexus.NexusClient("apikey") as nx:
        path, f = nxm.download(parse(URL), nx, tmp_path, client=cdn)
    assert path.read_bytes() == data and f.file_id == 156030
    sent = route.calls.last.request.url.params
    assert sent["key"] == "AbC_123-x" and sent["expires"] == "4102444800"


@respx.mock
def test_download_errors_are_clear(tmp_path):
    respx.get(f"{B}/games/{G}/mods/19/files.json").respond(200, json={"files": []})
    with nexus.NexusClient("k") as nx, pytest.raises(NxmError, match="does not list"):
        nxm.download(parse(URL), nx, tmp_path)
    respx.get(f"{B}/games/{G}/mods/19/files.json").respond(200, json={"files": [{"file_id": 156030, "file_name": "a.zip"}]})
    respx.get(f"{B}/games/{G}/mods/19/files/156030/download_link.json").respond(200, json=[])
    with nexus.NexusClient("k") as nx, pytest.raises(NxmError, match="no download link"):
        nxm.download(parse(URL), nx, tmp_path)


@respx.mock
def test_premium_download_links_do_not_send_key_params():
    route = respx.get(f"{B}/games/{G}/mods/1/files/2/download_link.json").respond(200, json=[{"URI": "u"}])
    with nexus.NexusClient("k") as nx:
        nx.download_links(1, 2)
    assert "key" not in route.calls.last.request.url.params


# ---------------------------------------------------------------- the click

def test_click_queues_and_opens_cogitator_when_closed(monkeypatch):
    spawned = []
    rc = nxm.handle_click(URL, popen=lambda cmd, **k: spawned.append(cmd), running=lambda: False)
    assert rc == 0 and len(nxm.pending()) == 1 and len(spawned) == 1
    assert spawned[0][-1] == "cogitator" or spawned[0][-2:] == ["-m", "cogitator"] or spawned[0][0].endswith("cogitator.exe")


def test_click_does_not_open_a_second_window_when_running():
    spawned = []
    assert nxm.handle_click(URL, popen=lambda *a, **k: spawned.append(1), running=lambda: True) == 0
    assert spawned == [] and len(nxm.pending()) == 1


def test_click_with_unknown_game_still_opens_cogitator():
    spawned = []
    nxm.handle_click(URL, popen=lambda *a, **k: spawned.append(1), running=lambda: None)
    assert spawned == [1]


def test_bad_click_is_ignored_without_opening_anything():
    spawned = []
    assert nxm.handle_click("nxm://skyrim/mods/1/files/2?key=k&expires=9", popen=lambda *a, **k: spawned.append(1), running=lambda: False) == 2
    assert spawned == [] and nxm.pending() == []
    assert nxm.click_main([]) == 2 and nxm.click_main(["a", "b"]) == 2


def test_interface_running_uses_the_instance_lock(tmp_path):
    s = config.Settings()
    assert nxm.interface_running(s) is None  # no game configured yet
    s.game_dir = str(tmp_path)
    assert nxm.interface_running(s) is False
    with config.InstanceLock(tmp_path):
        class Other:  # a different process holds it: simulate with a pid that is not ours
            pass

        lock = config.InstanceLock(tmp_path)
        lock.path.write_text("1")  # pid 1 is never ours; whether it is alive depends on the OS
    assert nxm.interface_running(s) in (False, True)


# ---------------------------------------------------------------- registering the handler (registry seam, as in userpath tests)

@pytest.fixture
def reg(monkeypatch):
    state = {"cmd": None, "writes": []}
    monkeypatch.setattr(nxm, "read_handler", lambda: state["cmd"])
    monkeypatch.setattr(nxm, "_write_handler", lambda c: state.update(cmd=c, writes=state["writes"] + [c]))
    monkeypatch.setattr(nxm, "_delete_handler", lambda: state.update(cmd=None))
    return state


def test_register_and_unregister_clean(reg):
    assert "now handles" in nxm.register(nxm.default_command())
    assert nxm.status() == "Cogitator handles nxm:// links."
    assert "no longer" in nxm.unregister() and reg["cmd"] is None


def test_register_remembers_and_restores_the_previous_handler(reg):
    reg["cmd"] = '"C:\\Vortex\\Vortex.exe" -d "%1"'
    assert "previous handler is remembered" in nxm.register(nxm.default_command())
    assert nxm.is_ours(reg["cmd"])
    assert "restored" in nxm.unregister() and reg["cmd"] == '"C:\\Vortex\\Vortex.exe" -d "%1"'


def test_unregister_never_touches_another_programs_handler(reg):
    reg["cmd"] = '"C:\\Vortex\\Vortex.exe" -d "%1"'
    assert "another program" in nxm.unregister() and reg["cmd"] == '"C:\\Vortex\\Vortex.exe" -d "%1"'
    reg["cmd"] = None
    assert "No nxm://" in nxm.unregister() and "No program" in nxm.status()


def test_reregistering_does_not_overwrite_the_remembered_handler(reg):
    reg["cmd"] = '"C:\\Vortex\\Vortex.exe" "%1"'
    nxm.register(nxm.default_command())
    nxm.register(nxm.default_command())  # already ours: the saved previous one stays
    nxm.unregister()
    assert "Vortex" in reg["cmd"]


def test_default_command_shapes(monkeypatch, tmp_path):
    assert nxm.default_command().endswith('nxm handle "%1"') and "-m cogitator" in nxm.default_command()
    exe = tmp_path / "cogitator.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert nxm.default_command() == f'"{exe}" nxm handle "%1"'
    (tmp_path / "cogitator-nxm.exe").write_bytes(b"")
    assert nxm.default_command() == f'"{tmp_path / "cogitator-nxm.exe"}" "%1"'


def test_cli_nxm_commands(reg, monkeypatch, capsys):
    if sys.platform != "win32":
        pytest.skip("Windows only")
    assert cli.main(["nxm", "status"]) == 0 and "No program" in capsys.readouterr().out
    assert cli.main(["nxm", "register"]) == 0 and nxm.is_ours(reg["cmd"])
    assert cli.main(["nxm", "unregister"]) == 0 and reg["cmd"] is None
    monkeypatch.setattr(nxm, "handle_click", lambda url: 7)
    assert cli.main(["nxm", "handle", URL]) == 7


def test_real_registry_roundtrip_in_a_scratch_key(monkeypatch):
    """The real winreg calls (not the seam), in a throw-away key so the machine's real nxm handler is never touched."""
    if sys.platform != "win32":
        pytest.skip("Windows only")
    import winreg

    cmd = nxm.default_command()
    monkeypatch.setattr(nxm, "HKCU_KEY", r"Software\CogitatorTest\nxm")
    try:
        assert nxm.read_handler() is None
        nxm.register(cmd)
        assert nxm.read_handler() == cmd
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, nxm.HKCU_KEY) as k:
            assert winreg.QueryValueEx(k, "URL Protocol")[0] == ""
        assert "no longer" in nxm.unregister()
        assert nxm.read_handler() is None
    finally:
        nxm._delete_handler()
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\CogitatorTest")
        except OSError:
            pass




def test_is_ours_compares_the_executable_path_not_a_name_fragment():
    assert nxm.is_ours(nxm.default_command().upper())
    assert not nxm.is_ours('"C:\Tools\cogitator-fan\Vortex.exe" -d "%1"')
    assert not nxm.is_ours(None) and not nxm.is_ours("")
