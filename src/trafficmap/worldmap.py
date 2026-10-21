"""Land raster of the world map.

The map background is the Natural Earth 110m land dataset, stored as
GeoJSON.  This module turns it into a flat *land mask*: one byte per
pixel, ``1`` for land and ``0`` for water, row-major, exactly the layout
:meth:`trafficmap.braille.Canvas.blit_land` expects.

Projection
----------
The mask covers the whole world with the same equirectangular convention
as :func:`trafficmap.projection.project`: longitude grows east, latitude
grows south, and a coordinate is mapped onto the grid with::

    x = int((lon + 180) / 360 * (width - 1) + 0.5)
    y = int((90 - lat) / 180 * (height - 1) + 0.5)

Natural Earth splits its polygons at the antimeridian, so no polygon has
to be wrapped here.

Caching
-------
Rasterizing is not cheap, and the resolution of the mask is known only at
runtime.  :func:`load_land` therefore keeps the raster on disk under
``land_{width}x{height}_{mtime_ns}.bin``; the modification time of the
GeoJSON file is part of the name, so a newer dataset never reuses the
raster of an older one.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

__all__ = ["rasterize", "load_land"]

#: Translation table that turns the gray pixels of the image into bits.
_TO_BIT = bytes(1 if value else 0 for value in range(256))


def rasterize(geojson: dict, width: int, height: int) -> bytearray:
    """Rasterize the land polygons of a GeoJSON document.

    Only the outer ring of a polygon is filled; holes are ignored, which
    is harmless for the 110m dataset (its polygons have no holes at all).

    Args:
        geojson: GeoJSON document: a ``FeatureCollection`` or a single
            ``Feature``.
        width: Width of the mask in pixels.
        height: Height of the mask in pixels.

    Returns:
        A flat ``bytearray`` of ``width * height`` entries, row-major,
        holding ``1`` for land and ``0`` for water.

    Raises:
        ValueError: If the size is not positive or the document is not a
            GeoJSON object.
    """
    if width < 1 or height < 1:
        raise ValueError(f"mask size must be positive, got {width}x{height}")
    if not isinstance(geojson, dict):
        raise ValueError(f"expected a GeoJSON object, got {type(geojson).__name__}")
    image = Image.new("1", (width, height), 0)
    draw = ImageDraw.Draw(image)
    for feature in _features(geojson):
        for ring in _exterior_rings(feature):
            points = [_to_pixel(lon, lat, width, height) for lon, lat in ring]
            if len(points) >= 3:
                draw.polygon(points, fill=1)
    # The "1" mode packs eight pixels into a byte, so the image is
    # widened to "L" first to get one byte per pixel.
    return bytearray(image.convert("L").tobytes().translate(_TO_BIT))


def load_land(
    cache_dir: Path, geojson_path: Path, width: int, height: int
) -> bytearray:
    """Return the land mask of the world, using an on-disk cache.

    Args:
        cache_dir: Directory the raster is cached in; it is created when
            it does not exist.
        geojson_path: Path of the land GeoJSON file.
        width: Width of the mask in pixels.
        height: Height of the mask in pixels.

    Returns:
        A flat ``bytearray`` of ``width * height`` entries, row-major,
        holding ``1`` for land and ``0`` for water.

    Raises:
        ValueError: If the size is not positive.
        OSError: If the GeoJSON file cannot be read.
        json.JSONDecodeError: If the GeoJSON file is malformed.
    """
    if width < 1 or height < 1:
        raise ValueError(f"mask size must be positive, got {width}x{height}")
    cache_dir = Path(cache_dir)
    geojson_path = Path(geojson_path)
    stamp = geojson_path.stat().st_mtime_ns
    cache_file = cache_dir / f"land_{width}x{height}_{stamp}.bin"
    cached = _read_cache(cache_file, width * height)
    if cached is not None:
        return cached
    with geojson_path.open("r", encoding="utf-8") as handle:
        geojson = json.load(handle)
    mask = rasterize(geojson, width, height)
    _write_cache(cache_file, mask)
    return mask


def _features(geojson: dict) -> list[dict]:
    """Return the features of a GeoJSON document.

    Args:
        geojson: A ``FeatureCollection`` or a single ``Feature``.

    Returns:
        The list of features; empty when the document holds none.
    """
    features = geojson.get("features")
    if isinstance(features, list):
        return [feature for feature in features if isinstance(feature, dict)]
    geometry = geojson.get("geometry")
    if isinstance(geometry, dict):
        return [{"geometry": geometry}]
    return []


def _exterior_rings(feature: dict) -> list[list[tuple[float, float]]]:
    """Return the outer rings of the polygons of one feature.

    Args:
        feature: A GeoJSON feature.

    Returns:
        One list of ``(lon, lat)`` pairs per polygon; empty for geometry
        types that are not ``Polygon`` or ``MultiPolygon``.
    """
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return []
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return []
    if kind == "Polygon":
        polygons = [coordinates]
    elif kind == "MultiPolygon":
        polygons = [c for c in coordinates if isinstance(c, list)]
    else:
        return []
    rings = []
    for polygon in polygons:
        if not polygon or not isinstance(polygon[0], list):
            continue
        rings.append([(float(lon), float(lat)) for lon, lat in polygon[0]])
    return rings


def _to_pixel(lon: float, lat: float, width: int, height: int) -> tuple[int, int]:
    """Map a geographic coordinate onto a pixel of the mask.

    Args:
        lon: Longitude in degrees.
        lat: Latitude in degrees.
        width: Width of the mask in pixels.
        height: Height of the mask in pixels.

    Returns:
        The ``(x, y)`` pixel, clamped to the mask.
    """
    x = int((lon + 180.0) / 360.0 * (width - 1) + 0.5)
    y = int((90.0 - lat) / 180.0 * (height - 1) + 0.5)
    return (min(max(x, 0), width - 1), min(max(y, 0), height - 1))


def _read_cache(cache_file: Path, size: int) -> bytearray | None:
    """Return the cached mask, if it is usable.

    Args:
        cache_file: Path of the cache file.
        size: Expected number of bytes.

    Returns:
        The cached mask, or ``None`` when the cache is missing or broken.
    """
    try:
        data = cache_file.read_bytes()
    except FileNotFoundError:
        return None
    if len(data) != size:
        return None
    return bytearray(data)


def _write_cache(cache_file: Path, mask: bytearray) -> None:
    """Write the mask to the cache file, atomically.

    Args:
        cache_file: Path of the cache file.
        mask: The mask to store.
    """
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        dir=cache_file.parent,
        prefix=f".{cache_file.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(bytes(mask))
        os.replace(handle.name, cache_file)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
