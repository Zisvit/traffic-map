"""Tests of the comet animation.

No network and no database: the peers are built by hand and the
routes are great circles between two fixed places.
"""

import pytest

from trafficmap.anim import (
    COMET_TTL,
    MAX_COMETS,
    MAX_PULSES,
    MIN_SPAWN_RATE,
    PEER_TTL,
    PULSE_BIG_BYTES,
    PULSE_TTL,
    SPAWN_COOLDOWN,
    STYLE_PULSE,
    STYLE_PULSE_DIM,
    TRAIL_LENGTH,
    Animator,
    Comet,
    Peer,
    Pulse,
)
from trafficmap.braille import Canvas
from trafficmap.flows import Closed, Flow, Opened, Updated
from trafficmap.geo import Geo
from trafficmap.projection import Viewport, great_circle, project

#: The place of this machine: on the Greenwich meridian.
ANCHOR = Geo("10.0.0.1", 0.0, 0.0, "GB", "London", 1, "Example")

#: The place of a peer: Greenwich.
GREENWICH = Geo("1.1.1.1", 51.4779, -0.0015, "GB", "London", 15169, "Example")

#: A peer far away from the anchor.
MOSCOW = Geo("8.8.8.8", 55.75, 37.61, "RU", "Moscow", 12345, "Example")

#: The whole world, in dots.
WORLD = Viewport(
    width=100,
    height=50,
    center_lat=0.0,
    center_lon=0.0,
    lon_span=360.0,
    lat_span=180.0,
)


def flow(
    key="1.1.1.1|100|tcp",
    remote=GREENWICH,
    tx_rate=0.0,
    rx_rate=0.0,
):
    """Build a flow of the table.

    Args:
        key: The key of the flow.
        remote: The place of the other side.
        tx_rate: The rate of sending, in bytes per second.
        rx_rate: The rate of receiving, in bytes per second.

    Returns:
        The flow.
    """
    return Flow(
        key=key,
        remote=remote,
        process="curl",
        pid=100,
        proto="tcp",
        port=443,
        tx=0,
        rx=0,
        tx_rate=tx_rate,
        rx_rate=rx_rate,
        first_seen=0.0,
        last_seen=0.0,
        state="ESTAB",
    )


def styles_on(canvas: Canvas) -> set[int]:
    """Return every style the canvas holds.

    Args:
        canvas: The canvas to look into.

    Returns:
        The set of the styles of the dots of the canvas, the empty
        style of the untouched dots left out.
    """
    return {value for row in canvas.cell_styles() for value in row if value}


def test_comet_position_over_time():
    c = Comet(great_circle((0, 0), (0, 90), 20), born=0.0, ttl=1.0)
    assert c.head(0.0) == (0.0, 0.0)
    assert c.head(1.0) == (0.0, 90.0)


def test_comet_expires():
    c = Comet(great_circle((0, 0), (0, 90), 20), born=0.0, ttl=1.0)
    assert c.expired(1.1)


def test_animator_caps_comets():
    a = Animator(max_comets=3)
    for i in range(10):
        a.spawn(kind="out", path=great_circle((0, 0), (0, 90), 20), now=float(i))
    assert len(a.comets) == 3


def test_the_oldest_comet_is_pushed_out():
    a = Animator(max_comets=2)
    a.spawn(kind="out", path=great_circle((0, 0), (0, 90), 20), now=0.0)
    a.spawn(kind="out", path=great_circle((0, 0), (0, 90), 20), now=1.0)
    a.spawn(kind="out", path=great_circle((0, 0), (0, 90), 20), now=2.0)
    assert [comet.born for comet in a.comets] == [1.0, 2.0]


def test_the_head_of_a_new_comet_is_at_the_start_of_its_route():
    path = great_circle((0, 0), (0, 90), 20)
    c = Comet(path, born=5.0, ttl=1.0)
    assert c.head(5.0) == path[0]
    assert c.head(4.0) == path[0]
    assert c.head(6.0) == path[-1]
    assert c.head(99.0) == path[-1]


def test_the_trail_grows_with_time():
    c = Comet(great_circle((0, 0), (0, 90), 20), born=0.0, ttl=1.0)
    assert len(c.trail(0.0)) == 1
    assert len(c.trail(0.5)) == 10
    assert len(c.trail(1.0)) == 12


def test_the_trail_is_capped_at_the_wanted_length():
    c = Comet(great_circle((0, 0), (0, 90), 20), born=0.0, ttl=1.0)
    assert len(c.trail(1.0, length=5)) == 5
    assert len(c.trail(0.5, length=40)) == 10


def test_the_trail_ends_at_the_head():
    path = great_circle((0, 0), (0, 90), 20)
    c = Comet(path, born=0.0, ttl=1.0)
    trail = c.trail(0.5)
    assert trail[-1] == c.head(0.5)
    assert trail[0] == path[0]


def test_a_comet_without_a_route_is_refused():
    with pytest.raises(ValueError):
        Comet([], born=0.0)


def test_a_comet_without_a_life_is_refused():
    with pytest.raises(ValueError):
        Comet(great_circle((0, 0), (0, 90), 20), born=0.0, ttl=0.0)


def test_the_kind_of_a_comet():
    assert Comet(great_circle((0, 0), (0, 90), 20), born=0.0).kind == "out"
    assert Comet(great_circle((0, 0), (0, 90), 20), born=0.0, kind="in").kind == "in"


def test_an_opened_flow_spawns_a_comet_and_a_peer():
    a = Animator()
    a.anchor = ANCHOR
    a.handle_event(Opened(flow=flow()), now=0.0)
    assert len(a.comets) == 1
    assert len(a.peers) == 1
    assert a.comets[0].kind == "out"


def test_an_opened_flow_spawns_a_comet_along_the_great_circle():
    a = Animator()
    a.anchor = ANCHOR
    a.handle_event(Opened(flow=flow(remote=MOSCOW)), now=0.0)
    comet = a.comets[0]
    assert comet.path[0] == (ANCHOR.lat, ANCHOR.lon)
    assert comet.path[-1] == (MOSCOW.lat, MOSCOW.lon)


def test_without_an_anchor_no_comet_is_spawned():
    a = Animator()
    a.handle_event(Opened(flow=flow()), now=0.0)
    assert a.comets == []
    # The peer is tracked all the same.
    assert len(a.peers) == 1


def test_a_peer_that_was_never_touched_is_not_expired():
    peer = Peer(GREENWICH)
    assert not peer.expired(0.0)
    assert not peer.expired(1000.0)


def test_a_peer_expires_after_ten_seconds():
    peer = Peer(GREENWICH)
    peer.touch(0.0, 1.0)
    assert not peer.expired(5.0)
    assert not peer.expired(10.0)
    assert peer.expired(10.1)


def test_a_touch_keeps_the_peer_alive():
    peer = Peer(GREENWICH)
    peer.touch(0.0, 1.0)
    peer.touch(9.0, 2.0)
    assert not peer.expired(12.0)
    assert peer.weight == 2.0


def test_an_update_below_the_rate_threshold_does_not_spawn():
    a = Animator()
    a.anchor = ANCHOR
    a.handle_event(
        Updated(flow=flow(tx_rate=1000.0, rx_rate=1000.0), dtx=1000, drx=1000),
        now=0.0,
    )
    assert a.comets == []


def test_an_update_over_the_rate_threshold_spawns():
    a = Animator()
    a.anchor = ANCHOR
    a.handle_event(
        Updated(
            flow=flow(tx_rate=MIN_SPAWN_RATE + 1.0, rx_rate=0.0),
            dtx=MIN_SPAWN_RATE + 1,
            drx=0,
        ),
        now=0.0,
    )
    assert len(a.comets) == 1


def test_an_update_at_the_rate_threshold_does_not_spawn():
    """The rate must exceed the threshold, not only reach it."""
    a = Animator()
    a.anchor = ANCHOR
    a.handle_event(
        Updated(
            flow=flow(tx_rate=MIN_SPAWN_RATE, rx_rate=0.0),
            dtx=MIN_SPAWN_RATE,
            drx=0,
        ),
        now=0.0,
    )
    assert a.comets == []


def test_the_rate_threshold_is_thirty_two_kibibytes():
    assert MIN_SPAWN_RATE == 32 * 1024


def test_the_second_update_within_the_cooldown_does_not_spawn():
    a = Animator()
    a.anchor = ANCHOR
    event = Updated(
        flow=flow(tx_rate=MIN_SPAWN_RATE * 2.0), dtx=MIN_SPAWN_RATE * 2, drx=0
    )
    a.handle_event(event, now=0.0)
    a.handle_event(event, now=0.1)
    assert len(a.comets) == 1


def test_an_update_after_the_cooldown_spawns_again():
    a = Animator()
    a.anchor = ANCHOR
    event = Updated(
        flow=flow(tx_rate=MIN_SPAWN_RATE * 2.0), dtx=MIN_SPAWN_RATE * 2, drx=0
    )
    a.handle_event(event, now=0.0)
    a.handle_event(event, now=SPAWN_COOLDOWN)
    assert len(a.comets) == 2


def test_the_cooldown_only_holds_the_same_flow_back():
    a = Animator()
    a.anchor = ANCHOR
    one = Updated(
        flow=flow(key="1.1.1.1|100|tcp", tx_rate=MIN_SPAWN_RATE * 2.0),
        dtx=MIN_SPAWN_RATE * 2,
        drx=0,
    )
    two = Updated(
        flow=flow(key="8.8.8.8|100|tcp", tx_rate=MIN_SPAWN_RATE * 2.0),
        dtx=MIN_SPAWN_RATE * 2,
        drx=0,
    )
    a.handle_event(one, now=0.0)
    a.handle_event(two, now=0.01)
    assert len(a.comets) == 2


def test_the_opened_event_starts_the_cooldown_of_its_flow():
    a = Animator()
    a.anchor = ANCHOR
    event = Updated(
        flow=flow(tx_rate=MIN_SPAWN_RATE * 2.0), dtx=MIN_SPAWN_RATE * 2, drx=0
    )
    a.handle_event(Opened(flow=event.flow), now=0.0)
    a.handle_event(event, now=0.01)
    assert len(a.comets) == 1


def test_the_direction_follows_the_bigger_delta():
    a = Animator()
    a.anchor = ANCHOR
    a.handle_event(
        Updated(
            flow=flow(tx_rate=MIN_SPAWN_RATE * 2.0),
            dtx=MIN_SPAWN_RATE * 2,
            drx=0,
        ),
        now=0.0,
    )
    assert a.comets[0].kind == "out"


def test_an_incoming_comet_flies_from_the_peer_to_the_anchor():
    a = Animator()
    a.anchor = ANCHOR
    a.handle_event(
        Updated(
            flow=flow(remote=MOSCOW, rx_rate=MIN_SPAWN_RATE * 2.0),
            dtx=0,
            drx=MIN_SPAWN_RATE * 2,
        ),
        now=0.0,
    )
    comet = a.comets[0]
    assert comet.kind == "in"
    assert comet.path[0] == (MOSCOW.lat, MOSCOW.lon)
    assert comet.path[-1] == (ANCHOR.lat, ANCHOR.lon)


def test_a_closed_flow_drops_its_peer():
    a = Animator()
    a.anchor = ANCHOR
    a.handle_event(Opened(flow=flow()), now=0.0)
    assert len(a.peers) == 1
    a.handle_event(Closed(key="1.1.1.1|100|tcp"), now=1.0)
    assert a.peers == []


def test_prune_removes_the_expired_comets():
    a = Animator()
    a.spawn(kind="out", path=great_circle((0, 0), (0, 90), 20), now=0.0)
    a.prune(0.5)
    assert len(a.comets) == 1
    a.prune(10.0)
    assert a.comets == []


def test_prune_removes_the_expired_peers():
    a = Animator()
    a.anchor = ANCHOR
    a.handle_event(Opened(flow=flow()), now=0.0)
    a.prune(PEER_TTL)
    assert len(a.peers) == 1
    a.prune(PEER_TTL + 0.1)
    assert a.peers == []


def test_the_animator_refuses_an_impossible_limit():
    with pytest.raises(ValueError):
        Animator(max_comets=0)
    with pytest.raises(ValueError):
        Animator(trail_length=0)


def test_the_default_limit_is_two_hundred_comets():
    assert MAX_COMETS == 200
    assert TRAIL_LENGTH == 12


def test_draw_marks_the_head_of_a_comet_with_style_six():
    """The head of a comet over Greenwich is a dot of style 6."""
    a = Animator()
    path = great_circle((0.0, 0.0), (GREENWICH.lat, GREENWICH.lon), 20)
    a.spawn(kind="out", path=path, now=0.0)
    canvas = Canvas(100, 50)
    a.draw(canvas, WORLD, now=COMET_TTL)
    x, y = project(GREENWICH.lat, GREENWICH.lon, WORLD)
    assert canvas.dot_style(x, y) == 6
    assert canvas.cell_style(x // 2, y // 4) == 6
    assert 6 in styles_on(canvas)


def test_draw_marks_the_anchor_with_style_seven():
    a = Animator()
    canvas = Canvas(100, 50)
    a.draw(canvas, WORLD, now=0.0, anchor=ANCHOR)
    x, y = project(ANCHOR.lat, ANCHOR.lon, WORLD)
    assert canvas.dot_style(x, y) == 7
    assert 7 in styles_on(canvas)


def test_draw_marks_the_peer_with_style_four():
    a = Animator()
    a.anchor = ANCHOR
    a.handle_event(Opened(flow=flow(remote=GREENWICH)), now=0.0)
    canvas = Canvas(100, 50)
    a.draw(canvas, WORLD, now=0.0)
    x, y = project(GREENWICH.lat, GREENWICH.lon, WORLD)
    assert canvas.dot_style(x, y) == 4
    assert 4 in styles_on(canvas)


def test_draw_marks_the_tail_with_the_fading_styles():
    a = Animator()
    a.spawn(
        kind="out",
        path=[(0.0, 0.0), (0.0, 45.0), (0.0, 90.0), (0.0, 135.0)],
        now=0.0,
    )
    canvas = Canvas(100, 50)
    a.draw(canvas, WORLD, now=COMET_TTL)
    assert canvas.dot_style(*project(0.0, 0.0, WORLD)) == 2
    assert canvas.dot_style(*project(0.0, 45.0, WORLD)) == 3
    assert canvas.dot_style(*project(0.0, 90.0, WORLD)) == 5
    assert canvas.dot_style(*project(0.0, 135.0, WORLD)) == 6
    assert {2, 3, 5, 6} <= styles_on(canvas)


def test_draw_without_an_anchor_draws_no_comet():
    a = Animator()
    a.handle_event(Opened(flow=flow()), now=0.0)
    canvas = Canvas(100, 50)
    a.draw(canvas, WORLD, now=0.0)
    assert a.comets == []
    assert styles_on(canvas) == {4}


def test_draw_skips_the_points_outside_the_viewport():
    a = Animator()
    a.spawn(kind="out", path=great_circle((0.0, 0.0), (0.0, 90.0), 20), now=0.0)
    canvas = Canvas(100, 50)
    # A viewport that holds nothing of the route: it looks at the
    # middle of Asia, the comet flies over the Atlantic.
    narrow = Viewport(
        width=100,
        height=50,
        center_lat=0.0,
        center_lon=90.0,
        lon_span=10.0,
        lat_span=10.0,
    )
    a.draw(canvas, narrow, now=0.0)
    assert styles_on(canvas) == set()


def test_draw_does_not_clear_the_canvas():
    a = Animator()
    canvas = Canvas(100, 50)
    canvas.set(0, 0, 1)
    a.draw(canvas, WORLD, now=0.0)
    assert canvas.dot_style(0, 0) == 1


def test_an_expired_comet_is_not_drawn():
    a = Animator()
    a.spawn(kind="out", path=great_circle((0.0, 0.0), (0.0, 90.0), 20), now=0.0)
    canvas = Canvas(100, 50)
    a.draw(canvas, WORLD, now=10.0)
    assert styles_on(canvas) == set()


def test_an_expired_peer_is_not_drawn():
    a = Animator()
    a.anchor = ANCHOR
    a.handle_event(Opened(flow=flow(remote=GREENWICH)), now=0.0)
    canvas = Canvas(100, 50)
    a.draw(canvas, WORLD, now=PEER_TTL + 0.1)
    assert 4 not in styles_on(canvas)


# -- the pulses of the packets -----------------------------------------


def test_a_pulse_is_drawn_as_a_spark():
    a = Animator()
    a.spawn_pulse(GREENWICH, born=0.0, weight=100.0)
    canvas = Canvas(100, 50)
    a.draw(canvas, WORLD, now=0.0)
    x, y = project(GREENWICH.lat, GREENWICH.lon, WORLD)
    assert canvas.dot_style(x, y) == STYLE_PULSE
    assert STYLE_PULSE in styles_on(canvas)


def test_a_pulse_turns_dim_before_it_goes_out():
    a = Animator()
    a.spawn_pulse(GREENWICH, born=0.0, weight=1.0)
    canvas = Canvas(100, 50)
    a.draw(canvas, WORLD, now=PULSE_TTL * 0.9)
    x, y = project(GREENWICH.lat, GREENWICH.lon, WORLD)
    assert canvas.dot_style(x, y) == STYLE_PULSE_DIM


def test_an_expired_pulse_is_taken_away():
    a = Animator()
    a.spawn_pulse(GREENWICH, born=0.0)
    a.prune(PULSE_TTL + 0.1)
    assert a.pulses == []


def test_a_heavy_pulse_is_a_block_of_four_dots():
    a = Animator()
    a.spawn_pulse(GREENWICH, born=0.0, weight=PULSE_BIG_BYTES)
    canvas = Canvas(100, 50)
    a.draw(canvas, WORLD, now=0.0)
    x, y = project(GREENWICH.lat, GREENWICH.lon, WORLD)
    for dy in range(2):
        for dx in range(2):
            assert canvas.dot_style(x + dx, y + dy) == STYLE_PULSE


def test_a_light_pulse_is_a_single_dot():
    a = Animator()
    a.spawn_pulse(GREENWICH, born=0.0, weight=PULSE_BIG_BYTES - 1)
    canvas = Canvas(100, 50)
    a.draw(canvas, WORLD, now=0.0)
    x, y = project(GREENWICH.lat, GREENWICH.lon, WORLD)
    assert canvas.dot_style(x, y) == STYLE_PULSE
    assert canvas.dot_style(x + 1, y) == 0


def test_the_pulse_of_a_hidden_flow_is_not_drawn():
    a = Animator()
    a.spawn_pulse(GREENWICH, born=0.0, flow_key="1.1.1.1|100|tcp")
    a.set_hidden({"1.1.1.1|100|tcp"})
    canvas = Canvas(100, 50)
    a.draw(canvas, WORLD, now=0.0)
    assert STYLE_PULSE not in styles_on(canvas)


def test_the_animator_caps_the_pulses():
    a = Animator(max_pulses=3)
    for index in range(10):
        a.spawn_pulse(GREENWICH, born=float(index))
    assert len(a.pulses) == 3
    assert MAX_PULSES > 0


def test_a_place_that_is_not_known_has_no_spark():
    a = Animator()
    unknown = Geo("1.1.1.1", None, None, "--", None, None, None)
    a.spawn_pulse(unknown, born=0.0)
    assert a.pulses == []


def test_a_pulse_needs_a_life():
    with pytest.raises(ValueError):
        Pulse(GREENWICH, born=0.0, ttl=0.0)


def test_the_animator_refuses_an_impossible_number_of_pulses():
    with pytest.raises(ValueError):
        Animator(max_pulses=0)
