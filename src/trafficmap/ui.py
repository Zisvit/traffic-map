"""The curses interface of the traffic map, and the headless snapshot.

The screen
----------
The map takes all the room but the three last lines: the rates and the
counts, the busiest flows and the state of the program.  The map itself
is a grid of Braille dots: every character cell gives two columns and
four rows of dots, so a terminal of 200x50 characters shows a world of
400x188 dots.

The colours
-----------
The land is dark blue, the anchor is white, the peers are yellow, the
comets that leave the machine are green and the ones that come to it
are cyan; the tail of a comet fades in three steps.  The status bar is
in the colours of the terminal, the warnings are yellow and bold, and
the panels are drawn on a blue background.

The threads
-----------
The collector owns the table of the flows: it polls the sockets, turns
them into flows and puts the events into a queue.  The interface thread
only reads that queue, so the two never wait for each other.  The
anchor is looked up once, in a thread of its own, so the map shows up
right away.

The snapshot
------------
:func:`snapshot` runs the same pipeline without a terminal: the frames
are rendered one after the other, the last one is written to a file as
plain text.  It is what the tests look at and what a demonstration
without a terminal needs.
"""

from __future__ import annotations

import curses
import logging
import operator
import os
import queue
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from trafficmap.anim import (
    STYLE_ANCHOR,
    STYLE_COMET_HEAD,
    STYLE_LAND,
    STYLE_PEER,
    STYLE_TRAIL_BRIGHT,
    STYLE_TRAIL_DIM,
    STYLE_TRAIL_MID,
    Animator,
)
from trafficmap.braille import Canvas
from trafficmap.collector_mock import MockCollector
from trafficmap.collector_ss import SocketRow, SsCollector
from trafficmap.config import Config
from trafficmap.flows import Closed, FlowEvent, FlowTable, Stats
from trafficmap.geo import Geo, GeoResolver
from trafficmap.projection import Viewport, project, unproject
from trafficmap.worldmap import load_land

__all__ = ["run", "snapshot"]

log = logging.getLogger(__name__)

#: Number of lines of the status bar.
STATUS_ROWS = 3

#: Smallest terminal the map is drawn on.
MIN_COLS = 80
MIN_ROWS = 24

#: Size of the terminal the snapshot pretends to be drawn on.
SNAPSHOT_COLS = 80
SNAPSHOT_ROWS = 24

#: The span of the world each zoom level shows.
ZOOM_SPANS: dict[int, tuple[float, float]] = {
    1: (360.0, 180.0),
    2: (120.0, 60.0),
    3: (30.0, 20.0),
}

#: Number of lines of the panel of the busiest flows.
TOP_PANEL_ROWS = 8

#: Width of the panel of the busiest flows.
TOP_PANEL_COLUMNS = 30

#: Width of the panel of the keys.
HELP_PANEL_COLUMNS = 46

#: The keys the panel of the keys shows.
HELP_LINES = (
    " keys",
    " q  quit            space  pause the map",
    " t  busiest flows   l  country labels",
    " 1 2 3  zoom        f  filter by process",
    " ?  this help       esc  cancels the filter",
)


@dataclass(frozen=True, slots=True)
class _Colour:
    """The colours of one kind of element of the map.

    Attributes:
        fg: The colour of the glyphs, ``-1`` for the default one.
        bg: The colour of the background, ``-1`` for the default one.
        attr: The attributes of the glyphs.
    """

    fg: int
    bg: int
    attr: int


#: The palette of the interface.  The first entry is the default one and
#: needs no colour pair: it is the colour of the terminal itself.
_PALETTE: tuple[_Colour, ...] = (
    _Colour(-1, -1, 0),  # 0: the default colour
    _Colour(curses.COLOR_BLUE, -1, 0),  # 1: the land
    _Colour(curses.COLOR_WHITE, -1, curses.A_BOLD),  # 2: the anchor
    _Colour(curses.COLOR_YELLOW, -1, 0),  # 3: a peer
    _Colour(curses.COLOR_GREEN, -1, curses.A_DIM),  # 4: an outgoing tail
    _Colour(curses.COLOR_GREEN, -1, 0),  # 5: an outgoing tail
    _Colour(curses.COLOR_GREEN, -1, curses.A_BOLD),  # 6: an outgoing comet
    _Colour(curses.COLOR_CYAN, -1, curses.A_DIM),  # 7: an incoming tail
    _Colour(curses.COLOR_CYAN, -1, 0),  # 8: an incoming tail
    _Colour(curses.COLOR_CYAN, -1, curses.A_BOLD),  # 9: an incoming comet
    _Colour(curses.COLOR_YELLOW, -1, curses.A_BOLD),  # 10: a warning
    _Colour(curses.COLOR_WHITE, curses.COLOR_BLUE, 0),  # 11: a panel
    _Colour(curses.COLOR_WHITE, -1, curses.A_DIM),  # 12: a country label
)

_C_DEFAULT = 0
_C_LAND = 1
_C_ANCHOR = 2
_C_PEER = 3
_C_OUT = (4, 5, 6)
_C_IN = (7, 8, 9)
_C_WARNING = 10
_C_PANEL = 11
_C_LABEL = 12

#: The colour of every style of the canvas, for the comets that leave.
_OUT_BY_STYLE: dict[int, int] = {
    STYLE_TRAIL_DIM: _C_OUT[0],
    STYLE_TRAIL_MID: _C_OUT[1],
    STYLE_TRAIL_BRIGHT: _C_OUT[2],
    STYLE_COMET_HEAD: _C_OUT[2],
}

#: The colour of every style of the canvas, for the comets that come.
_IN_BY_STYLE: dict[int, int] = {
    STYLE_TRAIL_DIM: _C_IN[0],
    STYLE_TRAIL_MID: _C_IN[1],
    STYLE_TRAIL_BRIGHT: _C_IN[2],
    STYLE_COMET_HEAD: _C_IN[2],
}

#: The colour of every style of the canvas that is not a comet.
_PLAIN_BY_STYLE: dict[int, int] = {
    0: _C_DEFAULT,
    STYLE_LAND: _C_LAND,
    STYLE_PEER: _C_PEER,
    STYLE_ANCHOR: _C_ANCHOR,
}

#: The units of the rates, in bytes per second.
_RATE_UNITS = (
    ("KB", 1_000.0),
    ("MB", 1_000.0**2),
    ("GB", 1_000.0**3),
    ("TB", 1_000.0**4),
)

#: The units of the amounts of bytes.
_BYTE_UNITS = (
    ("K", 1_000.0),
    ("M", 1_000.0**2),
    ("G", 1_000.0**3),
    ("T", 1_000.0**4),
)


@dataclass(frozen=True, slots=True)
class _Frame:
    """One frame of the interface, ready to be painted.

    Attributes:
        cols: The width of the screen, in characters.
        map_height: The number of lines the map takes.
        rows: The lines of the map, each a list of ``(text, colour)``.
        overlays: The pieces of text that are put on the map, each a
            ``(line, column, text, colour)``.
        status: The lines of the status bar, each a list of
            ``(text, colour)``.
    """

    cols: int
    map_height: int
    rows: list[list[tuple[str, int]]] = field(default_factory=list)
    overlays: list[tuple[int, int, str, int]] = field(default_factory=list)
    status: list[list[tuple[str, int]]] = field(default_factory=list)

    def text(self) -> str:
        """Return the frame as plain text, without any escape.

        Returns:
            The lines of the map and of the status bar, one per line.
        """
        lines = ["".join(text for text, _ in row) for row in self.rows]
        lines.extend("".join(text for text, _ in row) for row in self.status)
        return "\n".join(lines) + "\n"


@dataclass(slots=True)
class _FlowInfo:
    """What the interface knows about one flow.

    Attributes:
        label: The name of the flow, ``"process:port"``.
        country: The country of the other side.
        tx_rate: The rate of sending, in bytes per second.
        rx_rate: The rate of receiving, in bytes per second.
    """

    label: str
    country: str
    tx_rate: float
    rx_rate: float


class _State:
    """What the interface knows between two frames.

    Args:
        cfg: The configuration of the program.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.animator = Animator()
        self.stats = _empty_stats()
        self.anchor: Geo | None = None
        self.anchor_text = "anchor unknown"
        self.paused = False
        self.show_top = False
        self.show_labels = False
        self.show_help = False
        self.filter = cfg.filter_process
        self.filter_input: str | None = None
        self.directions: dict[str, str] = {}
        self.flows: dict[str, _FlowInfo] = {}
        #: The clock of the animation.  It stands still while the map
        #: is paused, so that the map is frozen as a whole.
        self.now = 0.0

    def remember(self, event: FlowEvent) -> None:
        """Take one event of the table into account.

        Args:
            event: The event: an opened, an updated or a closed one.
        """
        if isinstance(event, Closed):
            self.flows.pop(event.key, None)
            return
        flow = event.flow
        label = f"{flow.process or 'unknown'}:{flow.port}"
        self.directions[label] = "↑" if flow.tx_rate >= flow.rx_rate else "↓"
        self.flows[flow.key] = _FlowInfo(
            label=label,
            country=flow.remote.country,
            tx_rate=flow.tx_rate,
            rx_rate=flow.rx_rate,
        )

    def matches(self, event: FlowEvent) -> bool:
        """Tell whether an event passes the filter of the process.

        Args:
            event: The event.

        Returns:
            ``True`` when the filter is empty, or when the process of
            the flow of the event holds the name of the filter.
        """
        if not self.filter:
            return True
        if isinstance(event, Closed):
            return True
        return self.filter in (event.flow.process or "")


class _Map:
    """The map: the canvas, the land and the viewport.

    Args:
        cfg: The configuration of the program.
        cols: The width of the screen, in characters.
        rows: The height of the screen, in characters.
    """

    def __init__(self, cfg: Config, cols: int, rows: int) -> None:
        self._cfg = cfg
        self.zoom = 1
        self.anchor: Geo | None = None
        self.cols = cols
        self.rows = rows
        self.map_height = max(1, rows - STATUS_ROWS)
        self.canvas = Canvas(cols * 2, self.map_height * 4)
        self._world: bytearray | None = None
        self._world_size = (0, 0)
        self._visible: bytearray | None = None
        self._visible_key: tuple[float, ...] | None = None

    def resize(self, cols: int, rows: int) -> None:
        """Take the size of a new screen.

        Args:
            cols: The width of the screen, in characters.
            rows: The height of the screen, in characters.
        """
        self.cols = cols
        self.rows = rows
        self.map_height = max(1, rows - STATUS_ROWS)
        self.canvas = Canvas(cols * 2, self.map_height * 4)
        self._visible = None
        self._visible_key = None

    def viewport(self) -> Viewport:
        """Return the part of the world the canvas shows.

        Returns:
            The viewport, centred on the anchor when there is one.
        """
        lon_span, lat_span = ZOOM_SPANS.get(self.zoom, ZOOM_SPANS[1])
        center_lat = 0.0
        center_lon = 0.0
        anchor = self.anchor
        if anchor is not None and anchor.lat is not None and anchor.lon is not None:
            center_lat = anchor.lat
            center_lon = anchor.lon
        return Viewport(
            width=self.canvas.width,
            height=self.canvas.height,
            center_lat=center_lat,
            center_lon=center_lon,
            lon_span=lon_span,
            lat_span=lat_span,
        )

    def land_mask(self) -> bytearray:
        """Return the land of the visible part of the world.

        The whole world is rasterized once, in the size of the canvas.
        A zoomed map takes the land it shows out of that raster, with
        the nearest neighbour, so that the map is not rasterized again
        at every zoom level.

        Returns:
            One byte per dot of the canvas: ``1`` for land, ``0`` for
            water.
        """
        viewport = self.viewport()
        key = (
            float(self.canvas.width),
            float(self.canvas.height),
            viewport.lon_span,
            viewport.lat_span,
            round(viewport.center_lat, 6),
            round(viewport.center_lon, 6),
        )
        if self._visible is not None and self._visible_key == key:
            return self._visible
        world = self._world_mask()
        if viewport.lon_span >= 360.0 and viewport.lat_span >= 180.0:
            visible = world
        else:
            visible = _sample_mask(world, viewport)
        self._visible = visible
        self._visible_key = key
        return visible

    def _world_mask(self) -> bytearray:
        """Return the raster of the whole world, in the size of the canvas."""
        size = (self.canvas.width, self.canvas.height)
        if self._world is None or self._world_size != size:
            self._world = load_land(
                self._cfg.cache_dir, self._cfg.land_geojson, size[0], size[1]
            )
            self._world_size = size
        return self._world


def _sample_mask(world: bytearray, viewport: Viewport) -> bytearray:
    """Take the visible part of the world out of its raster.

    The dots of the canvas are mapped back onto the raster of the
    world, one axis at a time, and the value of the dot is copied.  The
    lookups of the two axes are made once, so that the copy is a
    simple walk over the rows.

    Args:
        world: The raster of the whole world, one byte per dot.
        viewport: The part of the world the canvas shows.

    Returns:
        One byte per dot of the canvas: ``1`` for land, ``0`` for water.
    """
    width = viewport.width
    height = viewport.height
    x_map: list[int] = []
    for x in range(width):
        lon = unproject(x, 0, viewport)[1]
        lon = (lon + 180.0) % 360.0 - 180.0
        index = int((lon + 180.0) / 360.0 * (width - 1) + 0.5)
        x_map.append(min(max(index, 0), width - 1))
    y_map: list[int] = []
    for y in range(height):
        lat = unproject(0, y, viewport)[0]
        index = int((90.0 - lat) / 180.0 * (height - 1) + 0.5)
        y_map.append(min(max(index, 0), height - 1))

    picker = operator.itemgetter(*x_map)
    visible = bytearray(width * height)
    for y in range(height):
        base = y_map[y] * width
        visible[y * width : (y + 1) * width] = bytes(picker(world[base : base + width]))
    return visible


def _empty_stats() -> Stats:
    """Return the statistics of a machine that has no traffic yet."""
    return Stats(
        tx_rate=0.0,
        rx_rate=0.0,
        flows=0,
        peers=0,
        countries=Counter(),
        top=[],
        local=0,
        unknown=0,
    )


def _format_rate(value: float) -> str:
    """Format a rate in bytes per second.

    Args:
        value: The rate, in bytes per second.

    Returns:
        The rate with a decimal unit, ``"2.4 MB/s"`` for example.
    """
    number = float(value)
    if number < 1000.0:
        return f"{int(number)} B/s"
    for unit, scale in _RATE_UNITS:
        if number < scale * 1000.0:
            return f"{number / scale:.1f} {unit}/s"
    return f"{number / _RATE_UNITS[-1][1]:.1f} PB/s"


def _format_bytes(value: float) -> str:
    """Format an amount of bytes.

    Args:
        value: The amount of bytes.

    Returns:
        The amount with a decimal unit, ``"9.1M"`` for example.
    """
    number = float(value)
    if number < 1000.0:
        return f"{int(number)}B"
    for unit, scale in _BYTE_UNITS:
        if number < scale * 1000.0:
            return f"{number / scale:.1f}{unit}"
    return f"{number / _BYTE_UNITS[-1][1]:.1f}P"


def _is_root() -> bool:
    """Tell whether the program runs as root."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    return geteuid() == 0


def _clock_of(collector: object) -> Callable[[], float]:
    """Return the clock of a collector.

    A collector that replays a recording has its own clock: the time
    of the tick it is at.  Every other collector is read in real time.

    Args:
        collector: The collector.

    Returns:
        A function that gives the time, in seconds.
    """
    if getattr(collector, "current_time", None) is not None:
        return lambda: collector.current_time  # type: ignore[attr-defined]
    return time.monotonic


def _make_collector(cfg: Config) -> SsCollector | MockCollector:
    """Build the collector of the program.

    Args:
        cfg: The configuration of the program.

    Returns:
        The mock collector when the configuration names a recording,
        the collector of ``ss`` otherwise.

    Raises:
        FileNotFoundError: If the recording does not exist.
        OSError: If the recording cannot be read.
    """
    if cfg.mock is not None:
        return MockCollector(cfg.mock)
    return SsCollector()


def _init_colours() -> None:
    """Give every colour of the palette a pair of the terminal."""
    if not curses.has_colors():
        return
    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:  # pragma: no cover - a terminal without colours
        return
    for index, colour in enumerate(_PALETTE):
        if index == 0:
            continue
        try:
            curses.init_pair(index, colour.fg, colour.bg)
        except curses.error:  # pragma: no cover - a terminal with few pairs
            return


def _attr(colour: int) -> int:
    """Return the attribute of a colour of the palette.

    Args:
        colour: The index of the colour.

    Returns:
        The attribute to give to ``addstr``.
    """
    if not curses.has_colors():
        return _PALETTE[colour].attr
    return curses.color_pair(colour) | _PALETTE[colour].attr


def _in_cells(
    animator: Animator, viewport: Viewport, width: int, height: int, now: float
) -> dict[int, dict[int, str]]:
    """Return the cells the incoming comets are on.

    The canvas holds the style of a dot, not the direction of the comet
    it belongs to, so the comets that come to the machine are drawn a
    second time on a scratch canvas: the cells that canvas touches are
    the cells to paint in cyan.

    Args:
        animator: The animation of the map.
        viewport: The part of the world the canvas shows.
        width: The width of the canvas, in dots.
        height: The height of the canvas, in dots.
        now: The moment of the frame, in seconds.

    Returns:
        The direction of the comets of every cell, by line and column.
    """
    cells: dict[int, dict[int, str]] = {}
    comets = [
        comet
        for comet in animator.comets
        if comet.kind == "in" and not comet.expired(now)
    ]
    if not comets:
        return cells
    scratch = Canvas(width, height)
    for comet in comets:
        points = comet.trail(now, animator.trail_length)
        if not points:
            continue
        previous: tuple[int, int] | None = None
        for lat, lon in points[:-1]:
            point = project(lat, lon, viewport)
            if point is None:
                previous = None
                continue
            if previous is not None:
                scratch.line(previous[0], previous[1], point[0], point[1], 1)
            previous = point
        lat, lon = points[-1]
        point = project(lat, lon, viewport)
        if point is not None and previous is not None:
            scratch.line(previous[0], previous[1], point[0], point[1], 1)
    for cy, row in enumerate(scratch.cell_styles()):
        for cx, style in enumerate(row):
            if style:
                cells.setdefault(cy, {})[cx] = "in"
    return cells


def _colour_of(style: int, direction: str | None) -> int:
    """Return the colour of a cell of the map.

    Args:
        style: The style of the cell.
        direction: The direction of the comet the cell belongs to, if
            any.

    Returns:
        The index of the colour in the palette.
    """
    if direction == "in" and style in _IN_BY_STYLE:
        return _IN_BY_STYLE[style]
    if direction == "out" and style in _OUT_BY_STYLE:
        return _OUT_BY_STYLE[style]
    if style in _OUT_BY_STYLE:
        return _OUT_BY_STYLE[style]
    return _PLAIN_BY_STYLE.get(style, _C_DEFAULT)


def _render(map_: _Map, state: _State, now: float, stats: Stats) -> _Frame:
    """Draw one frame of the map.

    Args:
        map_: The map: the canvas, the land and the viewport.
        state: What the interface knows.
        now: The moment of the frame, in seconds.
        stats: The statistics of the traffic.

    Returns:
        The frame, ready to be painted.
    """
    canvas = map_.canvas
    canvas.clear()
    canvas.blit_land(map_.land_mask())
    viewport = map_.viewport()
    state.animator.draw(canvas, viewport, now)

    text_rows = canvas.rows()
    style_rows = canvas.cell_styles()
    overrides = _in_cells(state.animator, viewport, canvas.width, canvas.height, now)

    rows: list[list[tuple[str, int]]] = []
    for y, style_row in enumerate(style_rows):
        text = text_rows[y]
        row_overrides = overrides.get(y, {})
        segments: list[tuple[str, int]] = []
        start = 0
        current = _colour_of(style_row[0], row_overrides.get(0))
        for x in range(1, len(style_row)):
            colour = _colour_of(style_row[x], row_overrides.get(x))
            if colour != current:
                segments.append((text[start:x], current))
                start = x
                current = colour
        segments.append((text[start:], current))
        rows.append(segments)

    overlays: list[tuple[int, int, str, int]] = []
    if state.show_labels:
        overlays.extend(_country_labels(state, viewport, map_))
    if state.show_top:
        overlays.extend(_top_panel(map_, state))
    if state.show_help:
        overlays.extend(_help_panel(map_))

    return _Frame(
        cols=map_.cols,
        map_height=map_.map_height,
        rows=rows,
        overlays=overlays,
        status=_status_lines(map_, state, stats),
    )


def _country_labels(
    state: _State, viewport: Viewport, map_: _Map
) -> list[tuple[int, int, str, int]]:
    """Return the country labels of the peers of the map.

    The label of a peer is written in the cell to the right of its dot.

    Args:
        state: What the interface knows.
        viewport: The part of the world the canvas shows.
        map_: The map.

    Returns:
        The labels, each a ``(line, column, text, colour)``.
    """
    labels: list[tuple[int, int, str, int]] = []
    for peer in state.animator.peers:
        geo = peer.geo
        if geo.lat is None or geo.lon is None:
            continue
        country = geo.country
        if not country or country == "--":
            continue
        point = project(geo.lat, geo.lon, viewport)
        if point is None:
            continue
        line = point[1] // 4
        column = point[0] // 2 + 1
        if line >= map_.map_height or column + 2 > map_.cols:
            continue
        labels.append((line, column, country, _C_LABEL))
    return labels


def _top_panel(map_: _Map, state: _State) -> list[tuple[int, int, str, int]]:
    """Return the panel of the busiest flows.

    The panel is drawn over the last columns of the map.

    Args:
        map_: The map.
        state: What the interface knows.

    Returns:
        The lines of the panel, each a ``(line, column, text, colour)``.
    """
    column = max(0, map_.cols - TOP_PANEL_COLUMNS)
    lines = [" busiest flows"]
    busiest = sorted(
        state.flows.values(), key=lambda info: -(info.tx_rate + info.rx_rate)
    )
    for info in busiest[:TOP_PANEL_ROWS]:
        arrow = "↑" if info.tx_rate >= info.rx_rate else "↓"
        rate = _format_bytes(info.tx_rate + info.rx_rate)
        label = info.label[:14].ljust(14)
        lines.append(f" {label} {info.country:<2} {arrow}{rate:>6}")
    return _panel_lines(lines, column, TOP_PANEL_COLUMNS)


def _help_panel(map_: _Map) -> list[tuple[int, int, str, int]]:
    """Return the panel of the keys.

    The panel is drawn on the left of the map, so that it does not
    cover the panel of the busiest flows.

    Args:
        map_: The map.

    Returns:
        The lines of the panel, each a ``(line, column, text, colour)``.
    """
    column = 2
    return _panel_lines(list(HELP_LINES), column, HELP_PANEL_COLUMNS)


def _panel_lines(
    lines: list[str], column: int, width: int
) -> list[tuple[int, int, str, int]]:
    """Return the lines of a panel, on a background of their own.

    Args:
        lines: The lines of the panel.
        column: The column the panel starts at.
        width: The width of the panel.

    Returns:
        The lines of the panel, each a ``(line, column, text, colour)``.
    """
    return [
        (index, column, line[:width].ljust(width), _C_PANEL)
        for index, line in enumerate(lines)
    ]


def _status_lines(
    map_: _Map, state: _State, stats: Stats
) -> list[list[tuple[str, int]]]:
    """Return the three lines of the status bar.

    Args:
        map_: The map.
        state: What the interface knows.
        stats: The statistics of the traffic.

    Returns:
        The lines of the status bar, each a list of ``(text, colour)``.
    """
    parts = [
        f"↑ {_format_rate(stats.tx_rate)}",
        f"↓ {_format_rate(stats.rx_rate)}",
        f"flows {stats.flows}",
        f"peers {stats.peers}",
        f"local {stats.local}",
    ]
    if stats.unknown:
        parts.append(f"unknown {stats.unknown}")
    countries = [code for code, _ in stats.countries.most_common(4) if code != "--"]
    if countries:
        parts.append(" ".join(countries))
    first = [("   ".join(parts)[: map_.cols], _C_DEFAULT)]

    items = []
    for label, rate in stats.top[:3]:
        arrow = state.directions.get(label, "·")
        items.append(f"{label} {arrow}{_format_bytes(rate)}")
    second = [(" · ".join(items)[: map_.cols], _C_DEFAULT)]

    third: list[tuple[str, int]] = []
    parts: list[tuple[str, int]] = []
    if not _is_root():
        parts.append(("no root: only your sockets", _C_WARNING))
    parts.append((state.anchor_text, _C_DEFAULT))
    if map_.zoom != 1:
        parts.append((f"zoom {map_.zoom}", _C_DEFAULT))
    if state.filter_input is not None:
        parts.append((f"filter: {state.filter_input}", _C_DEFAULT))
    elif state.filter:
        parts.append((f"filter: {state.filter}", _C_DEFAULT))
    if state.paused:
        parts.append(("paused", _C_WARNING))
    hints = ("q quit  ? help", _C_DEFAULT)
    parts.append(hints)
    # The last parts are the least important ones: they are the first
    # to go when the line is too long.
    while len(parts) > 1 and _length(_join(parts)) > map_.cols:
        parts.pop()
    third = _cut(_join(parts), map_.cols)

    return [first, second, third]


def _join(parts: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Join the pieces of a line with a dot between them.

    Args:
        parts: The pieces of the line.

    Returns:
        The pieces of the line, with the separators between them.
    """
    segments: list[tuple[str, int]] = []
    for index, (text, colour) in enumerate(parts):
        if index:
            segments.append((" · ", _C_DEFAULT))
        segments.append((text, colour))
    return segments


def _length(segments: list[tuple[str, int]]) -> int:
    """Return the length of a line, in characters.

    Args:
        segments: The pieces of the line.

    Returns:
        The number of characters of the line.
    """
    return sum(len(text) for text, _ in segments)


def _cut(segments: list[tuple[str, int]], width: int) -> list[tuple[str, int]]:
    """Cut a list of segments to a width.

    Args:
        segments: The segments of the line.
        width: The width of the line, in characters.

    Returns:
        The segments that fit in the width.
    """
    cut: list[tuple[str, int]] = []
    left = width
    for text, colour in segments:
        if left <= 0:
            break
        piece = text[:left]
        cut.append((piece, colour))
        left -= len(piece)
    return cut


class _CollectorThread(threading.Thread):
    """Poll the sockets and hand the events to the interface.

    The thread owns the table of the flows: the interface never reads
    it, it only takes the events out of the queue.

    Args:
        collector: The collector that reads the sockets.
        table: The table of the flows.
        out: The queue the events are put into.
        poll_ms: The length of a poll, in milliseconds.
        stop: The event that asks the thread to stop.
    """

    def __init__(
        self,
        collector: object,
        table: FlowTable,
        out: "queue.Queue[tuple[list[FlowEvent], Stats, float]]",
        poll_ms: int,
        stop: threading.Event,
    ) -> None:
        super().__init__(name="trafficmap-collector", daemon=True)
        self._collector = collector
        self._table = table
        self._out = out
        self._interval = poll_ms / 1000.0
        self._stop = stop
        self._clock = _clock_of(collector)

    def run(self) -> None:
        """Poll the sockets until the program stops."""
        previous: float | None = None
        while not self._stop.is_set():
            now = self._clock()
            try:
                rows: list[SocketRow] | None = self._collector.poll()  # type: ignore[attr-defined]
            except RuntimeError as exc:
                log.debug("cannot read the sockets: %s", exc)
                rows = None
            if rows is not None and (previous is None or now != previous):
                events = self._table.update(rows, now)
                previous = now
                self._out.put((events, self._table.stats(), now))
            self._stop.wait(self._interval)


def _resolve_anchor(
    resolver: GeoResolver, cfg: Config
) -> tuple[Geo | None, float | None]:
    """Find the place of this machine.

    Args:
        resolver: The resolver that knows the places.
        cfg: The configuration of the program.

    Returns:
        The place of the machine and the time the search took, in
        milliseconds.  Both are ``None`` when the place is not known.
    """
    if cfg.anchor is not None:
        lat, lon = cfg.anchor
        geo = Geo(
            ip=f"{lat:.4f},{lon:.4f}",
            lat=lat,
            lon=lon,
            country="--",
            city=None,
            asn=None,
            org=None,
        )
        return geo, None
    start = time.monotonic()
    geo = resolver.anchor()
    elapsed = (time.monotonic() - start) * 1000.0
    if geo is None:
        return None, None
    return geo, elapsed


def _anchor_label(geo: Geo | None, elapsed: float | None) -> str:
    """Return the text of the anchor of the status bar.

    Args:
        geo: The place of the machine, if it is known.
        elapsed: The time the search took, in milliseconds.

    Returns:
        The text of the anchor.
    """
    if geo is None:
        return "anchor unknown"
    parts = [f"anchor {geo.ip}"]
    if geo.country and geo.country != "--":
        parts.append(geo.country)
    if elapsed is not None:
        parts.append(f"geo {elapsed:.0f}ms")
    return " · ".join(parts)


def _paint(stdscr: "curses.window", frame: _Frame) -> None:
    """Draw a frame on the screen.

    Args:
        stdscr: The screen of the terminal.
        frame: The frame to draw.
    """
    for line, segments in enumerate(frame.rows):
        column = 0
        for text, colour in segments:
            _addstr(stdscr, line, column, text, colour)
            column += len(text)
    for line, column, text, colour in frame.overlays:
        _addstr(stdscr, line, column, text, colour)
    for index, segments in enumerate(frame.status):
        column = 0
        for text, colour in segments:
            _addstr(stdscr, frame.map_height + index, column, text, colour)
            column += len(text)
    stdscr.refresh()


def _addstr(
    stdscr: "curses.window", line: int, column: int, text: str, colour: int
) -> None:
    """Write a piece of text on the screen, and survive the edge of it.

    Args:
        stdscr: The screen of the terminal.
        line: The line of the text.
        column: The column of the text.
        text: The text.
        colour: The colour of the text.
    """
    if not text:
        return
    try:
        stdscr.addstr(line, column, text, _attr(colour))
    except curses.error:
        # The text does not fit: the edge of the screen is not worth a
        # crash.
        pass


def _handle_key(key: int, state: _State, map_: _Map) -> bool:
    """Take one key of the terminal into account.

    Args:
        key: The key.
        state: What the interface knows.
        map_: The map, whose zoom level the keys change.

    Returns:
        ``False`` when the program must stop.
    """
    if key == ord("q"):
        return False
    if key == ord(" "):
        state.paused = not state.paused
    elif key == ord("t"):
        state.show_top = not state.show_top
    elif key == ord("l"):
        state.show_labels = not state.show_labels
    elif key == ord("?"):
        state.show_help = not state.show_help
    elif key in (ord("1"), ord("2"), ord("3")):
        map_.zoom = key - ord("0")
    return True


def _read_filter(
    stdscr: "curses.window",
    state: _State,
    redraw: Callable[[], None],
) -> None:
    """Ask for the name of a process.

    The name is read until the enter key, which takes it, or the escape
    key, which gives it up.  The screen is drawn again after every
    key, so that the name is seen while it is written.

    Args:
        stdscr: The screen of the terminal.
        state: What the interface knows.
        redraw: The function that draws the frame again.
    """
    stdscr.nodelay(False)
    stdscr.timeout(100)
    text = ""
    state.filter_input = text
    redraw()
    while True:
        key = stdscr.getch()
        if key == -1:
            continue
        if key in (10, 13, curses.KEY_ENTER):
            state.filter = text.strip()
            break
        if key == 27:
            break
        if key in (curses.KEY_BACKSPACE, 127, 8):
            text = text[:-1]
        elif 32 <= key < 127:
            text += chr(key)
        else:
            continue
        state.filter_input = text
        redraw()
    state.filter_input = None
    stdscr.nodelay(True)


def _apply_filter(state: _State, now: float) -> None:
    """Take the peers of the flows the filter leaves out away.

    The filter is about the map: the flows that do not hold the name of
    the filter are not on it any more.

    Args:
        state: What the interface knows.
        now: The moment, in seconds.
    """
    if not state.filter:
        return
    for key, info in state.flows.items():
        if state.filter not in info.label.rsplit(":", 1)[0]:
            state.animator.handle_event(Closed(key=key), now)


def _loop(
    stdscr: "curses.window",
    cfg: Config,
    resolver: GeoResolver,
    collector: object,
) -> int:
    """Draw the map until the program stops.

    Args:
        stdscr: The screen of the terminal.
        cfg: The configuration of the program.
        resolver: The resolver that knows the places.
        collector: The collector that reads the sockets.

    Returns:
        ``0`` when the program was stopped with the ``q`` key.
    """
    curses.curs_set(0)
    curses.noecho()
    stdscr.keypad(True)
    stdscr.timeout(max(1, 1000 // cfg.fps))
    stdscr.nodelay(True)
    _init_colours()

    state = _State(cfg)
    rows, cols = stdscr.getmaxyx()
    map_ = _Map(cfg, cols, rows)
    events: "queue.Queue[tuple[list[FlowEvent], Stats, float]]" = queue.Queue()
    stop = threading.Event()
    table = FlowTable(resolver, poll_ms=cfg.poll_ms)
    collector_thread = _CollectorThread(collector, table, events, cfg.poll_ms, stop)
    collector_thread.start()

    anchor_holder: list[tuple[Geo | None, float | None]] = []
    if not cfg.no_anchor:
        anchor_holder = _start_anchor_search(resolver, cfg)

    interval = 1.0 / max(1, cfg.fps)
    next_frame = time.monotonic()
    running = True
    while running:
        now = time.monotonic()
        if now >= next_frame:
            next_frame = now + interval
            _draw(stdscr, map_, state, events, now, anchor_holder)
        key = stdscr.getch()
        if key == -1:
            delay = next_frame - time.monotonic()
            if delay > 0:
                time.sleep(min(delay, 0.05))
            continue
        if key == curses.KEY_RESIZE:
            rows, cols = stdscr.getmaxyx()
            map_.resize(cols, rows)
            continue
        if key == ord("f"):
            _read_filter(
                stdscr,
                state,
                lambda: _draw(
                    stdscr, map_, state, events, time.monotonic(), anchor_holder
                ),
            )
            _apply_filter(state, time.monotonic())
            continue
        running = _handle_key(key, state, map_)
    stop.set()
    return 0


def _start_anchor_search(
    resolver: GeoResolver, cfg: Config
) -> list[tuple[Geo | None, float | None]]:
    """Look the place of the machine up in a thread of its own.

    Args:
        resolver: The resolver that knows the places.
        cfg: The configuration of the program.

    Returns:
        A list of one element that holds the place of the machine and
        the time the search took.  The element is filled when the
        search is over.
    """
    holder: list[tuple[Geo | None, float | None]] = []

    def worker() -> None:
        """Look the place of the machine up."""
        try:
            holder.append(_resolve_anchor(resolver, cfg))
        except Exception as exc:  # noqa: BLE001 - the map works without it
            log.debug("cannot find the anchor: %s", exc)
            holder.append((None, None))

    thread = threading.Thread(target=worker, name="trafficmap-anchor", daemon=True)
    thread.start()
    return holder


def _draw(
    stdscr: "curses.window",
    map_: _Map,
    state: _State,
    events: "queue.Queue[tuple[list[FlowEvent], Stats, float]]",
    now: float,
    anchor_holder: list[tuple[Geo | None, float | None]],
) -> None:
    """Draw one frame of the map.

    Args:
        stdscr: The screen of the terminal.
        map_: The map: the canvas, the land and the viewport.
        state: What the interface knows.
        events: The queue the collector puts the events into.
        now: The moment of the frame, in seconds.
        anchor_holder: The place of the machine, when it is known.
    """
    rows, cols = stdscr.getmaxyx()
    if cols < MIN_COLS or rows < MIN_ROWS:
        stdscr.erase()
        _addstr(
            stdscr,
            0,
            0,
            f"terminal too small: need {MIN_COLS}x{MIN_ROWS}",
            _C_WARNING,
        )
        stdscr.refresh()
        return

    if anchor_holder:
        geo, elapsed = anchor_holder[0]
        state.anchor = geo
        state.anchor_text = _anchor_label(geo, elapsed)
        # The animator needs the anchor of its own: the routes of the
        # comets are built with it.
        state.animator.anchor = geo
        map_.anchor = geo

    batch: list[FlowEvent] = []
    while True:
        try:
            events_now, stats, _ = events.get_nowait()
        except queue.Empty:
            break
        batch.extend(events_now)
        state.stats = stats

    for event in batch:
        state.remember(event)
    if not state.paused:
        state.now = now
        for event in batch:
            if not state.matches(event):
                continue
            state.animator.handle_event(event, now)
    # The expired comets and peers are taken away at every frame, so
    # that they do not pile up.
    state.animator.prune(state.now)

    frame = _render(map_, state, state.now, state.stats)
    _paint(stdscr, frame)


def run(cfg: Config) -> int:
    """Show the live map in the terminal.

    Args:
        cfg: The configuration of the program.

    Returns:
        ``0`` when the program was stopped, ``2`` when it cannot run.
    """
    if not sys.stdout.isatty():
        print("TUI requires a terminal", file=sys.stderr)
        return 2
    try:
        resolver = GeoResolver(cfg.city_db, cfg.asn_db, cfg.cache_dir)
    except FileNotFoundError as exc:
        print(f"tmap: {exc}", file=sys.stderr)
        print("tmap: run 'make assets' to download the databases", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"tmap: {exc}", file=sys.stderr)
        return 2
    if not cfg.land_geojson.exists():
        print(f"tmap: land map not found: {cfg.land_geojson}", file=sys.stderr)
        print("tmap: run 'make assets' to download the map", file=sys.stderr)
        return 2
    try:
        collector = _make_collector(cfg)
    except OSError as exc:
        print(f"tmap: cannot read the recording: {exc}", file=sys.stderr)
        return 2

    try:
        return curses.wrapper(_loop, cfg, resolver, collector)
    except KeyboardInterrupt:
        return 0
    except curses.error as exc:
        print(f"tmap: the terminal cannot show the map: {exc}", file=sys.stderr)
        return 2


def snapshot(cfg: Config, path: Path, frames: int) -> int:
    """Render the map without a terminal and write the last frame.

    The frames are rendered one after the other, with the clock of the
    collector, so that the same recording always gives the same
    picture.

    Args:
        cfg: The configuration of the program.
        path: The file the last frame is written to.
        frames: The number of frames to render.

    Returns:
        ``0`` when the frame was written, ``2`` when it cannot be.
    """
    try:
        resolver = GeoResolver(cfg.city_db, cfg.asn_db, cfg.cache_dir)
    except FileNotFoundError as exc:
        print(f"tmap: {exc}", file=sys.stderr)
        print("tmap: run 'make assets' to download the databases", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"tmap: {exc}", file=sys.stderr)
        return 2
    if not cfg.land_geojson.exists():
        print(f"tmap: land map not found: {cfg.land_geojson}", file=sys.stderr)
        print("tmap: run 'make assets' to download the map", file=sys.stderr)
        return 2
    try:
        collector = _make_collector(cfg)
    except OSError as exc:
        print(f"tmap: cannot read the recording: {exc}", file=sys.stderr)
        return 2

    state = _State(cfg)
    map_ = _Map(cfg, SNAPSHOT_COLS, SNAPSHOT_ROWS)
    table = FlowTable(resolver, poll_ms=cfg.poll_ms)
    clock = _clock_of(collector)

    if not cfg.no_anchor:
        geo, elapsed = _resolve_anchor(resolver, cfg)
        state.anchor = geo
        state.anchor_text = _anchor_label(geo, elapsed)
        state.animator.anchor = geo
        map_.anchor = geo

    frame: _Frame | None = None
    previous: float | None = None
    for _ in range(max(1, frames)):
        now = clock()
        rows = collector.poll()
        events: list[FlowEvent] = []
        if previous is None or now != previous:
            events = table.update(rows, now)
            previous = now
        for event in events:
            state.remember(event)
            if state.matches(event):
                state.animator.handle_event(event, now)
        state.now = now
        state.animator.prune(state.now)
        state.stats = table.stats()
        frame = _render(map_, state, state.now, state.stats)

    if frame is None:  # pragma: no cover - the loop always runs once
        return 2
    try:
        Path(path).write_text(frame.text(), encoding="utf-8")
    except OSError as exc:
        print(f"tmap: cannot write the snapshot: {exc}", file=sys.stderr)
        return 2
    return 0
