"""Tests of the GeoIP resolver and of the anchor.

Nothing here touches the network: the HTTP calls are replaced by stubs,
and the databases are either the downloaded ones or a stand-in reader.
"""

import json
import threading
import time
from pathlib import Path

import pytest

from trafficmap import geo
from trafficmap.geo import Geo, GeoResolver, is_local

ASSETS = Path(__file__).resolve().parents[1] / "assets"
CITY_DB = ASSETS / "dbip-city-lite.mmdb"
ASN_DB = ASSETS / "dbip-asn-lite.mmdb"

needs_city_db = pytest.mark.skipif(
    not CITY_DB.exists(), reason="the city database is not downloaded"
)
needs_asn_db = pytest.mark.skipif(
    not ASN_DB.exists(), reason="the ASN database is not downloaded"
)

CITY_RECORD = {
    "city": {"names": {"en": "Mountain View"}},
    "country": {"iso_code": "US", "names": {"en": "United States"}},
    "location": {"latitude": 37.422, "longitude": -122.085},
}
ASN_RECORD = {
    "autonomous_system_number": 15169,
    "autonomous_system_organization": "Google LLC",
}


class FakeReader:
    """A stand-in for the MaxMind reader."""

    def __init__(self, records):
        self.records = records
        self.asked = []

    def get(self, ip):
        self.asked.append(ip)
        return self.records.get(ip)


def make_resolver(monkeypatch, tmp_path, city=None, asn=None):
    """Build a resolver whose databases are stubs.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: Directory of the anchor cache.
        city: Records of the city database.
        asn: Records of the ASN database.

    Returns:
        The resolver, the city reader and the ASN reader.
    """
    city_reader = FakeReader(city if city is not None else {})
    asn_reader = FakeReader(asn if asn is not None else {})
    opened = []

    def fake_open(path, kind):
        opened.append((Path(path), kind))
        return city_reader if kind == "city" else asn_reader

    monkeypatch.setattr(geo, "_open_database", fake_open)
    resolver = GeoResolver(CITY_DB, ASN_DB, tmp_path)
    return resolver, city_reader, asn_reader, opened


@pytest.fixture
def resolver(monkeypatch, tmp_path):
    """A resolver with one known address in both databases."""
    resolver, city_reader, asn_reader, _ = make_resolver(
        monkeypatch,
        tmp_path,
        city={"8.8.8.8": CITY_RECORD},
        asn={"8.8.8.8": ASN_RECORD},
    )
    resolver.city_reader = city_reader
    resolver.asn_reader = asn_reader
    return resolver


def no_network(monkeypatch):
    """Make every HTTP call fail loudly."""

    def boom(*args, **kwargs):
        raise AssertionError("the test must not use the network")

    monkeypatch.setattr(geo.urllib.request, "urlopen", boom)


def failing_network(monkeypatch):
    """Make every HTTP call fail the way an unreachable network does."""

    def boom(*args, **kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(geo.urllib.request, "urlopen", boom)


@pytest.mark.parametrize(
    "ip",
    [
        "10.1.2.3",
        "172.19.0.5",
        "192.168.1.1",
        "100.64.0.7",
        "127.0.0.1",
        "169.254.1.1",
        "fd00::1",
        "fe80::1",
        "::1",
    ],
)
def test_local_ranges(ip):
    assert is_local(ip)


def test_public_ip_is_not_local():
    assert not is_local("1.1.1.1")


@pytest.mark.parametrize(
    "ip",
    [
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "ff02::1",
        "10.0.0.0",
        "172.31.255.255",
        "192.168.0.0",
        "100.127.255.255",
        "169.254.0.0",
        "255.255.255.255",
    ],
)
def test_other_local_ranges(ip):
    assert is_local(ip)


@pytest.mark.parametrize(
    "ip",
    ["1.1.1.1", "8.8.8.8", "172.32.0.1", "100.128.0.1", "11.0.0.1", "2001:db8::1"],
)
def test_public_ranges_are_not_local(ip):
    assert not is_local(ip)


@pytest.mark.parametrize("ip", ["", "not-an-ip", "999.1.1.1", "1.2.3", "8.8.8.8/32"])
def test_malformed_ip_is_local(ip):
    assert is_local(ip)


def test_malformed_ip_lookup(resolver):
    geo_ = resolver.lookup("not-an-ip")
    assert geo_.lat is None
    assert geo_.lon is None
    assert geo_.country == "--"
    assert resolver.city_reader.asked == []


def test_lookup_of_a_public_address(resolver):
    geo_ = resolver.lookup("8.8.8.8")
    assert geo_ == Geo(
        ip="8.8.8.8",
        lat=37.422,
        lon=-122.085,
        country="US",
        city="Mountain View",
        asn=15169,
        org="Google LLC",
    )


def test_lookup_of_an_unknown_address(resolver):
    geo_ = resolver.lookup("203.0.113.7")
    assert geo_.lat is None
    assert geo_.lon is None
    assert geo_.country == "--"
    assert geo_.city is None
    assert geo_.asn is None
    assert geo_.org is None


def test_lookup_of_an_unknown_address_keeps_the_asn(monkeypatch, tmp_path):
    resolver, _, _, _ = make_resolver(
        monkeypatch, tmp_path, city={}, asn={"203.0.113.7": ASN_RECORD}
    )
    geo_ = resolver.lookup("203.0.113.7")
    assert geo_.country == "--"
    assert geo_.lat is None
    assert geo_.asn == 15169
    assert geo_.org == "Google LLC"


def test_lookup_of_a_local_address_skips_the_database(resolver):
    geo_ = resolver.lookup("192.168.1.1")
    assert geo_ == Geo("192.168.1.1", None, None, "--", None, None, None)
    assert resolver.city_reader.asked == []
    assert resolver.asn_reader.asked == []


def test_lookup_caches_the_result(resolver):
    first = resolver.lookup("8.8.8.8")
    second = resolver.lookup("8.8.8.8")
    assert first is second
    assert resolver.city_reader.asked == ["8.8.8.8"]


def test_lookup_of_a_city_record_without_a_location(monkeypatch, tmp_path):
    resolver, _, _, _ = make_resolver(
        monkeypatch,
        tmp_path,
        city={"9.9.9.9": {"country": {"iso_code": "CH"}}},
    )
    geo_ = resolver.lookup("9.9.9.9")
    assert geo_.country == "CH"
    assert geo_.lat is None
    assert geo_.lon is None


def test_lookup_falls_back_to_the_country_code(monkeypatch, tmp_path):
    resolver, _, _, _ = make_resolver(
        monkeypatch,
        tmp_path,
        city={"9.9.9.9": {"country": {"code": "CH", "names": {"en": "Switzerland"}}}},
    )
    assert resolver.lookup("9.9.9.9").country == "CH"


def test_lookup_falls_back_to_the_top_level_coordinates(monkeypatch, tmp_path):
    resolver, _, _, _ = make_resolver(
        monkeypatch,
        tmp_path,
        city={"9.9.9.9": {"latitude": 47.0, "longitude": 8.0}},
    )
    geo_ = resolver.lookup("9.9.9.9")
    assert (geo_.lat, geo_.lon) == (47.0, 8.0)


def test_anchor_from_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_open_database", lambda path, kind: FakeReader({}))
    no_network(monkeypatch)
    cache = tmp_path / "anchor.json"
    cache.write_text(
        json.dumps(
            {
                "ip": "203.0.113.9",
                "lat": 10.0,
                "lon": 20.0,
                "country": "US",
                "city": "Nowhere",
                "asn": 64500,
                "org": "Example",
                "expires_at": time.time() + 600,
            }
        ),
        encoding="utf-8",
    )

    resolver = GeoResolver(CITY_DB, ASN_DB, tmp_path)
    assert resolver.anchor() == Geo(
        ip="203.0.113.9",
        lat=10.0,
        lon=20.0,
        country="US",
        city="Nowhere",
        asn=64500,
        org="Example",
    )


def test_anchor_with_an_expired_cache_and_no_network(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_open_database", lambda path, kind: FakeReader({}))
    failing_network(monkeypatch)
    cache = tmp_path / "anchor.json"
    cache.write_text(
        json.dumps(
            {
                "ip": "203.0.113.9",
                "lat": 10.0,
                "lon": 20.0,
                "country": "US",
                "city": "Nowhere",
                "asn": 64500,
                "org": "Example",
                "expires_at": time.time() - 1,
            }
        ),
        encoding="utf-8",
    )

    resolver = GeoResolver(CITY_DB, ASN_DB, tmp_path)
    assert resolver.anchor() is None


def test_anchor_with_a_broken_cache_file(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_open_database", lambda path, kind: FakeReader({}))
    failing_network(monkeypatch)
    (tmp_path / "anchor.json").write_text("not json", encoding="utf-8")

    resolver = GeoResolver(CITY_DB, ASN_DB, tmp_path)
    assert resolver.anchor() is None


def test_anchor_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_open_database", lambda path, kind: FakeReader({}))
    monkeypatch.setattr(geo, "_fetch_public_ip", lambda *args, **kwargs: "203.0.113.9")

    resolver = GeoResolver(CITY_DB, ASN_DB, tmp_path)
    first = resolver.anchor()
    assert first == Geo(
        ip="203.0.113.9",
        lat=None,
        lon=None,
        country="--",
        city=None,
        asn=None,
        org=None,
    )

    cache = json.loads((tmp_path / "anchor.json").read_text(encoding="utf-8"))
    assert cache["ip"] == "203.0.113.9"
    assert cache["country"] == "--"
    assert cache["expires_at"] > time.time()

    no_network(monkeypatch)
    second = GeoResolver(CITY_DB, ASN_DB, tmp_path).anchor()
    assert second == first


def test_anchor_without_a_cache_dir_does_not_write_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_open_database", lambda path, kind: FakeReader({}))
    monkeypatch.setattr(geo, "_fetch_public_ip", lambda *args, **kwargs: "203.0.113.9")
    # The resolver must not write anything next to the working directory.
    monkeypatch.chdir(tmp_path)

    resolver = GeoResolver(CITY_DB, ASN_DB, None)
    assert resolver.anchor() is not None
    assert list(tmp_path.iterdir()) == []


def test_anchor_returns_none_when_the_network_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_open_database", lambda path, kind: FakeReader({}))

    def boom(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(geo.urllib.request, "urlopen", boom)

    resolver = GeoResolver(CITY_DB, ASN_DB, tmp_path)
    assert resolver.anchor() is None


def test_public_ip_falls_back_to_the_second_service(monkeypatch):
    calls = []

    class Response:
        def __init__(self, text):
            self.text = text

        def read(self, size=0):
            return self.text.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        calls.append(url)
        if url == geo.PUBLIC_IP_URLS[0]:
            raise OSError("no route to host")
        return Response(" 203.0.113.9\n")

    monkeypatch.setattr(geo.urllib.request, "urlopen", fake_urlopen)
    assert geo._fetch_public_ip() == "203.0.113.9"
    assert calls == list(geo.PUBLIC_IP_URLS)


def test_public_ip_ignores_a_garbage_answer(monkeypatch):
    class Response:
        def read(self, size=0):
            return b"not an address"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(geo.urllib.request, "urlopen", lambda *a, **k: Response())
    assert geo._fetch_public_ip() is None


def test_the_request_names_the_program_and_nothing_else(monkeypatch):
    """The User-Agent does not point at a site that does not exist."""
    agents = []

    class Response:
        def read(self, size=0):
            return b"203.0.113.9"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        agents.append(request.get_header("User-agent"))
        return Response()

    monkeypatch.setattr(geo.urllib.request, "urlopen", fake_urlopen)
    assert geo._fetch_public_ip() == "203.0.113.9"
    assert agents == ["trafficmap/0.1"]


def test_missing_city_database_raises(tmp_path):
    with pytest.raises(FileNotFoundError) as info:
        GeoResolver(tmp_path / "missing.mmdb")
    assert "missing.mmdb" in str(info.value)


def test_broken_city_database_raises(tmp_path):
    broken = tmp_path / "broken.mmdb"
    broken.write_bytes(b"not a database at all")
    with pytest.raises(ValueError) as info:
        GeoResolver(broken)
    assert "broken.mmdb" in str(info.value)


def test_a_missing_asn_database_is_skipped(monkeypatch, tmp_path):
    opened = []

    def fake_open(path, kind):
        opened.append(kind)
        return FakeReader({})

    monkeypatch.setattr(geo, "_open_database", fake_open)
    resolver = GeoResolver(CITY_DB, tmp_path / "missing-asn.mmdb", tmp_path)
    assert opened == ["city"]
    assert resolver.lookup("8.8.8.8").asn is None


@needs_city_db
def test_real_database_lookup():
    resolver = GeoResolver(CITY_DB, ASN_DB)
    geo_ = resolver.lookup("8.8.8.8")
    assert geo_.country == "US"
    assert geo_.city == "Mountain View"
    assert geo_.lat == pytest.approx(37.422)
    assert geo_.lon == pytest.approx(-122.085)


@needs_city_db
def test_real_database_lookup_of_an_unknown_address():
    resolver = GeoResolver(CITY_DB, ASN_DB)
    geo_ = resolver.lookup("203.0.113.7")
    assert geo_.country == "--"
    assert geo_.lat is None


@needs_city_db
@needs_asn_db
def test_real_database_asn_lookup():
    resolver = GeoResolver(CITY_DB, ASN_DB)
    geo_ = resolver.lookup("8.8.8.8")
    assert geo_.asn == 15169
    assert geo_.org == "Google LLC"


def public_ip(number):
    """Return a distinct public address for a number.

    The addresses come from the benchmark range 198.18.0.0/15, they are
    not local and not in the databases either.
    """
    return f"198.{18 + number // 65536}.{number // 256 % 256}.{number % 256}"


def test_the_module_constants():
    assert geo.HTTP_TIMEOUT == 2.0
    assert geo.ANCHOR_TTL == 3600


def test_an_ipv4_mapped_address_is_local():
    assert is_local("::ffff:127.0.0.1")


def test_an_ipv4_mapped_address_is_looked_up_as_ipv4(monkeypatch, tmp_path):
    resolver, city_reader, asn_reader, _ = make_resolver(
        monkeypatch,
        tmp_path,
        city={"8.8.8.8": CITY_RECORD},
        asn={"8.8.8.8": ASN_RECORD},
    )

    mapped = resolver.lookup("::ffff:8.8.8.8")
    plain = resolver.lookup("8.8.8.8")

    assert mapped == plain
    assert mapped.country == "US"
    # The databases are asked about the address the mapped one hides.
    assert city_reader.asked == ["8.8.8.8"]
    assert asn_reader.asked == ["8.8.8.8"]


def test_anchor_cache_roundtrip_keeps_the_coordinates(tmp_path, monkeypatch):
    resolver, _, _, _ = make_resolver(
        monkeypatch,
        tmp_path,
        city={"203.0.113.9": CITY_RECORD},
    )
    monkeypatch.setattr(geo, "_fetch_public_ip", lambda *args, **kwargs: "203.0.113.9")

    first = resolver.anchor()
    assert (first.lat, first.lon) == (37.422, -122.085)

    cache = json.loads((tmp_path / "anchor.json").read_text(encoding="utf-8"))
    assert cache["lat"] == 37.422
    assert cache["lon"] == -122.085

    no_network(monkeypatch)
    second = GeoResolver(CITY_DB, ASN_DB, tmp_path).anchor()
    assert second == first
    assert (second.lat, second.lon) == (37.422, -122.085)
    # The two coordinates differ, a swap cannot pass unnoticed.
    assert second.lat != second.lon


@pytest.mark.parametrize(
    "entry",
    [
        {"ip": "203.0.113.9", "lat": float("nan"), "lon": 20.0},
        {"ip": "203.0.113.9", "lat": 10.0, "lon": float("inf")},
        {"ip": "203.0.113.9", "lat": 999.0, "lon": 20.0},
        {"ip": "203.0.113.9", "lat": 10.0, "lon": 999.0},
        {"ip": "not-an-ip", "lat": 10.0, "lon": 20.0},
    ],
)
def test_a_broken_anchor_cache_is_ignored(tmp_path, monkeypatch, entry):
    monkeypatch.setattr(geo, "_open_database", lambda path, kind: FakeReader({}))
    failing_network(monkeypatch)
    payload = dict(entry)
    payload["country"] = "US"
    payload["expires_at"] = time.time() + 600
    (tmp_path / "anchor.json").write_text(json.dumps(payload), encoding="utf-8")

    resolver = GeoResolver(CITY_DB, ASN_DB, tmp_path)
    assert resolver.anchor() is None


def test_an_anchor_cache_without_a_country_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_open_database", lambda path, kind: FakeReader({}))
    failing_network(monkeypatch)
    (tmp_path / "anchor.json").write_text(
        json.dumps(
            {
                "ip": "203.0.113.9",
                "lat": 10.0,
                "lon": 20.0,
                "country": 42,
                "expires_at": time.time() + 600,
            }
        ),
        encoding="utf-8",
    )

    resolver = GeoResolver(CITY_DB, ASN_DB, tmp_path)
    assert resolver.anchor() is None


def test_the_cache_stays_bounded(monkeypatch, tmp_path):
    resolver, _, _, _ = make_resolver(monkeypatch, tmp_path)

    for number in range(geo.CACHE_LIMIT + 100):
        resolver.lookup(public_ip(number))

    assert len(resolver._cache) <= geo.CACHE_LIMIT


def test_the_cache_is_safe_to_use_from_many_threads(monkeypatch, tmp_path):
    resolver, _, _, _ = make_resolver(monkeypatch, tmp_path)
    errors = []
    threads_count = 4
    lookups = 4000

    def worker(index):
        try:
            for number in range(lookups):
                resolver.lookup(public_ip(index * lookups + number))
                # A hit as well, the cache is written while it is read.
                resolver.lookup("8.8.8.8")
        except BaseException as exc:  # noqa: BLE001 - the test reports it
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(index,)) for index in range(threads_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert errors == []
    assert len(resolver._cache) <= geo.CACHE_LIMIT


def test_the_anchor_search_has_one_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(geo, "_open_database", lambda path, kind: FakeReader({}))

    def slow_urlopen(request, timeout=None):
        time.sleep(5)
        raise OSError("too late")

    monkeypatch.setattr(geo.urllib.request, "urlopen", slow_urlopen)

    resolver = GeoResolver(CITY_DB, ASN_DB, tmp_path)
    start = time.monotonic()
    assert resolver.anchor() is None
    assert time.monotonic() - start < 2.2


def test_the_body_is_read_with_the_rest_of_the_budget(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.timeout = None

        def settimeout(self, value):
            self.timeout = value

    class FakeRaw:
        def __init__(self, sock):
            self._sock = sock

    class FakeStream:
        def __init__(self, sock):
            self.raw = FakeRaw(sock)

    class Response:
        def __init__(self):
            self.sock = FakeSocket()
            self.fp = FakeStream(self.sock)

        def read(self, size=0):
            return b"203.0.113.9"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    response = Response()
    monkeypatch.setattr(geo.urllib.request, "urlopen", lambda *a, **k: response)

    assert geo._fetch_public_ip() == "203.0.113.9"
    assert response.sock.timeout is not None
    assert 0 < response.sock.timeout <= geo.HTTP_TIMEOUT
