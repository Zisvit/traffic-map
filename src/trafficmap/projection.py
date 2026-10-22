"""Equirectangular projection and great-circle geometry for the TUI map.

The terminal map is drawn on a *dot grid*: every character cell provides
2x4 braille dots, so the renderer works in integer dot coordinates. This
module owns the mapping between geographic coordinates (latitude/longitude
in degrees) and those dot indices, plus the great-circle helper used to
draw flight paths.

Conventions
-----------
* ``project`` returns ``(x, y)`` where ``x`` grows east and ``y`` grows
  south; ``None`` means "the point is outside the viewport".
* Longitudes are normalized to ``[-180, 180)`` before the viewport test,
  so a world viewport wraps seamlessly across the antimeridian.
* ``great_circle`` takes and returns ``(lat, lon)`` pairs in degrees.
"""

import math
from dataclasses import dataclass

__all__ = ["Viewport", "project", "unproject", "great_circle"]

# Angular tolerance for "the two points coincide" / "the two points are
# antipodal" in the great-circle computation (radians).
_EPS = 1e-9
# Norm of the sum of the two unit vectors below which the points count as
# antipodal. The sum norm is ~0 at opposition and ~2*cos(omega/2) elsewhere,
# so it detects antipodal pairs without the precision loss of acos near pi.
# 1e-6 corresponds to an angular error of ~5e-7 rad.
_ANTIPODAL_EPS = 1e-6
# Z component above which a point is treated as parallel to the north pole
# axis, so a different auxiliary axis is used to build a great circle.
_PARALLEL_EPS = 0.999


@dataclass(frozen=True, slots=True)
class Viewport:
    """Visible window of the world map, in dot-grid cells.

    Attributes:
        width: Dot-grid width in cells (2 dots per character).
        height: Dot-grid height in cells (4 dots per character).
        center_lat: Latitude of the viewport center, in degrees.
        center_lon: Longitude of the viewport center, in degrees.
        lon_span: Longitude span covered by the viewport, in degrees.
        lat_span: Latitude span covered by the viewport, in degrees.
    """

    width: int
    height: int
    center_lat: float = 0.0
    center_lon: float = 0.0
    lon_span: float = 360.0
    lat_span: float = 180.0


def project(lat: float, lon: float, vp: Viewport) -> tuple[int, int] | None:
    """Project a geographic point to dot-grid indices.

    The projection is equirectangular: the viewport covers ``lon_span``
    degrees of longitude and ``lat_span`` degrees of latitude, mapped
    linearly onto the dot grid.

    Args:
        lat: Latitude in degrees.
        lon: Longitude in degrees.
        vp: Viewport to project into.

    Returns:
        ``(x, y)`` dot indices, or ``None`` if the point falls outside
        the viewport.

    Raises:
        ValueError: If the coordinates are not finite, or if the viewport
            has a non-positive size or span.
    """
    _check_finite(lat, lon)
    _check_viewport(vp)
    dlon = (lon - vp.center_lon + 180.0) % 360.0 - 180.0
    if abs(dlon) > vp.lon_span / 2:
        return None
    if abs(lat - vp.center_lat) > vp.lat_span / 2:
        return None
    x = int((dlon + vp.lon_span / 2) / vp.lon_span * (vp.width - 1) + 0.5)
    y = int(
        (vp.lat_span / 2 - (lat - vp.center_lat)) / vp.lat_span * (vp.height - 1) + 0.5
    )
    return (min(max(x, 0), vp.width - 1), min(max(y, 0), vp.height - 1))


def unproject(x: int, y: int, vp: Viewport) -> tuple[float, float]:
    """Convert dot-grid indices back to a geographic point.

    This is the exact inverse of the unclamped mapping used by
    :func:`project`; rounding to the grid means the result can differ
    from the original point by up to half a cell.

    Args:
        x: Dot index along the east axis.
        y: Dot index along the south axis.
        vp: Viewport the indices belong to.

    Returns:
        ``(lat, lon)`` in degrees.

    Raises:
        ValueError: If the viewport has a non-positive size or span, or is
            smaller than two cells in either axis, which makes the mapping
            undefined.
    """
    _check_viewport(vp)
    if vp.width < 2 or vp.height < 2:
        raise ValueError(
            f"viewport must be at least 2x2 cells, got {vp.width}x{vp.height}"
        )
    lon = vp.center_lon + (x / (vp.width - 1) - 0.5) * vp.lon_span
    lat = vp.center_lat + (0.5 - y / (vp.height - 1)) * vp.lat_span
    return (lat, lon)


def great_circle(
    a: tuple[float, float], b: tuple[float, float], n: int
) -> list[tuple[float, float]]:
    """Sample the great circle that connects two points.

    The path is a spherical linear interpolation (slerp) over unit
    vectors. Coincident points degenerate to a straight line in
    lat/lon space; antipodal points have no unique great circle, so one
    is built through ``a`` using an auxiliary orthogonal axis.

    Args:
        a: Start point as ``(lat, lon)`` in degrees.
        b: End point as ``(lat, lon)`` in degrees.
        n: Number of samples, at least 2. The first sample is ``a`` and
            the last one is ``b``.

    Returns:
        A list of exactly ``n`` ``(lat, lon)`` pairs in degrees.

    Raises:
        ValueError: If ``n`` is smaller than 2, or if a coordinate is
            not finite.
    """
    if n < 2:
        raise ValueError(f"n must be at least 2, got {n}")

    lat_a, lon_a = a
    lat_b, lon_b = b
    _check_finite(lat_a, lon_a)
    _check_finite(lat_b, lon_b)
    va = _to_xyz(lat_a, lon_a)
    vb = _to_xyz(lat_b, lon_b)

    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(va, vb))))
    omega = math.acos(dot)
    # The chord is a numerically stabler measure of "same point" than
    # acos(dot), which loses precision near 1.0.
    chord = math.sqrt(sum((x - y) ** 2 for x, y in zip(va, vb)))
    if omega < _EPS or chord < _EPS:
        # Interpolate the short way around: a longitude delta of +350 degrees
        # must sweep 10 degrees westwards, not 350 degrees eastwards.
        dlon = (lon_b - lon_a + 180.0) % 360.0 - 180.0
        return [
            (
                lat_a + (lat_b - lat_a) * (i / (n - 1)),
                lon_a + dlon * (i / (n - 1)),
            )
            for i in range(n)
        ]

    sin_omega = math.sin(omega)
    # Near opposition acos loses precision: an exact antipode can yield
    # omega = pi - 1.5e-8, whose sine is far above _EPS, and the slerp then
    # cancels catastrophically. The norm of the vector sum is ~0 exactly at
    # opposition and ~2*cos(omega/2) elsewhere, so it is a stable test.
    sum_norm = math.sqrt(sum((x + y) ** 2 for x, y in zip(va, vb)))
    if abs(sin_omega) < _EPS or sum_norm < _ANTIPODAL_EPS:
        return _antipodal_path(va, n)

    points: list[tuple[float, float]] = []
    for i in range(n):
        t = i / (n - 1)
        f = math.sin((1.0 - t) * omega) / sin_omega
        g = math.sin(t * omega) / sin_omega
        points.append(
            _to_geo(
                f * va[0] + g * vb[0],
                f * va[1] + g * vb[1],
                f * va[2] + g * vb[2],
            )
        )
    return points


def _antipodal_path(v: tuple[float, float, float], n: int) -> list[tuple[float, float]]:
    """Build a great circle from ``v`` to its antipode via an orthogonal axis."""
    axis = _orthogonal_axis(v)
    points: list[tuple[float, float]] = []
    for i in range(n):
        angle = math.pi * (i / (n - 1))
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        points.append(
            _to_geo(
                v[0] * cos_a + axis[0] * sin_a,
                v[1] * cos_a + axis[1] * sin_a,
                v[2] * cos_a + axis[2] * sin_a,
            )
        )
    return points


def _orthogonal_axis(v: tuple[float, float, float]) -> tuple[float, float, float]:
    """Return a unit vector orthogonal to the unit vector ``v``."""
    up = (0.0, 0.0, 1.0)
    if abs(v[2]) > _PARALLEL_EPS:
        # v is nearly parallel to the north pole: cross it with the x axis
        # instead, so the result stays well conditioned.
        up = (1.0, 0.0, 0.0)
    return _normalize(_cross(v, up))


def _cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Cross product of two 3D vectors."""
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _normalize(v: tuple[float, float, float]) -> tuple[float, float, float]:
    """Scale a 3D vector to unit length."""
    length = math.sqrt(sum(c * c for c in v))
    if length == 0.0:
        raise ValueError("cannot normalize a zero-length vector")
    return (v[0] / length, v[1] / length, v[2] / length)


def _to_xyz(lat: float, lon: float) -> tuple[float, float, float]:
    """Convert a lat/lon pair in degrees to a unit vector on the sphere."""
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    cos_lat = math.cos(lat_rad)
    return (
        cos_lat * math.cos(lon_rad),
        cos_lat * math.sin(lon_rad),
        math.sin(lat_rad),
    )


def _to_geo(x: float, y: float, z: float) -> tuple[float, float]:
    """Convert a unit vector to a lat/lon pair in degrees."""
    lat = math.degrees(math.asin(max(-1.0, min(1.0, z))))
    lon = math.degrees(math.atan2(y, x))
    return (lat, lon)


def _check_viewport(vp: Viewport) -> None:
    """Reject viewports that cannot be projected into."""
    if vp.width < 1 or vp.height < 1:
        raise ValueError(
            f"viewport must have a positive size, got {vp.width}x{vp.height}"
        )
    if vp.lon_span <= 0.0 or vp.lat_span <= 0.0:
        raise ValueError(
            "viewport spans must be positive, got "
            f"lon_span={vp.lon_span}, lat_span={vp.lat_span}"
        )


def _check_finite(lat: float, lon: float) -> None:
    """Reject coordinates that are not finite numbers."""
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError(f"coordinates must be finite, got lat={lat!r}, lon={lon!r}")
