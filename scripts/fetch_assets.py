#!/usr/bin/env python3
"""Download the offline assets the traffic map needs.

Six files are fetched into the ``assets`` directory of the repository:

* ``land.geojson`` -- Natural Earth 110m land polygons (public domain),
  the raster background of the world map;
* ``borders.geojson`` -- Natural Earth 110m country borders (public
  domain), drawn over the land when the map is zoomed in;
* ``countries.geojson`` -- Natural Earth 110m countries (public domain),
  the country names and the points their labels belong to;
* ``cities.geojson`` -- Natural Earth 50m populated places (public
  domain), the dots and names of the cities;
* ``dbip-city-lite.mmdb`` -- DB-IP Lite City database (CC BY 4.0);
* ``dbip-asn-lite.mmdb`` -- DB-IP Lite ASN database (CC BY 4.0).

The DB-IP databases are published per month.  The script asks for the
current month and falls back to older months while the server answers with
HTTP 403, 404 or 410 (DB-IP answers 403 for a month that has not been
published yet), at most three months back.

Only the standard library is used.  An asset that already exists and passes
validation is kept, so the script can be re-run safely; ``--force``
re-downloads everything.  The exit code is 0 when every asset is in place
and 1 when at least one of them failed.
"""

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

#: Timeout of a single HTTP request, in seconds.
TIMEOUT = 60.0

#: User agent sent with every request.
USER_AGENT = "trafficmap-asset-fetcher/0.1 (offline map assets; python-urllib)"

#: How many months back a monthly database is looked up.
MONTHS_BACK = 3

#: HTTP statuses that mean "this publication is not available (yet)".
#: DB-IP answers 403 for a month that has not been published yet.
MISSING_PUBLICATION_STATUSES = frozenset({403, 404, 410})

#: Marker bytes of the metadata section of every MaxMind DB file.
MMDB_MARKER = b"\xab\xcd\xefMaxMind.com"

#: Maximum size of the metadata section of a MaxMind DB file, in bytes.
#: The metadata marker sits at the very end of the file, right before it.
MMDB_METADATA_WINDOW = 128 * 1024

#: First bytes of a gzip stream.
GZIP_MARKER = b"\x1f\x8b"

#: Root of the repository, used as the default asset directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSETS_DIR = REPO_ROOT / "assets"

LAND_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
    "/master/geojson/ne_110m_land.geojson"
)
BORDERS_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
    "/master/geojson/ne_110m_admin_0_boundary_lines_land.geojson"
)
COUNTRIES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
    "/master/geojson/ne_110m_admin_0_countries.geojson"
)
CITIES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
    "/master/geojson/ne_50m_populated_places.geojson"
)
CITY_URL = "https://download.db-ip.com/free/dbip-city-lite-{ym}.mmdb.gz"
ASN_URL = "https://download.db-ip.com/free/dbip-asn-lite-{ym}.mmdb.gz"


class AssetError(Exception):
    """An asset could not be downloaded or did not pass validation.

    Attributes:
        status: HTTP status code of the failed request, when there was one.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Asset:
    """One downloadable file.

    Attributes:
        name: File name inside the asset directory.
        url: Download URL; ``{ym}`` is replaced by a ``YYYY-MM`` label.
        validate: Callable that raises :class:`AssetError` on bad content.
        compressed: Whether the payload is a gzip stream that has to be
            decompressed before it is written.
        monthly: Whether the URL depends on the publication month.
    """

    name: str
    url: str
    validate: Callable[[bytes], None]
    compressed: bool = False
    monthly: bool = False


def validate_geojson(data: bytes) -> None:
    """Check that the payload is a GeoJSON FeatureCollection.

    Args:
        data: Raw file content.

    Raises:
        AssetError: If the payload is not JSON or has no ``features`` key.
    """
    try:
        parsed = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssetError(f"not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or "features" not in parsed:
        raise AssetError("not a GeoJSON FeatureCollection: no 'features' key")


def validate_mmdb(data: bytes) -> None:
    """Check that the payload is a MaxMind DB file.

    A MaxMind DB starts with the binary search tree and ends with the
    metadata section, which is introduced by the marker bytes
    ``ab cd ef MaxMind.com``.  The metadata section is at most 128 KiB, so
    the marker is searched in the tail of the file.

    Args:
        data: Raw file content.

    Raises:
        AssetError: If the marker bytes are missing.
    """
    if data.startswith(MMDB_MARKER):
        return
    if MMDB_MARKER not in data[-MMDB_METADATA_WINDOW:]:
        raise AssetError(
            f"not a MaxMind DB file: the marker {MMDB_MARKER.hex(' ')} is "
            f"neither at the start nor in the last {MMDB_METADATA_WINDOW} bytes"
        )


ASSETS: tuple[Asset, ...] = (
    Asset(name="land.geojson", url=LAND_URL, validate=validate_geojson),
    Asset(name="borders.geojson", url=BORDERS_URL, validate=validate_geojson),
    Asset(name="countries.geojson", url=COUNTRIES_URL, validate=validate_geojson),
    Asset(name="cities.geojson", url=CITIES_URL, validate=validate_geojson),
    Asset(
        name="dbip-city-lite.mmdb",
        url=CITY_URL,
        validate=validate_mmdb,
        compressed=True,
        monthly=True,
    ),
    Asset(
        name="dbip-asn-lite.mmdb",
        url=ASN_URL,
        validate=validate_mmdb,
        compressed=True,
        monthly=True,
    ),
)


def month_labels(today: date | None = None) -> list[str]:
    """Return the month labels to try, newest first.

    Args:
        today: Date to count back from; the current date by default.

    Returns:
        ``YYYY-MM`` labels: the month of ``today`` followed by
        :data:`MONTHS_BACK` older months.
    """
    if today is None:
        today = date.today()
    labels = []
    year, month = today.year, today.month
    for _ in range(MONTHS_BACK + 1):
        labels.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    return labels


def fetch(url: str) -> bytes:
    """Download a URL and return the raw body.

    Args:
        url: Absolute URL to fetch.

    Returns:
        The response body.

    Raises:
        AssetError: If the request fails, the server answers with an
            error status or the transfer is cut short.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise AssetError(f"HTTP {exc.code} for {url}", status=exc.code) from exc
    except http.client.HTTPException as exc:
        # An incomplete transfer: the server closed the connection or the
        # body was shorter than announced.  This is not an OSError, so it
        # would escape the error handling of main() without this clause.
        detail = str(exc) or exc.__class__.__name__
        raise AssetError(f"incomplete transfer of {url}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AssetError(f"cannot fetch {url}: {exc}") from exc


def download(asset: Asset) -> tuple[str, bytes]:
    """Download an asset, falling back to older months when needed.

    A monthly publication that is not available yet is looked up in the
    older months, as long as the server answers with one of the statuses
    in :data:`MISSING_PUBLICATION_STATUSES`.

    Args:
        asset: The asset to download.

    Returns:
        A ``(url, payload)`` pair; the URL is the one the payload came from.

    Raises:
        AssetError: If no publication could be fetched.
    """
    if not asset.monthly:
        return asset.url, fetch(asset.url)
    missing = []
    for label in month_labels():
        url = asset.url.format(ym=label)
        try:
            return url, fetch(url)
        except AssetError as exc:
            # Only a missing publication is worth retrying with an older
            # month; every other error is a real failure.
            if exc.status not in MISSING_PUBLICATION_STATUSES:
                raise
            missing.append(label)
    raise AssetError(
        f"no publication found for {asset.name} in the last "
        f"{MONTHS_BACK + 1} months ({', '.join(missing)})"
    )


def decompress(payload: bytes) -> bytes:
    """Decompress a gzip payload.

    Args:
        payload: Raw response body.

    Returns:
        The decompressed bytes, or the payload itself when it is not a
        gzip stream.

    Raises:
        AssetError: If the payload is a broken gzip stream.
    """
    if not payload.startswith(GZIP_MARKER):
        return payload
    try:
        return gzip.decompress(payload)
    except (OSError, EOFError) as exc:
        raise AssetError(f"cannot decompress the gzip stream: {exc}") from exc


def fetch_asset(asset: Asset, dest: Path, force: bool = False) -> str:
    """Make sure the asset file exists and is valid.

    Args:
        asset: The asset to fetch.
        dest: Path of the file inside the asset directory.
        force: Re-download the file even when it already exists.

    Returns:
        A one line description of what happened.

    Raises:
        AssetError: If the asset could not be downloaded, written or
            validated.
    """
    if dest.exists() and not force:
        try:
            asset.validate(dest.read_bytes())
        except AssetError as exc:
            print(f"{asset.name}: replacing the invalid file ({exc})")
        else:
            size = dest.stat().st_size
            return f"{asset.name}: {size} bytes (kept {dest})"
    url, payload = download(asset)
    data = decompress(payload) if asset.compressed else payload
    asset.validate(data)
    try:
        _write_atomic(dest, data)
    except OSError as exc:
        raise AssetError(f"cannot write {dest}: {exc}") from exc
    return f"{asset.name}: {len(data)} bytes <- {url}"


def _write_atomic(dest: Path, data: bytes) -> None:
    """Write the payload to the destination file, atomically.

    The bytes go to a temporary file in the same directory and are moved
    into place with :func:`os.replace`, so a reader never sees a half
    written file.

    Args:
        dest: Path of the file to write.
        data: The bytes to store.

    Raises:
        OSError: If the file cannot be written.
    """
    handle = tempfile.NamedTemporaryFile(
        dir=dest.parent,
        prefix=f".{dest.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(data)
        os.replace(handle.name, dest)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments to parse; ``sys.argv`` by default.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="Download the offline assets of the traffic map."
    )
    parser.add_argument(
        "--assets-dir",
        type=Path,
        default=DEFAULT_ASSETS_DIR,
        help="directory the assets are written to (default: %(default)s)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download assets even when a valid file already exists",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Fetch every asset.

    Args:
        argv: Arguments to parse; ``sys.argv`` by default.

    Returns:
        0 when every asset is in place, 1 when at least one failed.
    """
    args = parse_args(argv)
    assets_dir: Path = args.assets_dir
    assets_dir.mkdir(parents=True, exist_ok=True)
    failed = []
    for asset in ASSETS:
        try:
            print(fetch_asset(asset, assets_dir / asset.name, args.force))
        except AssetError as exc:
            print(f"error: {asset.name}: {exc}", file=sys.stderr)
            failed.append(asset.name)
    if failed:
        print(f"failed assets: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
