"""Flows: the peers of the machine, their traffic and their rates.

Two levels of state
-------------------
The collector gives *sockets*: a socket has its own byte counters, and
every socket has its own life.  The map draws *flows*: a flow is one
remote address of one process, and all the sockets of that process to
that address belong to it.  A browser with twenty connections to one
server is one peer on the map, not twenty.

The socket is the unit of the deltas.  ``ss`` prints the counters of a
socket since it was opened, so the difference between two polls is the
traffic of that second.  A socket that was just seen has no delta: its
counters are a baseline, not new traffic.  A socket whose counter went
*down* was recycled by the kernel: the new value is taken as the delta
of that poll, and the delta is never negative.

The flow is the unit of the map.  Its counters are the sums of the
counters of its sockets, and its rates are the sums of the rates of the
same.  A rate is the delta of the poll divided by the *real* length of
the poll, smoothed with an exponential moving average, so a rate does
not jump with every packet and does not die the second the traffic
stops.  A rate that decays below one byte per second is put back to
zero: at that point it is noise, not traffic.

The key of a flow is ``"{remote_ip}|{pid}|{proto}"``: the port is not
part of it, so the sockets of one process to one address make one flow
even when they talk to different ports.  The address of the key is the
*normalized* one, the address as the resolver knows it: an IPv4-mapped
address (``::ffff:1.1.1.1``) and the address it hides (``1.1.1.1``) are
one flow, and the closed records of the dump join the opened and the
updated ones.  The port of a flow is the *dominant* one: the port of
the socket that carries the most bytes.  It follows the sockets of the
flow, so the list of the busiest flows and the interface name a flow
the same way.

Local addresses
---------------
A private, loopback or link-local address has no place on the map.
Such a peer is not looked up, not turned into a flow and not drawn: it
is only counted, so the status bar can say how many of them there are.
The same goes for a public address the GeoIP database does not know:
there is no point to draw a point that is not known.

The life of a flow
------------------
A flow is *visible* (it is on the map), *hidden* (it is kept in the
table but it is not on the map) or *gone* (it is removed from the
table).  The rules are these:

* A flow is visible unless it has been idle for more than
  :data:`IDLE_SECONDS` seconds.  A flow is idle when it carried no byte
  during that time.
* When a visible flow goes idle, a :class:`Closed` event is sent, but
  the flow *stays* in the table.  It is only out of sight.
* When the traffic of a hidden flow starts again, the flow becomes
  visible again: a second :class:`Opened` event is sent, followed by
  the :class:`Updated` of the poll.  An event-driven user interface
  hears about the flow again.
* A flow whose sockets are all gone for more than :data:`CLOSE_POLLS`
  polls is removed from the table.  The deadline of that removal is the
  *real* time of those polls, the same time the rates are computed
  with, so a slow ``ss`` does not close the flows that are still there.
  A :class:`Closed` event is sent for it, unless it was already closed
  when it went out of sight: a flow is closed once and once only.

The sockets of a flow
---------------------
A socket that is missing from a poll is dropped: it no longer counts
in the totals of its flow, nor in its rates, nor in the set of sockets
the table keeps for it.  A flow without sockets that has been quiet for
three polls is closed and forgotten.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from dataclasses import dataclass, field

from trafficmap.collector_ss import SocketRow
from trafficmap.geo import Geo, GeoResolver, is_local

__all__ = [
    "Closed",
    "Flow",
    "FlowEvent",
    "FlowTable",
    "Opened",
    "Stats",
    "Updated",
]

#: Number of polls without a sign of life after which a flow is closed.
CLOSE_POLLS = 3

#: Seconds after which a flow that carries nothing is left out.
IDLE_SECONDS = 5.0

#: Weight of the new value in the moving average of a rate.
RATE_ALPHA = 0.5

#: Number of entries of the list of the busiest flows.
TOP_LIMIT = 5

#: Label of a flow whose process is not known.
UNKNOWN_PROCESS = "unknown"

#: Rate below which a rate that decays is put back to zero.
MIN_RATE = 1.0


@dataclass(slots=True)
class Flow:
    """One remote address of one process.

    Attributes:
        key: The key of the flow, ``f"{remote_ip}|{pid}|{proto}"``.
        remote: The place of the remote address.
        process: The name of the process, empty when it is not known.
        pid: The number of the process, when it is known.
        proto: The protocol of the sockets, ``"tcp"`` or ``"udp"``.
        port: The dominant port of the flow: the port of the socket
            that carries the most bytes.
        tx: The bytes the flow has sent.
        rx: The bytes the flow has received.
        tx_rate: The rate of sending, in bytes per second.
        rx_rate: The rate of receiving, in bytes per second.
        first_seen: When the flow appeared, in seconds.
        last_seen: When the flow was seen the last time, in seconds.
        state: The state of the sockets, ``"ESTAB"`` for example.
    """

    key: str
    remote: Geo
    process: str
    pid: int | None
    proto: str
    port: int
    tx: int
    rx: int
    tx_rate: float
    rx_rate: float
    first_seen: float
    last_seen: float
    state: str


@dataclass(frozen=True, slots=True)
class Opened:
    """The flow appeared."""

    flow: Flow


@dataclass(frozen=True, slots=True)
class Updated:
    """The flow carried traffic.

    Attributes:
        flow: The flow after the poll.
        dtx: The bytes the flow sent during the poll.
        drx: The bytes the flow received during the poll.
    """

    flow: Flow
    dtx: int
    drx: int


@dataclass(frozen=True, slots=True)
class Closed:
    """The flow is gone.

    Attributes:
        key: The key of the flow that is closed.
    """

    key: str


#: What the table tells about the flows after a poll.
FlowEvent = Opened | Updated | Closed


@dataclass(frozen=True, slots=True)
class Stats:
    """What the map shows about the traffic of the machine.

    Attributes:
        tx_rate: The rate of sending of every flow together.
        rx_rate: The rate of receiving of every flow together.
        flows: The number of the live flows.
        peers: The number of the distinct remote addresses.
        countries: The number of the flows per country.
        top: The busiest flows, ``(label, rate)`` pairs, the label being
            ``"process:port"``, the port being the dominant one of the
            flow.
        local: The number of the distinct local addresses of the last
            poll.  They are not on the map.
        unknown: The number of the distinct public addresses of the last
            poll that the database does not know.
    """

    tx_rate: float
    rx_rate: float
    flows: int
    peers: int
    countries: Counter[str]
    top: list[tuple[str, float]]
    local: int
    unknown: int


@dataclass(slots=True)
class _Counters:
    """The bytes of one socket.

    Attributes:
        sent: The value of the counter of the socket at the last poll.
        received: The value of the other counter.
        total_tx: The bytes the socket sent since it was seen.
        total_rx: The bytes the socket received since it was seen.
        port: The port of the other side of the socket.
    """

    sent: int
    received: int
    total_tx: int
    total_rx: int
    port: int = 0


@dataclass(slots=True)
class _FlowState:
    """What the table knows about one flow.

    Attributes:
        key: The key of the flow.
        geo: The place of the remote address.
        process: The name of the process, empty when it is not known.
        pid: The number of the process.
        proto: The protocol of the sockets.
        port: The dominant port of the remote address.
        state: The state of the sockets.
        first_seen: When the flow appeared.
        last_seen: When the flow was seen the last time.
        last_activity: When the flow carried bytes the last time.
        visible: Whether the flow is on the map.
        tx_rate: The rate of sending, ``None`` while the flow never
            carried anything.
        rx_rate: The rate of receiving.
        sockets: The counters of the sockets of the flow, by socket key.
    """

    key: str
    geo: Geo
    process: str
    pid: int | None
    proto: str
    port: int
    state: str
    first_seen: float
    last_seen: float
    last_activity: float
    visible: bool = True
    tx_rate: float | None = None
    rx_rate: float | None = None
    sockets: dict[str, _Counters] = field(default_factory=dict)


class FlowTable:
    """Turn the sockets of every poll into flows, rates and events.

    The collector thread calls :meth:`update` and the user interface
    reads :meth:`flows` and :meth:`stats`.  A lock guards the table, so
    the two may run at the same time without seeing a half-written
    poll.  The objects the table returns are copies, so the caller
    cannot change the state of the table by accident.

    Args:
        resolver: The resolver that tells where the remote addresses
            are.
        poll_ms: The length of a poll, in milliseconds.  It is the
            length of the poll when the table has no measure of it yet.

    Raises:
        ValueError: If the length of a poll is not positive.
    """

    def __init__(self, resolver: GeoResolver, poll_ms: int = 1000) -> None:
        if poll_ms <= 0:
            raise ValueError(f"poll_ms must be positive, got {poll_ms}")
        self._resolver = resolver
        self._poll_ms = poll_ms
        self._poll_seconds = poll_ms / 1000.0
        self._flows: dict[str, _FlowState] = {}
        self._local_peers: set[str] = set()
        self._unknown_peers: set[str] = set()
        self._last_update: float | None = None
        #: The real lengths of the recent polls, in seconds.  Their sum
        #: is the real time of :data:`CLOSE_POLLS` polls: it is the
        #: deadline of the removal of a flow that nobody sees any more.
        self._poll_lengths: deque[float] = deque(maxlen=CLOSE_POLLS)
        # The lock is re-entrant: :meth:`stats` reads the flows while it
        # holds it.  It is never held while the program waits for
        # something.
        self._lock = threading.RLock()

    @property
    def poll_ms(self) -> int:
        """The length of a poll, in milliseconds."""
        return self._poll_ms

    def update(self, rows: list[SocketRow], now: float) -> list[FlowEvent]:
        """Read one poll of sockets and return what happened.

        Args:
            rows: The sockets of the poll.
            now: The moment of the poll, in seconds.

        Returns:
            The events of the poll: an :class:`Opened` for every flow
            that appears, an :class:`Opened` again for every flow that
            was out of sight and carries bytes again, an
            :class:`Updated` for every flow that carried traffic, and a
            :class:`Closed` for every flow that went out of sight or
            that is removed.
        """
        with self._lock:
            return self._update(rows, now)

    def flows(self) -> list[Flow]:
        """Return the flows that are on the map.

        A flow that has been idle for more than five seconds is left
        out: those are the leftovers of UDP sockets that hang around
        without talking to anybody, and the connections that went quiet.

        Returns:
            A copy of every visible flow, in the order the flows
            appeared.
        """
        with self._lock:
            return [_snapshot(state) for state in self._visible_states()]

    def stats(self) -> Stats:
        """Return what the map shows about the traffic.

        Returns:
            The rates, the number of the flows and of the peers, the
            countries, the busiest flows and the number of the local and
            of the unknown addresses.
        """
        with self._lock:
            alive = self._visible_states()
            totals: dict[str, float] = {}
            countries: Counter[str] = Counter()
            peers: set[str] = set()
            tx_rate = 0.0
            rx_rate = 0.0

            for state in alive:
                tx = state.tx_rate if state.tx_rate is not None else 0.0
                rx = state.rx_rate if state.rx_rate is not None else 0.0
                tx_rate += tx
                rx_rate += rx
                peers.add(state.geo.ip)
                countries[state.geo.country] += 1
                label = f"{state.process or UNKNOWN_PROCESS}:{state.port}"
                totals[label] = totals.get(label, 0.0) + tx + rx

            top = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
            return Stats(
                tx_rate=tx_rate,
                rx_rate=rx_rate,
                flows=len(alive),
                peers=len(peers),
                countries=countries,
                top=top[:TOP_LIMIT],
                local=len(self._local_peers),
                unknown=len(self._unknown_peers),
            )

    def _update(self, rows: list[SocketRow], now: float) -> list[FlowEvent]:
        """Do the work of a poll, with the lock held.

        Args:
            rows: The sockets of the poll.
            now: The moment of the poll, in seconds.

        Returns:
            The events of the poll.
        """
        elapsed = self._elapsed(now)
        local: set[str] = set()
        unknown: set[str] = set()
        opened: list[str] = []
        seen: dict[str, None] = {}
        socket_keys: dict[str, set[str]] = {}
        deltas: dict[str, tuple[int, int]] = {}

        for row in rows:
            host = row.remote[0]
            if is_local(host):
                # A local peer has no place on the map.
                local.add(host)
                continue
            geo = self._resolver.lookup(host)
            if geo.lat is None:
                # The database does not know the address: there is no
                # point to draw a point that is not known.
                unknown.add(host)
                continue

            # The key holds the address as the resolver knows it, so
            # that an IPv4-mapped address and the address it hides are
            # one flow.
            key = f"{geo.ip}|{row.pid}|{row.proto}"
            state = self._flows.get(key)
            if state is None:
                state = _FlowState(
                    key=key,
                    geo=geo,
                    process=row.process or "",
                    pid=row.pid,
                    proto=row.proto,
                    port=row.remote[1],
                    state=row.state,
                    first_seen=now,
                    last_seen=now,
                    last_activity=now,
                )
                self._flows[key] = state
                opened.append(key)
            else:
                state.state = row.state
            state.last_seen = now
            seen[key] = None
            socket_keys.setdefault(key, set()).add(row.key)

            dtx, drx = self._count_socket(state, row)
            if dtx or drx:
                old_tx, old_rx = deltas.get(key, (0, 0))
                deltas[key] = (old_tx + dtx, old_rx + drx)

        self._local_peers = local
        self._unknown_peers = unknown

        # The sockets that are gone do not count any more.
        self._prune(socket_keys)

        # The rates, the dominant port and the moment of the last byte.
        for key in seen:
            state = self._flows[key]
            dtx, drx = deltas.get(key, (0, 0))
            if dtx or drx:
                state.last_activity = now
            state.tx_rate = _smooth(state.tx_rate, dtx / elapsed)
            state.rx_rate = _smooth(state.rx_rate, drx / elapsed)
            # The port of the flow is the port of the socket that
            # carries the most bytes: it follows the sockets of the
            # flow.
            state.port = _dominant_port(state)

        # A flow that was out of sight and carries bytes again is
        # visible again.
        woken: list[str] = []
        for key in seen:
            if self._wake(self._flows[key], now):
                woken.append(key)

        events: list[FlowEvent] = []
        for key in opened:
            events.append(Opened(flow=_snapshot(self._flows[key])))
        for key in woken:
            events.append(Opened(flow=_snapshot(self._flows[key])))
        for key in seen:
            if key in deltas:
                dtx, drx = deltas[key]
                events.append(
                    Updated(flow=_snapshot(self._flows[key]), dtx=dtx, drx=drx)
                )

        events.extend(self._hide(now))
        events.extend(self._close(now))
        return events

    def _elapsed(self, now: float) -> float:
        """Return the length of the poll that ends now, in seconds.

        The length is the real one, the time between the two polls, so
        that a slow command does not inflate the rates.  The length of
        the poll of the configuration is used when the table has no
        measure of it yet.

        Args:
            now: The moment of the poll, in seconds.

        Returns:
            The length of the poll, in seconds.
        """
        previous = self._last_update
        self._last_update = now
        if previous is None:
            return self._poll_seconds
        elapsed = now - previous
        if elapsed <= 0.0:
            return self._poll_seconds
        # The real length of the poll is kept: it is the deadline of
        # the removal of a flow that is not seen any more.
        self._poll_lengths.append(elapsed)
        return elapsed

    def _visible_states(self) -> list[_FlowState]:
        """Return the states of the flows that are on the map.

        The lock is held by the caller.

        Returns:
            The states of the visible flows, in the order the flows
            appeared.
        """
        return [state for state in self._flows.values() if state.visible]

    def _wake(self, state: _FlowState, now: float) -> bool:
        """Tell whether a flow that was out of sight is on the map again.

        Args:
            state: The state of the flow.
            now: The moment of the poll, in seconds.

        Returns:
            ``True`` when the flow was hidden and carries bytes again.
            The flow is then visible again.
        """
        if state.visible:
            return False
        if now - state.last_activity > IDLE_SECONDS:
            # The flow is still idle: it stays out of sight.
            return False
        state.visible = True
        return True

    def _hide(self, now: float) -> list[Closed]:
        """Take the flows that have been idle for too long out of sight.

        The flows stay in the table: they may wake up and be opened
        again.

        Args:
            now: The moment of the poll, in seconds.

        Returns:
            A :class:`Closed` event for every flow that goes out of
            sight.
        """
        closed: list[Closed] = []
        for key in list(self._flows):
            state = self._flows[key]
            if not state.visible:
                continue
            if now - state.last_activity <= IDLE_SECONDS:
                continue
            state.visible = False
            closed.append(Closed(key=key))
        return closed

    def _close(self, now: float) -> list[Closed]:
        """Remove the flows that have been quiet for too long.

        The deadline is the real time of :data:`CLOSE_POLLS` polls, the
        same time the rates are computed with: a slow command does not
        close the flows that are still there.

        Args:
            now: The moment of the poll, in seconds.

        Returns:
            A :class:`Closed` event for every flow that is removed.  A
            flow that was already closed when it went out of sight is
            removed without a second :class:`Closed`.
        """
        deadline = self._close_deadline()
        closed: list[Closed] = []
        for key in list(self._flows):
            state = self._flows[key]
            if now - state.last_seen <= deadline:
                continue
            del self._flows[key]
            if state.visible:
                state.visible = False
                closed.append(Closed(key=key))
        return closed

    def _close_deadline(self) -> float:
        """Return the real time of :data:`CLOSE_POLLS` polls, in seconds.

        The lengths of the recent polls are added up: they are the real
        time of the polls, so a poll that took long gives the flows
        more time to come back.  The length of the poll of the
        configuration is used when the table has no measure of its own
        yet.

        Returns:
            The deadline of the removal of a flow, in seconds.
        """
        if not self._poll_lengths:
            return CLOSE_POLLS * self._poll_seconds
        return sum(self._poll_lengths)

    def _prune(self, socket_keys: dict[str, set[str]]) -> None:
        """Drop the sockets that are gone from the flows they were in.

        A socket that is missing from the poll does not count any more:
        not in the totals of its flow, nor in its rates, nor in the set
        of sockets the table keeps.

        Args:
            socket_keys: The keys of the sockets of the poll, by flow.
        """
        for key, state in self._flows.items():
            alive = socket_keys.get(key)
            if alive is None:
                # The flow has no socket at all any more.
                state.sockets.clear()
                continue
            for socket_key in list(state.sockets):
                if socket_key not in alive:
                    del state.sockets[socket_key]

    def _count_socket(self, state: _FlowState, row: SocketRow) -> tuple[int, int]:
        """Record the bytes of one socket and return its delta.

        Args:
            state: The state of the flow of the socket.
            row: The socket of this poll.

        Returns:
            The bytes the socket sent and received since the last poll.
            A socket that is seen for the first time has no delta: its
            counters are a baseline.
        """
        sent = row.bytes_sent or 0
        received = row.bytes_received or 0
        counters = state.sockets.get(row.key)
        if counters is None:
            state.sockets[row.key] = _Counters(
                sent=sent,
                received=received,
                total_tx=sent,
                total_rx=received,
                port=row.remote[1],
            )
            return (0, 0)

        dtx = sent - counters.sent if sent >= counters.sent else sent
        drx = (
            received - counters.received if received >= counters.received else received
        )
        counters.sent = sent
        counters.received = received
        counters.total_tx += dtx
        counters.total_rx += drx
        return (dtx, drx)


def _smooth(previous: float | None, instant: float) -> float | None:
    """Smooth a rate with the values of the polls.

    Args:
        previous: The rate of the previous poll, ``None`` when the flow
            never carried anything yet.
        instant: The rate of this poll.

    Returns:
        The new rate.  A flow without a rate yet takes the instant one;
        a flow that was quiet keeps half of its rate and loses the other
        half.  A rate that falls below one byte per second is put back
        to zero: it is noise, not traffic.
    """
    if previous is None:
        if instant <= 0.0:
            return None
        value = instant
    else:
        value = RATE_ALPHA * instant + (1.0 - RATE_ALPHA) * previous
    if value < MIN_RATE:
        return 0.0
    return value


def _dominant_port(state: _FlowState) -> int:
    """Return the port of the socket that carries the most bytes.

    Args:
        state: The state of the flow.

    Returns:
        The port of the busiest socket of the flow.  The port of the
        flow itself when no socket of it carries anything.
    """
    best_port = state.port
    best_bytes = 0
    for counters in state.sockets.values():
        total = counters.total_tx + counters.total_rx
        if total > best_bytes:
            best_bytes = total
            best_port = counters.port
        elif total == best_bytes and total > 0 and counters.port < best_port:
            best_port = counters.port
    return best_port


def _totals(state: _FlowState) -> tuple[int, int]:
    """Return the bytes the sockets of a flow sent and received.

    Args:
        state: The state of the flow.

    Returns:
        The bytes sent and the bytes received.
    """
    tx = 0
    rx = 0
    for counters in state.sockets.values():
        tx += counters.total_tx
        rx += counters.total_rx
    return tx, rx


def _snapshot(state: _FlowState) -> Flow:
    """Build the copy of a flow that the caller gets.

    Args:
        state: The state of the flow.

    Returns:
        The flow as the caller sees it.
    """
    tx, rx = _totals(state)
    return Flow(
        key=state.key,
        remote=state.geo,
        process=state.process,
        pid=state.pid,
        proto=state.proto,
        port=state.port,
        tx=tx,
        rx=rx,
        tx_rate=state.tx_rate if state.tx_rate is not None else 0.0,
        rx_rate=state.rx_rate if state.rx_rate is not None else 0.0,
        first_seen=state.first_seen,
        last_seen=state.last_seen,
        state=state.state,
    )
