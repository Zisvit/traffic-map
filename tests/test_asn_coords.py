"""Tests of the table of the places of the networks that answer everywhere."""

import math

import pytest

from trafficmap.asn_coords import (
    ANYCAST_POPS,
    EARTH_RADIUS,
    Place,
    anycast_place,
    distance,
    nearest_place,
)

#: The place of the machine of the tests: Moscow.
HOME = (55.7558, 37.6173)

#: Two places of a made-up network, far from each other.
PLACES = (
    Place("First", "AA", 0.0, 0.0),
    Place("Second", "BB", 10.0, 10.0),
)


def test_an_unknown_network_has_no_place():
    assert anycast_place(None, None) is None
    assert anycast_place(None, HOME) is None
    assert anycast_place(64500, HOME) is None


def test_the_closest_point_of_presence_is_chosen():
    place = anycast_place(13335, HOME)
    assert place is not None
    assert place.city == "Moscow"
    assert place.country == "RU"
    assert (place.lat, place.lon) == (55.7558, 37.6173)


def test_a_machine_in_asia_gets_an_asian_point_of_presence():
    place = anycast_place(13335, (1.35, 103.82))
    assert place is not None
    assert place.city == "Singapore"


def test_the_nearest_place_is_chosen():
    assert nearest_place(PLACES, (9.0, 9.0)).city == "Second"
    assert nearest_place(PLACES, (1.0, 1.0)).city == "First"


def test_a_machine_without_a_place_gets_the_main_one():
    assert nearest_place(PLACES, None).city == "First"
    assert anycast_place(15169, None) == Place("Mountain View", "US", 37.422, -122.085)


def test_an_empty_list_of_places_is_an_error():
    with pytest.raises(ValueError):
        nearest_place((), None)


def test_the_distance_of_one_degree_of_latitude():
    """One degree of latitude is a sixtieth of a quarter of the meridian."""
    one_degree = 2.0 * math.pi * EARTH_RADIUS / 360.0
    assert distance(0.0, 0.0, 1.0, 0.0) == pytest.approx(one_degree, rel=1e-3)
    assert distance(10.0, 20.0, 10.0, 20.0) == 0.0
    # The distance does not depend on the order of the places.
    assert distance(0.0, 0.0, 1.0, 1.0) == pytest.approx(distance(1.0, 1.0, 0.0, 0.0))


def test_the_table_is_sane():
    assert ANYCAST_POPS
    for asn, places in ANYCAST_POPS.items():
        assert asn > 0
        assert places, f"AS{asn} has no place"
        for place in places:
            assert place.city
            assert len(place.country) == 2 and place.country.isupper()
            assert -90.0 <= place.lat <= 90.0
            assert -180.0 <= place.lon <= 180.0


@pytest.mark.parametrize(
    "asn",
    [13335, 15169, 16625, 20940, 54113, 32934, 13414, 2906, 62041, 36459],
)
def test_the_networks_whose_city_is_not_trusted_are_in_the_table(asn):
    assert asn in ANYCAST_POPS


def test_a_network_answers_from_many_cities():
    """The networks of the table are the ones that are not one city."""
    for asn, places in ANYCAST_POPS.items():
        assert len(places) >= 3, f"AS{asn} has {len(places)} places"
