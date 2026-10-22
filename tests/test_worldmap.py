"""Tests of the land raster and its on-disk cache."""

import json
from pathlib import Path

import pytest

from trafficmap.projection import Viewport
from trafficmap.worldmap import load_geojson, load_land, rasterize

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


@pytest.mark.skipif(not REAL_LAND.exists(), reason="assets/land.geojson is missing")
def test_the_caspian_is_the_only_hole_and_is_painted_as_land():
    """The dataset has one hole, the Caspian Sea, and it is painted as land."""
    geojson = _load(REAL_LAND)
    holes = []
    for feature in geojson["features"]:
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        if geometry.get("type") == "Polygon":
            polygons = [coordinates]
        elif geometry.get("type") == "MultiPolygon":
            polygons = coordinates
        else:
            continue
        for polygon in polygons:
            if len(polygon) > 1:
                holes.append(polygon[1])
    assert len(holes) == 1
    hole = holes[0]

    width, height = 400, 240
    mask = rasterize(geojson, width, height)
    # The coastline of the hole lies inside the Eurasia polygon, and holes
    # are not subtracted: the Caspian is part of the land mask.
    assert all(mask[_index(lon, lat, width, height)] == 1 for lon, lat in hole)


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


def test_a_broken_cache_file_is_rasterized_again(tmp_path):
    """A file of the right length with a broken content is a cache miss."""
    mask = load_land(tmp_path, SAMPLE, WIDTH, HEIGHT)
    cache_file = tmp_path / f"land_{WIDTH}x{HEIGHT}_{SAMPLE.stat().st_mtime_ns}.bin"
    cache_file.write_bytes(b"\x02" * (WIDTH * HEIGHT))

    again = load_land(tmp_path, SAMPLE, WIDTH, HEIGHT)
    assert again == mask
    # The cache file was rewritten with a valid mask.
    assert set(cache_file.read_bytes()) <= {0, 1}


def test_stale_cache_files_are_removed(tmp_path):
    """A cache file of an older dataset does not stay in the cache."""
    mask = load_land(tmp_path, SAMPLE, WIDTH, HEIGHT)
    current = tmp_path / f"land_{WIDTH}x{HEIGHT}_{SAMPLE.stat().st_mtime_ns}.bin"
    stale = tmp_path / f"land_{WIDTH}x{HEIGHT}_1.bin"
    stale.write_bytes(bytes(WIDTH * HEIGHT))
    other = tmp_path / f"land_{WIDTH * 2}x{HEIGHT * 2}_1.bin"
    other.write_bytes(bytes(WIDTH * 2 * HEIGHT * 2))

    # Drop the current file, so that the mask is written again.
    current.unlink()
    again = load_land(tmp_path, SAMPLE, WIDTH, HEIGHT)

    assert again == mask
    assert not stale.exists()
    assert current.exists()
    # The cache of another resolution is still in use.
    assert other.exists()


# -- the viewports of the zoom ------------------------------------------


def _viewport_index(
    lon: float, lat: float, viewport: Viewport
) -> int:
    """Return the index of a coordinate in the mask of a viewport.

    The longitude is wrapped around the centre of the view, the way
    :func:`trafficmap.projection.project` wraps it.
    """
    dlon = (lon - viewport.center_lon + 180.0) % 360.0 - 180.0
    x = int((dlon / viewport.lon_span + 0.5) * (viewport.width - 1) + 0.5)
    y = int(
        (0.5 - (lat - viewport.center_lat) / viewport.lat_span)
        * (viewport.height - 1)
        + 0.5
    )
    assert 0 <= x < viewport.width, f"x={x} is outside the mask"
    assert 0 <= y < viewport.height, f"y={y} is outside the mask"
    return y * viewport.width + x


def test_a_viewport_draws_the_part_it_shows():
    """A view of the small square shows the square and nothing else."""
    geojson = _load(SAMPLE)
    viewport = Viewport(
        width=WIDTH,
        height=HEIGHT,
        center_lat=0.0,
        center_lon=0.0,
        lon_span=6.0,
        lat_span=3.0,
    )
    mask = rasterize(geojson, WIDTH, HEIGHT, viewport)
    assert len(mask) == WIDTH * HEIGHT
    assert mask[_viewport_index(0.0, 0.0, viewport)] == 1
    # The edge of the square is at one degree: just outside it is water.
    assert mask[_viewport_index(1.5, 0.0, viewport)] == 0


@pytest.mark.skipif(not REAL_LAND.exists(), reason="assets/land.geojson is missing")
def test_a_zoom_of_a_known_place_is_land():
    """A narrow view of Europe has Berlin on land and the Baltic in water."""
    geojson = load_geojson(REAL_LAND)
    viewport = Viewport(
        width=200,
        height=100,
        center_lat=50.0,
        center_lon=10.0,
        lon_span=20.0,
        lat_span=10.0,
    )
    mask = rasterize(geojson, 200, 100, viewport)
    assert mask[_viewport_index(13.4, 52.5, viewport)] == 1
    assert mask[_viewport_index(13.0, 54.5, viewport)] == 0


def test_a_viewport_over_the_antimeridian_draws_both_sides():
    """The land of the two sides of the antimeridian is drawn where it is."""
    east = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[170.0, 10.0], [180.0, 10.0], [180.0, 30.0], [170.0, 30.0], [170.0, 10.0]]
            ],
        },
    }
    west = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-180.0, -30.0],
                    [-170.0, -30.0],
                    [-170.0, -10.0],
                    [-180.0, -10.0],
                    [-180.0, -30.0],
                ]
            ],
        },
    }
    geojson = {"type": "FeatureCollection", "features": [east, west]}
    viewport = Viewport(
        width=WIDTH,
        height=HEIGHT,
        center_lat=0.0,
        center_lon=180.0,
        lon_span=40.0,
        lat_span=80.0,
    )
    mask = rasterize(geojson, WIDTH, HEIGHT, viewport)
    assert mask[_viewport_index(175.0, 20.0, viewport)] == 1
    assert mask[_viewport_index(-175.0, -20.0, viewport)] == 1
    assert mask[_viewport_index(165.0, 0.0, viewport)] == 0


def test_a_viewport_does_not_borrow_the_land_of_the_other_side():
    """A view that stays on one side does not draw the polygons of the other."""
    east = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[170.0, 10.0], [180.0, 10.0], [180.0, 30.0], [170.0, 30.0], [170.0, 10.0]]
            ],
        },
    }
    geojson = {"type": "FeatureCollection", "features": [east]}
    viewport = Viewport(
        width=WIDTH,
        height=HEIGHT,
        center_lat=0.0,
        center_lon=0.0,
        lon_span=40.0,
        lat_span=80.0,
    )
    mask = rasterize(geojson, WIDTH, HEIGHT, viewport)
    assert set(mask) == {0}


def test_a_viewport_needs_a_positive_span():
    with pytest.raises(ValueError):
        rasterize(
            _load(SAMPLE),
            WIDTH,
            HEIGHT,
            Viewport(width=WIDTH, height=HEIGHT, lon_span=0.0, lat_span=10.0),
        )
