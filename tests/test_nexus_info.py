"""Details panel backend: tolerant parsing, plain-text sanitising, the 14-day cache, the quota guard (no call when it must not call)
and the strict image fetch."""
import json
import time

import httpx
import pytest

from cogitator import nexus_info as ni
from cogitator.nexus import NexusError, NotFound, Quota, RateLimited

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
GOOD = {"name": "Cool Mod", "author": "someone", "summary": "A <b>short</b> one", "description": "Line one[b]bold[/b]<br />[url=https://x.y/z]site[/url]",
        "version": "1.2", "picture_url": "https://staticdelivery.nexusmods.com/mods/1/images/9.jpg", "endorsement_count": 12, "mod_unique_downloads": 345,
        "updated_timestamp": 1700000000}


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("COGITATOR_CACHE_DIR", str(tmp_path / "cache"))
    ni._failed.clear()


class Reg:
    def __init__(self, quota=None):
        self.data = {"updates": {"quota": quota} if quota else {}}
        self.saved = 0

    def save(self):
        self.saved += 1


class FakeNexus:
    """Stands in for NexusClient: counts calls; `hourly` is what the reply reports."""
    calls: list = []
    payload = GOOD
    error: Exception | None = None
    hourly = 1500

    def __init__(self, key):
        self.key, self.requests, self.exhausted, self.quota = key, 0, False, Quota()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _go(self, what, value):
        FakeNexus.calls.append(what)
        self.requests += 1
        self.quota = Quota(daily=9000, hourly=FakeNexus.hourly)
        if FakeNexus.error:
            raise FakeNexus.error
        return value

    def mod(self, mod_id):
        return self._go(("mod", mod_id), FakeNexus.payload)

    def changelogs(self, mod_id):
        return self._go(("changes", mod_id), {"1.0": ["first"], "1.1": ["<script>x</script>fix &amp; more"]})


@pytest.fixture(autouse=True)
def fake():
    FakeNexus.calls, FakeNexus.payload, FakeNexus.error, FakeNexus.hourly = [], GOOD, None, 1500


def details(mod_id=7, reg=None, key="K", now=None):
    return ni.details_for(mod_id, reg or Reg(), api_key=key, client_factory=FakeNexus, now=now or time.time)


# ---------------------------------------------------------------- sanitiser
def test_plain_text_strips_markup_and_decodes_entities():
    out = ni.plain_text("<p>Hi &amp; <b>bye</b></p>[b]bold[/b] [url=https://a.b/c]link[/url][list][*]one[*]two[/list]&lt;i&gt;x&lt;/i&gt;")
    assert "<" not in out and "[b]" not in out and "&amp;" not in out
    assert "Hi & bye" in out and "bold" in out and "link (https://a.b/c)" in out and "- one" in out and "- two" in out and out.count("x") == 1


def test_plain_text_never_leaves_script_or_images_behind():
    out = ni.plain_text("a<script>alert(1)</script>b&lt;script&gt;alert(2)&lt;/script&gt;c[img]http://evil/x.png[/img]d<style>p{}</style>e<img src=x onerror=y>")
    assert out == "a b c d e" or ("alert" not in out and "script" not in out and "evil" not in out and "onerror" not in out)
    assert "alert" not in out


def test_plain_text_is_bounded_and_tolerant():
    big = "word " * 2_000_000
    out = ni.plain_text(big)
    assert len(out) <= ni.DESCRIPTION_CAP + 3 and out.endswith("...")
    assert ni.plain_text(None) == "" and ni.plain_text(123) == "" and ni.plain_text("<" * 50000 + "x")  # no catastrophic backtracking
    assert "\x00" not in ni.plain_text("a\x00b\u202ec")


# ---------------------------------------------------------------- parser
@pytest.mark.parametrize("bad", [None, [], "text", 5, {}, {"summary": "no name or id"}])
def test_parser_rejects_non_mods(bad):
    assert ni.parse_details(bad) is None


def test_parser_tolerates_odd_field_types():
    d = ni.parse_details({"name": ["x"], "mod_id": 1, "endorsement_count": "n/a", "mod_unique_downloads": -4, "mod_downloads": "55",
                          "picture_url": "http://staticdelivery.nexusmods.com/a.jpg", "updated_timestamp": {"a": 1}, "author": 7})
    assert d["name"] == "" and d["endorsements"] is None and d["downloads"] == 55 and d["picture_url"] == "" and d["updated"] is None
    ok = ni.parse_details(GOOD)
    assert (ok["name"], ok["author"], ok["summary"], ok["version"], ok["endorsements"], ok["downloads"]) == ("Cool Mod", "someone", "A short one", "1.2", 12, 345)
    assert "<" not in ok["description"] and "[b]" not in ok["description"] and ok["picture_url"].startswith("https://staticdelivery.nexusmods.com/")


def test_malformed_cache_files_are_ignored_and_refetched(tmp_path):
    p = ni._path(7)
    p.parent.mkdir(parents=True)
    for junk in ("{not json", "[]", '{"at": "x", "data": {}}', '{"at": 1, "data": 5}', "\xff"):
        p.write_text(junk, encoding="utf-8", errors="ignore")
        FakeNexus.calls.clear()
        r = details()
        assert r.status == ni.OK and FakeNexus.calls == [("mod", 7)], junk


# ---------------------------------------------------------------- cache and TTL
def test_one_call_per_mod_per_ttl():
    t0 = 1_000_000.0
    assert details(now=lambda: t0).status == ni.OK
    assert details(now=lambda: t0 + ni.TTL_S - 5).status == ni.OK
    assert FakeNexus.calls == [("mod", 7)]
    assert details(now=lambda: t0 + ni.TTL_S + 5).status == ni.OK  # expired: exactly one more
    assert FakeNexus.calls == [("mod", 7), ("mod", 7)]


def test_stale_cache_is_served_when_the_refresh_cannot_happen():
    t0 = 1_000_000.0
    details(now=lambda: t0)
    later = lambda: t0 + ni.TTL_S + 10  # noqa: E731
    r = ni.details_for(7, Reg(), api_key=None, now=later, client_factory=FakeNexus)  # no key
    assert r.status == ni.CACHED and r.stale and r.data["name"] == "Cool Mod" and r.extra["why"] == ni.NO_KEY
    FakeNexus.error = NexusError("Network error talking to Nexus: ConnectError")
    r = details(now=later)
    assert r.status == ni.CACHED and r.extra["why"] == ni.OFFLINE


def test_not_found_is_cached_so_it_is_not_asked_again():
    FakeNexus.error = NotFound("404")
    assert details().status == ni.NOT_FOUND
    FakeNexus.error = None
    assert details().status == ni.NOT_FOUND and len(FakeNexus.calls) == 1


def test_failures_back_off_instead_of_hammering():
    FakeNexus.error = NexusError("Network error talking to Nexus: ConnectError")
    assert details(now=lambda: 100.0).status == ni.OFFLINE
    FakeNexus.error = None
    assert details(now=lambda: 160.0).status == ni.OFFLINE and len(FakeNexus.calls) == 1
    assert details(now=lambda: 100.0 + ni.FAIL_BACKOFF_S + 1).status == ni.OK


def test_rate_limit_and_unusable_replies():
    FakeNexus.error = RateLimited("429")
    assert details().status == ni.RATE_LIMITED
    FakeNexus.error = None
    FakeNexus.payload = {"weird": True}
    assert details(mod_id=8).status == ni.ERROR


# ---------------------------------------------------------------- quota guard
def test_no_call_without_a_key_or_an_id():
    assert ni.details_for(7, Reg(), api_key=None, client_factory=FakeNexus).status in (ni.NO_KEY,)
    assert ni.details_for(None, Reg(), api_key="K", client_factory=FakeNexus).status == ni.UNLINKED
    assert FakeNexus.calls == []


@pytest.mark.parametrize("hourly,calls", [(49, 0), (0, 0), (50, 1), (1500, 1)])
def test_low_remembered_quota_means_no_call(hourly, calls):
    t = time.time()
    reg = Reg({"text": f"9000 daily / {hourly} hourly left", "at": t - 60})
    r = details(reg=reg, now=lambda: t)
    assert len(FakeNexus.calls) == calls
    assert (r.status == ni.LOW_QUOTA) == (calls == 0)


def test_an_old_remembered_quota_is_ignored():
    t = time.time()
    reg = Reg({"text": "9000 daily / 3 hourly left", "at": t - 2 * 3600})
    assert details(reg=reg, now=lambda: t).status == ni.OK


def test_the_quota_a_call_reports_is_remembered_and_blocks_the_next_mod():
    reg = Reg()
    FakeNexus.hourly = 20
    assert details(7, reg).status == ni.OK
    assert "20 hourly" in reg.data["updates"]["quota"]["text"] and reg.saved
    assert details(8, reg).status == ni.LOW_QUOTA and FakeNexus.calls == [("mod", 7)]


def test_an_exhausted_client_makes_no_call():
    class Done(FakeNexus):
        def __init__(self, key):
            super().__init__(key)
            self.exhausted = True

    r = ni.details_for(7, Reg(), api_key="K", client_factory=Done)
    assert r.status == ni.RATE_LIMITED and FakeNexus.calls == []


def test_changes_are_a_separate_call_cached_and_plain_text():
    r = ni.changes_for(7, Reg(), api_key="K", client_factory=FakeNexus)
    assert r.status == ni.OK and [v["version"] for v in r.data["versions"]] == ["1.1", "1.0"]
    assert r.data["versions"][0]["lines"] == ["fix & more"]
    ni.changes_for(7, Reg(), api_key="K", client_factory=FakeNexus)
    assert FakeNexus.calls == [("changes", 7)]
    later = time.time() + ni.TTL_S + 9
    assert ni.changes_for(7, Reg({"text": "1 daily / 1 hourly left", "at": later}), api_key="K", client_factory=FakeNexus, now=lambda: later).status == ni.CACHED


# ---------------------------------------------------------------- images
URL = "https://staticdelivery.nexusmods.com/mods/1/images/9.png"


def client(handler):
    seen = []

    def h(req):
        seen.append(req)
        return handler(req)

    c = httpx.Client(transport=httpx.MockTransport(h), headers={"User-Agent": "Cogitator-test"})
    c.seen = seen
    return c


def ok(req):
    return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})


def test_a_good_image_is_cached_and_returned_as_a_data_uri():
    c = client(ok)
    uri = ni.fetch_image(URL, c)
    assert uri.startswith("data:image/png;base64,") and len(c.seen) == 1
    assert "apikey" not in {k.lower() for k in c.seen[0].headers}
    assert ni.cached_image(URL) == uri and ni.fetch_image(URL, c) == uri and len(c.seen) == 1  # second time: no network


@pytest.mark.parametrize("url", ["http://staticdelivery.nexusmods.com/a.png", "https://evilnexusmods.com/a.png", "https://nexusmods.com.evil.io/a.png",
                                 "https://user:pw@staticdelivery.nexusmods.com/a.png", "https://staticdelivery.nexusmods.com:8443/a.png", "file:///c:/x.png", ""])
def test_wrong_host_scheme_or_port_makes_no_request(url):
    c = client(ok)
    assert ni.fetch_image(url, c) is None and c.seen == []


def test_redirects_inside_the_suffix_are_followed_but_not_off_host():
    def inside(req):
        if req.url.path.endswith("/a.png"):
            return httpx.Response(302, headers={"location": "https://other.nexusmods.com/b.png"})
        return ok(req)

    c = client(inside)
    assert ni.fetch_image(URL.replace("9.png", "a.png"), c) and [r.url.host for r in c.seen] == ["staticdelivery.nexusmods.com", "other.nexusmods.com"]
    for target in ("https://evil.example/b.png", "http://other.nexusmods.com/b.png", "https://nexusmods.com.evil.io/b.png"):
        c2 = client(lambda req, t=target: httpx.Response(302, headers={"location": t}))
        assert ni.fetch_image(URL.replace("9", "r" + str(abs(hash(target)))), c2) is None and len(c2.seen) == 1


def test_redirect_loops_are_bounded():
    c = client(lambda req: httpx.Response(302, headers={"location": "https://a.nexusmods.com/x.png"}))
    assert ni.fetch_image("https://a.nexusmods.com/loop.png", c) is None and len(c.seen) == ni.MAX_REDIRECTS + 1


def test_wrong_magic_oversize_and_bad_status_are_refused():
    for resp in (httpx.Response(200, content=b"<svg onload=x/>", headers={"content-type": "image/png"}),
                 httpx.Response(200, content=b"GIF89a" + b"\0" * 20, headers={"content-type": "image/gif"}),
                 httpx.Response(200, content=PNG + b"\0" * (ni.MAX_IMAGE_BYTES + 1), headers={"content-type": "image/png"}),
                 httpx.Response(200, content=PNG, headers={"content-type": "text/html"}),
                 httpx.Response(404, content=PNG)):
        c = client(lambda req, r=resp: r)
        assert ni.fetch_image(URL.replace("9", str(id(resp))), c) is None
    assert not list((ni.cache_dir() / "nexus" / "img").glob("*")) if (ni.cache_dir() / "nexus" / "img").exists() else True


def test_network_errors_return_none():
    def boom(req):
        raise httpx.ConnectError("down")

    assert ni.fetch_image(URL, client(boom)) is None


def test_cached_image_never_reads_non_images(tmp_path):
    p = ni._image_path(URL, ".png")
    p.parent.mkdir(parents=True)
    p.write_bytes(b"not an image")
    assert ni.cached_image(URL) is None and json.dumps(ni.cached_image("https://x.com/a.png")) == "null"
