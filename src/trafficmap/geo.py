"""GeoIP lookups and the local anchor.

Every remote address on the map needs a place: a point on the world map
plus a label (country, city, ASN, network name).  Two MaxMind databases
answer that, a city database with the coordinates and the names, and an
ASN database with the network of the address.

Local addresses
---------------
Private, loopback, link-local, multicast and unspecified addresses never
reach the map.  The list of networks is explicit on purpose:
:func:`ipaddress.is_private` treats the CGNAT range (100.64.0.0/10,
where Tailscale lives) differently from release to release, and this
module has to count it as local.

An IPv4-mapped IPv6 address (``::ffff:8.8.8.8``) is an IPv4 address in
disguise, and it is treated as one everywhere: it is classified as
local or public like the address it hides, and the databases are asked
about the address it hides too.

Databases
---------
DB-IP Lite is the default, but the reader does not care about the
vendor.  The field names of GeoLite2 (``location.latitude``,
``country.iso_code``, ``city.names.en``) and the DB-IP ones are the
same, so a swapped-in GeoLite2 file keeps working.  A field that is
missing simply leaves its value empty.

The cache
---------
The place of an address is kept in memory.  A lock guards the cache:
the collector thread fills it while the user interface reads it.  The
lock is never held while a database is read, so a slow lookup does not
block the other threads.

The anchor
----------
The anchor is our own address on the map.  Its IP is asked from
``api.ipify.org`` (``ifconfig.me/ip`` as a fallback) and cached in
``<cache dir>/anchor.json`` for an hour, so the app starts without a
network round trip.  The whole search has a deadline of
:data:`HTTP_TIMEOUT` seconds: a service that answers late is of no use
to a program that draws a map.  The cache file is checked before it is
used, a broken one is ignored.  Every network failure is swallowed: the
anchor is a convenience, and the map works without it.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import maxminddb

__all__ = ["Geo", "GeoResolver", "is_local"]

log = logging.getLogger(__name__)

#: Country code of an address whose country is not known.
UNKNOWN_COUNTRY = "--"

#: Networks that never reach the map: private, loopback, link-local,
#: multicast, unspecified and the CGNAT range.  The list is explicit on
#: purpose, see the module docstring.
LOCAL_NETS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)

#: Services that report the public IP of the caller, in order.
PUBLIC_IP_URLS = ("https://api.ipify.org", "https://ifconfig.me/ip")

#: Seconds an anchor stays valid in the cache file.
ANCHOR_TTL = 3600.0

#: Seconds the whole search for the public address may take.
HTTP_TIMEOUT = 2.0

#: Smallest timeout given to a request, so a nearly spent budget still
#: tries the service instead of giving up right away.
MIN_HTTP_TIMEOUT = 0.1

#: Range of a latitude.
LAT_RANGE = (-90.0, 90.0)

#: Range of a longitude.
LON_RANGE = (-180.0, 180.0)

#: Maximum number of addresses kept in the in-memory cache.
CACHE_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class Geo:
    """The place of one IP address.

    Attributes:
        ip: The address itself.
        lat: Latitude in degrees, or ``None`` when it is unknown.
        lon: Longitude in degrees, or ``None`` when it is unknown.
        country: ISO 3166-1 alpha-2 code, or ``"--"`` when unknown.
        city: City name in English, or ``None`` when unknown.
        asn: Autonomous system number, or ``None`` when unknown.
        org: Name of the network, or ``None`` when unknown.
    """

    ip: str
    lat: float | None
    lon: float | None
    country: str
    city: str | None
    asn: int | None
    org: str | None


def is_local(ip: str) -> bool:
    """Tell whether an address stays off the map.

    Args:
        ip: The address to check.

    Returns:
        ``True`` for the private, loopback, link-local, multicast and
        unspecified ranges, and also for a malformed address, which is
        unusable anyway.
    """
    address = _parse_address(ip.strip() if isinstance(ip, str) else str(ip))
    if address is None:
        return True
    return _in_local_net(_unmap(address))


def _parse_address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the address a text holds, or ``None`` when it holds none."""
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _unmap(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Return the IPv4 address hidden in an IPv4-mapped IPv6 address."""
    if address.version == 6 and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _in_local_net(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Tell whether an address belongs to one of the local networks."""
    return any(address in network for network in LOCAL_NETS)


def _normalize(ip: str) -> str:
    """Return the text of an address, with IPv4-mapped IPv6 unmapped.

    Args:
        ip: The address to normalize.

    Returns:
        The IPv4 address an IPv4-mapped IPv6 address hides, and the text
        itself for every other address.  A text that is not an address is
        kept as it is.
    """
    text = ip.strip() if isinstance(ip, str) else str(ip)
    address = _parse_address(text)
    if address is None:
        return text
    if address.version == 6 and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return text


class GeoResolver:
    """Resolve addresses to places, and find the anchor of the machine.

    Args:
        city_db: Path of the city MaxMind database.
        asn_db: Path of the ASN MaxMind database.  It is optional and
            skipped when the file does not exist.
        cache_dir: Directory of the anchor cache file.  Without it the
            anchor is resolved on every call and never cached.

    Raises:
        FileNotFoundError: If the city database does not exist.
        ValueError: If a database exists but cannot be read.
    """

    def __init__(
        self,
        city_db: Path,
        asn_db: Path | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self._city: maxminddb.Reader = _open_database(Path(city_db), "city")
        self._asn: maxminddb.Reader | None = None
        if asn_db is not None:
            asn_path = Path(asn_db)
            if asn_path.exists():
                self._asn = _open_database(asn_path, "asn")
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._cache: OrderedDict[str, Geo] = OrderedDict()
        self._cache_lock = threading.Lock()

    def lookup(self, ip: str) -> Geo:
        """Resolve one address to its place.

        Args:
            ip: The address to resolve.

        Returns:
            The place of the address.  A local address gets no
            coordinates and no country, an address that is not in the
            databases gets no coordinates either, but keeps its ASN
            when the ASN database knows it.
        """
        key = _normalize(ip)

        cached = self._cache_get(key)
        if cached is not None:
            return cached

        address = _parse_address(key)
        if address is None or _in_local_net(address):
            geo = Geo(key, None, None, UNKNOWN_COUNTRY, None, None, None)
        else:
            # The databases are read without the lock, a lookup of a
            # single address must not hold up the other threads.
            geo = self._lookup_public(key)

        self._cache_put(key, geo)
        return geo

    def _cache_get(self, key: str) -> Geo | None:
        """Return the cached place of an address, if there is one.

        The entry is marked as the most recently used one, so the oldest
        one is the first to go when the cache is full.
        """
        with self._cache_lock:
            geo = self._cache.get(key)
            if geo is not None:
                self._cache.move_to_end(key)
            return geo

    def _cache_put(self, key: str, geo: Geo) -> None:
        """Store the place of an address and keep the cache bounded."""
        with self._cache_lock:
            self._cache[key] = geo
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_LIMIT:
                try:
                    self._cache.popitem(last=False)
                except KeyError:
                    # Another thread emptied the cache, nothing to evict.
                    break

    def anchor(self) -> Geo | None:
        """Resolve the public address of this machine.

        The result is read from the cache file when it is still valid,
        so a warm start costs no network round trip.

        Returns:
            The place of this machine, or ``None`` when the address
            cannot be found.  No network or parse error is raised.
        """
        deadline = time.monotonic() + HTTP_TIMEOUT

        cached = self._read_anchor_cache()
        if cached is not None:
            log.debug("anchor from cache: %s", cached.ip)
            return cached

        ip = _fetch_public_ip(deadline=deadline)
        if ip is None:
            log.debug("cannot resolve the public address")
            return None

        geo = self.lookup(ip)
        self._write_anchor_cache(geo)
        return geo

    def _lookup_public(self, ip: str) -> Geo:
        """Look a public address up in both databases."""
        record = _record(self._city, ip)
        lat = lon = None
        country = UNKNOWN_COUNTRY
        country_name = None
        city = None
        if record:
            location = record.get("location")
            if not isinstance(location, dict):
                location = {}
            lat = _as_float(location.get("latitude"))
            if lat is None:
                lat = _as_float(record.get("latitude"))
            lon = _as_float(location.get("longitude"))
            if lon is None:
                lon = _as_float(record.get("longitude"))

            country_record = record.get("country")
            if not isinstance(country_record, dict):
                country_record = {}
            code = country_record.get("iso_code")
            if not isinstance(code, str) or not code:
                code = country_record.get("code")
            if isinstance(code, str) and code:
                country = code
            country_name = _english_name(country_record)

            city_record = record.get("city")
            if isinstance(city_record, dict):
                city = _english_name(city_record)

        asn, org = self._lookup_asn(ip)
        log.debug("%s: %s, %s, asn %s", ip, country_name or country, city or "-", asn)
        return Geo(ip, lat, lon, country, city, asn, org)

    def _lookup_asn(self, ip: str) -> tuple[int | None, str | None]:
        """Return the autonomous system of an address, if it is known."""
        record = _record(self._asn, ip)
        if not record:
            return None, None
        asn = _as_int(record.get("autonomous_system_number"))
        org = record.get("autonomous_system_organization")
        return asn, org if isinstance(org, str) and org else None

    def _anchor_path(self) -> Path | None:
        """Return the path of the anchor cache file, if it is used."""
        if self._cache_dir is None:
            return None
        return self._cache_dir / "anchor.json"

    def _read_anchor_cache(self) -> Geo | None:
        """Return the cached anchor, when there is a valid one."""
        path = self._anchor_path()
        if path is None:
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.debug("cannot read the anchor cache %s: %s", path, exc)
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.debug("broken anchor cache %s: %s", path, exc)
            return None
        if not isinstance(data, dict):
            return None

        expires_at = data.get("expires_at")
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(expires_at)
            or expires_at <= time.time()
        ):
            log.debug("the anchor cache %s is expired", path)
            return None

        geo = _anchor_from_payload(data)
        if geo is None:
            log.debug("the anchor cache %s holds broken data", path)
        return geo

    def _write_anchor_cache(self, geo: Geo) -> None:
        """Store the anchor in the cache file."""
        path = self._anchor_path()
        if path is None:
            return
        payload = {
            "ip": geo.ip,
            "lat": geo.lat,
            "lon": geo.lon,
            "country": geo.country,
            "city": geo.city,
            "asn": geo.asn,
            "org": geo.org,
            "expires_at": time.time() + ANCHOR_TTL,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                mode="w",
                encoding="utf-8",
                delete=False,
            )
            try:
                with handle:
                    json.dump(payload, handle)
                os.replace(handle.name, path)
            except BaseException:
                Path(handle.name).unlink(missing_ok=True)
                raise
        except (OSError, TypeError, ValueError) as exc:
            log.debug("cannot write the anchor cache %s: %s", path, exc)


def _open_database(path: Path, kind: str) -> maxminddb.Reader:
    """Open a MaxMind database.

    Args:
        path: Path of the database file.
        kind: What the database holds, for the error message.

    Returns:
        The reader of the database.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file exists but is not a readable database.
    """
    if not path.exists():
        raise FileNotFoundError(f"{kind} database not found: {path}")
    try:
        return maxminddb.open_database(str(path))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{kind} database not found: {path}") from exc
    except maxminddb.InvalidDatabaseError as exc:
        raise ValueError(f"{kind} database {path} is not readable: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"{kind} database {path} cannot be read: {exc}") from exc


def _fetch_public_ip(
    timeout: float | None = None, deadline: float | None = None
) -> str | None:
    """Ask a service for the public address of this machine.

    The whole search has one budget, not one per request: a service that
    answers late is of no use, and the next one has to be asked right
    away.

    Args:
        timeout: Seconds the whole search may take.  It is
            :data:`HTTP_TIMEOUT` when it is missing.
        deadline: Moment the search has to end, counted the way
            :func:`time.monotonic` counts.  It is computed from
            ``timeout`` when it is missing.

    Returns:
        The address, or ``None`` when every service failed or the budget
        ran out.
    """
    if deadline is None:
        if timeout is None:
            timeout = HTTP_TIMEOUT
        deadline = time.monotonic() + timeout

    for url in PUBLIC_IP_URLS:
        try:
            candidate = _call_before(deadline, _public_ip_from, url, deadline)
        except TimeoutError as exc:
            log.debug("public address from %s did not arrive in time: %s", url, exc)
            return None
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            OSError,
            ValueError,
        ) as exc:
            log.debug("public address from %s failed: %s", url, exc)
            continue
        if candidate is None:
            continue
        return candidate
    return None


def _public_ip_from(url: str, deadline: float) -> str | None:
    """Ask one service for the public address, within the deadline.

    Args:
        url: Address of the service.
        deadline: Moment the request has to end.

    Returns:
        The address, or ``None`` when the service failed or answered
        something that is not an address.
    """
    remaining = deadline - time.monotonic()
    request = urllib.request.Request(
        url, headers={"User-Agent": "trafficmap/0.1 (+https://example.invalid)"}
    )
    try:
        with urllib.request.urlopen(
            request, timeout=max(MIN_HTTP_TIMEOUT, remaining)
        ) as response:
            # The timeout of a socket covers one operation, not the whole
            # answer, so the rest of the budget is given to the body.
            _set_socket_timeout(response, deadline - time.monotonic())
            raw = response.read(64).decode("utf-8", "replace")
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        ValueError,
    ) as exc:
        log.debug("public address from %s failed: %s", url, exc)
        return None
    candidate = raw.strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        log.debug("public address from %s is not an address: %r", url, candidate)
        return None
    return candidate


def _call_before(deadline: float, function: Callable[..., Any], *args: Any) -> Any:
    """Run a function and stop waiting for it at the deadline.

    The function may block in a way no timeout covers, a name lookup
    that never ends for example.  It runs in a helper thread, so the
    wait for it stays bounded.

    Args:
        deadline: Moment the call has to end.
        function: The function to run.
        args: The arguments of the function.

    Returns:
        Whatever the function returned.

    Raises:
        TimeoutError: When the deadline passed before the call ended.
        BaseException: Whatever the function raised.
    """
    outcome: list[Any] = []

    def worker() -> None:
        try:
            outcome.append(function(*args))
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            outcome.append(exc)

    thread = threading.Thread(target=worker, name="trafficmap-anchor", daemon=True)
    thread.start()
    thread.join(max(0.0, deadline - time.monotonic()))
    if thread.is_alive():
        raise TimeoutError("the deadline passed")
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


def _set_socket_timeout(response: object, seconds: float) -> None:
    """Set the timeout of the socket of a response, when it has one.

    Args:
        response: The response to look at.
        seconds: The timeout the socket gets.
    """
    if seconds <= 0:
        seconds = MIN_HTTP_TIMEOUT
    stream = getattr(response, "fp", None)
    raw = getattr(stream, "raw", None)
    for sock in (
        getattr(raw, "_sock", None),
        getattr(stream, "_sock", None),
        raw,
    ):
        settimeout = getattr(sock, "settimeout", None)
        if settimeout is None:
            continue
        try:
            settimeout(seconds)
            return
        except (OSError, ValueError) as exc:
            log.debug("cannot set the timeout of the socket: %s", exc)


def _anchor_from_payload(data: dict) -> Geo | None:
    """Build the anchor of a cache file, or ``None`` when it is broken.

    A hand-edited file may hold anything, and a broken coordinate puts
    the map in the middle of the ocean.

    Args:
        data: The content of the cache file.

    Returns:
        The anchor, or ``None`` when the content is not a usable one.
    """
    ip = data.get("ip")
    if not isinstance(ip, str):
        return None
    ip = ip.strip()
    if _parse_address(ip) is None:
        return None

    try:
        lat = _coordinate(data.get("lat"), LAT_RANGE)
        lon = _coordinate(data.get("lon"), LON_RANGE)
    except ValueError as exc:
        log.debug("broken anchor cache: %s", exc)
        return None

    country = data.get("country", UNKNOWN_COUNTRY)
    if not isinstance(country, str):
        log.debug("broken anchor cache: the country is not a text")
        return None

    return Geo(
        ip=ip,
        lat=lat,
        lon=lon,
        country=country or UNKNOWN_COUNTRY,
        city=_as_optional_str(data.get("city")),
        asn=_as_int(data.get("asn")),
        org=_as_optional_str(data.get("org")),
    )


def _coordinate(value: object, limits: tuple[float, float]) -> float | None:
    """Return a coordinate of the cache file, or ``None`` when it has none.

    Args:
        value: The value of the coordinate.
        limits: The lowest and the highest value it may have.

    Returns:
        The coordinate, or ``None`` when the file holds none.

    Raises:
        ValueError: When the value is not a usable coordinate.
    """
    if value is None:
        return None
    number = _as_float(value)
    if number is None:
        raise ValueError(f"not a number: {value!r}")
    if not math.isfinite(number):
        raise ValueError(f"not a finite number: {value!r}")
    low, high = limits
    if not (low <= number <= high):
        raise ValueError(f"out of range: {number!r}")
    return number


def _record(reader: maxminddb.Reader | None, ip: str) -> dict | None:
    """Return the record of an address, or ``None`` when there is none."""
    if reader is None:
        return None
    try:
        record = reader.get(ip)
    except ValueError as exc:
        # An IPv6 address in an IPv4-only database, for example.
        log.debug("no record for %s: %s", ip, exc)
        return None
    except maxminddb.InvalidDatabaseError as exc:
        log.debug("broken database while reading %s: %s", ip, exc)
        return None
    return record if isinstance(record, dict) else None


def _english_name(record: dict) -> str | None:
    """Return the English name of a record, if it has one."""
    names = record.get("names")
    if not isinstance(names, dict):
        return None
    name = names.get("en")
    return name if isinstance(name, str) and name else None


def _as_float(value: object) -> float | None:
    """Return a number as a float, or ``None`` when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_int(value: object) -> int | None:
    """Return a number as an int, or ``None`` when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_optional_str(value: object) -> str | None:
    """Return a string, or ``None`` when the value is not one."""
    return value if isinstance(value, str) and value else None
