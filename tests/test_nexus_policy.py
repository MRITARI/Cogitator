"""Nexus Mods API Acceptable Use Policy: identity headers on every request, secrets never leaking, strict nxm:// handling.
Nothing here contacts Nexus or touches the registry: every request is answered by a mock, and a guard fails on the rest."""
import io
import logging
import re
import zipfile
from pathlib import Path

import httpx
import pytest

from cogitator import __version__, cli, config, diag, doctor, nexus, nxm
from cogitator.loader import LoaderError, download_asset, Asset
from cogitator.nexus import NexusClient, NexusError, app_headers, make_download_client, user_agent
from cogitator.nxm import NxmError, NxmExpired, parse

G = nexus.GAME
UA_RE = re.compile(r"^Cogitator/\S+ \(Windows; Python \d+\.\d+\)$")
FAR = 4102444800  # year 2100
URL_OK = f"nxm://{G}/mods/1234/files/5678?key=FAKEKEY&expires={FAR}&user_id=1"
URL_EXPIRED = f"nxm://{G}/mods/1234/files/5678?key=FAKEKEY&expires=1000000000&user_id=1"
URL_OTHER_GAME = "nxm://starwarsbattlefront22017/mods/14720/files/41133?key=FAKEKEY&expires=1795106077&user_id=1"


def zip_bytes() -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("m/m.mod", "return {}\n")
    return b.getvalue()


def check_identity(req: httpx.Request) -> None:
    h = req.headers
    assert h.get("application-name") == "Cogitator", f"Application-Name missing/wrong on {req.url}"
    assert h.get("application-version") == __version__ != "", f"Application-Version missing/wrong on {req.url}"
    assert UA_RE.match(h.get("user-agent", "")), f"User-Agent on {req.url} is {h.get('user-agent')!r}"


@pytest.fixture(autouse=True)
def no_registry(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a test touched the real registry")

    monkeypatch.setattr(nxm, "_winreg", boom)


@pytest.fixture
def net(monkeypatch):
    """Answers every request made through ANY httpx client in the process, and fails any Nexus request without the identity.
    Yields the list of requests seen."""
    seen: list[httpx.Request] = []

    def handle(self, request):
        seen.append(request)
        host = request.url.host
        if host.endswith("nexusmods.com"):
            check_identity(request)
        p = request.url.path
        if host == "api.nexusmods.com":
            if p.endswith("/users/validate.json"):
                return httpx.Response(200, json={"name": "x", "is_premium": False})
            if p.endswith("/files.json"):
                return httpx.Response(200, json={"files": [{"file_id": 5678, "file_name": "m.zip", "category_name": "MAIN",
                                                            "version": "1", "size_in_bytes": len(zip_bytes())}]})
            if p.endswith("download_link.json"):
                return httpx.Response(200, json=[{"URI": "https://cf-files.nexusmods.com/f/m.zip?md5=T&expires=1&user_id=1", "name": "x"}])
            if p.endswith("/graphql"):
                return httpx.Response(200, json={"data": {"mods": {"nodes": [{"modId": 1, "name": "A"}]}}})
            return httpx.Response(200, json={"name": "Some mod"})
        if host.endswith("nexusmods.com"):
            return httpx.Response(200, content=zip_bytes())
        raise AssertionError(f"unexpected request to {host}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return seen


@pytest.fixture
def no_net(monkeypatch):
    def boom(self, request):
        raise AssertionError(f"a request was made to {request.url.host}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)


# ------------------------------------------------------------------ B1 / B2: identity on every request

def mock_client(seen, ua="python-httpx/9", extra=None):
    def handler(req):
        seen.append(req)
        if req.url.path.endswith("/graphql"):
            return httpx.Response(200, json={"data": {"mods": {"nodes": []}}})
        if req.url.path.endswith(("updated.json", "download_link.json")):
            return httpx.Response(200, json=[])
        if req.url.path.endswith("changelogs.json"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"files": []} if req.url.path.endswith("files.json") else {})

    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": ua, **(extra or {})})


def test_every_nexus_call_carries_the_identity_headers():
    seen: list[httpx.Request] = []
    with NexusClient("k", client=mock_client(seen)) as nx:
        nx.validate()
        nx.mod(1)
        nx.files(1)
        nx.changelogs(1)
        nx.updated()
        nx.search_mods("hud")
        nx.download_links(1, 2)
        nx.download_links(1, 2, key="FAKEKEY", expires=FAR)
    assert len(seen) == 8
    for r in seen:
        check_identity(r)
    assert any(r.url.path.endswith("/graphql") for r in seen)  # v2 GraphQL is covered too


def test_a_client_with_its_own_user_agent_cannot_change_the_identity():
    seen: list[httpx.Request] = []
    with NexusClient("k", client=mock_client(seen, ua="python-httpx/0.27", extra={"Application-Name": "Vortex"})) as nx:
        nx.validate()
    check_identity(seen[0])


def test_default_user_agent_is_not_python_httpx():
    assert UA_RE.match(user_agent())
    assert user_agent().startswith(f"Cogitator/{__version__} (Windows; Python {__import__('sys').version_info.major}.")
    assert "httpx" not in make_download_client().headers["User-Agent"].lower()


def test_blank_identity_is_refused(monkeypatch):
    for bad in ("", "  "):
        monkeypatch.setattr(nexus, "__version__", bad)
        with pytest.raises(RuntimeError):
            app_headers()
    monkeypatch.undo()
    assert all(v.strip() for v in app_headers().values())


def test_version_header_is_the_package_version():
    root = Path(__file__).resolve().parents[1]
    pyproject = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8")).group(1)
    assert app_headers()["Application-Version"] == __version__ == pyproject


def test_only_nexus_module_knows_the_api_address():
    root = Path(__file__).resolve().parents[1] / "cogitator"
    offenders = [str(p.relative_to(root)) for p in root.rglob("*.py")
                 if p.name != "nexus.py" and "api.nexusmods.com" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_search_doctor_and_downloads_all_send_the_identity(net, tmp_path, capsys):
    monkey_key = "K" * 12
    assert cli.main(["--no-update-check", "search", "hud"]) == 2  # no key stored: refuses before any request
    assert net == []
    config.set_api_key(monkey_key)
    assert cli.main(["--no-update-check", "search", "hud"]) == 0
    assert doctor._nexus(False, monkey_key, None).level == doctor.OK
    link = parse(URL_OK)
    with NexusClient(monkey_key) as nx:
        zp, _ = nxm.download(link, nx, tmp_path / "dl", now=lambda: 1.0)  # CDN request goes through the same identity
        assert zp.is_file()
        from cogitator.modinstall import fetch_premium_download

        fetch_premium_download(nx, 1234, 5678, "m.zip", len(zip_bytes()), tmp_path / "dl2", premium=True)
    hosts = {r.url.host for r in net}
    assert {"api.nexusmods.com", "cf-files.nexusmods.com"} <= hosts  # check_identity ran on every one of them


# ------------------------------------------------------------------ B3: secrets

SECRET = "SeCr3tKeyValue"


def test_redact_covers_key_and_user_id_query_values():
    url = f"https://api.nexusmods.com/v1/games/{G}/mods/1/files/2/download_link.json?key={SECRET}&expires=123&user_id=424242"
    out = config.redact(url)
    assert SECRET not in out and "424242" not in out and "expires=123" in out
    assert config.redact(f"nxm://{G}/mods/1/files/2?KEY={SECRET}&Expires=1&USER_ID=424242").count("***") == 2
    assert config.redact("apikey: abc123456") == "apikey: ***"
    assert config.redact("monkey=banana") == "monkey=banana"  # only the real parameters
    assert SECRET not in nxm.redact_url(f"nxm://{G}/mods/1/files/2?key={SECRET}&user_id=424242") and \
        "424242" not in nxm.redact_url(f"nxm://{G}/mods/1/files/2?key={SECRET}&user_id=424242")


MD5 = "9f2c0d41aa77b3e5"
URI = f"https://cf-files.nexusmods.com/f/m.zip?md5={MD5}&expires=1&user_id=424242"


def test_md5_download_token_is_redacted_everywhere(tmp_path):
    out = config.redact(f"fetching {URI}")
    assert MD5 not in out and "424242" not in out and "expires=1" in out
    assert config.redact("xmd5=keepme") == "xmd5=keepme"
    assert MD5 not in nxm.redact_url(URI)

    def go(lg):
        lg.warning("GET %s", URI)
        try:
            raise RuntimeError(f"boom {URI}")
        except RuntimeError:
            lg.exception("download broke")

    assert MD5 not in log_through_filter(go)  # message and traceback
    p = config.log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"GET {URI}" + chr(10), encoding="utf-8")
    with zipfile.ZipFile(diag.build(None, None, tmp_path)[0]) as z:  # the diag zip
        assert MD5 not in "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())

    def boom(req):
        raise httpx.ConnectError(f"cannot reach {URI}")

    with httpx.Client(transport=httpx.MockTransport(boom)) as c, pytest.raises(LoaderError) as e:  # error text
        download_asset(c, Asset("f.zip", URI, 0), tmp_path, sleep=lambda s: None)
    assert MD5 not in str(e.value)


def log_through_filter(fn):
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    h.addFilter(config.RedactFilter())
    lg = logging.getLogger("test-redact")
    lg.handlers, lg.propagate = [h], False
    lg.setLevel(logging.DEBUG)
    fn(lg)
    return stream.getvalue()


def test_log_lines_and_tracebacks_are_redacted():
    url = f"https://x/download_link.json?key={SECRET}&expires=1&user_id=424242"

    def go(lg):
        lg.warning("fetching %s", url)
        try:
            raise RuntimeError(f"failed: {url}")
        except RuntimeError:
            lg.exception("download broke")

    out = log_through_filter(go)
    assert "RuntimeError" in out and "download broke" in out  # the traceback is still there
    assert SECRET not in out and "424242" not in out


def test_diag_bundle_never_contains_nexus_secrets(tmp_path):
    p = config.log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"GET https://api.nexusmods.com/v1/x/download_link.json?key={SECRET}&expires=1&user_id=424242\n"
                 f"nxm://{G}/mods/1/files/2?key={SECRET}&expires=9&user_id=424242\n", encoding="utf-8")
    path, _ = diag.build(None, None, tmp_path)
    with zipfile.ZipFile(path) as z:
        blob = "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
    assert "download_link.json" in blob and SECRET not in blob and "424242" not in blob


def test_error_messages_do_not_carry_the_query():
    def handler(req):
        return httpx.Response(500) if "files/1/" in req.url.path else httpx.Response(404)

    with NexusClient("apikey-value-1", client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None) as nx:
        for fid in (1, 2):
            with pytest.raises(NexusError) as e:
                nx.download_links(5, fid, key=SECRET, expires=FAR)
            assert SECRET not in str(e.value) and "apikey-value-1" not in str(e.value)

    def boom(req):
        raise httpx.ConnectError(f"cannot reach {req.url}")

    with NexusClient("k", client=httpx.Client(transport=httpx.MockTransport(boom)), sleep=lambda s: None) as nx:
        with pytest.raises(NexusError) as e:
            nx.download_links(5, 1, key=SECRET, expires=FAR)
        assert SECRET not in str(e.value)


def test_failed_file_download_error_is_redacted(tmp_path):
    def boom(req):
        raise httpx.ConnectError(f"cannot reach https://cdn/f.zip?key={SECRET}&user_id=424242")

    with httpx.Client(transport=httpx.MockTransport(boom)) as c, pytest.raises(LoaderError) as e:
        download_asset(c, Asset("f.zip", "https://cdn/f.zip", 0), tmp_path, sleep=lambda s: None)
    assert SECRET not in str(e.value) and "424242" not in str(e.value)


# ------------------------------------------------------------------ B4: the nxm handler

def click(url, spawned=None):
    said = []
    rc = nxm.handle_click(url, popen=lambda *a, **k: (spawned if spawned is not None else []).append(1),
                          running=lambda: False, notify=said.append)
    return rc, said


def test_other_games_link_is_rejected_with_a_message_and_nothing_happens(no_net):
    with pytest.raises(NxmError, match="another game"):
        parse(URL_OTHER_GAME)
    spawned: list = []
    rc, said = click(URL_OTHER_GAME, spawned)
    assert rc == 2 and spawned == [] and "another game" in said[0] and "FAKEKEY" not in said[0]
    assert not nxm.queue_dir().exists() or not list(nxm.queue_dir().iterdir())


def test_valid_darktide_link_is_queued(no_net):
    link = parse(URL_OK)
    assert (link.mod_id, link.file_id, link.key, link.expires, link.user_id) == (1234, 5678, "FAKEKEY", FAR, 1)
    spawned: list = []
    rc, said = click(URL_OK, spawned)
    assert rc == 0 and said == [] and spawned == [1] and len(nxm.pending()) == 1


def test_expired_link_says_so_and_is_not_queued(no_net):
    link = parse(URL_EXPIRED)  # well formed; expiry is checked separately
    with pytest.raises(NxmExpired, match="link expired"):
        nxm.check_expiry(link)
    spawned: list = []
    rc, said = click(URL_EXPIRED, spawned)
    assert rc == 2 and spawned == [] and "link expired" in said[0] and "FAKEKEY" not in said[0]
    assert not nxm.queue_dir().exists() or not list(nxm.queue_dir().iterdir())


def test_link_that_expires_while_queued_is_reported_and_dropped(no_net):
    p = nxm.enqueue(URL_OK, now=lambda: 1000.0)
    heard = []
    assert nxm.pending(now=lambda: FAR + 1, on_expired=heard.append) == [] and not p.exists()
    # (MAX_AGE_S also applies, but the expiry callback is what tells the user why)
    assert [h.file_id for h in heard] == [5678]


def test_download_refuses_an_expired_link_before_any_request(no_net, tmp_path):
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("Nexus was contacted for an expired link")

    with pytest.raises(NxmExpired):
        nxm.download(parse(URL_OK), Boom(), tmp_path, now=lambda: FAR + 1)


@pytest.mark.parametrize("url", [
    f"nxm://user@{G}/mods/1/files/2?key=k&expires=9", f"nxm://{G}:8080/mods/1/files/2?key=k&expires=9",
    f"nxm://{G}:abc/mods/1/files/2?key=k&expires=9", f"nxm://{G}/mods/1/files/2?key=k&expires=9#x",
    f"nxm://{G}/mods/1/files/2?key=k&key=j&expires=9", f"nxm://{G}/mods/1/files/2?key=k&expires=9&expires=10",
    f"nxm://{G}/mods/1/files/2?key=k&expires=9&user_id=1&user_id=2", "nxm://[::1/mods/1/files/2?key=k&expires=9",
    f"nxm://{G}.evil.com/mods/1/files/2?key=k&expires=9",
])
def test_parse_is_strict_about_the_authority_and_repeats(url):
    with pytest.raises(NxmError):
        parse(url)


def test_download_links_are_requested_fresh_each_time_and_never_stored(tmp_path):
    seen: list[httpx.Request] = []
    uri = "https://cf-files.nexusmods.com/f/m.zip?md5=ABC&expires=1&user_id=1"

    def api(req):
        seen.append(req)
        if req.url.path.endswith("files.json"):
            return httpx.Response(200, json={"files": [{"file_id": 5678, "file_name": "m.zip", "category_name": "MAIN",
                                                        "size_in_bytes": len(zip_bytes())}]})
        return httpx.Response(200, json=[{"URI": uri}])

    cdn = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=zip_bytes())))
    link = parse(URL_OK)
    with NexusClient("k", client=httpx.Client(transport=httpx.MockTransport(api))) as nx:
        nxm.download(link, nx, tmp_path / "a", client=cdn, now=lambda: 1.0)
        nxm.download(link, nx, tmp_path / "b", client=cdn, now=lambda: 1.0)
    dl = [r for r in seen if r.url.path.endswith("download_link.json")]
    assert len(dl) == 2  # asked again, not remembered
    assert dl[0].url.path == f"/v1/games/{G}/mods/1234/files/5678/download_link.json"
    assert dict(dl[0].url.params) == {"key": "FAKEKEY", "expires": str(FAR)}  # user_id is not sent
    for d in (config.config_dir(), config.cache_dir()):
        for f in d.rglob("*") if d.exists() else []:
            if f.is_file():
                assert "md5=ABC" not in f.read_text(encoding="utf-8", errors="replace"), f


def test_registering_the_handler_stays_opt_in():
    iss = (Path(__file__).resolve().parents[1] / "packaging" / "cogitator.iss").read_text(encoding="utf-8")
    task = next(line for line in iss.splitlines() if line.startswith('Name: "nxm"'))
    assert "unchecked" in task  # the installer does not register it unless the user ticks the box
