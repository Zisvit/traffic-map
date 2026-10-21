"""Comet animation of the traffic map.

The map is not a picture of the traffic, it is a film of it.  Every
remote address is a *peer*: a dot on the map that lives ten seconds
after the last sign of life.  Every burst of traffic that is worth
showing is a *comet*: a dot that flies from the anchor to the peer (or
the other way round) and drags a fading tail behind it.

The three kinds of element
--------------------------

* A **peer** is a place somebody talks to.  It is a single dot in
  style 4 and it fades out on its own when nobody talks to it any
  more.
* A **comet** is a burst of traffic.  It flies along the great circle
  between the anchor and the peer, its head is a dot in style 6 and its
  tail is a handful of older points that fade with their age: the
  newest ones are bright (style 5), the middle ones are half bright
  (style 3) and the oldest ones are dim (style 2).
* The **anchor** is our own place on the map, a single dot in style 7.

The direction of a comet says which way the bytes went: ``"out"`` for
the traffic that leaves the machine and ``"in"`` for the traffic that
comes to it.  A comet of kind ``"in"`` flies from the peer to the
anchor, so the arrow on the map always points the way the data went.

The spawn policy
----------------

The map must not become a fireworks show.  A comet is spawned:

* once for every flow that appears (:class:`trafficmap.flows.Opened`),
  whatever its rate is: a new connection is worth a look;
* for every update of a flow (:class:`trafficmap.flows.Updated`) whose
  rate exceeds :data:`MIN_SPAWN_RATE`, but not more than one comet
  per flow every :data:`SPAWN_COOLDOWN` seconds.

The rate that counts is the rate of the flow itself, in bytes per
second: the deltas of an update are bytes, not a rate, and the
animator knows nothing about the length of a poll.

The peers
---------

A peer is kept as long as its flow lives: every event of the flow
refreshes it, and a flow that is closed takes it away.  A peer that
nobody touches any more fades away after :data:`PEER_TTL` seconds.

The filter of the process
-------------------------

:meth:`Animator.set_hidden` hides some flows without losing them: the
peers and the comets of the hidden flows stay in the animator, they are
only not drawn.  The filter of the interface is therefore reversible:
when the filter is removed, the peers are on the map again at once,
without waiting for the flow to be seen again.

The animator needs the anchor to build the route of a comet.  Without
an anchor there is no route and no comet, but the peers are tracked
all the same: the map shows the dots as soon as the anchor shows up.

At most :data:`MAX_COMETS` comets live at the same time; the oldest
one is pushed out when the limit is reached.
"""

from __future__ import annotations

from trafficmap.braille import Canvas
from trafficmap.flows import Closed, Flow, FlowEvent, Opened, Updated
from trafficmap.geo import Geo
from trafficmap.projection import Viewport, great_circle, project

__all__ = [
    "Animator",
    "COMET_TTL",
    "Comet",
    "MAX_COMETS",
    "MIN_SPAWN_RATE",
    "PATH_SAMPLES",
    "PEER_TTL",
    "Peer",
    "SPAWN_COOLDOWN",
    "STYLE_ANCHOR",
    "STYLE_COMET_HEAD",
    "STYLE_LAND",
    "STYLE_PEER",
    "STYLE_TRAIL_BRIGHT",
    "STYLE_TRAIL_DIM",
    "STYLE_TRAIL_MID",
    "TRAIL_LENGTH",
]

#: Style of the land background.
STYLE_LAND = 1

#: Style of the oldest points of a tail.
STYLE_TRAIL_DIM = 2

#: Style of the middle points of a tail.
STYLE_TRAIL_MID = 3

#: Style of a peer.
STYLE_PEER = 4

#: Style of the newest points of a tail.
STYLE_TRAIL_BRIGHT = 5

#: Style of the head of a comet.
STYLE_COMET_HEAD = 6

#: Style of the anchor.
STYLE_ANCHOR = 7

#: Rate a flow must exceed before a comet is spawned for it, in bytes
#: per second: 32 KiB/s.
MIN_SPAWN_RATE = 32 * 1024

#: Shortest time between two comets of the same flow, in seconds.
SPAWN_COOLDOWN = 0.3

#: Life of a peer without a touch, in seconds.
PEER_TTL = 10.0

#: Life of a comet, in seconds.
COMET_TTL = 1.2

#: Number of points of the tail of a comet.
TRAIL_LENGTH = 12

#: Greatest number of comets that live at the same time.
MAX_COMETS = 200

#: Number of samples of the great circle a comet flies along.
#:
#: The samples are the points the head of the comet walks through, so
#: there must be enough of them for the head to move about one dot per
#: frame: a coarse path makes the comet hop over the map instead of
#: sliding over it.
PATH_SAMPLES = 160


class Comet:
    """One burst of traffic, flying from one place to another.

    The comet walks along its route: the head is the point of the
    route that belongs to the moment, and the tail is the points it
    has just left behind.

    Args:
        path: The points of the route, in the order they are flown
            through.  The first one is where the comet starts.
        born: The moment the comet appeared, in seconds.
        ttl: How long the comet lives, in seconds.
        kind: The direction of the traffic: ``"out"`` for the traffic
            that leaves the machine, ``"in"`` for the traffic that
            comes to it.
        weight: How heavy the traffic the comet carries is.  The
            animator does not use it yet, the renderer may.
        flow_key: The key of the flow the comet belongs to.  It is
            empty when the comet does not belong to a flow, and it is
            what the filter of the process hides the comet by.

    Raises:
        ValueError: If the route is empty or the life is not
            positive.
    """

    __slots__ = ("path", "born", "ttl", "weight", "flow_key", "_kind")

    def __init__(
        self,
        path: list[tuple[float, float]],
        born: float,
        ttl: float = COMET_TTL,
        kind: str = "out",
        weight: float = 1.0,
        flow_key: str = "",
    ) -> None:
        if not path:
            raise ValueError("a comet needs a route")
        if ttl <= 0:
            raise ValueError(f"ttl must be positive, got {ttl}")
        self.path = list(path)
        self.born = born
        self.ttl = ttl
        self.weight = weight
        self.flow_key = flow_key
        self._kind = kind

    @property
    def kind(self) -> str:
        """The direction of the traffic the comet carries."""
        return self._kind

    def head(self, now: float) -> tuple[float, float]:
        """Return the point of the route the comet is at.

        The comet walks from the first point of the route to the last
        one in ``ttl`` seconds.  Before it was born it is still at the
        start of the route, and after its life it is at the end of it.

        Args:
            now: The moment, in seconds.

        Returns:
            The ``(lat, lon)`` pair the head is at.
        """
        return self.path[self._index(now)]

    def trail(
        self, now: float, length: int = TRAIL_LENGTH
    ) -> list[tuple[float, float]]:
        """Return the points the comet has just left behind.

        The tail ends at the head and grows to the left: a young comet
        has a short tail, an old one has a tail of the wanted length.
        The last point of the tail is the head itself.

        Args:
            now: The moment, in seconds.
            length: The greatest number of points of the tail.

        Returns:
            The ``(lat, lon)`` pairs of the tail, the oldest one first.
        """
        index = self._index(now)
        start = max(0, index - length + 1)
        return self.path[start : index + 1]

    def expired(self, now: float) -> bool:
        """Tell whether the comet is past its life.

        Args:
            now: The moment, in seconds.

        Returns:
            ``True`` when the comet lived longer than its life.
        """
        return now - self.born > self.ttl

    def _index(self, now: float) -> int:
        """Return the index of the point of the route the head is at."""
        fraction = (now - self.born) / self.ttl
        fraction = min(1.0, max(0.0, fraction))
        return int(fraction * (len(self.path) - 1))


class Peer:
    """One remote address of the map.

    The peer is a dot that stays on the map while somebody talks to
    it, and fades away ten seconds after the last sign of life.  A
    flow that is closed takes its peer away at once, without waiting
    for the life to run out.

    Args:
        geo: The place of the address.
        weight: How heavy the traffic of the peer is.
    """

    __slots__ = ("geo", "weight", "last_seen")

    def __init__(self, geo: Geo, weight: float = 0.0) -> None:
        self.geo = geo
        self.weight = weight
        self.last_seen: float | None = None

    def touch(self, now: float, weight: float) -> None:
        """Note that the peer is alive and say how heavy it is.

        Args:
            now: The moment, in seconds.
            weight: How heavy the traffic of the peer is.
        """
        self.last_seen = now
        self.weight = weight

    def expired(self, now: float) -> bool:
        """Tell whether the peer is past its life.

        A peer that was never touched is not expired: it is a peer
        that nobody said anything about yet.

        Args:
            now: The moment, in seconds.

        Returns:
            ``True`` when the peer was not touched for ten seconds.
        """
        if self.last_seen is None:
            return False
        return now - self.last_seen > PEER_TTL


class Animator:
    """The animation of the map: the peers and the comets.

    The animator is driven by the events of the flow table: every
    event is handed over with :meth:`handle_event`, and the frame is
    drawn with :meth:`draw`.  Both happen in the thread of the user
    interface, so no lock is needed.

    Args:
        max_comets: The greatest number of comets that live at the
            same time.  The oldest one is pushed out when the limit is
            reached.
        trail_length: The number of points of the tail of a comet.

    Raises:
        ValueError: If the limit of the comets or the length of the
            tail is not positive.
    """

    __slots__ = (
        "anchor",
        "max_comets",
        "trail_length",
        "_comets",
        "_hidden",
        "_peers",
        "_last_spawn",
    )

    def __init__(
        self, max_comets: int = MAX_COMETS, trail_length: int = TRAIL_LENGTH
    ) -> None:
        if max_comets < 1:
            raise ValueError(f"max_comets must be positive, got {max_comets}")
        if trail_length < 1:
            raise ValueError(f"trail_length must be positive, got {trail_length}")
        self.max_comets = max_comets
        self.trail_length = trail_length
        #: The place of this machine, without it there is no route and
        #: no comet.
        self.anchor: Geo | None = None
        self._comets: list[Comet] = []
        self._peers: dict[str, Peer] = {}
        self._last_spawn: dict[str, float] = {}
        #: The keys of the flows that are not on the map.  The peers
        #: and the comets of those flows are kept, they are only not
        #: drawn.
        self._hidden: set[str] = set()

    @property
    def comets(self) -> list[Comet]:
        """The comets that are alive, the oldest one first."""
        return list(self._comets)

    @property
    def peers(self) -> list[Peer]:
        """The peers that are alive, the oldest one first."""
        return list(self._peers.values())

    @property
    def visible_peers(self) -> list[Peer]:
        """The peers that are on the map, the oldest one first.

        The peers of the hidden flows are alive, but they are not
        drawn: see :meth:`set_hidden`.
        """
        return [peer for key, peer in self._peers.items() if key not in self._hidden]

    @property
    def hidden(self) -> frozenset[str]:
        """The keys of the flows that are not on the map."""
        return frozenset(self._hidden)

    def set_hidden(self, keys: set[str]) -> None:
        """Hide the flows of some keys without losing them.

        The peers and the comets of the hidden flows stay in the
        animator: they are drawn again as soon as the keys are not
        hidden any more.  This is what makes the filter of the process
        reversible.

        Args:
            keys: The keys of the flows that are not on the map.
        """
        self._hidden = set(keys)

    def handle_event(self, event: FlowEvent, now: float) -> None:
        """Take one event of the flow table into account.

        A flow that appears gets a comet and a peer.  A flow that
        carries traffic gets a comet when its rate is worth it, and
        keeps its peer alive.  A flow that is closed takes its peer
        away.

        Args:
            event: The event: an :class:`trafficmap.flows.Opened`, an
                :class:`trafficmap.flows.Updated` or a
                :class:`trafficmap.flows.Closed`.
            now: The moment of the event, in seconds.
        """
        if isinstance(event, Opened):
            self._touch_peer(event.flow, now)
            self._spawn_for(event.flow, "out", now)
        elif isinstance(event, Updated):
            self._touch_peer(event.flow, now)
            self._spawn_for_update(event, now)
        elif isinstance(event, Closed):
            self._peers.pop(event.key, None)

    def spawn(
        self,
        kind: str,
        path: list[tuple[float, float]],
        now: float,
        flow_key: str = "",
    ) -> None:
        """Put a comet on the map.

        The comet is born now and lives :data:`COMET_TTL` seconds.  The
        oldest comet is pushed out when there are too many of them.

        Args:
            kind: The direction of the traffic: ``"out"`` or ``"in"``.
            path: The points of the route of the comet.
            now: The moment the comet is born, in seconds.
            flow_key: The key of the flow the comet belongs to.  It
                starts the cooldown of that flow, and it is what the
                filter of the process hides the comet by.
        """
        self._comets.append(
            Comet(path, born=now, ttl=COMET_TTL, kind=kind, flow_key=flow_key)
        )
        while len(self._comets) > self.max_comets:
            self._comets.pop(0)
        if flow_key:
            self._last_spawn[flow_key] = now

    def prune(self, now: float) -> None:
        """Take the comets and the peers that are past their life out.

        Args:
            now: The moment, in seconds.
        """
        self._comets = [comet for comet in self._comets if not comet.expired(now)]
        self._peers = {
            key: peer for key, peer in self._peers.items() if not peer.expired(now)
        }
        self._last_spawn = {
            key: moment
            for key, moment in self._last_spawn.items()
            if now - moment <= SPAWN_COOLDOWN
        }

    def draw(
        self,
        canvas: Canvas,
        viewport: Viewport,
        now: float,
        anchor: Geo | None = None,
    ) -> None:
        """Draw the animation on the canvas.

        The canvas is not cleared: the caller paints the land and
        clears the frame, and the animation is drawn on top of it.

        Args:
            canvas: The canvas to draw on.
            viewport: The part of the world the canvas shows.
            now: The moment of the frame, in seconds.
            anchor: The place of this machine.  The anchor of the
                animator is used when it is not given.
        """
        spot = anchor if anchor is not None else self.anchor
        self._draw_anchor(canvas, viewport, spot)
        self._draw_peers(canvas, viewport, now)
        self._draw_comets(canvas, viewport, now)

    def _draw_anchor(
        self, canvas: Canvas, viewport: Viewport, anchor: Geo | None
    ) -> None:
        """Draw the anchor as a single dot."""
        if anchor is None or anchor.lat is None or anchor.lon is None:
            return
        point = project(anchor.lat, anchor.lon, viewport)
        if point is None:
            return
        canvas.set(point[0], point[1], STYLE_ANCHOR)

    def _draw_peers(self, canvas: Canvas, viewport: Viewport, now: float) -> None:
        """Draw every peer that is alive as a single dot.

        The peers of the hidden flows are not drawn, but they are kept:
        they come back as soon as the flow is not hidden any more.
        """
        for key, peer in self._peers.items():
            if key in self._hidden:
                continue
            if peer.expired(now):
                continue
            geo = peer.geo
            if geo.lat is None or geo.lon is None:
                continue
            point = project(geo.lat, geo.lon, viewport)
            if point is None:
                continue
            canvas.set(point[0], point[1], STYLE_PEER)

    def _draw_comets(self, canvas: Canvas, viewport: Viewport, now: float) -> None:
        """Draw every comet that is alive: its tail and its head.

        The tail is a streak, not a row of dots: the points that are
        left behind are joined with a line, so that a route that is
        longer than the tail keeps a continuous trail.  A point that
        falls outside the viewport breaks the streak: the line is not
        drawn across the screen.
        """
        for comet in self._comets:
            if comet.expired(now):
                continue
            if comet.flow_key and comet.flow_key in self._hidden:
                continue
            # The last point of the trail is the head: it is painted
            # with the style of the head, not with the style of the
            # tail.
            points = comet.trail(now, self.trail_length)
            if not points:
                continue
            tail = points[:-1]
            previous: tuple[int, int] | None = None
            previous_style = STYLE_TRAIL_DIM
            for index, (lat, lon) in enumerate(tail):
                style = _trail_style(index, len(tail))
                point = project(lat, lon, viewport)
                if point is None:
                    previous = None
                    continue
                if previous is not None:
                    canvas.line(
                        previous[0], previous[1], point[0], point[1], previous_style
                    )
                canvas.set(point[0], point[1], style)
                previous = point
                previous_style = style
            lat, lon = points[-1]
            point = project(lat, lon, viewport)
            if point is None:
                continue
            if previous is not None:
                canvas.line(
                    previous[0], previous[1], point[0], point[1], STYLE_TRAIL_BRIGHT
                )
            canvas.set(point[0], point[1], STYLE_COMET_HEAD)

    def _touch_peer(self, flow: Flow, now: float) -> None:
        """Note that the peer of a flow is alive.

        Args:
            flow: The flow of the peer.
            now: The moment, in seconds.
        """
        peer = self._peers.get(flow.key)
        if peer is None:
            peer = Peer(flow.remote)
            self._peers[flow.key] = peer
        peer.touch(now, flow.tx_rate + flow.rx_rate)

    def _spawn_for_update(self, event: Updated, now: float) -> None:
        """Spawn the comet of an update, when the flow deserves one.

        Args:
            event: The update of the flow.
            now: The moment of the event, in seconds.
        """
        flow = event.flow
        if flow.tx_rate + flow.rx_rate <= MIN_SPAWN_RATE:
            return
        if not self._may_spawn(flow.key, now):
            return
        kind = "out" if event.dtx >= event.drx else "in"
        self._spawn_for(flow, kind, now)

    def _may_spawn(self, flow_key: str, now: float) -> bool:
        """Tell whether a comet may be spawned for a flow.

        Args:
            flow_key: The key of the flow.
            now: The moment, in seconds.

        Returns:
            ``True`` when the last comet of the flow is older than
            :data:`SPAWN_COOLDOWN`.
        """
        last = self._last_spawn.get(flow_key)
        if last is None:
            return True
        return now - last >= SPAWN_COOLDOWN

    def _spawn_for(self, flow: Flow, kind: str, now: float) -> None:
        """Spawn the comet of a flow along the route to its peer.

        Without an anchor there is no route, and no comet is spawned.

        Args:
            flow: The flow of the comet.
            kind: The direction of the traffic: ``"out"`` or ``"in"``.
            now: The moment of the event, in seconds.
        """
        anchor = self.anchor
        remote = flow.remote
        if anchor is None or anchor.lat is None or anchor.lon is None:
            return
        if remote.lat is None or remote.lon is None:
            return
        here = (anchor.lat, anchor.lon)
        there = (remote.lat, remote.lon)
        if kind == "in":
            path = great_circle(there, here, PATH_SAMPLES)
        else:
            path = great_circle(here, there, PATH_SAMPLES)
        self.spawn(kind, path, now, flow.key)


def _trail_style(index: int, count: int) -> int:
    """Return the style of a point of a tail, by its age.

    The tail is cut in three: the newest points are bright, the middle
    ones are half bright and the oldest ones are dim.

    Args:
        index: The place of the point in the tail, the oldest one
            first.
        count: The number of points of the tail.

    Returns:
        The style of the point.
    """
    if count <= 1:
        return STYLE_TRAIL_BRIGHT
    third = max(1, count // 3)
    if index >= count - third:
        return STYLE_TRAIL_BRIGHT
    if index >= count - 2 * third:
        return STYLE_TRAIL_MID
    return STYLE_TRAIL_DIM
