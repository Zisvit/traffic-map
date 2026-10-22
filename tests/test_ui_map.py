"""Tests of the map: the land, the zoom, the filter and the collector.

The land is the easiest thing to get wrong: the viewport is centred on
the anchor, so the land has to be moved with it, or the peers are drawn
over the sea.  These tests use the helpers of the interface itself and
not a copy of them.
"""

import curses
import queue
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

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
from trafficmap.collector_pcap import Burst
from trafficmap.config import Config
from trafficmap.flows import Flow, FlowTable, Opened, Stats
from trafficmap.geo import Geo, GeoResolver
from trafficmap.projection import great_circle, project, unproject
from trafficmap.ui import (
    RETRY_FIRST,
    RETRY_MAX,
    SNAPSHOT_COLS,
    SNAPSHOT_ROWS,
    MIN_ZOOM,
    ZOOM_PRESETS,
    ZOOM_STEP,
    WORLD_SPAN,
    _ASCII_GLYPHS,
    _C_IN,
    _C_WARNING,
    _CollectorThread,
    _FallbackCollector,
    _Map,
    _State,
    _colour_of,
    _format_rate,
    _handle_key,
    _in_cells,
    _spawn_pulses,
    _status_lines,
    _text_rows,
    _top_panel,
    _zoom_with_mouse,
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

#: The place of this machine in the tests: Amsterdam, away from the
#: Greenwich meridian, so that the frame of the map is not the frame of
#: the world.
ANCHOR = Geo("198.51.100.42", 52.4, 4.9, "NL", "Amsterdam", 47172, "Example")

#: An anchor whose view of the world holds every known point of the
#: world, and is not the middle of the world all the same.
WORLD_ANCHOR = Geo("198.51.100.42", 45.0, 4.9, "NL", "Amsterdam", 47172, "Example")

#: Points that are on the land, and points that are on the sea.
LAND_POINTS = ((0.0, 20.0), (40.0, -100.0), (50.0, 100.0))
SEA_POINTS = ((0.0, -150.0), (-40.0, -30.0))

#: Points that are on the map of the second zoom level.
ZOOM2_LAND = ((40.0, 20.0), (55.0, 30.0), (25.0, 55.0))
ZOOM2_SEA = ((60.0, -25.0), (55.0, 2.0), (40.0, -30.0))

#: Points that are on the map of the third zoom level.
ZOOM3_LAND = ((52.0, 5.0), (48.0, 8.0), (45.0, 12.0), (50.0, 19.0))
ZOOM3_SEA = ((56.0, 3.0), (57.0, 4.0), (56.0, 0.0))

#: The known points of every zoom level: the land and the sea.
POINTS_BY_ZOOM = {
    1: (LAND_POINTS, SEA_POINTS),
    2: (ZOOM2_LAND, ZOOM2_SEA),
    3: (ZOOM3_LAND, ZOOM3_SEA),
}

#: Addresses of the country of the machine, to find a peer of it.
SAME_COUNTRY = ("145.100.128.9", "194.109.5.14", "213.127.129.83", "84.25.32.9")

#: A peer in the country of the machine.
LOCAL_PEER = Geo("145.100.128.9", 48.0, 2.0, "FR", "Paris", 1, "Example")

#: A peer on the other side of the world.
FAR_PEER = Geo("8.8.8.8", 37.422, -122.085, "US", "Mountain View", 1, "Example")


def make_map(anchor: Geo | None = None, zoom: int = 1) -> _Map:
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


def land_at(map_: _Map, lat: float, lon: float) -> int:
    """Return the land flag of the cell a point falls on.

    Args:
        map_: The map of the interface.
        lat: The latitude of the point.
        lon: The longitude of the point.

    Returns:
        ``1`` when the cell is land, ``0`` when it is water.
    """
    viewport = map_.viewport()
    point = project(lat, lon, viewport)
    assert point is not None, f"{lat},{lon} is not on the map"
    return map_.land_mask()[point[1] * map_.canvas.width + point[0]]


def visible_points(map_: _Map, points: tuple[tuple[float, float], ...]) -> list:
    """Return the points of a list that are on the map.

    Args:
        map_: The map of the interface.
        points: The ``(lat, lon)`` pairs of the points.

    Returns:
        The points that the viewport shows.
    """
    viewport = map_.viewport()
    return [
        (lat, lon) for lat, lon in points if project(lat, lon, viewport) is not None
    ]


def make_flow(key: str, process: str, remote: Geo, rate: float = 1000.0) -> Flow:
    """Build a flow of the table.

    Args:
        key: The key of the flow.
        process: The name of the process of the flow.
        remote: The place of the other side.
        rate: The rate of the flow, in bytes per second.

    Returns:
        The flow.
    """
    return Flow(
        key=key,
        remote=remote,
        process=process,
        pid=100,
        proto="tcp",
        port=443,
        tx=0,
        rx=0,
        tx_rate=rate,
        rx_rate=0.0,
        first_seen=0.0,
        last_seen=0.0,
        state="ESTAB",
    )


def stats_of(flows: list[Flow]) -> Stats:
    """Return the statistics of a list of flows.

    Args:
        flows: The flows.

    Returns:
        The statistics of the flows.
    """
    countries: Counter[str] = Counter()
    for flow in flows:
        countries[flow.remote.country] += 1
    return Stats(
        tx_rate=sum(flow.tx_rate for flow in flows),
        rx_rate=sum(flow.rx_rate for flow in flows),
        flows=len(flows),
        peers=len({flow.remote.ip for flow in flows}),
        countries=countries,
        top=[],
        local=0,
        unknown=0,
    )


def styles_on(canvas: Canvas) -> set[int]:
    """Return every style the canvas holds.

    Args:
        canvas: The canvas to look into.

    Returns:
        The set of the styles of the cells of the canvas.
    """
    return {value for row in canvas.cell_styles() for value in row if value}


def wait_for(condition, timeout: float = 5.0) -> bool:
    """Wait until a condition is true.

    Args:
        condition: The function that tells whether it is time.
        timeout: The greatest number of seconds to wait.

    Returns:
        ``True`` when the condition became true in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


class FakeResolver:
    """A resolver that knows no place at all."""

    def lookup(self, ip: str) -> Geo:
        """Return a place without coordinates.

        Args:
            ip: The address to look up.

        Returns:
            A place that is nowhere.
        """
        return Geo(ip, None, None, "--", None, None, None)


class ScriptedCollector:
    """A collector that follows a script of failures and successes.

    Args:
        script: One letter per poll: ``"F"`` for a failure, ``"S"`` for
            a poll that works.  The last letter is repeated.
        message: The message of the failures.
    """

    def __init__(self, script: str, message: str = "the command failed") -> None:
        self.script = script
        self.message = message
        self.calls = 0
        self.thread: _CollectorThread | None = None

    def poll(self) -> list:
        """Answer with the sockets of the poll, or fail.

        Returns:
            An empty list of sockets: the tests do not need any.

        Raises:
            RuntimeError: When the script says the poll fails.
        """
        self.calls += 1
        index = min(self.calls, len(self.script)) - 1
        if self.script[index] == "F":
            raise RuntimeError(self.message)
        return []


class SlowCollector(ScriptedCollector):
    """A collector that waits for the test before it fails.

    The test looks at the state of the thread while the collector is
    down, so the collector has to stay down long enough.
    """

    def __init__(
        self,
        script: str,
        go: threading.Event,
        message: str = "the command failed",
    ) -> None:
        super().__init__(script, message)
        self.go = go

    def poll(self) -> list:
        """Fail, and wait for the test first.

        Returns:
            An empty list of sockets.

        Raises:
            RuntimeError: When the script says the poll fails.
        """
        self.calls += 1
        index = min(self.calls, len(self.script)) - 1
        if self.script[index] == "F":
            self.go.wait(10.0)
            raise RuntimeError(self.message)
        return []


def make_thread(
    collector: object,
    stop: threading.Event,
    wait=None,
    poll_ms: int = 200,
) -> tuple[_CollectorThread, "queue.Queue"]:
    """Build the thread of the collector and the queue it fills.

    Args:
        collector: The collector that reads the sockets.
        stop: The event that asks the thread to stop.
        wait: The function that waits between two polls.
        poll_ms: The length of a poll, in milliseconds.

    Returns:
        The thread and the queue.
    """
    table = FlowTable(FakeResolver(), poll_ms=poll_ms)
    out: "queue.Queue" = queue.Queue()
    thread = _CollectorThread(collector, table, out, poll_ms, stop, wait=wait)
    return thread, out


@needs_land
@pytest.mark.parametrize("zoom", [1, 2, 3])
def test_the_land_is_in_the_frame_of_the_anchor(zoom):
    """The land must be where the peers are, at every zoom level.

    The viewport is centred on the anchor: the land of the world has to
    be moved with it, or the peers are drawn over the sea.
    """
    map_ = make_map(ANCHOR, zoom)
    known_land, known_sea = POINTS_BY_ZOOM[zoom]
    land = visible_points(map_, known_land)
    sea = visible_points(map_, known_sea)
    assert land, "no land point is on the map"
    assert sea, "no sea point is on the map"
    for lat, lon in land:
        assert land_at(map_, lat, lon) == 1, f"{lat},{lon} should be land"
    for lat, lon in sea:
        assert land_at(map_, lat, lon) == 0, f"{lat},{lon} should be water"


@needs_land
def test_the_known_points_of_the_world_are_on_the_right_side():
    """Every known point of the world is land or sea, as it should be.

    The view of the world is centred on an anchor that is not in the
    middle of the world: the land of the world is moved with it.
    """
    map_ = make_map(WORLD_ANCHOR, 1)
    for lat, lon in LAND_POINTS:
        assert land_at(map_, lat, lon) == 1, f"{lat},{lon} should be land"
    for lat, lon in SEA_POINTS:
        assert land_at(map_, lat, lon) == 0, f"{lat},{lon} should be water"


@needs_land
def test_the_anchor_is_on_the_land():
    """The dot of the machine is on the land of its own country."""
    map_ = make_map(ANCHOR, 1)
    assert land_at(map_, ANCHOR.lat, ANCHOR.lon) == 1


@needs_land
@needs_city_db
def test_the_anchor_and_a_peer_of_the_machine_are_on_the_land():
    """The dot of the machine and the dot of a peer are on the land.

    The place of the machine is the one of the machine: the anchor is
    read from the cache of the resolver.
    """
    resolver = GeoResolver(CITY_DB, None, Config().cache_dir)
    geo = resolver.anchor()
    if geo is None or geo.lat is None or geo.lon is None:
        pytest.skip("the place of the machine is not known")
    map_ = make_map(geo, 1)
    assert land_at(map_, geo.lat, geo.lon) == 1
    peer = None
    for ip in SAME_COUNTRY:
        candidate = resolver.lookup(ip)
        if candidate.country != geo.country:
            continue
        if candidate.lat is None or candidate.lon is None:
            continue
        peer = candidate
        break
    if peer is None:
        pytest.skip("no peer of the country of the machine is known")
    assert land_at(map_, peer.lat, peer.lon) == 1


def test_the_zoom_changes_the_span_of_the_viewport():
    """Every preset shows another part of the world."""
    map_ = make_map(ANCHOR, 1)
    spans = []
    for zoom in (1, 2, 3):
        map_.set_zoom(ZOOM_PRESETS[zoom])
        viewport = map_.viewport()
        spans.append((viewport.lon_span, viewport.lat_span))
    assert spans == [(360.0, 180.0), (120.0, 60.0), (30.0, 15.0)]
    assert spans == [
        (WORLD_SPAN[0] / factor, WORLD_SPAN[1] / factor)
        for factor in ZOOM_PRESETS.values()
    ]
    # The viewport stays on the anchor, at every zoom level.
    assert map_.viewport().center_lat == ANCHOR.lat
    assert map_.viewport().center_lon == ANCHOR.lon


def test_the_zoom_shows_another_land():
    """The map of a zoomed level is not the map of the world."""
    map_ = make_map(ANCHOR, 1)
    first = bytes(map_.land_mask())
    for zoom in (2, 3):
        map_.set_zoom(ZOOM_PRESETS[zoom])
        assert bytes(map_.land_mask()) != first


def test_the_zoom_steps_are_continuous():
    """A step of the keys divides the span by the step of the zoom."""
    map_ = make_map(ANCHOR, 1)
    before = map_.viewport()
    map_.zoom_by(ZOOM_STEP)
    after = map_.viewport()
    assert after.lon_span == pytest.approx(before.lon_span / ZOOM_STEP)
    assert after.lat_span == pytest.approx(before.lat_span / ZOOM_STEP)
    map_.zoom_by(1.0 / ZOOM_STEP)
    assert map_.viewport().lon_span == pytest.approx(before.lon_span)


def test_the_zoom_stops_at_the_limits():
    """The zoom cannot go under the world and not past its limit."""
    map_ = make_map(ANCHOR, 1)
    for _ in range(100):
        map_.zoom_by(1.0 / ZOOM_STEP)
    assert map_.zoom == MIN_ZOOM
    for _ in range(200):
        map_.zoom_by(ZOOM_STEP)
    assert map_.zoom == pytest.approx(120.0)
    assert map_.viewport().lon_span == pytest.approx(WORLD_SPAN[0] / 120.0)


def test_a_zoom_keeps_the_point_of_the_cursor_in_place():
    """The city under the mouse stays under the mouse."""
    map_ = make_map(ANCHOR, 1)
    cursor = (map_.canvas.width // 3, map_.canvas.height // 3)
    before = unproject(cursor[0], cursor[1], map_.viewport())
    map_.zoom_by(ZOOM_STEP, cursor)
    after = unproject(cursor[0], cursor[1], map_.viewport())
    assert after[0] == pytest.approx(before[0], abs=1e-6)
    assert after[1] == pytest.approx(before[1], abs=1e-6)


def test_a_preset_takes_the_view_back_to_the_anchor():
    """A preset centres the view on the anchor again, after a wheel zoom."""
    map_ = make_map(ANCHOR, 1)
    map_.zoom_by(ZOOM_STEP, (0, 0))
    assert map_.center is not None
    map_.set_zoom(ZOOM_PRESETS[2])
    assert map_.center is None
    assert map_.viewport().center_lat == ANCHOR.lat
    assert map_.viewport().center_lon == ANCHOR.lon


def test_the_zero_key_shows_the_whole_world_again():
    """The key ``0`` goes back to the world, wherever the view was."""
    map_ = make_map(ANCHOR, 3)
    map_.zoom_by(ZOOM_STEP, (0, 0))
    map_.reset_zoom()
    assert map_.zoom == MIN_ZOOM
    assert map_.center is None
    assert map_.viewport().lon_span == WORLD_SPAN[0]


def test_a_failing_collector_does_not_kill_the_thread():
    """The thread survives the failures and the error is remembered."""
    stop = threading.Event()
    go = threading.Event()
    collector = SlowCollector("F", go)
    thread, out = make_thread(collector, stop, wait=lambda delay: stop.wait(0.01))
    collector.thread = thread
    thread.start()
    try:
        assert wait_for(lambda: collector.calls >= 1)
        go.set()
        assert wait_for(lambda: thread.error is not None)
        assert thread.error == "the command failed"
        assert thread.is_alive()
    finally:
        stop.set()
        go.set()
        thread.join(5)
    assert not thread.is_alive()


def test_the_error_of_the_collector_is_gone_after_a_poll_that_worked():
    """A poll that works clears the error of the collector."""
    stop = threading.Event()
    collector = ScriptedCollector("FS")
    thread, out = make_thread(collector, stop, wait=lambda delay: stop.wait(0.01))
    thread.start()
    try:
        assert wait_for(lambda: collector.calls >= 2 and thread.error is None)
        assert not out.empty(), "the poll that worked sent nothing"
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()


def test_the_delay_of_the_retry_grows_and_stops_at_five_seconds():
    """The collector is asked again later and later, but not too late."""
    stop = threading.Event()
    delays: list[float] = []

    def wait(delay: float) -> bool:
        delays.append(delay)
        if len(delays) > 6:
            stop.set()
        return stop.is_set()

    collector = ScriptedCollector("F")
    thread, out = make_thread(collector, stop, wait=wait)
    thread.start()
    thread.join(5)
    assert delays[:6] == [1.0, 2.0, 3.0, 4.0, 5.0, 5.0]
    assert delays[0] == RETRY_FIRST
    assert max(delays[:6]) == RETRY_MAX


def test_the_delay_of_the_retry_starts_again_after_a_poll_that_worked():
    """A poll that works puts the delay back to one second."""
    stop = threading.Event()
    delays: list[float] = []

    def wait(delay: float) -> bool:
        delays.append(delay)
        return stop.is_set()

    collector = ScriptedCollector("FSF")
    thread, out = make_thread(collector, stop, wait=wait)
    thread.start()
    assert wait_for(lambda: len(delays) >= 4)
    stop.set()
    thread.join(5)
    # The failure, the poll that worked, the failure, the failure: the
    # delay goes back to one second after the poll that worked.
    assert delays[:4] == [1.0, 0.2, 1.0, 2.0]


def test_the_state_shows_the_error_of_the_collector():
    """The status bar tells the user that the collector is down."""
    stop = threading.Event()
    go = threading.Event()
    collector = SlowCollector("F", go)
    thread, out = make_thread(collector, stop, wait=lambda delay: stop.wait(0.01))
    collector.thread = thread
    state = _State(Config())
    state.collector = thread
    thread.start()
    try:
        assert wait_for(lambda: collector.calls >= 1)
        go.set()
        assert wait_for(lambda: thread.error is not None)
        state.read_error()
        assert state.error == "the command failed"
        map_ = make_map(ANCHOR, 1)
        lines = _status_lines(map_, state, state.stats)
        text = "".join(part for part, _ in lines[2])
        assert "collector error: the command failed" in text
        colours = [colour for part, colour in lines[2] if "collector error" in part]
        assert colours == [_C_WARNING]
    finally:
        stop.set()
        go.set()
        thread.join(5)


def test_the_data_stays_on_the_screen_when_the_collector_is_down():
    """The flows of the last poll are still there after a failure."""
    state = _State(Config())
    flow = make_flow("1.1.1.1|100|tcp", "curl", LOCAL_PEER, rate=2000.0)
    state.update([flow], stats_of([flow]))
    state.error = "the command failed"
    assert state.stats.flows == 1
    assert state.stats.peers == 1
    assert state.visible_flows == [flow]


def test_the_filter_hides_the_peers_and_the_numbers():
    """The filter takes the peers of the other processes off the map."""
    map_ = make_map(ANCHOR, 1)
    viewport = map_.viewport()
    curl = make_flow("1.1.1.1|100|tcp", "curl", LOCAL_PEER)
    firefox = make_flow("8.8.8.8|200|tcp", "firefox", FAR_PEER)
    state = _State(Config())
    state.animator.anchor = ANCHOR
    for flow in (curl, firefox):
        state.animator.handle_event(Opened(flow=flow), now=0.0)
    state.update([curl, firefox], stats_of([curl, firefox]))

    canvas = Canvas(map_.canvas.width, map_.canvas.height)
    state.animator.draw(canvas, viewport, 0.0)
    curl_point = project(curl.remote.lat, curl.remote.lon, viewport)
    firefox_point = project(firefox.remote.lat, firefox.remote.lon, viewport)
    assert canvas.dot_style(*curl_point) == STYLE_PEER
    assert canvas.dot_style(*firefox_point) == STYLE_PEER

    # The filter hides the peer of the other process, and the numbers
    # of the status bar follow it.
    state.set_filter("curl")
    assert state.hidden == {firefox.key}
    canvas.clear()
    state.animator.draw(canvas, viewport, 0.0)
    assert canvas.dot_style(*curl_point) == STYLE_PEER
    assert canvas.dot_style(*firefox_point) == 0
    assert state.stats.flows == 1
    assert state.stats.peers == 1
    line = "".join(part for part, _ in _status_lines(map_, state, state.stats)[0])
    assert "flows 1" in line
    assert "peers 1" in line

    # The filter that is removed brings the peer back at once.
    state.set_filter("")
    assert state.hidden == set()
    canvas.clear()
    state.animator.draw(canvas, viewport, 0.0)
    assert canvas.dot_style(*firefox_point) == STYLE_PEER
    assert state.stats.flows == 2
    assert state.stats.peers == 2
    line = "".join(part for part, _ in _status_lines(map_, state, state.stats)[0])
    assert "flows 2" in line
    assert "peers 2" in line


def test_the_filter_works_without_any_flow():
    """The filter of an empty map is not a failure."""
    state = _State(Config())
    state.set_filter("curl")
    assert state.visible_flows == []
    assert state.hidden == set()
    assert state.stats.flows == 0
    assert state.stats.peers == 0
    map_ = make_map(None, 1)
    lines = _status_lines(map_, state, state.stats)
    line = "".join(part for part, _ in lines[0])
    assert "flows 0" in line


def test_the_filter_hides_the_comets_of_the_other_processes():
    """The comets of the hidden flows are not on the map either."""
    animator = Animator()
    animator.anchor = ANCHOR
    animator.spawn(
        "out",
        great_circle((ANCHOR.lat, ANCHOR.lon), (FAR_PEER.lat, FAR_PEER.lon), 40),
        now=0.0,
        flow_key="8.8.8.8|200|tcp",
    )
    map_ = make_map(ANCHOR, 1)
    canvas = Canvas(map_.canvas.width, map_.canvas.height)
    animator.draw(canvas, map_.viewport(), 0.5)
    assert styles_on(canvas) - {STYLE_ANCHOR}, "the comet is not on the map"

    animator.set_hidden({"8.8.8.8|200|tcp"})
    canvas.clear()
    animator.draw(canvas, map_.viewport(), 0.5)
    assert styles_on(canvas) == {STYLE_ANCHOR}


def test_the_head_of_a_new_incoming_comet_is_cyan():
    """The head of a comet that comes in is cyan, even when it is born.

    A comet that is born has no tail yet: the head is the only point
    of its trace, and it has to be marked all the same.
    """
    animator = Animator()
    animator.anchor = ANCHOR
    animator.spawn(
        "in",
        great_circle((FAR_PEER.lat, FAR_PEER.lon), (ANCHOR.lat, ANCHOR.lon), 40),
        now=0.0,
    )
    map_ = make_map(ANCHOR, 1)
    viewport = map_.viewport()
    cells = _in_cells(animator, viewport, map_.canvas.width, map_.canvas.height, 0.0)
    point = project(FAR_PEER.lat, FAR_PEER.lon, viewport)
    assert cells[point[1] // 4][point[0] // 2] == "in"
    assert _colour_of(6, "in") == _C_IN[2]


def test_every_style_has_its_ascii_glyph():
    """The ASCII mode has a glyph for every style of the canvas."""
    assert _ASCII_GLYPHS[0] == " "
    assert _ASCII_GLYPHS[STYLE_LAND] == "#"
    assert _ASCII_GLYPHS[STYLE_TRAIL_DIM] == "-"
    assert _ASCII_GLYPHS[STYLE_TRAIL_MID] == "-"
    assert _ASCII_GLYPHS[STYLE_PEER] == "*"
    assert _ASCII_GLYPHS[STYLE_TRAIL_BRIGHT] == "-"
    assert _ASCII_GLYPHS[STYLE_COMET_HEAD] == "+"
    assert _ASCII_GLYPHS[STYLE_ANCHOR] == "O"


def test_the_ascii_rows_hold_the_glyph_of_the_style():
    """A cell of the ASCII map holds the glyph of its dominant style."""
    canvas = Canvas(4, 8)
    canvas.set(0, 0, STYLE_PEER)
    styles = canvas.cell_styles()
    assert styles == [[STYLE_PEER, 0], [0, 0]]
    assert _text_rows(canvas, styles, True) == ["* ", "  "]
    # The Braille mode is not touched by the ASCII one.
    assert _text_rows(canvas, styles, False) == canvas.rows()
    assert all("⠀" <= char <= "⣿" for char in canvas.rows()[0])


def test_the_rates_are_read_as_rates():
    """A rate is a rate per second, not an amount of bytes."""
    assert _format_rate(512) == "512 B/s"
    assert _format_rate(9_000_000) == "9.0 MB/s"
    assert _format_rate(1_200_000) == "1.2 MB/s"
    assert _format_rate(101_000) == "101.0 KB/s"


def test_the_second_line_of_the_status_bar_shows_rates():
    """The busiest flows of the status bar show their rate."""
    map_ = make_map(ANCHOR, 1)
    flow = make_flow("1.1.1.1|100|tcp", "curl", LOCAL_PEER, rate=9_000_000.0)
    state = _State(Config())
    state.update([flow], stats_of([flow]))
    line = "".join(part for part, _ in _status_lines(map_, state, state.stats)[1])
    assert "curl:443 ↑9.0 MB/s" in line
    # The rate is not an amount of bytes.
    assert "9.0M" not in line


def test_the_panel_of_the_busiest_flows_shows_rates():
    """The panel of the busiest flows shows the rate of every flow."""
    map_ = make_map(ANCHOR, 1)
    flow = make_flow("1.1.1.1|100|tcp", "curl", LOCAL_PEER, rate=9_000_000.0)
    state = _State(Config())
    state.update([flow], stats_of([flow]))
    lines = _top_panel(map_, state)
    text = " ".join(text for _, _, text, _ in lines)
    assert "curl:443" in text
    assert "9.0 MB/s" in text
    assert "9.0M" not in text


def test_the_escape_key_takes_the_filter_away():
    """The escape key clears the filter, as the panel of the keys says."""
    map_ = make_map(ANCHOR, 1)
    curl = make_flow("1.1.1.1|100|tcp", "curl", LOCAL_PEER)
    firefox = make_flow("8.8.8.8|200|tcp", "firefox", FAR_PEER)
    state = _State(Config())
    state.update([curl, firefox], stats_of([curl, firefox]))
    state.set_filter("curl")
    assert state.filter == "curl"
    assert state.stats.flows == 1
    line = "".join(part for part, _ in _status_lines(map_, state, state.stats)[2])
    assert "filter: curl" in line

    # The escape key gives the filter up: every flow is on the map.
    assert _handle_key(27, state, map_) is True
    assert state.filter == ""
    assert state.stats.flows == 2
    assert state.hidden == set()
    line = "".join(part for part, _ in _status_lines(map_, state, state.stats)[2])
    assert "filter:" not in line


# -- the banner of the privileges ----------------------------------------


def _third_line_of_status(state: _State, map_: _Map) -> str:
    """Return the last line of the status bar as plain text."""
    return "".join(part for part, _ in _status_lines(map_, state, state.stats)[2])


def test_the_banner_of_the_root_speaks_of_the_sockets(monkeypatch):
    """Without root the sockets are only the sockets of the user."""
    state = _State(Config())
    map_ = make_map(ANCHOR, 1)
    state.sockets_only = True
    monkeypatch.setattr("trafficmap.ui._is_root", lambda: False)
    assert "no root: only your sockets" in _third_line_of_status(state, map_)


def test_the_banner_of_the_root_is_gone_with_the_packets(monkeypatch):
    """Without root the packets are still every packet of the machine."""
    state = _State(Config())
    map_ = make_map(ANCHOR, 1)
    state.sockets_only = False
    monkeypatch.setattr("trafficmap.ui._is_root", lambda: False)
    assert "no root" not in _third_line_of_status(state, map_)


def test_the_banner_of_the_root_needs_the_sockets():
    """A state that does not say what is read does not claim anything."""
    state = _State(Config())
    map_ = make_map(ANCHOR, 1)
    assert "no root" not in _third_line_of_status(state, map_)


# -- the fallback from the packets to the sockets ------------------------


class _FakeCollector:
    """A collector with a fixed answer, for the tests of the fallback."""

    error_label = "collector error"
    sockets_only = True

    def __init__(self, rows=None, error=None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls = 0
        self.closed = False

    def poll(self):
        """Return the fixed rows, or fail with the fixed error."""
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.rows

    def drain_bursts(self):
        """Return no bursts: the fake reads no packets."""
        return []

    def close(self):
        """Remember that the collector was closed."""
        self.closed = True


def test_the_capture_that_cannot_start_reads_the_sockets():
    """A capture that failed at the start leaves the sockets to the map."""
    sockets = _FakeCollector()
    fallback = _FallbackCollector(None, sockets, "no privileges")

    assert fallback.error_label == "capture error"
    assert fallback.sockets_only is True
    assert fallback.warning == "no privileges"
    assert fallback.poll() == []
    assert sockets.calls == 1
    assert fallback.drain_bursts() == []


def test_the_capture_that_dies_switches_to_the_sockets():
    """A capture that fails later does not stop the map either."""
    packets = _FakeCollector(error=RuntimeError("Operation not permitted"))
    packets.sockets_only = False
    sockets = _FakeCollector()
    fallback = _FallbackCollector(packets, sockets)
    assert fallback.sockets_only is False

    fallback.poll()
    assert fallback.sockets_only is True
    assert fallback.warning == "Operation not permitted (sockets only)"

    fallback.poll()
    assert packets.calls == 1
    assert sockets.calls == 2


def test_the_capture_that_works_keeps_the_packets():
    """A capture that works never touches the sockets."""
    packets = _FakeCollector()
    packets.sockets_only = False
    sockets = _FakeCollector()
    fallback = _FallbackCollector(packets, sockets)

    fallback.poll()

    assert fallback.sockets_only is False
    assert fallback.warning is None
    assert sockets.calls == 0


def test_the_fallback_closes_both_collectors():
    """Both the capture and the sockets are given up together."""
    packets = _FakeCollector()
    sockets = _FakeCollector()
    _FallbackCollector(packets, sockets).close()
    assert packets.closed is True
    assert sockets.closed is True


# -- the sparks of the packets -----------------------------------------


def test_the_bursts_of_the_packets_become_sparks():
    state = _State(Config())
    flow = make_flow("1.1.1.1|100|tcp", "curl", LOCAL_PEER)
    state.update([flow], stats_of([flow]))
    burst = Burst(remote=LOCAL_PEER.ip, bytes=2048, packets=12, proto="tcp", age=0.25)
    _spawn_pulses(state, [burst], now=10.0)
    pulses = state.animator.pulses
    assert len(pulses) == 1
    assert pulses[0].geo == LOCAL_PEER
    assert pulses[0].born == pytest.approx(9.75)
    assert pulses[0].weight == 2048
    assert pulses[0].flow_key == flow.key


def test_a_burst_of_a_peer_without_a_flow_has_no_spark():
    state = _State(Config())
    burst = Burst(remote="9.9.9.9", bytes=100, packets=1, proto="tcp")
    _spawn_pulses(state, [burst], now=1.0)
    assert state.animator.pulses == []


def test_the_rate_of_the_packets_is_smoothed():
    state = _State(Config())
    flow = make_flow("1.1.1.1|100|tcp", "curl", LOCAL_PEER)
    state.update([flow], stats_of([flow]))

    def burst(packets: int) -> Burst:
        return Burst(
            remote=LOCAL_PEER.ip,
            bytes=packets * 100,
            packets=packets,
            proto="tcp",
        )

    _spawn_pulses(state, [burst(50)], now=1.0)
    assert state.packet_rate == 0.0
    _spawn_pulses(state, [burst(50)], now=2.0)
    assert state.packet_rate == pytest.approx(50.0)
    _spawn_pulses(state, [burst(10)], now=3.0)
    assert state.packet_rate == pytest.approx(0.7 * 50 + 0.3 * 10)


def test_the_rate_of_the_packets_is_forgotten_when_they_stop():
    state = _State(Config())
    flow = make_flow("1.1.1.1|100|tcp", "curl", LOCAL_PEER)
    state.update([flow], stats_of([flow]))
    burst = Burst(remote=LOCAL_PEER.ip, bytes=100, packets=50, proto="tcp")
    _spawn_pulses(state, [burst], now=1.0)
    _spawn_pulses(state, [burst], now=2.0)
    assert state.packet_rate > 0
    _spawn_pulses(state, [], now=5.0)
    assert state.packet_rate == 0.0


def test_the_status_bar_shows_the_rate_of_the_packets():
    state = _State(Config())
    state.packet_rate = 1234.0
    map_ = make_map(ANCHOR, 1)
    lines = _status_lines(map_, state, state.stats)
    text = "".join(part for part, _ in lines[0])
    assert "1.2k pkt/s" in text


# -- the keys and the wheel of the zoom ---------------------------------


def test_the_zoom_keys_step_and_reset_the_map():
    """The keys step the zoom, and the key ``0`` goes back to the world."""
    state = _State(Config())
    map_ = make_map(ANCHOR, 1)
    before = map_.viewport().lon_span
    assert _handle_key(ord("+"), state, map_) is True
    assert map_.viewport().lon_span == pytest.approx(before / ZOOM_STEP)
    assert _handle_key(ord("="), state, map_) is True
    assert map_.viewport().lon_span == pytest.approx(before / ZOOM_STEP**2)
    assert _handle_key(ord("-"), state, map_) is True
    assert map_.viewport().lon_span == pytest.approx(before / ZOOM_STEP)
    assert _handle_key(ord("0"), state, map_) is True
    assert map_.zoom == MIN_ZOOM
    assert map_.viewport().lon_span == before
    assert _handle_key(ord("3"), state, map_) is True
    assert map_.zoom == ZOOM_PRESETS[3]


def test_the_wheel_zooms_around_the_pointer():
    """The dot under the pointer stays under the pointer, wheel or not."""
    map_ = make_map(ANCHOR, 1)
    column, line = 40, 20
    dots = (column * 2, line * 4)
    before = unproject(dots[0], dots[1], map_.viewport())
    _zoom_with_mouse(map_, (0, column, line, 0, curses.BUTTON4_PRESSED))
    assert map_.zoom == pytest.approx(ZOOM_STEP)
    after = unproject(dots[0], dots[1], map_.viewport())
    assert after[0] == pytest.approx(before[0], abs=1e-6)
    assert after[1] == pytest.approx(before[1], abs=1e-6)
    _zoom_with_mouse(map_, (0, column, line, 0, curses.BUTTON5_PRESSED))
    assert map_.zoom == pytest.approx(MIN_ZOOM)


def test_the_wheel_on_the_status_bar_zooms_the_middle():
    """A wheel that is not on the map does not move the point of the map."""
    map_ = make_map(ANCHOR, 1)
    middle = (map_.canvas.width // 2, map_.canvas.height // 2)
    before = unproject(middle[0], middle[1], map_.viewport())
    _zoom_with_mouse(map_, (0, 5, map_.map_height + 1, 0, curses.BUTTON4_PRESSED))
    after = unproject(middle[0], middle[1], map_.viewport())
    assert map_.zoom == pytest.approx(ZOOM_STEP)
    assert after[0] == pytest.approx(before[0], abs=1e-6)
    assert after[1] == pytest.approx(before[1], abs=1e-6)
