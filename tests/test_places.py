"""Tests of the country and city labels: the files, the zooms, the layout."""

import json
from pathlib import Path

from trafficmap.places import (
    CITY_MARKER,
    CITY_MIN_ZOOM,
    COUNTRY_MIN_ZOOM,
    Place,
    layout_labels,
    load_cities,
    load_countries,
)
from trafficmap.projection import Viewport


def viewport(
    center_lat: float = 50.0,
    center_lon: float = 10.0,
    lat_span: float = 60.0,
    lon_span: float = 120.0,
    width: int = 160,
    height: int = 80,
) -> Viewport:
    """Return a viewport of a zoomed map.

    Args:
        center_lat: The latitude of the centre.
        center_lon: The longitude of the centre.
        lat_span: The height of the view, in degrees.
        lon_span: The width of the view, in degrees.
        width: The width of the dot grid.
        height: The height of the dot grid.

    Returns:
        The viewport.
    """
    return Viewport(
        width=width,
        height=height,
        center_lat=center_lat,
        center_lon=center_lon,
        lon_span=lon_span,
        lat_span=lat_span,
    )


def write_geojson(path: Path, properties: list[dict]) -> Path:
    """Write a FeatureCollection with one point feature per property set.

    Args:
        path: The file to write.
        properties: The properties of the features.

    Returns:
        The path of the file.
    """
    document = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": item,
                "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
            }
            for item in properties
        ],
    }
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def test_countries_take_the_russian_name_and_the_label_point(tmp_path):
    path = write_geojson(
        tmp_path / "countries.geojson",
        [
            {
                "NAME": "Germany",
                "NAME_RU": "Германия",
                "LABEL_X": 10.0,
                "LABEL_Y": 51.0,
                "LABELRANK": 2,
            },
            {
                "NAME": "Nowhere",
                "LABEL_X": 1.0,
                "LABEL_Y": 2.0,
                "LABELRANK": 6,
            },
        ],
    )

    countries = load_countries(path)

    assert [place.name for place in countries] == ["Германия", "Nowhere"]
    germany = countries[0]
    assert (germany.lat, germany.lon) == (51.0, 10.0)
    assert germany.min_zoom == COUNTRY_MIN_ZOOM
    assert germany.kind == "country"


def test_a_country_without_a_point_is_left_out(tmp_path):
    path = write_geojson(
        tmp_path / "countries.geojson",
        [{"NAME": "Nowhere", "LABEL_X": None, "LABEL_Y": 10.0}],
    )

    assert load_countries(path) == []


def test_a_country_labelrank_sets_its_zoom(tmp_path):
    path = write_geojson(
        tmp_path / "countries.geojson",
        [
            {
                "NAME": "A",
                "NAME_RU": "А",
                "LABEL_X": 1.0,
                "LABEL_Y": 1.0,
                "LABELRANK": 2,
            },
            {
                "NAME": "B",
                "NAME_RU": "Б",
                "LABEL_X": 2.0,
                "LABEL_Y": 2.0,
                "LABELRANK": 4,
            },
            {
                "NAME": "C",
                "NAME_RU": "В",
                "LABEL_X": 3.0,
                "LABEL_Y": 3.0,
                "LABELRANK": 6,
            },
            {
                "NAME": "D",
                "NAME_RU": "Г",
                "LABEL_X": 4.0,
                "LABEL_Y": 4.0,
                "LABELRANK": 7,
            },
        ],
    )

    zooms = [place.min_zoom for place in load_countries(path)]

    assert zooms == [3.0, 4.5, 7.5, 9.0]


def test_a_city_zoom_follows_its_size_and_status(tmp_path):
    path = write_geojson(
        tmp_path / "cities.geojson",
        [
            {
                "NAME": "World",
                "LATITUDE": 1.0,
                "LONGITUDE": 1.0,
                "POP_MAX": 10,
                "WORLDCITY": 1,
            },
            {"NAME": "Big", "LATITUDE": 2.0, "LONGITUDE": 2.0, "POP_MAX": 5_000_000},
            {
                "NAME": "Capital",
                "LATITUDE": 3.0,
                "LONGITUDE": 3.0,
                "POP_MAX": 50_000,
                "ADM0CAP": 1,
            },
            {
                "NAME": "Million",
                "LATITUDE": 4.0,
                "LONGITUDE": 4.0,
                "POP_MAX": 1_000_000,
            },
            {"NAME": "Half", "LATITUDE": 5.0, "LONGITUDE": 5.0, "POP_MAX": 500_000},
            {"NAME": "Town", "LATITUDE": 6.0, "LONGITUDE": 6.0, "POP_MAX": 200_000},
            {"NAME": "Village", "LATITUDE": 7.0, "LONGITUDE": 7.0, "POP_MAX": 100_000},
            {"NAME": "Hamlet", "LATITUDE": 8.0, "LONGITUDE": 8.0, "POP_MAX": 50_000},
        ],
    )

    cities = load_cities(path)

    assert [place.min_zoom for place in cities] == [
        CITY_MIN_ZOOM,
        CITY_MIN_ZOOM,
        4.0,
        4.0,
        9.0,
        14.0,
        18.0,
        26.0,
    ]
    assert cities[2].capital is True
    assert cities[0].population == 10
    assert cities[0].kind == "city"


def test_a_city_without_a_point_is_left_out(tmp_path):
    path = write_geojson(
        tmp_path / "cities.geojson",
        [{"NAME": "Nowhere", "LATITUDE": None, "LONGITUDE": 10.0}],
    )

    assert load_cities(path) == []


def test_the_labels_start_at_zoom_three():
    countries = [Place("Германия", 51.0, 10.0, COUNTRY_MIN_ZOOM)]
    cities = [Place("Берлин", 52.5, 13.4, CITY_MIN_ZOOM, population=3_000_000)]

    assert layout_labels(countries, cities, viewport(), 2.9, 80, 21) == []
    labels = layout_labels(countries, cities, viewport(), 3.0, 80, 21)
    assert {label.text for label in labels} >= {"Германия", "Берлин", CITY_MARKER}


def test_the_labels_of_places_outside_the_view_are_left_out():
    countries = [Place("Германия", 51.0, 10.0, COUNTRY_MIN_ZOOM)]
    far = [Place("Сидней", -33.9, 151.2, CITY_MIN_ZOOM, population=5_000_000)]

    labels = layout_labels(countries, far, viewport(), 3.0, 80, 21)

    assert [label.text for label in labels] == ["Германия"]


def test_labels_do_not_overlap_each_other():
    countries = [
        Place("Германия", 51.0, 10.0, COUNTRY_MIN_ZOOM, rank=2),
        Place("Франция", 46.7, 2.6, COUNTRY_MIN_ZOOM, rank=2),
    ]
    cities = [
        Place("Берлин", 52.5, 13.4, CITY_MIN_ZOOM, population=3_000_000, capital=True),
        Place("Гамбург", 53.5, 10.0, CITY_MIN_ZOOM, population=1_800_000),
        Place("Мюнхен", 48.1, 11.6, CITY_MIN_ZOOM, population=1_500_000),
    ]

    labels = layout_labels(countries, cities, viewport(), 6.0, 80, 21)

    taken: set[tuple[int, int]] = set()
    for label in labels:
        cells = {
            (label.line, label.column + offset) for offset in range(len(label.text))
        }
        assert not cells & taken, f"{label.text!r} overlaps another label"
        taken |= cells


def test_a_country_label_steps_aside_when_its_place_is_taken():
    first = Place("Германия", 51.0, 10.0, COUNTRY_MIN_ZOOM, rank=2)
    second = Place("Франция", 51.0, 10.0, COUNTRY_MIN_ZOOM, rank=2)

    labels = layout_labels([first, second], [], viewport(), 3.0, 40, 11)

    assert len(labels) == 2
    assert (labels[0].line, labels[0].column) != (labels[1].line, labels[1].column)


def test_a_label_stays_out_of_the_blocked_cells():
    country = Place("Германия", 51.0, 10.0, COUNTRY_MIN_ZOOM, rank=2)
    city = Place("Берлин", 52.5, 13.4, CITY_MIN_ZOOM, population=3_000_000)
    blocked = {(line, column) for line in range(21) for column in range(30)}

    labels = layout_labels([country], [city], viewport(), 3.0, 80, 21, blocked)

    for label in labels:
        for offset in range(len(label.text)):
            assert (label.line, label.column + offset) not in blocked


def test_a_city_gets_a_dot_next_to_its_name():
    city = Place("Берлин", 52.5, 13.4, CITY_MIN_ZOOM, population=3_000_000)

    labels = layout_labels([], [city], viewport(), 3.0, 80, 21)

    marker = next(label for label in labels if label.marker)
    text = next(label for label in labels if label.text == "Берлин")
    assert marker.text == CITY_MARKER
    assert marker.kind == "city"
    assert abs(marker.line - text.line) <= 2
    assert abs(marker.column - text.column) <= len(text.text) + 2


def test_a_label_that_cannot_fit_is_left_out():
    country = Place("Очень длинное название страны", 51.0, 10.0, COUNTRY_MIN_ZOOM)

    labels = layout_labels([country], [], viewport(), 3.0, 10, 21)

    assert labels == []
