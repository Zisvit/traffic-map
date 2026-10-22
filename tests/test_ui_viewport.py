"""Tests of the viewport: the view of the world stays inside the world.

The latitudes stop at ninety degrees: a view that is centred higher
than that shows rows of nothing at the top and cuts the south pole
off.  The centre of the view is therefore clamped, and the live
interface and the snapshot have to build the very same view.
"""

import queue
from pathlib import Path

import pytest

from trafficmap.config import Config
from trafficmap.geo import Geo
from trafficmap.projection import Viewport, project, unproject
from trafficmap.ui import (
    SNAPSHOT_COLS,
    SNAPSHOT_ROWS,
    STATUS_ROWS,
    WORLD_SPAN,
    ZOOM_PRESETS,
    _Frame,
    _Map,
    _State,
    _draw,
    _paint,
    snapshot,
)

ASSETS = Path(__file__).resolve().parents[1] / "assets"
LAND = ASSETS / "land.geojson"
CITY_DB = ASSETS / "dbip-city-lite.mmdb"

needs_land = pytest.mark.skipif(
    not LAND.exists(), reason="the land map is not downloaded"
)
needs_city_db = pytest.mark.skipif(
    not CITY_DB.exists(), reason="the city database is not downloaded"
)

#: The place of the machine in the tests: Amsterdam, far from the poles
#: and from the Greenwich meridian.
ANCHOR = Geo("198.51.100.42", 52.4, 4.9, "NL", "Amsterdam", 47172, "Example")

#: A place of the machine that is almost the north pole.
POLAR_ANCHOR = Geo("193.29.139.249", 89.0, 4.9, "NO", "Tromso", 47172, "Example")

#: The recording the snapshot replays.
FIXTURE = Path(__file__).parent / "fixtures" / "mock.jsonl"

#: The number of frames of the snapshot.
FRAMES = 5

#: The number of lines of the map in a snapshot.
MAP_LINES = SNAPSHOT_ROWS - STATUS_ROWS

#: The Braille character of a cell that holds nothing.
BLANK = "⠀"


class FakeScreen:
    """A terminal that remembers what is written on it.

    The live interface draws its frames on a curses screen: the tests
    need a screen that can be read, without a terminal.

    Args:
        rows: The number of lines of the screen.
        cols: The number of columns of the screen.
    """

    def __init__(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        self.lines: list[str] = [""] * rows
        self.cursor: tuple[int, int] = (0, 0)

    def move(self, line: int, column: int) -> None:
        """Move the cursor of the fake terminal.

        Args:
            line: The line of the cursor.
            column: The column of the cursor.
        """
        self.cursor = (line, column)

    def clrtoeol(self) -> None:
        """Erase the rest of the line the cursor is on."""
        line, column = self.cursor
        self.lines[line] = self.lines[line][:column]

    def getmaxyx(self) -> tuple[int, int]:
        """Return the size of the screen.

        Returns:
            The number of lines and the number of columns.
        """
        return (self.rows, self.cols)

    def addstr(self, line: int, column: int, text: str, attr: int = 0) -> None:
        """Write a piece of text on the screen.

        Args:
            line: The line of the text.
            column: The column of the text.
            text: The text.
            attr: The attributes of the text.
        """
        current = self.lines[line].ljust(column)
        self.lines[line] = current[:column] + text + current[column + len(text) :]

    def refresh(self) -> None:
        """Draw the screen: there is nothing to draw here."""


def test_a_shorter_status_line_clears_the_tail_of_the_old_one(monkeypatch):
    """The coordinates of a zoom do not stay after the whole world returns."""
    monkeypatch.setattr("trafficmap.ui._attr", lambda colour: 0)
    screen = FakeScreen(5, 30)
    zoomed = _Frame(
        cols=30,
        map_height=2,
        rows=[[("map", 0)], [("map", 0)]],
        status=[
            [],
            [],
            [("anchor 1.2.3.4 · zoom x12 · at 52.4N 4.9E", 0)],
        ],
    )
    world = _Frame(
        cols=30,
        map_height=2,
        rows=[[("map", 0)], [("map", 0)]],
        status=[[], [], [("anchor 1.2.3.4", 0)]],
    )
    _paint(screen, zoomed)
    _paint(screen, world)
    assert screen.lines[4] == "anchor 1.2.3.4"


def make_map(anchor: Geo | None, zoom: int) -> _Map:
    """Build the map of the interface, with its anchor and its zoom.

    Args:
        anchor: The place of the machine.
        zoom: The zoom level of the map.

    Returns:
        The map.
    """
    map_ = _Map(Config(), SNAPSHOT_COLS, SNAPSHOT_ROWS)
    map_.anchor = anchor
    map_.zoom = ZOOM_PRESETS[zoom]
    return map_


def visible_latitudes(viewport: Viewport) -> tuple[float, float]:
    """Return the latitudes the top and the bottom of the view show.

    Args:
        viewport: The part of the world the canvas shows.

    Returns:
        The latitude of the bottom of the view and the latitude of its
        top.
    """
    bottom = unproject(0, viewport.height - 1, viewport)[0]
    top = unproject(0, 0, viewport)[0]
    return (bottom, top)


def draw_live(anchor: Geo, zoom: int) -> FakeScreen:
    """Draw one frame the way the live interface does.

    Args:
        anchor: The place of the machine.
        zoom: The zoom level of the map.

    Returns:
        The screen the frame was drawn on.
    """
    cfg = Config()
    state = _State(cfg)
    map_ = _Map(cfg, SNAPSHOT_COLS, SNAPSHOT_ROWS)
    map_.zoom = ZOOM_PRESETS[zoom]
    screen = FakeScreen(SNAPSHOT_ROWS, SNAPSHOT_COLS)
    _draw(screen, map_, state, queue.Queue(), 0.0, [(anchor, None)])
    return screen


def test_the_view_of_the_world_shows_the_whole_world():
    """The view of the world cannot hang over the north pole.

    The span of the latitudes of the world is a hundred and eighty
    degrees: the view has to be centred on the equator, or the rows of
    the top show nothing and the south pole is cut off.
    """
    map_ = make_map(ANCHOR, 1)
    viewport = map_.viewport()
    assert viewport.lat_span == WORLD_SPAN[1] == 180.0
    assert viewport.center_lat == 0.0
    bottom, top = visible_latitudes(viewport)
    assert -90.0 <= bottom <= top <= 90.0
    assert (bottom, top) == (-90.0, 90.0)
    # The anchor of the machine is still on the map.
    assert project(ANCHOR.lat, ANCHOR.lon, viewport) is not None


def test_the_anchor_is_the_centre_of_the_view_when_it_fits():
    """A view that fits around the anchor is centred on it."""
    map_ = make_map(ANCHOR, 3)
    viewport = map_.viewport()
    assert viewport.lat_span == WORLD_SPAN[1] / ZOOM_PRESETS[3] == 15.0
    assert viewport.center_lat == ANCHOR.lat == 52.4
    assert project(ANCHOR.lat, ANCHOR.lon, viewport) is not None


def test_the_centre_of_the_view_slides_to_the_anchor_near_the_pole():
    """A view that cannot fit around the anchor slides to the pole.

    The anchor is at eighty-nine degrees: a view of twenty degrees
    cannot be centred there without hanging over the pole, so it is
    moved to the closest place where it fits.  The anchor stays on the
    map, because it is a place of the world itself.
    """
    map_ = make_map(POLAR_ANCHOR, 3)
    viewport = map_.viewport()
    assert viewport.center_lat == 82.5
    assert project(POLAR_ANCHOR.lat, POLAR_ANCHOR.lon, viewport) is not None
    bottom, top = visible_latitudes(viewport)
    assert -90.0 <= bottom <= top <= 90.0


def test_the_live_and_the_snapshot_build_the_same_viewport(monkeypatch, tmp_path):
    """The two ways of drawing the map have to build the same view.

    The live interface and the snapshot draw the same map: they have
    to agree on the part of the world the map shows.
    """
    seen: list[Viewport] = []
    original = _Map.viewport

    def spy(self: _Map) -> Viewport:
        """Record the viewport the map builds."""
        viewport = original(self)
        seen.append(viewport)
        return viewport

    monkeypatch.setattr(_Map, "viewport", spy)

    seen.clear()
    draw_live(ANCHOR, 1)
    live = list(seen)
    assert live, "the live interface built no viewport"

    seen.clear()
    out = tmp_path / "frame.txt"
    cfg = Config(anchor=(ANCHOR.lat, ANCHOR.lon), mock=FIXTURE)
    assert snapshot(cfg, out, FRAMES) == 0
    headless = list(seen)
    assert headless, "the snapshot built no viewport"

    assert live[0] == headless[0]
    assert live[0].center_lat == 0.0
    assert live[0].center_lon == ANCHOR.lon


@needs_land
@needs_city_db
def test_the_map_of_the_world_has_no_empty_row(tmp_path):
    """The map of the world uses every one of its rows.

    A view that hangs over the pole shows rows of nothing at the top:
    the map has to show the whole world instead.
    """
    out = tmp_path / "frame.txt"
    cfg = Config(anchor=(ANCHOR.lat, ANCHOR.lon), mock=FIXTURE)
    assert snapshot(cfg, out, FRAMES) == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == SNAPSHOT_ROWS
    map_lines = lines[:MAP_LINES]
    empty = [index for index, line in enumerate(map_lines) if not line.strip(BLANK)]
    assert empty == [], f"the map has empty rows: {empty}"
