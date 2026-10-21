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
same.  A rate is the delta of the poll divided by the length of the
poll, smoothed with an exponential moving average, so a rate does not
jump with every packet and does not die the second the traffic stops.

The key of a flow is ``"{remote_ip}|{pid}|{proto}"``: the port is not
part of it, so the sockets of one process to one address make one flow
even when they talk to different ports.  Such a flow keeps the port of
the socket that opened it.

Local addresses
---------------
A private, loopback or link-local address has no place on the map.
Such a peer is not looked up, not turned into a flow and not drawn: it
is only counted, so the status bar can say how many of them there are.
The same goes for a public address the GeoIP database does not know:
there is no point to draw a point that is not known.

Closing
-------
A socket that is missing from a poll is dropped.  A flow without sockets
that has been quiet for three polls is closed and forgotten.
"""

from __future__ import annotations

from collections import Counter
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

#: Seconds after which a flow that never carried a byte is left out.
IDLE_SECONDS = 5.0

#: Weight of the new value in the moving average of a rate.
RATE_ALPHA = 0.5

#: Number of entries of the list of the busiest flows.
TOP_LIMIT = 5

#: Label of a flow whose process is not known.
UNKNOWN_PROCESS = "unknown"


@dataclass(slots=True)
class Flow:
    """One remote address of one process.

    Attributes:
        key: The key of the flow, ``f"{remote_ip}|{pid}|{proto}"``.
        remote: The place of the remote address.
        process: The name of the process, empty when it is not known.
        pid: The number of the process, when it is known.
        proto: The protocol of the sockets, ``"tcp"`` or ``"udp"``.
        port: The port of the remote address.
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
            ``"process:port"``.
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
    """

    sent: int
    received: int
    total_tx: int
    total_rx: int


@dataclass(slots=True)
class _FlowState:
    """What the table knows about one flow.

    Attributes:
        key: The key of the flow.
        geo: The place of the remote address.
        process: The name of the process, empty when it is not known.
        pid: The number of the process.
        proto: The protocol of the sockets.
        port: The port of the remote address.
        state: The state of the sockets.
        first_seen: When the flow appeared.
        last_seen: When the flow was seen the last time.
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
    tx_rate: float | None = None
    rx_rate: float | None = None
    sockets: dict[str, _Counters] = field(default_factory=dict)


class FlowTable:
    """Turn the sockets of every poll into flows, rates and events.

    The table is used by one thread at a time: the collector thread
    calls :meth:`update` and the user interface reads :meth:`flows` and
    :meth:`stats`.  The objects it returns are copies, so the caller
    cannot change the state of the table by accident.

    Args:
        resolver: The resolver that tells where the remote addresses
            are.
        poll_ms: The length of a poll, in milliseconds.

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
        self._now: float | None = None

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
            that appears, an :class:`Updated` for every flow that
            carried traffic, and a :class:`Closed` for every flow that
            was quiet for too long.
        """
        self._now = now
        local: set[str] = set()
        unknown: set[str] = set()
        opened: list[str] = []
        seen: dict[str, None] = {}
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

            key = f"{host}|{row.pid}|{row.proto}"
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
                )
                self._flows[key] = state
                opened.append(key)
            else:
                state.state = row.state
            state.last_seen = now
            seen[key] = None

            dtx, drx = self._count_socket(state, row)
            if dtx or drx:
                old_tx, old_rx = deltas.get(key, (0, 0))
                deltas[key] = (old_tx + dtx, old_rx + drx)

        self._local_peers = local
        self._unknown_peers = unknown

        events: list[FlowEvent] = []
        for key in seen:
            state = self._flows[key]
            dtx, drx = deltas.get(key, (0, 0))
            state.tx_rate = _smooth(state.tx_rate, dtx / self._poll_seconds)
            state.rx_rate = _smooth(state.rx_rate, drx / self._poll_seconds)

        for key in opened:
            events.append(Opened(flow=_snapshot(self._flows[key])))
        for key in seen:
            if key in deltas:
                dtx, drx = deltas[key]
                events.append(
                    Updated(flow=_snapshot(self._flows[key]), dtx=dtx, drx=drx)
                )

        events.extend(self._close(now))
        return events

    def flows(self) -> list[Flow]:
        """Return the flows that are on the map.

        A flow that never carried a byte and is older than five seconds
        is left out: those are the leftovers of UDP sockets that hang
        around without talking to anybody.

        Returns:
            A copy of every live flow, in the order the flows appeared.
        """
        now = self._now
        alive: list[Flow] = []
        # The list is a copy: the collector thread may add a flow while
        # the user interface reads the table.
        for state in list(self._flows.values()):
            if now is not None and _is_idle(state, now):
                continue
            alive.append(_snapshot(state))
        return alive

    def stats(self) -> Stats:
        """Return what the map shows about the traffic.

        Returns:
            The rates, the number of the flows and of the peers, the
            countries, the busiest flows and the number of the local and
            of the unknown addresses.
        """
        alive = self.flows()
        totals: dict[str, float] = {}
        countries: Counter[str] = Counter()
        peers: set[str] = set()
        tx_rate = 0.0
        rx_rate = 0.0

        for flow in alive:
            tx_rate += flow.tx_rate
            rx_rate += flow.rx_rate
            peers.add(flow.remote.ip)
            countries[flow.remote.country] += 1
            label = f"{flow.process or UNKNOWN_PROCESS}:{flow.port}"
            totals[label] = totals.get(label, 0.0) + flow.tx_rate + flow.rx_rate

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

    def _close(self, now: float) -> list[Closed]:
        """Close the flows that have been quiet for too long.

        Args:
            now: The moment of the poll, in seconds.

        Returns:
            A :class:`Closed` event for every flow that is closed.
        """
        deadline = CLOSE_POLLS * self._poll_seconds
        closed: list[Closed] = []
        for key in list(self._flows):
            state = self._flows[key]
            if now - state.last_seen > deadline:
                del self._flows[key]
                closed.append(Closed(key=key))
        return closed


def _smooth(previous: float | None, instant: float) -> float | None:
    """Smooth a rate with the values of the polls.

    Args:
        previous: The rate of the previous poll, ``None`` when the flow
            never carried anything yet.
        instant: The rate of this poll.

    Returns:
        The new rate.  A flow without a rate yet takes the instant one;
        a flow that was quiet keeps half of its rate and loses the other
        half.
    """
    if previous is None:
        return instant if instant > 0.0 else None
    return RATE_ALPHA * instant + (1.0 - RATE_ALPHA) * previous


def _is_idle(state: _FlowState, now: float) -> bool:
    """Tell whether a flow may be left out of the map.

    Args:
        state: The state of the flow.
        now: The moment of the last poll, in seconds.

    Returns:
        ``True`` for a flow that never carried a byte and that is older
        than five seconds.
    """
    if now - state.first_seen <= IDLE_SECONDS:
        return False
    tx, rx = _totals(state)
    return tx + rx == 0


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
