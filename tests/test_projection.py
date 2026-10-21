import math

import pytest

from trafficmap.projection import Viewport, great_circle, project, unproject


def _angular_distance(p: tuple[float, float], q: tuple[float, float]) -> float:
    """Great-circle distance between two lat/lon points, in radians."""
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


def test_antipodes_do_not_crash():
    pts = great_circle((0, 0), (0, 180), 10)
    assert len(pts) == 10 and all(-90 <= la <= 90 for la, _ in pts)


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
        assert abs(back_lat - lat) <= 1.0
        assert abs(back_lon - lon) <= 1.0


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
    assert min(distance, vp.width - distance) <= 3


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


def test_great_circle_near_identical_points_stay_on_a_line():
    a = (10.0, 20.0)
    b = (10.00000005, 20.0)
    pts = great_circle(a, b, 5)
    assert len(pts) == 5
    for index, (lat, lon) in enumerate(pts):
        expected_lat = a[0] + (b[0] - a[0]) * (index / 4)
        assert lon == 20.0
        assert abs(lat - expected_lat) < 1e-12


def test_great_circle_stays_on_the_sphere():
    pts = great_circle((55.75, 37.61), (40.71, -74.0), 64)
    for lat, lon in pts:
        assert -90.0 <= lat <= 90.0
        assert -180.0 <= lon <= 180.0


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
    distances = [_angular_distance(a, p) for p in pts]
    assert distances[0] == pytest.approx(0.0, abs=1e-9)
    assert distances[-1] == pytest.approx(_angular_distance(a, b), abs=1e-6)
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


def test_non_finite_coordinates_raise():
    vp = Viewport(width=10, height=10)
    with pytest.raises(ValueError):
        project(float("nan"), 0.0, vp)
    with pytest.raises(ValueError):
        project(0.0, float("inf"), vp)
    with pytest.raises(ValueError):
        great_circle((float("nan"), 0.0), (0.0, 0.0), 3)
