"""A collector that replays a recorded file of sockets.

The mock collector reads a JSONL file: one line per tick, one tick per
second, every line holding the sockets of that moment::

    {"t": 0.0, "sockets": [{"proto": "tcp", "local": ["10.0.0.2", 51000],
     "remote": ["1.1.1.1", 443], "state": "ESTAB", "pid": 100,
     "process": "curl", "bytes_sent": 1200, "bytes_received": 9000}]}

It answers like the real collector of ``ss`` does, so the program cannot
tell the two apart: the map, the tests and the demonstration run
without root, without a network and without the sockets of the machine.

The clock
---------
The collector keeps a pointer to the tick it is at.  :meth:`poll`
answers with the sockets of that tick and moves the pointer to the next
one; the last tick is answered again and again when the file is over.
:attr:`current_time` is the time of the tick the pointer is at, so it
is read *before* the poll to know the time of the sockets the poll
answers with::

    now = collector.current_time
    rows = collector.poll()

A line that cannot be read is skipped: a broken recording must not
stop the map.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from trafficmap.collector_ss import SocketRow

__all__ = ["MockCollector"]

log = logging.getLogger(__name__)


class _Tick:
    """One moment of the recording.

    Args:
        time: The moment of the tick, in seconds.
        rows: The sockets that were open at that moment.
    """

    __slots__ = ("time", "rows")

    def __init__(self, time: float, rows: list[SocketRow]) -> None:
        self.time = time
        self.rows = rows


class MockCollector:
    """Replay the sockets of a JSONL file.

    Args:
        path: Path of the recording.

    Raises:
        FileNotFoundError: If the recording does not exist.
        OSError: If the recording cannot be read.
    """

    #: The recording is neither the packets nor the sockets of the
    #: machine: no privilege changes what it shows, and no warning of
    #: the privileges belongs to it.
    sockets_only = False

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._ticks = _read_ticks(self._path)
        self._index = 0
        self._time = self._ticks[0].time if self._ticks else 0.0

    @property
    def path(self) -> Path:
        """The path of the recording."""
        return self._path

    @property
    def current_time(self) -> float:
        """The time of the tick the next poll answers with.

        The pointer of the collector moves when it is polled, so the
        time is read before the poll to know the time of the sockets
        the poll answers with.
        """
        return self._time

    @property
    def ticks(self) -> int:
        """The number of ticks of the recording."""
        return len(self._ticks)

    def poll(self) -> list[SocketRow]:
        """Answer with the sockets of the current tick and move on.

        The last tick of the recording is answered again and again, so
        a recording that is over never runs dry.

        Returns:
            The sockets of the current tick.
        """
        if not self._ticks:
            return []
        tick = self._ticks[self._index]
        if self._index < len(self._ticks) - 1:
            self._index += 1
        self._time = self._ticks[self._index].time
        return list(tick.rows)


def _read_ticks(path: Path) -> list[_Tick]:
    """Read the ticks of a recording.

    Args:
        path: Path of the recording.

    Returns:
        The ticks of the recording, the broken lines left out.
    """
    ticks: list[_Tick] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                log.debug("line %d of %s is not JSON: %s", number, path, exc)
                continue
            tick = _read_tick(payload)
            if tick is None:
                log.debug("line %d of %s is not a tick", number, path)
                continue
            ticks.append(tick)
    return ticks


def _read_tick(payload: object) -> _Tick | None:
    """Read one tick of a recording.

    Args:
        payload: The object of the line.

    Returns:
        The tick, or ``None`` when the line does not hold one.
    """
    if not isinstance(payload, dict):
        return None
    time = _as_float(payload.get("t"))
    sockets = payload.get("sockets")
    if time is None or not isinstance(sockets, list):
        return None
    rows = [row for row in (_read_socket(item) for item in sockets) if row is not None]
    return _Tick(time, rows)


def _read_socket(payload: object) -> SocketRow | None:
    """Read one socket of a tick.

    Args:
        payload: The object of the socket.

    Returns:
        The socket, or ``None`` when the object does not hold one.
    """
    if not isinstance(payload, dict):
        return None
    proto = payload.get("proto")
    if not isinstance(proto, str) or not proto:
        return None
    local = _read_address(payload.get("local"))
    remote = _read_address(payload.get("remote"))
    if local is None or remote is None:
        return None

    state = payload.get("state")
    if not isinstance(state, str) or not state:
        state = "ESTAB"
    pid = _as_int(payload.get("pid"))
    process = payload.get("process")
    if not isinstance(process, str) or not process:
        process = None
    sent = _as_int(payload.get("bytes_sent"))
    received = _as_int(payload.get("bytes_received"))

    key = f"{proto}|{local[0]}:{local[1]}|{remote[0]}:{remote[1]}|{pid}"
    return SocketRow(
        proto=proto,
        local=local,
        remote=remote,
        state=state,
        pid=pid,
        process=process,
        bytes_sent=sent,
        bytes_received=received,
        key=key,
    )


def _read_address(payload: object) -> tuple[str, int] | None:
    """Read the address of one side of a socket.

    Args:
        payload: The object of the address: a ``[host, port]`` pair.

    Returns:
        The address and its port, or ``None`` when the object does not
        hold one.
    """
    if not isinstance(payload, (list, tuple)) or len(payload) != 2:
        return None
    host = payload[0]
    port = payload[1]
    if not isinstance(host, str) or not host:
        return None
    if isinstance(port, bool) or not isinstance(port, int):
        return None
    if not 0 <= port <= 65535:
        return None
    return host, port


def _as_float(value: object) -> float | None:
    """Return a number as a float, or ``None`` when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_int(value: object) -> int | None:
    """Return a number as an int, or ``None`` when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
