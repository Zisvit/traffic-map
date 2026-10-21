"""The headless dump: the flows of the machine as lines of JSON.

The dump is the way to see what the map would draw without a terminal.
The program polls the sockets, turns them into flows and writes an
event per line::

    {"t": 12.0, "type": "updated", "ip": "1.1.1.1", "port": 443,
     "proto": "tcp", "process": "curl", "pid": 100, "tx": 2000,
     "rx": 1000, "dtx": 2000, "drx": 1000, "lat": 37.4, "lon": -122.0,
     "country": "US"}

One object per line, so the file is read by ``jq``, by a script, or by
a person with ``grep``.  A closed flow has nothing to show but its key::

    {"t": 13.0, "type": "closed", "key": "1.1.1.1|100|tcp"}

The dump never touches the terminal: it is a plain file, and the
program that writes it can run in a window, in a container or in a
service.

A poll that fails changes nothing.  The sockets are not read, the table
is not touched and no event is written: a command that fails once may
well work the next time, and a failed poll must not close the flows
that are still there.
"""

from __future__ import annotations

import json
import logging
import signal
import time
from typing import Any, Callable, TextIO

from trafficmap.collector_mock import MockCollector
from trafficmap.collector_ss import SocketRow, SsCollector
from trafficmap.config import Config
from trafficmap.flows import Closed, FlowEvent, FlowTable, Opened, Updated
from trafficmap.geo import GeoResolver

__all__ = ["dump_events", "run_dump"]

log = logging.getLogger(__name__)


def dump_events(events: list[FlowEvent], now: float, out: TextIO) -> None:
    """Write the events of a poll as lines of JSON.

    Args:
        events: The events of the poll.
        now: The moment of the poll, in seconds.
        out: The file the lines are written to.
    """
    for event in events:
        record = _record(event, now)
        out.write(json.dumps(record, ensure_ascii=False))
        out.write("\n")


def run_dump(
    cfg: Config, resolver: GeoResolver, collector: SsCollector | MockCollector
) -> int:
    """Poll the sockets and write the events until the program stops.

    Args:
        cfg: The configuration of the program.  Its ``dump`` field says
            where the lines go and its ``poll_ms`` field how often the
            sockets are read.
        resolver: The resolver that tells where the remote addresses
            are.
        collector: The collector that reads the sockets: the one of
            ``ss``, or the one that replays a recording.

    Returns:
        ``0`` when the program was stopped with ``Ctrl-C`` or with
        ``SIGTERM``.

    Raises:
        ValueError: If the configuration has no path for the dump.
    """
    if cfg.dump is None:
        raise ValueError("the dump path is not set")

    table = FlowTable(resolver, poll_ms=cfg.poll_ms)
    interval = cfg.poll_ms / 1000.0
    restore = _install_term_handler()
    try:
        with open(cfg.dump, "w", encoding="utf-8") as out:
            while True:
                now = time.monotonic()
                rows = _poll(collector)
                if rows is not None:
                    events = table.update(rows, now)
                    dump_events(events, now, out)
                    out.flush()
                time.sleep(interval)
    except KeyboardInterrupt:
        log.debug("the dump was stopped")
        return 0
    finally:
        if restore is not None:
            restore()


def _poll(collector: SsCollector | MockCollector) -> list[SocketRow] | None:
    """Read the sockets once and survive a failure of the command.

    Args:
        collector: The collector that reads the sockets.

    Returns:
        The sockets of the poll, or ``None`` when the command failed.
        A failed poll leaves the table alone: an empty list would close
        every flow and open it again at the next poll.
    """
    try:
        return collector.poll()
    except RuntimeError as exc:
        log.debug("cannot read the sockets: %s", exc)
        return None


def _install_term_handler() -> Callable[[], None] | None:
    """Make ``SIGTERM`` stop the dump like ``Ctrl-C`` does.

    Returns:
        A function that puts the old handler back, or ``None`` when the
        handler could not be installed: the program is not the main
        thread, or the system has no ``SIGTERM``.
    """
    if not hasattr(signal, "SIGTERM"):
        return None
    try:
        previous = signal.signal(signal.SIGTERM, _stop_the_dump)
    except (OSError, ValueError):
        # Not the main thread: the signal is not ours to handle.
        return None

    def restore() -> None:
        """Put the old handler of ``SIGTERM`` back."""
        try:
            signal.signal(signal.SIGTERM, previous)
        except (OSError, ValueError):
            pass

    return restore


def _stop_the_dump(signum: int, frame: object) -> None:
    """Stop the dump the way ``Ctrl-C`` does.

    Args:
        signum: The number of the signal.
        frame: The frame the signal arrived in.
    """
    raise KeyboardInterrupt


def _record(event: FlowEvent, now: float) -> dict[str, Any]:
    """Build the object that describes one event.

    Args:
        event: The event of the poll.
        now: The moment of the poll, in seconds.

    Returns:
        The object that is written to the dump.
    """
    if isinstance(event, Closed):
        return {"t": now, "type": "closed", "key": event.key}

    if isinstance(event, Opened):
        kind = "opened"
        dtx = 0
        drx = 0
    else:
        kind = "updated"
        dtx = event.dtx
        drx = event.drx

    flow = event.flow
    return {
        "t": now,
        "type": kind,
        "ip": flow.remote.ip,
        "port": flow.port,
        "proto": flow.proto,
        "process": flow.process or None,
        "pid": flow.pid,
        "tx": flow.tx,
        "rx": flow.rx,
        "dtx": dtx,
        "drx": drx,
        "lat": flow.remote.lat,
        "lon": flow.remote.lon,
        "country": flow.remote.country,
    }
