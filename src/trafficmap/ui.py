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

A collector that fails does not kill the thread: the error is kept and
shown in the status bar, and the poll is tried again with a growing
delay, from one second to five.  The data that was collected stays on
the screen while the collector is down.

The filter
----------
The filter of the process hides the flows that do not hold its name.
The peers of the hidden flows are kept, they are only not drawn: the
filter is therefore reversible, and the numbers of the status bar
count the flows that are on the map.  The escape key takes the filter
away, and the map shows every flow again.

The ASCII mode
--------------
The Braille characters of the Unicode block U+2800 need a font that has
them.  When the terminal has none, the ``--ascii`` option draws the map
with plain ASCII characters instead: one character per cell, the glyph
of the style of the cell.  The colours are the same, the canvas is the
same, only the characters change.

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
    STYLE_PULSE,
    STYLE_PULSE_DIM,
    STYLE_TRAIL_BRIGHT,
    STYLE_TRAIL_DIM,
    STYLE_TRAIL_MID,
    Animator,
)
from trafficmap.braille import Canvas
from trafficmap.collector_mock import MockCollector
from trafficmap.collector_pcap import Burst, PcapCollector
from trafficmap.collector_ss import SocketRow, SsCollector
from trafficmap.config import Config
from trafficmap.flows import (
    TOP_LIMIT,
    UNKNOWN_PROCESS,
    Flow,
    FlowEvent,
    FlowTable,
    Stats,
)
from trafficmap.geo import Geo, GeoResolver
from trafficmap.projection import Viewport, project, unproject
from trafficmap.worldmap import load_geojson, load_land, rasterize

__all__ = ["make_collector", "run", "snapshot"]

log = logging.getLogger(__name__)

#: Number of lines of the status bar.
STATUS_ROWS = 3

#: Seconds without packets after which the rate of the packets is
#: forgotten.
PACKET_RATE_TTL = 2.0

#: Smallest terminal the map is drawn on.
MIN_COLS = 80
MIN_ROWS = 24

#: Size of the terminal the snapshot pretends to be drawn on.
SNAPSHOT_COLS = 80
SNAPSHOT_ROWS = 24

#: The span of the world at the first zoom level.
WORLD_SPAN: tuple[float, float] = (360.0, 180.0)

#: The zoom factor of the preset keys ``1``, ``2`` and ``3``: the span
#: of the world is divided by it.
ZOOM_PRESETS: dict[int, float] = {
    1: 1.0,
    2: 3.0,
    3: 12.0,
}

#: The smallest zoom factor: the whole world.
MIN_ZOOM = 1.0

#: The greatest zoom factor: a span of three degrees of longitude, some
#: three hundred kilometres, where the scatter of a city is seen.
MAX_ZOOM = 120.0

#: The factor one step of the zoom keys and of the wheel changes.
ZOOM_STEP = 1.25

#: Number of lines of the panel of the busiest flows.
TOP_PANEL_ROWS = 8

#: Width of the panel of the busiest flows.
TOP_PANEL_COLUMNS = 30

#: Width of the panel of the keys.
HELP_PANEL_COLUMNS = 46

#: The first delay between two polls after a failure of the collector,
#: in seconds.
RETRY_FIRST = 1.0

#: The step between two delays after a failure of the collector, in
#: seconds.
RETRY_STEP = 1.0

#: The greatest delay between two polls after a failure of the
#: collector, in seconds.
RETRY_MAX = 5.0

#: The keys the panel of the keys shows.
HELP_LINES = (
    " keys",
    " q  quit            space  pause the map",
    " t  busiest flows   l  country labels",
    " 1 2 3  zoom        + -  finer zoom",
    " f  filter a process 0  whole world",
    " ?  this help       esc  clears the filter",
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
    _Colour(curses.COLOR_MAGENTA, -1, curses.A_BOLD),  # 13: a pulse
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
_C_PULSE = 13

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
    STYLE_PULSE: _C_PULSE,
    STYLE_PULSE_DIM: _C_PULSE,
}

#: The glyph of every style of the canvas, for the ASCII mode.  The
#: empty cell is a space, so that the grid keeps its places.
_ASCII_GLYPHS: dict[int, str] = {
    0: " ",
    STYLE_LAND: "#",
    STYLE_TRAIL_DIM: "-",
    STYLE_TRAIL_MID: "-",
    STYLE_TRAIL_BRIGHT: "-",
    STYLE_PEER: "*",
    STYLE_COMET_HEAD: "+",
    STYLE_ANCHOR: "O",
    STYLE_PULSE: "*",
    STYLE_PULSE_DIM: "*",
}

#: The glyph of a cell whose style is not known.
_ASCII_UNKNOWN = " "

#: The canvas the incoming comets are drawn on a second time, to know
#: which cells they touch.  It is kept between the frames and cleared
#: at every frame: a new one at every frame would cost an allocation
#: the size of the map at every frame.
_scratch: Canvas | None = None

#: The size the scratch canvas was built for.
_scratch_size: tuple[int, int] = (0, 0)

#: The units of the rates, in bytes per second.
_RATE_UNITS = (
    ("KB", 1_000.0),
    ("MB", 1_000.0**2),
    ("GB", 1_000.0**3),
    ("TB", 1_000.0**4),
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


@dataclass(frozen=True, slots=True)
class _Sample:
    """One poll of the collector, as the interface receives it.

    Attributes:
        events: The events of the poll: the flows that appeared, that
            carried traffic or that went away.
        flows: The flows that are on the map.
        stats: The statistics of the whole machine.
        now: The moment of the poll, in seconds.
        bursts: The bursts of the packets, by peer.  They are empty
            when the collector does not read the packets.
    """

    events: list[FlowEvent]
    flows: list[Flow]
    stats: Stats
    now: float
    bursts: list[Burst] = field(default_factory=list)


class _State:
    """What the interface knows between two frames.

    Args:
        cfg: The configuration of the program.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.animator = Animator()
        self.stats = _empty_stats()
        self.table_stats = _empty_stats()
        self.anchor: Geo | None = None
        self.anchor_text = "anchor unknown"
        self.paused = False
        self.show_top = False
        self.show_labels = False
        self.show_help = False
        self.filter = cfg.filter_process
        self.filter_input: str | None = None
        self.directions: dict[str, str] = {}
        #: Every flow that is on the map, by key.
        self.flows: dict[str, Flow] = {}
        #: The flows that pass the filter of the process.
        self.visible_flows: list[Flow] = []
        #: The keys of the flows the filter leaves out.
        self.hidden: set[str] = set()
        #: The thread that polls the sockets, when the map is live.
        self.collector: _CollectorThread | None = None
        #: Whether the collector reads the sockets, and only them: the
        #: status bar warns about the privileges only then.
        self.sockets_only = False
        #: The message of the last failure of the collector.
        self.error: str | None = None
        #: The words that tell the user what is broken.
        self.error_label = "collector error"
        #: The rate of the packets that were read, in packets per
        #: second.  It is zero when the packets are not read.
        self.packet_rate = 0.0
        #: The moment of the last poll that carried packets.
        self.packet_time: float | None = None
        #: The clock of the animation.  It stands still while the map
        #: is paused, so that the map is frozen as a whole.
        self.now = 0.0
        self.refresh()

    def update(self, flows: list[Flow], stats: Stats) -> None:
        """Take the flows and the statistics of the last poll into account.

        The flows are the ones the table shows: they are the truth of
        the map, and they replace the ones of the previous poll.

        Args:
            flows: The flows that are on the map.
            stats: The statistics of the whole machine.
        """
        self.table_stats = stats
        self.flows = {flow.key: flow for flow in flows}
        self.directions = {
            _label_of(flow): "↑" if flow.tx_rate >= flow.rx_rate else "↓"
            for flow in flows
        }
        self.refresh()

    def set_filter(self, text: str) -> None:
        """Set the filter of the process and apply it at once.

        An empty text removes the filter: the peers of the flows that
        were hidden come back at once.

        Args:
            text: The name of the process to keep.
        """
        self.filter = text
        self.refresh()

    def refresh(self) -> None:
        """Recompute the flows that pass the filter and the statistics.

        The hidden flows keep their peers: the animator is told which
        keys are not on the map, and it does not draw them.
        """
        visible: list[Flow] = []
        hidden: set[str] = set()
        for flow in self.flows.values():
            if self.matches(flow):
                visible.append(flow)
            else:
                hidden.add(flow.key)
        self.visible_flows = visible
        self.hidden = hidden
        self.animator.set_hidden(hidden)
        self.stats = _filtered_stats(self, self.table_stats)

    def matches(self, flow: Flow) -> bool:
        """Tell whether a flow passes the filter of the process.

        Args:
            flow: The flow.

        Returns:
            ``True`` when the filter is empty, or when the process of
            the flow holds the name of the filter.
        """
        if not self.filter:
            return True
        return self.filter in (flow.process or "")

    def read_error(self) -> None:
        """Take the error of the collector into account.

        The error is ``None`` when the last poll of the collector
        worked.
        """
        collector = self.collector
        self.error = collector.error if collector is not None else None
        self.error_label = (
            collector.error_label if collector is not None else "collector error"
        )


class _Map:
    """The map: the canvas, the land and the viewport.

    Args:
        cfg: The configuration of the program.
        cols: The width of the screen, in characters.
        rows: The height of the screen, in characters.
    """

    def __init__(self, cfg: Config, cols: int, rows: int) -> None:
        self._cfg = cfg
        #: The zoom factor: the span of the world is divided by it.
        self.zoom = MIN_ZOOM
        #: The place the view is centred on, or ``None`` to follow the
        #: anchor.  A zoom around a point of the screen moves it away
        #: from the anchor, and the map stays there.
        self.center: tuple[float, float] | None = None
        self.anchor: Geo | None = None
        self.cols = cols
        self.rows = rows
        self.map_height = max(1, rows - STATUS_ROWS)
        self.canvas = Canvas(cols * 2, self.map_height * 4)
        self._world: bytearray | None = None
        self._world_size = (0, 0)
        self._geojson: dict | None = None
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

    def spans(self) -> tuple[float, float]:
        """Return the spans of the view at the current zoom.

        Returns:
            The span of the longitudes and the span of the latitudes,
            in degrees.
        """
        zoom = min(MAX_ZOOM, max(MIN_ZOOM, self.zoom))
        return (WORLD_SPAN[0] / zoom, WORLD_SPAN[1] / zoom)

    def viewport(self) -> Viewport:
        """Return the part of the world the canvas shows.

        The view follows the anchor when it has one, and it stays where
        a zoom around a point of the screen left it otherwise.  The
        centre never leaves the range of the latitudes: a view that
        hangs over the pole shows rows of nothing and cuts the other
        pole off.

        Returns:
            The viewport, centred on the anchor when the zoom did not
            move it.
        """
        lon_span, lat_span = self.spans()
        center_lat = 0.0
        center_lon = 0.0
        if self.center is not None:
            center_lat, center_lon = self.center
        else:
            anchor = self.anchor
            if anchor is not None and anchor.lat is not None and anchor.lon is not None:
                center_lat = anchor.lat
                center_lon = anchor.lon
        if center_lon < -180.0 or center_lon >= 180.0:
            center_lon = (center_lon + 180.0) % 360.0 - 180.0
        return Viewport(
            width=self.canvas.width,
            height=self.canvas.height,
            center_lat=_clamp_lat(center_lat, lat_span),
            center_lon=center_lon,
            lon_span=lon_span,
            lat_span=lat_span,
        )

    def set_zoom(self, zoom: float) -> None:
        """Set the zoom factor and centre the view on the anchor again.

        Args:
            zoom: The zoom factor: the span of the world is divided by
                it.  A factor out of the limits is brought back into
                them.
        """
        self.zoom = min(MAX_ZOOM, max(MIN_ZOOM, zoom))
        self.center = None
        self._visible = None
        self._visible_key = None

    def reset_zoom(self) -> None:
        """Show the whole world again, centred on the anchor."""
        self.set_zoom(MIN_ZOOM)

    def zoom_by(self, factor: float, cursor: tuple[int, int] | None = None) -> None:
        """Zoom in or out, keeping the point under the cursor in place.

        The point of the world the cursor is on does not move on the
        screen: the view is moved around it, so that a wheel over a
        city zooms on that city and not on the middle of the screen.

        Args:
            factor: The factor the zoom is multiplied by: a factor
                greater than one zooms in.
            cursor: The dot of the canvas the zoom is centred on, or
                ``None`` for the middle of the map.

        Raises:
            ValueError: If the factor is not positive.
        """
        if factor <= 0:
            raise ValueError(f"zoom factor must be positive, got {factor}")
        new_zoom = min(MAX_ZOOM, max(MIN_ZOOM, self.zoom * factor))
        if new_zoom == self.zoom:
            return
        old = self.viewport()
        if cursor is None:
            cursor = (old.width // 2, old.height // 2)
        x = min(max(cursor[0], 0), old.width - 1)
        y = min(max(cursor[1], 0), old.height - 1)
        lat, lon = unproject(x, y, old)
        self.zoom = new_zoom
        lon_span, lat_span = self.spans()
        # The point of the world stays under the cursor: its place on
        # the screen is the one of the old view, and the centre moves
        # by what the new span puts between the cursor and the middle.
        center_lon = lon - (x / (old.width - 1) - 0.5) * lon_span
        center_lat = lat - (0.5 - y / (old.height - 1)) * lat_span
        self.center = (center_lat, center_lon)
        self._visible = None
        self._visible_key = None

    def land_mask(self) -> bytearray:
        """Return the land of the visible part of the world.

        The whole world is rasterized once and cached on disk, and the
        view of the whole world is taken out of that raster.  A zoomed
        view is rasterized again from the polygons, at the resolution
        of the canvas: the coast of a zoomed map is as sharp as the
        dots allow, and it does not show the blocks of the world raster
        enlarged.

        The land is always taken in the frame of the viewport: the
        peers, the comets and the anchor are placed in that same frame,
        and the land has to be there too.

        Returns:
            One byte per dot of the canvas: ``1`` for land, ``0`` for
            water.
        """
        viewport = self.viewport()
        key = (
            float(self.canvas.width),
            float(self.canvas.height),
            round(viewport.lon_span, 9),
            round(viewport.lat_span, 9),
            round(viewport.center_lat, 6),
            round(viewport.center_lon, 6),
        )
        if self._visible is not None and self._visible_key == key:
            return self._visible
        if viewport.lon_span >= WORLD_SPAN[0] and viewport.lat_span >= WORLD_SPAN[1]:
            visible = self._world_mask()
            if not _shows_the_whole_world(viewport):
                visible = _sample_mask(visible, viewport)
        else:
            visible = self._zoom_mask(viewport)
        self._visible = visible
        self._visible_key = key
        return visible

    def _zoom_mask(self, viewport: Viewport) -> bytearray:
        """Return the land of a zoomed view, drawn from the polygons.

        Args:
            viewport: The part of the world the canvas shows.

        Returns:
            One byte per dot of the canvas: ``1`` for land, ``0`` for
            water.
        """
        if self._geojson is None:
            self._geojson = load_geojson(self._cfg.land_geojson)
        return rasterize(self._geojson, self.canvas.width, self.canvas.height, viewport)

    def _world_mask(self) -> bytearray:
        """Return the raster of the whole world, in the size of the canvas."""
        size = (self.canvas.width, self.canvas.height)
        if self._world is None or self._world_size != size:
            self._world = load_land(
                self._cfg.cache_dir, self._cfg.land_geojson, size[0], size[1]
            )
            self._world_size = size
        return self._world


def _clamp_lat(center_lat: float, lat_span: float) -> float:
    """Keep the centre of the view inside the range of the latitudes.

    The latitudes stop at ninety degrees: a view that is centred higher
    than that shows rows of nothing at the top and cuts the south pole
    off.  The centre is moved to the closest place where the whole span
    of the view fits in the world.  The anchor stays inside the view,
    because it is a place of the world itself.

    Args:
        center_lat: The latitude the view is centred on.
        lat_span: The span of the view, in degrees.

    Returns:
        The latitude of the centre of the view.
    """
    half = lat_span / 2
    return max(-90.0 + half, min(90.0 - half, center_lat))


def _shows_the_whole_world(viewport: Viewport) -> bool:
    """Tell whether the viewport is the one of the whole world.

    The raster of the world is the picture of the world in the frame
    of the world: the viewport that is centred on the Greenwich
    meridian and that spans the whole world is that frame, dot for
    dot.  Every other viewport has to take the land it shows out of
    the raster, or the land would be painted in the wrong place.

    Args:
        viewport: The part of the world the canvas shows.

    Returns:
        ``True`` when the raster of the world is the visible part.
    """
    return (
        viewport.lon_span >= 360.0
        and viewport.lat_span >= 180.0
        and viewport.center_lon == 0.0
        and viewport.center_lat == 0.0
    )


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


def _label_of(flow: Flow) -> str:
    """Return the name of a flow, as the status bar shows it.

    Args:
        flow: The flow.

    Returns:
        The name of the process and the port of the flow,
        ``"firefox:443"`` for example.
    """
    return f"{flow.process or UNKNOWN_PROCESS}:{flow.port}"


def _filtered_stats(state: _State, table_stats: Stats) -> Stats:
    """Return the statistics of the flows that are on the map.

    The rates of the machine, and the number of the local and of the
    unknown addresses, are the ones of the whole machine: they tell
    what the machine does, not what the map shows.  The number of the
    flows, the number of the peers, the countries and the busiest
    flows are the ones of the flows that pass the filter.

    Args:
        state: What the interface knows.
        table_stats: The statistics of the table of the flows.

    Returns:
        The statistics of the visible flows.
    """
    countries: Counter[str] = Counter()
    peers: set[str] = set()
    totals: dict[str, float] = {}
    for flow in state.visible_flows:
        peers.add(flow.remote.ip)
        countries[flow.remote.country] += 1
        label = _label_of(flow)
        totals[label] = totals.get(label, 0.0) + flow.tx_rate + flow.rx_rate
    top = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return Stats(
        tx_rate=table_stats.tx_rate,
        rx_rate=table_stats.rx_rate,
        flows=len(state.visible_flows),
        peers=len(peers),
        countries=countries,
        top=top[:TOP_LIMIT],
        local=table_stats.local,
        unknown=table_stats.unknown,
    )


def _format_rate(value: float) -> str:
    """Format a rate in bytes per second.

    A rate is not an amount of bytes: it is what flows every second.
    The units are the decimal ones, and the value has one decimal, so
    that a rate of nine megabytes a second reads ``"9.0 MB/s"`` and not
    ``"9.0M"``.

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


def _format_zoom(zoom: float) -> str:
    """Format the zoom factor for the status bar.

    Args:
        zoom: The zoom factor.

    Returns:
        The factor, ``"x3"`` for a preset and ``"x1.6"`` for a step of
        the wheel.
    """
    rounded = round(zoom)
    if abs(zoom - rounded) < 0.05:
        return f"x{rounded}"
    return f"x{zoom:.1f}"


def _format_packets(value: float) -> str:
    """Format a rate of packets per second.

    Args:
        value: The rate, in packets per second.

    Returns:
        The rate, ``"834 pkt/s"`` or ``"12.4k pkt/s"`` for example.
    """
    number = float(value)
    if number < 1000.0:
        return f"{int(number)} pkt/s"
    return f"{number / 1000.0:.1f}k pkt/s"


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


class _ProcessLookup:
    """The processes of the machine, by the port of their socket.

    The packets of a capture do not know the process that sent them:
    the table of the sockets of the machine does.  The table is read
    at most once a second, because ``ss`` is not cheap.

    Args:
        collector: The collector that reads the sockets.
        interval: The smallest time between two reads of the table, in
            seconds.
    """

    def __init__(
        self, collector: SsCollector | None = None, interval: float = 1.0
    ) -> None:
        self._collector = collector if collector is not None else SsCollector()
        self._interval = interval
        self._ports: dict[int, tuple[int | None, str | None]] = {}
        self._read_at: float | None = None

    def __call__(self) -> dict[int, tuple[int | None, str | None]]:
        """Return the processes of the machine, by port.

        Returns:
            The table of the processes, ``{port: (pid, name)}``.  The
            last table is kept when the sockets cannot be read.
        """
        now = time.monotonic()
        if self._read_at is not None and now - self._read_at < self._interval:
            return self._ports
        self._read_at = now
        try:
            rows = self._collector.poll()
        except RuntimeError as exc:
            log.debug("cannot read the sockets: %s", exc)
            return self._ports
        ports: dict[int, tuple[int | None, str | None]] = {}
        for row in rows:
            ports[row.local[1]] = (row.pid, row.process)
        self._ports = ports
        return ports


class _FallbackCollector:
    """The packets of the network when they can be read, the sockets otherwise.

    The reading of the packets needs privileges that the user may not
    have given to the program.  The map is worth showing anyway: the
    sockets of the machine are read instead, and the status bar says
    that the packets are not tracked, so that the user knows what the
    map shows.

    Args:
        primary: The collector of the packets, or ``None`` when it
            could not even be built.
        fallback: The collector of the sockets.
        warning: The message of the failure of the capture, when it is
            already known.
    """

    #: The words that tell the user that the capture is broken.
    error_label = "capture error"

    def __init__(
        self,
        primary: object | None,
        fallback: object,
        warning: str | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._warning = warning
        self._switched = primary is None

    @property
    def warning(self) -> str | None:
        """The message that stays on the screen while the sockets are read."""
        return self._warning

    @property
    def sockets_only(self) -> bool:
        """Whether the collector reads the sockets, and only them.

        Returns:
            ``True`` when the packets are not read: the sockets of the
            machine are read instead, and without root they are only
            the sockets of the user.
        """
        return self._switched

    def poll(self) -> list[SocketRow]:
        """Read the traffic of the machine.

        Returns:
            The sockets that carry a flow.  The packets are read when
            they can be, the sockets when they cannot.
        """
        if not self._switched:
            try:
                return self._primary.poll()  # type: ignore[attr-defined]
            except RuntimeError as exc:
                # The capture is not allowed: the sockets are read
                # instead, and the map keeps working.
                self._switched = True
                self._warning = f"{exc} (sockets only)"
                log.info("cannot read the packets: %s", exc)
        return self._fallback.poll()  # type: ignore[attr-defined]

    def drain_bursts(self) -> list[Burst]:
        """Return the bursts of the packets, when there are any.

        Returns:
            The bursts of the packets since the last poll.  The list is
            empty when the packets are not read.
        """
        if self._switched:
            return []
        drain = getattr(self._primary, "drain_bursts", None)
        if drain is None:
            return []
        try:
            return list(drain())
        except Exception as exc:  # noqa: BLE001 - the map must survive
            log.debug("cannot read the bursts: %s", exc)
            return []

    def close(self) -> None:
        """Stop the collector that is read, and the one that is not."""
        for collector in (self._primary, self._fallback):
            close = getattr(collector, "close", None)
            if close is not None:
                close()


def make_collector(cfg: Config) -> SsCollector | PcapCollector | MockCollector:
    """Build the collector of the program.

    Args:
        cfg: The configuration of the program.

    Returns:
        The mock collector when the configuration names a recording,
        the collector of the packets when the configuration asks for
        the packets, the collector of ``ss`` otherwise.  A capture
        that cannot start does not stop the program: the sockets are
        read instead, and the status bar shows what is broken.

    Raises:
        FileNotFoundError: If the recording does not exist.
        OSError: If the recording cannot be read.
    """
    if cfg.mock is not None:
        return MockCollector(cfg.mock)
    if cfg.capture == "pcap":
        try:
            capture: object | None = PcapCollector(
                interface=cfg.iface, process_lookup=_ProcessLookup()
            )
            warning: str | None = None
        except RuntimeError as exc:
            log.info("cannot start the capture: %s", exc)
            capture = None
            warning = f"{exc} (sockets only)"
        return _FallbackCollector(capture, SsCollector(), warning)
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


def _scratch_canvas(width: int, height: int) -> Canvas:
    """Return the scratch canvas of the incoming comets, empty.

    The canvas of the last frame is reused when it has the right size,
    and it is cleared: it holds nothing but the comets of this frame.

    Args:
        width: The width of the canvas, in dots.
        height: The height of the canvas, in dots.

    Returns:
        The scratch canvas, empty.
    """
    global _scratch, _scratch_size
    if _scratch is None or _scratch_size != (width, height):
        _scratch = Canvas(width, height)
        _scratch_size = (width, height)
    else:
        _scratch.clear()
    return _scratch


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
    scratch = _scratch_canvas(width, height)
    for comet in comets:
        points = comet.trail(now, animator.trail_length)
        if not points:
            continue
        previous: tuple[int, int] | None = None
        for lat, lon in points:
            point = project(lat, lon, viewport)
            if point is None:
                previous = None
                continue
            if previous is not None:
                scratch.line(previous[0], previous[1], point[0], point[1], 1)
            else:
                # The first point of the trail has no line to it: it
                # is marked by itself, or the head of a newborn comet
                # would be painted in the colour of the other comets.
                scratch.set(point[0], point[1], 1)
            previous = point
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


def _text_rows(canvas: Canvas, styles: list[list[int]], ascii_mode: bool) -> list[str]:
    """Return the text of every character row of the map.

    The Braille mode uses the characters the canvas builds: one cell of
    the canvas is one character of the Unicode block U+2800, and the
    eight dots of the cell are the eight dots of the character.

    The ASCII mode uses one character per cell instead: the character is
    the glyph of the style of the cell, so a terminal whose font has no
    Braille glyphs still shows the map.  The styles are the same, only
    the characters change.

    Args:
        canvas: The canvas of the map.
        styles: The style of every cell of the canvas, one list per row.
        ascii_mode: Whether the map is drawn with ASCII characters.

    Returns:
        The text of the rows of the map, one string per character row.
    """
    if not ascii_mode:
        return canvas.rows()
    return [
        "".join(_ASCII_GLYPHS.get(style, _ASCII_UNKNOWN) for style in row)
        for row in styles
    ]


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

    style_rows = canvas.cell_styles()
    text_rows = _text_rows(canvas, style_rows, state.cfg.ascii)
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
    for peer in state.animator.visible_peers:
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
        state.visible_flows, key=lambda flow: -(flow.tx_rate + flow.rx_rate)
    )
    for flow in busiest[:TOP_PANEL_ROWS]:
        arrow = "↑" if flow.tx_rate >= flow.rx_rate else "↓"
        rate = _format_rate(flow.tx_rate + flow.rx_rate)
        label = _label_of(flow)[:14].ljust(14)
        lines.append(f" {label} {flow.remote.country:<2} {arrow}{rate:>6}")
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
    if state.packet_rate > 0:
        parts.append(_format_packets(state.packet_rate))
    countries = [code for code, _ in stats.countries.most_common(4) if code != "--"]
    if countries:
        parts.append(" ".join(countries))
    first = [("   ".join(parts)[: map_.cols], _C_DEFAULT)]

    items = []
    for label, rate in stats.top[:3]:
        arrow = state.directions.get(label, "·")
        items.append(f"{label} {arrow}{_format_rate(rate)}")
    second = [(" · ".join(items)[: map_.cols], _C_DEFAULT)]

    third: list[tuple[str, int]] = []
    parts: list[tuple[str, int]] = []
    if state.error:
        # The collector is down: the banner comes first, it is the
        # most important thing the user has to know.
        parts.append((f"{state.error_label}: {state.error}", _C_WARNING))
    sockets_without_root = state.sockets_only and not _is_root()
    if sockets_without_root:
        # Not root and the sockets are read: they show only the
        # sockets of the user.  With root, or with the packets of the
        # machine, this warning would be a lie.
        parts.append(("no root: only your sockets", _C_WARNING))
    parts.append((state.anchor_text, _C_DEFAULT))
    if map_.zoom > MIN_ZOOM:
        parts.append((f"zoom {_format_zoom(map_.zoom)}", _C_DEFAULT))
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

    A collector that fails does not kill the thread: the error is
    remembered and shown in the status bar, and the poll is tried
    again with a growing delay, from :data:`RETRY_FIRST` to
    :data:`RETRY_MAX` seconds.  The delay goes back to
    :data:`RETRY_FIRST` after a poll that worked.  The flows that were
    collected stay on the screen all the time.

    Args:
        collector: The collector that reads the sockets.
        table: The table of the flows.
        out: The queue the events are put into.
        poll_ms: The length of a poll, in milliseconds.
        stop: The event that asks the thread to stop.
        wait: The function that waits between two polls.  The wait of
            the stop event is used when it is missing.  The tests give
            their own, to keep the pace of the thread.
    """

    def __init__(
        self,
        collector: object,
        table: FlowTable,
        out: "queue.Queue[_Sample]",
        poll_ms: int,
        stop: threading.Event,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        super().__init__(name="trafficmap-collector", daemon=True)
        self._collector = collector
        self._table = table
        self._out = out
        self._interval = poll_ms / 1000.0
        self._stop = stop
        self._clock = _clock_of(collector)
        self._wait = wait if wait is not None else stop.wait
        self._error: str | None = None
        self._error_lock = threading.Lock()

    @property
    def error(self) -> str | None:
        """The message of the last failure of the collector.

        The message is ``None`` when the last poll worked.
        """
        with self._error_lock:
            return self._error

    @property
    def error_label(self) -> str:
        """The words that tell the user what is broken.

        They are ``"capture error"`` when the collector reads the
        packets, and ``"collector error"`` otherwise.
        """
        return getattr(self._collector, "error_label", "collector error")

    @property
    def sockets_only(self) -> bool:
        """Whether the collector reads the sockets, and only them.

        Returns:
            ``True`` when the collector does not read the packets: what
            it sees is the sockets of the machine.
        """
        return bool(getattr(self._collector, "sockets_only", False))

    def _set_error(self, message: str | None) -> None:
        """Remember the message of the last failure of the collector.

        Args:
            message: The message of the failure, or ``None`` when the
                last poll worked.
        """
        with self._error_lock:
            self._error = message

    def run(self) -> None:
        """Poll the sockets until the program stops."""
        previous: float | None = None
        retry = RETRY_FIRST
        while not self._stop.is_set():
            now = self._clock()
            collector = self._collector
            try:
                rows: list[SocketRow] = collector.poll()  # type: ignore[attr-defined]
                bursts = _drain_bursts(collector)
            except Exception as exc:  # noqa: BLE001 - the map must survive
                # The collector is down: the error is shown in the
                # status bar and the poll is tried again, later and
                # later.  The message is logged at the info level: a
                # warning would be written on the screen of the
                # terminal, in the middle of the map.
                log.info("cannot read the sockets: %s", exc)
                self._set_error(_error_text(exc))
                self._wait(retry)
                retry = min(retry + RETRY_STEP, RETRY_MAX)
                continue
            # A collector that works may still have something to say:
            # the capture of the packets falls back to the sockets
            # when it is not allowed, and the message stays on the
            # screen.
            self._set_error(getattr(collector, "warning", None))
            retry = RETRY_FIRST
            if previous is None or now != previous:
                events = self._table.update(rows, now)
                previous = now
                self._out.put(
                    _Sample(
                        events=events,
                        flows=self._table.flows(),
                        stats=self._table.stats(),
                        now=now,
                        bursts=bursts,
                    )
                )
            self._wait(self._interval)


def _drain_bursts(collector: object) -> list[Burst]:
    """Take the bursts of the packets of a collector, if it has any.

    Args:
        collector: The collector that reads the sockets.

    Returns:
        The bursts of the packets of the last poll.  The list is empty
        for the collectors that do not read the packets.
    """
    drain = getattr(collector, "drain_bursts", None)
    if drain is None:
        return []
    try:
        return list(drain())
    except Exception as exc:  # noqa: BLE001 - the map must survive
        log.debug("cannot read the bursts: %s", exc)
        return []


def _error_text(exc: BaseException) -> str:
    """Return the text of a failure of the collector.

    Args:
        exc: The failure.

    Returns:
        The message of the failure, or the name of its class when the
        failure has no message of its own.
    """
    return str(exc) or type(exc).__name__


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
        resolver.set_home(lat, lon)
        return geo, None
    start = time.monotonic()
    geo = resolver.anchor()
    elapsed = (time.monotonic() - start) * 1000.0
    if geo is None:
        return None, None
    if geo.lat is not None and geo.lon is not None:
        # The network of an address answers from the point of presence
        # closest to this machine: the resolver needs to know where the
        # machine is.
        resolver.set_home(geo.lat, geo.lon)
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

    The escape key takes the filter of the process away: the flows of
    the other processes come back on the map at once.

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
        map_.set_zoom(ZOOM_PRESETS[key - ord("0")])
    elif key in (ord("+"), ord("=")):
        map_.zoom_by(ZOOM_STEP)
    elif key in (ord("-"), ord("_")):
        map_.zoom_by(1.0 / ZOOM_STEP)
    elif key == ord("0"):
        map_.reset_zoom()
    elif key == 27:
        # The escape key gives the filter up: every flow is on the map
        # again, and the status bar does not show a filter any more.
        state.set_filter("")
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

    The filter is applied as soon as it is known: the peers of the
    flows that it leaves out are hidden at once, and they come back at
    once when it is removed.

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
            state.set_filter(text.strip())
            break
        if key == 27:
            # The escape key gives the filter up: the map shows every
            # flow again.
            state.set_filter("")
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


def _zoom_with_mouse(map_: _Map, event: tuple[int, int, int, int, int]) -> None:
    """Zoom the map in or out with the wheel of the mouse.

    The wheel zooms around the point under the pointer: the city under
    the mouse stays under the mouse.  A wheel that is not on the map
    zooms around the middle of the map.

    Args:
        map_: The map to zoom.
        event: The event of the mouse, as ``curses.getmouse`` returns
            it: the id, the column, the line, the state of the buttons
            and the buttons.
    """
    _, mouse_x, mouse_y, _, buttons = event
    if mouse_y >= map_.map_height:
        cursor = None
    else:
        # A character cell of the map holds two dots in width and four
        # in height.
        cursor = (mouse_x * 2, mouse_y * 4)
    if buttons & curses.BUTTON4_PRESSED:
        map_.zoom_by(ZOOM_STEP, cursor)
    elif buttons & curses.BUTTON5_PRESSED:
        map_.zoom_by(1.0 / ZOOM_STEP, cursor)


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
    # The screen is read without waiting: the timeout of the screen
    # would be of no use, the frame is drawn by the clock of the
    # program and not by the keys.
    stdscr.nodelay(True)
    # The wheel of the mouse zooms the map.  A terminal that has no
    # mouse reports no events, and the keys are enough.
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    except curses.error:
        log.debug("the terminal has no mouse")
    _init_colours()

    state = _State(cfg)
    rows, cols = stdscr.getmaxyx()
    map_ = _Map(cfg, cols, rows)
    events: "queue.Queue[_Sample]" = queue.Queue()
    stop = threading.Event()
    table = FlowTable(resolver, poll_ms=cfg.poll_ms)
    collector_thread = _CollectorThread(collector, table, events, cfg.poll_ms, stop)
    state.collector = collector_thread
    state.sockets_only = collector_thread.sockets_only
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
        if key == curses.KEY_MOUSE:
            try:
                event = curses.getmouse()
            except curses.error:
                continue
            _zoom_with_mouse(map_, event)
            continue
        if key == ord("f"):
            _read_filter(
                stdscr,
                state,
                lambda: _draw(
                    stdscr, map_, state, events, time.monotonic(), anchor_holder
                ),
            )
            continue
        running = _handle_key(key, state, map_)
    stop.set()
    collector_thread.join(timeout=2.0)
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


def _spawn_pulses(state: _State, bursts: list[Burst], now: float) -> None:
    """Put the sparks of the packets on the map.

    A burst is the traffic of one peer since the last poll.  The place
    of the peer comes from the flows of the map: a peer that has no
    flow has no place, and no spark.

    Args:
        state: What the interface knows.
        bursts: The bursts of the packets.
        now: The moment of the frame, in seconds.
    """
    if not bursts:
        # The packets stopped: the rate is forgotten, so that a quiet
        # machine does not show the rate of the last burst forever.
        if state.packet_time is not None and now - state.packet_time > PACKET_RATE_TTL:
            state.packet_rate = 0.0
        return
    places: dict[str, Flow] = {}
    for flow in state.visible_flows:
        places.setdefault(flow.remote.ip, flow)
    packets = 0
    for burst in bursts:
        packets += burst.packets
        flow = places.get(burst.remote)
        if flow is None:
            continue
        # The spark is put at the moment its packets happened, not at
        # the moment they were read: the age of the burst is taken
        # away from the clock of the frame.
        state.animator.spawn_pulse(flow.remote, now - burst.age, burst.bytes, flow.key)
    previous = state.packet_time
    state.packet_time = now
    if previous is not None and now > previous:
        instant = packets / (now - previous)
        state.packet_rate = (
            instant
            if state.packet_rate == 0.0
            else 0.7 * state.packet_rate + 0.3 * instant
        )


def _draw(
    stdscr: "curses.window",
    map_: _Map,
    state: _State,
    events: "queue.Queue[_Sample]",
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

    state.read_error()

    batch: list[FlowEvent] = []
    bursts: list[Burst] = []
    flows: list[Flow] | None = None
    stats: Stats | None = None
    while True:
        try:
            sample = events.get_nowait()
        except queue.Empty:
            break
        batch.extend(sample.events)
        bursts.extend(sample.bursts)
        flows = sample.flows
        stats = sample.stats

    if flows is not None and stats is not None:
        # The flows of the table are the truth of the map: they are
        # filtered again here, for the numbers of the status bar.
        state.update(flows, stats)

    if not state.paused:
        state.now = now
        for event in batch:
            # Every event is taken into account, even the ones of the
            # flows the filter leaves out: their peers are kept, so
            # that they come back when the filter is removed.
            state.animator.handle_event(event, now)
        _spawn_pulses(state, bursts, now)
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
        resolver = GeoResolver(
            cfg.city_db, cfg.asn_db, cfg.cache_dir, scatter=True
        )
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
        collector = make_collector(cfg)
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
    finally:
        close = getattr(collector, "close", None)
        if close is not None:
            close()


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
        resolver = GeoResolver(
            cfg.city_db, cfg.asn_db, cfg.cache_dir, scatter=True
        )
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
        collector = make_collector(cfg)
    except OSError as exc:
        print(f"tmap: cannot read the recording: {exc}", file=sys.stderr)
        return 2

    state = _State(cfg)
    state.sockets_only = bool(getattr(collector, "sockets_only", False))
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
    events: list[FlowEvent] = []
    for _ in range(max(1, frames)):
        now = clock()
        try:
            rows = collector.poll()
        except Exception as exc:  # noqa: BLE001 - the map must survive
            # The collector is down: the last frame stays on the
            # screen and the error is shown in the status bar.
            log.info("cannot read the sockets: %s", exc)
            state.error = _error_text(exc)
        else:
            # A collector that works may still have something to say:
            # the capture of the packets falls back to the sockets
            # when it is not allowed.
            state.error = getattr(collector, "warning", None)
            state.error_label = getattr(collector, "error_label", "collector error")
            if previous is None or now != previous:
                events = table.update(rows, now)
                previous = now
            for event in events:
                state.animator.handle_event(event, now)
            state.update(table.flows(), table.stats())
            # The sparks of the packets are put on the frame as well:
            # a demonstration of the packets is a demonstration of the
            # sparks.
            _spawn_pulses(state, _drain_bursts(collector), now)
        state.now = now
        state.animator.prune(state.now)
        frame = _render(map_, state, state.now, state.stats)

    close = getattr(collector, "close", None)
    if close is not None:
        close()
    if frame is None:  # pragma: no cover - the loop always runs once
        return 2
    try:
        Path(path).write_text(frame.text(), encoding="utf-8")
    except OSError as exc:
        print(f"tmap: cannot write the snapshot: {exc}", file=sys.stderr)
        return 2
    return 0
