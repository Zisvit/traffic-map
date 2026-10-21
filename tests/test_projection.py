import math

import pytest

from trafficmap.projection import Viewport, great_circle, project, unproject


def _central_angle(p: tuple[float, float], q: tuple[float, float]) -> float:
    """Central angle (haversine) between two lat/lon points, in radians."""
    lat1, lon1 = (math.radians(v) for v in p)
    lat2, lon2 = (math.radians(v) for v in q)
    hav = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * (
        math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2.0 * math.asin(math.sqrt(min(1.0, hav)))


def test_center_maps_to_center():
    vp = Viewport(width=360, height=180)
    assert project(0, 0, vp) == (180, 90)


def test_outside_viewport_is_none():
    vp = Viewport(width=360, height=180, lon_span=90, lat_span=45)
    assert project(0, 120, vp) is None


def test_great_circle_endpoints():
    pts = great_circle((55.75, 37.61), (40.71, -74.0), 32)
    assert len(pts) == 32
    assert abs(pts[0][0] - 55.75) < 1e-6 and abs(pts[-1][1] + 74.0) < 1e-6
    mid = pts[16]
    assert 55 < mid[0] < 70 and -40 < mid[1] < -20  # над Атлантикой


# Exact antipodal pairs. For roughly a quarter of them the float dot product
# is -0.9999999999999999 instead of -1.0, which used to push the slerp branch
# into catastrophic cancellation; the list deliberately mixes both kinds.
ANTIPODAL_PAIRS = [
    ((0.0, 0.0), (0.0, 180.0)),  # dot == -1.0
    ((80.0, 0.0), (-80.0, 180.0)),  # dot == -0.9999999999999999
    ((35.0, 17.0), (-35.0, -163.0)),  # dot == -0.9999999999999999
    ((-62.5, 133.75), (62.5, -46.25)),  # dot == -0.9999999999999999
    ((45.5, -73.25), (-45.5, 106.75)),  # dot == -1.0
    ((70.0, 20.0), (-70.0, -160.0)),  # dot == -0.9999999999999999
]


def _dot(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Dot product of the unit vectors of two lat/lon points."""
    return math.cos(_central_angle(a, b))


def test_antipodal_pairs_cover_the_imprecise_dot_product():
    """The antipodal fixtures must include the numerically awkward pairs."""
    dots = [_dot(a, b) for a, b in ANTIPODAL_PAIRS]
    assert any(dot > -1.0 for dot in dots), "no pair with dot > -1.0 in fixtures"


@pytest.mark.parametrize(("a", "b"), ANTIPODAL_PAIRS)
def test_antipodal_path_is_half_a_great_circle(a, b):
    n = 17
    pts = great_circle(a, b, n)
    assert len(pts) == n
    steps = [_central_angle(pts[i], pts[i + 1]) for i in range(n - 1)]
    # The path covers exactly half of a great circle ...
    assert sum(steps) == pytest.approx(math.pi, abs=1e-6)
    # ... and the samples are evenly spaced along it.
    assert max(steps) - min(steps) < 1e-6


def test_antipodal_path_endpoints_are_the_given_points():
    a, b = (80.0, 0.0), (-80.0, 180.0)
    pts = great_circle(a, b, 32)
    assert _central_angle(pts[0], a) == pytest.approx(0.0, abs=1e-9)
    assert _central_angle(pts[-1], b) == pytest.approx(0.0, abs=1e-9)


def test_antipodal_path_has_no_jumps():
    """The historical failure produced steps from 0.027 to 3.14 rad."""
    pts = great_circle((80.0, 0.0), (-80.0, 180.0), 32)
    steps = [_central_angle(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    assert sum(steps) == pytest.approx(math.pi, abs=1e-6)
    assert max(steps) - min(steps) < 1e-6
    assert max(steps) == pytest.approx(math.pi / 31, abs=1e-6)


def test_project_unproject_round_trip():
    vp = Viewport(
        width=360,
        height=180,
        center_lat=40.0,
        center_lon=10.0,
        lon_span=120.0,
        lat_span=60.0,
    )
    for lat, lon in [(40.0, 10.0), (10.0, -20.0), (69.0, 55.0), (11.0, 65.0)]:
        xy = project(lat, lon, vp)
        assert xy is not None
        back_lat, back_lon = unproject(xy[0], xy[1], vp)
        # A round trip may lose at most half a cell: the grid holds 360 cells
        # for 120 degrees of longitude and 180 cells for 60 degrees of latitude.
        assert abs(back_lat - lat) <= 0.5
        assert abs(back_lon - lon) <= 0.5


def test_unproject_inverts_center():
    vp = Viewport(width=360, height=180)
    assert unproject(180, 90, vp) == pytest.approx((0.0, 0.0), abs=1.0)


def test_latitude_outside_span_is_none():
    vp = Viewport(width=360, height=180, lon_span=90, lat_span=45)
    assert project(80, 0, vp) is None
    assert project(0, 0, vp) is not None


def test_longitude_wraps_around_antimeridian():
    vp = Viewport(width=360, height=180)
    east = project(0.0, 179.0, vp)
    west = project(0.0, -179.0, vp)
    assert east is not None and west is not None
    # The two points are 2 degrees apart across the seam, so they land on
    # neighbouring cells once the distance is measured with wrap-around.
    distance = abs(east[0] - west[0])
    assert min(distance, vp.width - distance) <= 4


def test_great_circle_two_points_are_endpoints():
    pts = great_circle((55.75, 37.61), (40.71, -74.0), 2)
    assert len(pts) == 2
    assert pts[0] == pytest.approx((55.75, 37.61), abs=1e-6)
    assert pts[1] == pytest.approx((40.71, -74.0), abs=1e-6)


@pytest.mark.parametrize("n", [0, 1, -5])
def test_great_circle_requires_at_least_two_points(n):
    with pytest.raises(ValueError):
        great_circle((0, 0), (10, 10), n)


def test_great_circle_same_point_is_straight_line():
    pts = great_circle((10.0, 20.0), (10.0, 20.0), 5)
    assert len(pts) == 5
    assert all(p == (10.0, 20.0) for p in pts)


def test_great_circle_coincident_points_take_the_short_way():
    """(0, 0) -> (0, 360) must not sweep 0 -> 90 -> 180 -> 270 -> 360."""
    pts = great_circle((0, 0), (0, 360), 5)
    assert len(pts) == 5
    assert [lon for _, lon in pts] == pytest.approx([0.0, 0.0, 0.0, 0.0, 0.0], abs=1e-9)


def test_great_circle_short_way_across_the_antimeridian():
    """A +350 degree longitude delta must sweep 10 degrees westwards."""
    pts = great_circle((0.0, 0.0), (0.0, 350.0), 5)
    lons = [lon for _, lon in pts]
    assert lons == pytest.approx([0.0, -2.5, -5.0, -7.5, -10.0], abs=1e-9)


def test_great_circle_near_identical_points_stay_on_a_line():
    a = (10.0, 20.0)
    b = (10.00000005, 20.0)
    pts = great_circle(a, b, 5)
    assert len(pts) == 5
    for index, (lat, lon) in enumerate(pts):
        expected_lat = a[0] + (b[0] - a[0]) * (index / 4)
        assert lon == 20.0
        assert abs(lat - expected_lat) < 1e-12


def test_great_circle_steps_are_uniform():
    """Samples along a normal arc must be evenly spaced on the sphere."""
    a = (55.75, 37.61)
    b = (40.71, -74.0)
    pts = great_circle(a, b, 64)
    steps = [_central_angle(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    assert sum(steps) == pytest.approx(_central_angle(a, b), abs=1e-9)
    assert max(steps) - min(steps) < 1e-9
    assert max(steps) == pytest.approx(_central_angle(a, b) / 63, abs=1e-9)


def test_great_circle_polar_endpoints():
    pts = great_circle((90.0, 0.0), (-90.0, 0.0), 8)
    assert len(pts) == 8
    assert all(-90.0 <= lat <= 90.0 for lat, _ in pts)
    assert pts[0][0] == pytest.approx(90.0, abs=1e-6)
    assert pts[-1][0] == pytest.approx(-90.0, abs=1e-6)


def test_great_circle_antipodal_poles_do_not_crash():
    pts = great_circle((90.0, 0.0), (-90.0, 180.0), 6)
    assert len(pts) == 6
    assert all(-90.0 <= lat <= 90.0 for lat, _ in pts)


def test_great_circle_angular_distance_is_preserved():
    a = (10.0, 20.0)
    b = (11.0, 21.0)
    pts = great_circle(a, b, 33)
    distances = [_central_angle(a, p) for p in pts]
    assert distances[0] == pytest.approx(0.0, abs=1e-9)
    assert distances[-1] == pytest.approx(_central_angle(a, b), abs=1e-6)
    # the arc length grows monotonically along the path
    assert all(
        distances[i] <= distances[i + 1] + 1e-9 for i in range(len(distances) - 1)
    )


def test_project_uses_outermost_columns_for_the_viewport_edges():
    vp = Viewport(width=10, height=10, lon_span=20.0, lat_span=20.0)
    assert project(0.0, 10.0, vp)[0] == vp.width - 1
    assert project(0.0, -10.0, vp)[0] == 0
    assert project(10.0, 0.0, vp)[1] == 0
    assert project(-10.0, 0.0, vp)[1] == vp.height - 1


def test_degenerate_viewport_raises():
    with pytest.raises(ValueError):
        project(0, 0, Viewport(width=10, height=10, lon_span=0.0))
    with pytest.raises(ValueError):
        project(0, 0, Viewport(width=0, height=10))
    with pytest.raises(ValueError):
        unproject(0, 0, Viewport(width=1, height=10))
    with pytest.raises(ValueError):
        unproject(0, 0, Viewport(width=10, height=10, lon_span=0.0))
    with pytest.raises(ValueError):
        unproject(0, 0, Viewport(width=10, height=10, lat_span=0.0))
    with pytest.raises(ValueError):
        unproject(0, 0, Viewport(width=0, height=10))


def test_non_finite_coordinates_raise():
    vp = Viewport(width=10, height=10)
    with pytest.raises(ValueError):
        project(float("nan"), 0.0, vp)
    with pytest.raises(ValueError):
        project(0.0, float("inf"), vp)
    with pytest.raises(ValueError):
        great_circle((float("nan"), 0.0), (0.0, 0.0), 3)
