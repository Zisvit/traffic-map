"""Countries and cities on the map.

Two optional Natural Earth datasets are read next to the land polygons:

* ``countries.geojson`` -- the 110m countries, with the Russian name and
  the point a country label belongs to;
* ``cities.geojson`` -- the 50m populated places, with the Russian name,
  the population and whether the place is a capital.

The whole world is the picture of the traffic, and a hundred names would
hide it: a place is shown only when the map is zoomed in.  Both datasets
give every place a *minimum zoom* at load time, so the renderer only has
to compare one number.  A major country is there from x3, a village only
from x12.

The labels are laid out on the character grid of the map.  A label never
covers another label or a panel.  When the preferred place of a label is
taken, it steps aside by a row or two, and it is left out when there is
no room left.  A city also gets a dot on its place, so that its name is
not taken for a country name.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Literal

from trafficmap.projection import Viewport, project
from trafficmap.worldmap import load_geojson

__all__ = [
    "CITY_MARKER",
    "CITY_MIN_ZOOM",
    "COUNTRY_MIN_ZOOM",
    "Label",
    "Place",
    "layout_labels",
    "load_cities",
    "load_countries",
]

log = logging.getLogger(__name__)

#: The zoom the first countries and cities appear at.
COUNTRY_MIN_ZOOM = 3.0
CITY_MIN_ZOOM = 3.0

#: The glyph of a city on the map.
CITY_MARKER = "•"

#: From this zoom on the cities are placed before the countries: the
#: view is smaller than a country, and the cities are what the traffic
#: is about.
_CITY_FIRST_ZOOM = 6.0

#: The zoom a country of a label rank appears at.  Natural Earth ranks
#: the countries by the room their name needs; the rank is the stable
#: way to keep the big names of the world from being crowded out by
#: small ones.
_COUNTRY_MIN_ZOOM: dict[int, float] = {
    2: COUNTRY_MIN_ZOOM,
    3: 3.5,
    4: 4.5,
    5: 6.0,
    6: 7.5,
}

#: The population a city needs for a zoom, from the greatest one down.
#: The steps are wide on purpose: a country view has room for the big
#: cities, and a town of fifty thousand waits for a much closer view.
_CITY_POPULATION_ZOOMS: tuple[tuple[int, float], ...] = (
    (5_000_000, CITY_MIN_ZOOM),
    (1_000_000, 4.0),
    (500_000, 9.0),
    (200_000, 14.0),
    (100_000, 18.0),
    (50_000, 26.0),
)

#: The zoom of a city that none of the population thresholds covers.
_CITY_LAST_ZOOM = 30.0

#: How many cells of the map one label may take, on average.  The
#: cities are the first to be left out when the map is small: a name
#: that cannot be read is worse than a name that is not there.
_CELLS_PER_CITY = 80

#: The same average for the countries: their names are longer, and
#: there are fewer of them.
_CELLS_PER_COUNTRY = 200

#: The shifts tried for a country label whose preferred place is taken.
#: A country can step a little away from its label point; more than a
#: couple of rows would put the name over a neighbour.
_COUNTRY_OFFSETS: tuple[tuple[int, int], ...] = (
    (0, 0),
    (0, -3),
    (0, 3),
    (0, -6),
    (0, 6),
    (-1, 0),
    (1, 0),
    (-1, -4),
    (1, -4),
    (-1, 4),
    (1, 4),
    (-2, 0),
    (2, 0),
)

#: The shifts tried for the dot of a city whose place is taken.
_MARKER_OFFSETS: tuple[tuple[int, int], ...] = (
    (0, 0),
    (0, -1),
    (0, 1),
    (-1, 0),
    (1, 0),
)

Kind = Literal["country", "city"]


@dataclass(frozen=True, slots=True)
class Place:
    """A country or a city that can be named on the map.

    Attributes:
        name: The name of the place.
        lat: The latitude of the place, in degrees.
        lon: The longitude of the place, in degrees.
        min_zoom: The smallest zoom the place is shown at.
        population: The population of a city, ``0`` for a country.
        capital: Whether a city is a capital.
        rank: The Natural Earth label rank of a country.
        kind: ``"country"`` or ``"city"``.
    """

    name: str
    lat: float
    lon: float
    min_zoom: float
    population: int = 0
    capital: bool = False
    rank: int = 99
    kind: Kind = "country"


@dataclass(frozen=True, slots=True)
class Label:
    """One piece of text of the map.

    Attributes:
        line: The character line of the map.
        column: The character column of the map.
        text: The text to draw.
        kind: ``"country"`` or ``"city"``: the colour follows it.
        marker: Whether the label is the dot of a city.
    """

    line: int
    column: int
    text: str
    kind: Kind
    marker: bool = False


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A place of the view, with its point on the dot grid."""

    place: Place
    x: int
    y: int


class _Grid:
    """The character cells of the map that a label may not use.

    Args:
        cols: The width of the map, in characters.
        rows: The height of the map, in characters.
        blocked: The cells that are taken by something else: a panel, a
            peer, the dot of the anchor.
    """

    __slots__ = ("cols", "rows", "cells")

    def __init__(
        self,
        cols: int,
        rows: int,
        blocked: Iterable[tuple[int, int]] = (),
    ) -> None:
        self.cols = cols
        self.rows = rows
        self.cells = bytearray(cols * rows)
        for line, column in blocked:
            if 0 <= line < rows and 0 <= column < cols:
                self.cells[line * cols + column] = 1

    def free(self, line: int, column: int, width: int) -> bool:
        """Tell whether a run of cells is inside the map and untouched.

        Args:
            line: The line of the run.
            column: The first column of the run.
            width: The number of characters of the run.

        Returns:
            ``True`` when every cell of the run is free.
        """
        if line < 0 or line >= self.rows:
            return False
        if column < 0 or width < 1 or column + width > self.cols:
            return False
        base = line * self.cols + column
        return not any(self.cells[base : base + width])

    def take(self, line: int, column: int, width: int, padding: int = 1) -> None:
        """Mark a run of cells, and the gap around it, as taken.

        Args:
            line: The line of the run.
            column: The first column of the run.
            width: The number of characters of the run.
            padding: The number of free cells to leave on both sides.
        """
        if line < 0 or line >= self.rows:
            return
        base = line * self.cols
        for offset in range(-padding, width + padding):
            x = column + offset
            if 0 <= x < self.cols:
                self.cells[base + x] = 1


def load_countries(path: Path) -> list[Place]:
    """Read the countries of a Natural Earth GeoJSON file.

    The Russian name is used when the dataset has one; the English name
    is the fallback.  A feature without a name or a label point is left
    out: there is nothing to draw for it.

    Args:
        path: Path of the countries GeoJSON file.

    Returns:
        The countries, in the order of the file.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If the file is not a GeoJSON document.
    """
    document = load_geojson(path)
    places: list[Place] = []
    for feature in _features(document):
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            continue
        name = _name_of(properties)
        lat = _number(properties.get("LABEL_Y"))
        lon = _number(properties.get("LABEL_X"))
        if not name or lat is None or lon is None:
            continue
        rank = _integer(properties.get("LABELRANK"), 7)
        places.append(
            Place(
                name=name,
                lat=lat,
                lon=lon,
                min_zoom=_country_min_zoom(rank),
                rank=rank,
                kind="country",
            )
        )
    return places


def load_cities(path: Path) -> list[Place]:
    """Read the populated places of a Natural Earth GeoJSON file.

    Args:
        path: Path of the cities GeoJSON file.

    Returns:
        The cities, in the order of the file.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If the file is not a GeoJSON document.
    """
    document = load_geojson(path)
    places: list[Place] = []
    for feature in _features(document):
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            continue
        name = _name_of(properties)
        lat = _number(properties.get("LATITUDE"))
        lon = _number(properties.get("LONGITUDE"))
        if not name or lat is None or lon is None:
            continue
        population = _integer(properties.get("POP_MAX"), 0)
        capital = bool(properties.get("ADM0CAP"))
        world_city = bool(properties.get("WORLDCITY"))
        places.append(
            Place(
                name=name,
                lat=lat,
                lon=lon,
                min_zoom=_city_min_zoom(population, capital, world_city),
                population=population,
                capital=capital,
                kind="city",
            )
        )
    return places


def layout_labels(
    countries: Iterable[Place],
    cities: Iterable[Place],
    viewport: Viewport,
    zoom: float,
    cols: int,
    rows: int,
    blocked: Iterable[tuple[int, int]] = (),
) -> list[Label]:
    """Place the names of the countries and the cities on the map.

    The countries and the cities of the view are ordered by importance;
    a label that does not fit next to a more important one steps aside
    or is left out.  The result never has two labels on one cell.

    Args:
        countries: The countries of the map.
        cities: The cities of the map.
        viewport: The part of the world the map shows.
        zoom: The zoom factor of the map.
        cols: The width of the map, in characters.
        rows: The height of the map, in characters.
        blocked: The cells that are taken by something else.

    Returns:
        The labels, in the order they were placed.
    """
    if cols < 4 or rows < 1 or zoom < min(COUNTRY_MIN_ZOOM, CITY_MIN_ZOOM):
        return []
    grid = _Grid(cols, rows, blocked)
    labels: list[Label] = []
    country_items = _visible(countries, viewport, zoom)
    city_items = _visible(cities, viewport, zoom)
    country_items.sort(key=lambda item: (item.place.rank, item.place.name))
    city_items.sort(
        key=lambda item: (
            -item.place.population,
            not item.place.capital,
            item.place.name,
        )
    )
    if zoom < _CITY_FIRST_ZOOM:
        _place_countries(grid, country_items, labels, cols, rows)
        _place_cities(grid, city_items, labels, cols, rows)
    else:
        _place_cities(grid, city_items, labels, cols, rows)
        _place_countries(grid, country_items, labels, cols, rows)
    return labels


def _place_countries(
    grid: _Grid,
    candidates: list[_Candidate],
    labels: list[Label],
    cols: int,
    rows: int,
) -> None:
    """Place the country names of the view, most important first.

    Args:
        grid: The cells that are already taken.
        candidates: The countries, sorted by importance.
        labels: The labels placed so far.
        cols: The width of the map, in characters.
        rows: The height of the map, in characters.
    """
    limit = max(4, cols * rows // _CELLS_PER_COUNTRY)
    placed = 0
    for candidate in candidates:
        if placed >= limit:
            break
        if _place_country(grid, candidate, labels):
            placed += 1


def _place_cities(
    grid: _Grid,
    candidates: list[_Candidate],
    labels: list[Label],
    cols: int,
    rows: int,
) -> None:
    """Place the city dots and names of the view, biggest first.

    Args:
        grid: The cells that are already taken.
        candidates: The cities, sorted by importance.
        labels: The labels placed so far.
        cols: The width of the map, in characters.
        rows: The height of the map, in characters.
    """
    limit = max(6, cols * rows // _CELLS_PER_CITY)
    placed = 0
    for candidate in candidates:
        if placed >= limit:
            break
        if _place_city(grid, candidate, labels):
            placed += 1


def _features(document: object) -> Iterator[dict]:
    """Return the features of a GeoJSON document.

    Args:
        document: A parsed GeoJSON document.

    Returns:
        The features that are objects; a document of another shape
        gives nothing.
    """
    if not isinstance(document, dict):
        return
    features = document.get("features")
    if not isinstance(features, list):
        return
    for feature in features:
        if isinstance(feature, dict):
            yield feature


def _name_of(properties: dict) -> str:
    """Return the Russian name of a feature, or its English name.

    Args:
        properties: The properties of a GeoJSON feature.

    Returns:
        The name, stripped; an empty string when the feature has none.
    """
    for key in ("NAME_RU", "NAME", "NAME_EN"):
        value = properties.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _number(value: object) -> float | None:
    """Return a finite number from a GeoJSON property.

    Args:
        value: The property value.

    Returns:
        The number, or ``None`` when the value is not a finite number.
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: object, default: int) -> int:
    """Return an integer from a GeoJSON property.

    Args:
        value: The property value.
        default: The value to use when the property is not an integer.

    Returns:
        The integer.
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _country_min_zoom(rank: int) -> float:
    """Return the zoom a country of a label rank appears at.

    Args:
        rank: The Natural Earth label rank.

    Returns:
        The smallest zoom factor of the country.
    """
    if rank <= 2:
        return COUNTRY_MIN_ZOOM
    return _COUNTRY_MIN_ZOOM.get(rank, 9.0)


def _city_min_zoom(population: int, capital: bool, world_city: bool) -> float:
    """Return the zoom a city appears at.

    A world city and a capital are worth a name before a city of the
    same size; the rest follows the population.

    Args:
        population: The population of the city.
        capital: Whether the city is a capital.
        world_city: Whether Natural Earth counts it as a world city.

    Returns:
        The smallest zoom factor of the city.
    """
    if world_city or population >= 5_000_000:
        return CITY_MIN_ZOOM
    if capital or population >= 1_000_000:
        return 4.0
    for threshold, zoom in _CITY_POPULATION_ZOOMS:
        if population >= threshold:
            return zoom
    return _CITY_LAST_ZOOM


def _visible(
    places: Iterable[Place], viewport: Viewport, zoom: float
) -> list[_Candidate]:
    """Return the places of the view that the zoom is close enough for.

    Args:
        places: The places to filter.
        viewport: The part of the world the map shows.
        zoom: The zoom factor of the map.

    Returns:
        The places on the map, with their points on the dot grid.
    """
    candidates: list[_Candidate] = []
    for place in places:
        if zoom < place.min_zoom:
            continue
        point = project(place.lat, place.lon, viewport)
        if point is None:
            continue
        candidates.append(_Candidate(place, point[0], point[1]))
    return candidates


def _place_country(
    grid: _Grid,
    candidate: _Candidate,
    labels: list[Label],
) -> bool:
    """Place a country name near its label point.

    Args:
        grid: The cells that are already taken.
        candidate: The country and its point.
        labels: The labels placed so far; the new one is appended here.

    Returns:
        ``True`` when the name was placed.
    """
    name = candidate.place.name
    width = len(name)
    if width < 1 or width > grid.cols - 2:
        return False
    line = candidate.y // 4
    column = candidate.x // 2 - width // 2
    for down, right in _COUNTRY_OFFSETS:
        position = _fit(grid, line + down, column + right, width)
        if position is None:
            continue
        fit_line, fit_column = position
        if not grid.free(fit_line, fit_column, width):
            continue
        grid.take(fit_line, fit_column, width, padding=1)
        labels.append(Label(fit_line, fit_column, name, "country"))
        return True
    return False


def _place_city(
    grid: _Grid,
    candidate: _Candidate,
    labels: list[Label],
) -> bool:
    """Place the dot and the name of a city.

    The name is written to the right of the dot when it fits, then to
    the left, then above or below it.  A city without room for its name
    keeps no dot: a map of nameless dots is not more readable.

    Args:
        grid: The cells that are already taken.
        candidate: The city and its point.
        labels: The labels placed so far; the new ones are appended here.

    Returns:
        ``True`` when the dot and the name were placed.
    """
    name = candidate.place.name
    if not name or len(name) > grid.cols - 4:
        return False
    line = candidate.y // 4
    column = candidate.x // 2
    marker = _marker_position(grid, line, column)
    if marker is None:
        return False
    position = _city_text_position(grid, name, marker[0], marker[1])
    if position is None:
        return False
    text_line, text_column = position
    grid.take(marker[0], marker[1], 1, padding=0)
    grid.take(text_line, text_column, len(name), padding=1)
    labels.append(Label(marker[0], marker[1], CITY_MARKER, "city", marker=True))
    labels.append(Label(text_line, text_column, name, "city"))
    return True


def _marker_position(grid: _Grid, line: int, column: int) -> tuple[int, int] | None:
    """Return a free cell for the dot of a city.

    Args:
        grid: The cells that are already taken.
        line: The preferred line of the dot.
        column: The preferred column of the dot.

    Returns:
        The cell of the dot, or ``None`` when every cell around the
        place is taken.
    """
    for down, right in _MARKER_OFFSETS:
        candidate_line = line + down
        candidate_column = column + right
        if grid.free(candidate_line, candidate_column, 1):
            return (candidate_line, candidate_column)
    return None


def _city_text_position(
    grid: _Grid, name: str, line: int, column: int
) -> tuple[int, int] | None:
    """Return a free place for the name of a city.

    The place is chosen around the dot of the city: to the right, to
    the left, above and below.  A place that would cover the dot itself
    is not accepted.

    Args:
        grid: The cells that are already taken.
        name: The name of the city.
        line: The line of the dot.
        column: The column of the dot.

    Returns:
        The line and column of the name, or ``None`` when no place
        around the dot is free.
    """
    width = len(name)
    middle = width // 2
    candidates = (
        (line, column + 2),
        (line, column - width - 2),
        (line - 1, column - middle),
        (line + 1, column - middle),
        (line - 2, column - middle),
        (line + 2, column - middle),
        (line - 1, column + 2),
        (line + 1, column + 2),
    )
    for candidate_line, candidate_column in candidates:
        position = _fit(grid, candidate_line, candidate_column, width)
        if position is None:
            continue
        fit_line, fit_column = position
        if fit_line == line and fit_column <= column < fit_column + width:
            continue
        if grid.free(fit_line, fit_column, width):
            return position
    return None


def _fit(grid: _Grid, line: int, column: int, width: int) -> tuple[int, int] | None:
    """Bring a run of text inside the map.

    Args:
        grid: The map.
        line: The line of the text.
        column: The first column of the text.
        width: The number of characters of the text.

    Returns:
        The line and column that fit, or ``None`` when the line is
        outside the map or the text is wider than the map.
    """
    if line < 0 or line >= grid.rows or width > grid.cols:
        return None
    return (line, max(0, min(grid.cols - width, column)))
