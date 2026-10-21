"""Tests of the land raster and its on-disk cache."""

import json
from pathlib import Path

import pytest

from trafficmap.worldmap import load_land, rasterize

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "land_sample.geojson"
REAL_LAND = Path(__file__).resolve().parents[1] / "assets" / "land.geojson"

#: Size of the grid the fixture is rasterized on.
WIDTH = 80
HEIGHT = 40


def _pixel(lon: float, lat: float, width: int, height: int) -> tuple[int, int]:
    """Return the pixel of a geographic coordinate.

    This is the equirectangular mapping of ``worldmap.rasterize``::

        x = int((lon + 180) / 360 * (width - 1) + 0.5)
        y = int((90 - lat) / 180 * (height - 1) + 0.5)

    Longitude grows east, latitude grows south.
    """
    x = int((lon + 180.0) / 360.0 * (width - 1) + 0.5)
    y = int((90.0 - lat) / 180.0 * (height - 1) + 0.5)
    return (min(max(x, 0), width - 1), min(max(y, 0), height - 1))


def _index(lon: float, lat: float, width: int, height: int) -> int:
    """Return the index of a coordinate in the flat mask."""
    x, y = _pixel(lon, lat, width, height)
    return y * width + x


def _load(path: Path) -> dict:
    """Return the parsed content of a GeoJSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def test_rasterize_marks_land_and_water():
    """The square near the origin is land, the Pacific is water."""
    mask = rasterize(_load(SAMPLE), WIDTH, HEIGHT)
    assert len(mask) == WIDTH * HEIGHT
    assert set(mask) <= {0, 1}

    # The small square covers lon -1..1 and lat -1..1, so its center is the
    # origin.  The Africa-like polygon stops at the Gulf of Guinea and
    # does not reach that point.
    assert mask[_index(0.0, 0.0, WIDTH, HEIGHT)] == 1

    # The middle of the Pacific: nowhere near any polygon.
    assert mask[_index(-150.0, 0.0, WIDTH, HEIGHT)] == 0


def test_rasterize_draws_the_small_square_on_its_own():
    """The square is a separate feature and carries its own pixels."""
    geojson = _load(SAMPLE)
    features = [
        f
        for f in geojson["features"]
        if f["properties"]["name"] != "gulf-of-guinea-square"
    ]
    without_square = {**geojson, "features": features}
    mask_without = rasterize(without_square, WIDTH, HEIGHT)
    mask_with = rasterize(geojson, WIDTH, HEIGHT)
    index = _index(0.0, 0.0, WIDTH, HEIGHT)
    assert mask_without[index] == 0
    assert mask_with[index] == 1


def test_rasterize_handles_multipolygon():
    """A MultiPolygon rasterizes like the equivalent separate polygons."""
    geojson = _load(SAMPLE)
    continents = [
        f
        for f in geojson["features"]
        if f["properties"]["name"] != "gulf-of-guinea-square"
    ]
    square = [
        f
        for f in geojson["features"]
        if f["properties"]["name"] == "gulf-of-guinea-square"
    ]
    multipolygon = {
        "type": "Feature",
        "properties": {"name": "continents"},
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [f["geometry"]["coordinates"] for f in continents],
        },
    }
    separate = {**geojson, "features": continents + square}
    combined = {**geojson, "features": [multipolygon] + square}
    assert rasterize(combined, WIDTH, HEIGHT) == rasterize(separate, WIDTH, HEIGHT)


def test_rasterize_skips_unknown_geometry_types():
    """Geometry types other than Polygon and MultiPolygon are ignored."""
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[-1, -1], [1, -1], [1, 1], [-1, 1]]],
                },
            },
        ],
    }
    mask = rasterize(geojson, WIDTH, HEIGHT)
    assert mask[_index(0.0, 0.0, WIDTH, HEIGHT)] == 1
    assert sum(mask) == 4  # the 2x2 pixel square


def test_rasterize_rejects_an_empty_size():
    with pytest.raises(ValueError):
        rasterize(_load(SAMPLE), 0, HEIGHT)
    with pytest.raises(ValueError):
        rasterize(_load(SAMPLE), WIDTH, -1)


@pytest.mark.skipif(not REAL_LAND.exists(), reason="assets/land.geojson is missing")
def test_land_fraction_with_real_asset():
    """The real dataset covers a sane share of the world."""
    geojson = _load(REAL_LAND)
    width, height = 160, 80
    mask = rasterize(geojson, width, height)
    fraction = sum(mask) / len(mask)
    # The equirectangular grid stretches the poles, so Antarctica alone
    # covers about a sixth of it: the share of land is well above the
    # 29 % of the real surface and it shrinks with the resolution
    # (0.41 at 160x80, 0.37 at 320x160, 0.35 at 720x360).
    assert 0.20 < fraction < 0.45, f"land fraction is {fraction:.3f}"

    # Central Africa and the middle of the Pacific.
    assert mask[_index(20.0, 0.0, width, height)] == 1
    assert mask[_index(-150.0, 0.0, width, height)] == 0


def test_cache_roundtrip(tmp_path):
    """The second call is served from the cache and does not rewrite it."""
    first = load_land(tmp_path, SAMPLE, WIDTH, HEIGHT)
    cache_file = tmp_path / f"land_{WIDTH}x{HEIGHT}_{SAMPLE.stat().st_mtime_ns}.bin"
    assert cache_file.exists()
    mtime = cache_file.stat().st_mtime_ns

    second = load_land(tmp_path, SAMPLE, WIDTH, HEIGHT)
    assert second == first
    assert cache_file.stat().st_mtime_ns == mtime
    assert cache_file.read_bytes() == bytes(first)


def test_cache_is_kept_per_resolution(tmp_path):
    """Every resolution gets its own cache file."""
    load_land(tmp_path, SAMPLE, WIDTH, HEIGHT)
    load_land(tmp_path, SAMPLE, WIDTH * 2, HEIGHT * 2)
    stamp = SAMPLE.stat().st_mtime_ns
    assert (tmp_path / f"land_{WIDTH}x{HEIGHT}_{stamp}.bin").exists()
    assert (tmp_path / f"land_{WIDTH * 2}x{HEIGHT * 2}_{stamp}.bin").exists()


def test_cache_file_is_created_in_a_missing_directory(tmp_path):
    """The cache directory is created on demand."""
    cache_dir = tmp_path / "missing" / "cache"
    mask = load_land(cache_dir, SAMPLE, WIDTH, HEIGHT)
    assert len(mask) == WIDTH * HEIGHT
    assert list(cache_dir.glob("land_*.bin"))
