"""Tests of the headless dump.

Nothing here starts a terminal and nothing touches the network: the
collector is a stub that hands out sockets, and the resolver is a stub
that answers with a fixed place.
"""

import io
import json

import pytest

from trafficmap.collector_ss import SocketRow
from trafficmap.config import Config
from trafficmap.dump import dump_events, run_dump
from trafficmap.flows import Closed, FlowTable, Opened, Updated
from trafficmap.geo import Geo

#: The place of the address of the stub.
PLACE = Geo("1.1.1.1", 37.4, -122.0, "US", "Mountain View", 15169, "Example")

#: The keys of an opened or an updated record.
DOCUMENTED = {
    "t",
    "type",
    "ip",
    "port",
    "proto",
    "process",
    "pid",
    "tx",
    "rx",
    "dtx",
    "drx",
    "lat",
    "lon",
    "country",
}


class FakeResolver:
    """A resolver that knows one address."""

    def __init__(self):
        self.asked: list[str] = []

    def lookup(self, ip):
        """Return the place of an address.

        Args:
            ip: The address to look up.

        Returns:
            The place of the address.
        """
        self.asked.append(ip)
        return PLACE


class FakeCollector:
    """A collector that hands out the same sockets again and again.

    Args:
        polls: The number of polls before the collector gives up.
    """

    def __init__(self, polls=2):
        self.polls = polls
        self.calls = 0

    def poll(self):
        """Return the sockets of one poll.

        Returns:
            One socket whose counters grow with every poll.

        Raises:
            KeyboardInterrupt: When the polls are over, so that the dump
                stops as it would when a person presses ``Ctrl-C``.
        """
        if self.calls >= self.polls:
            raise KeyboardInterrupt
        self.calls += 1
        return [
            SocketRow(
                proto="tcp",
                local=("10.0.0.2", 51000),
                remote=("1.1.1.1", 443),
                state="ESTAB",
                pid=100,
                process="curl",
                bytes_sent=1000 * self.calls,
                bytes_received=500 * self.calls,
                key="tcp|10.0.0.2:51000|1.1.1.1:443|100",
            )
        ]


def _flow():
    """Build a flow as the table would hand it over."""
    table = FlowTable(FakeResolver(), poll_ms=1000)
    events = table.update(
        [
            SocketRow(
                proto="tcp",
                local=("10.0.0.2", 51000),
                remote=("1.1.1.1", 443),
                state="ESTAB",
                pid=100,
                process="curl",
                bytes_sent=2000,
                bytes_received=1000,
                key="tcp|10.0.0.2:51000|1.1.1.1:443|100",
            )
        ],
        now=0.0,
    )
    return events[0].flow


def test_dump_events_writes_one_object_per_line():
    out = io.StringIO()
    flow = _flow()
    dump_events(
        [Opened(flow=flow), Updated(flow=flow, dtx=2000, drx=1000), Closed(key="k")],
        now=12.0,
        out=out,
    )
    lines = out.getvalue().splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert [record["type"] for record in records] == ["opened", "updated", "closed"]
    assert all(record["t"] == 12.0 for record in records)


def test_an_opened_record_holds_the_documented_keys():
    out = io.StringIO()
    dump_events([Opened(flow=_flow())], now=12.0, out=out)
    record = json.loads(out.getvalue())
    assert set(record) == DOCUMENTED
    assert record["ip"] == "1.1.1.1"
    assert record["port"] == 443
    assert record["proto"] == "tcp"
    assert record["process"] == "curl"
    assert record["pid"] == 100
    assert record["tx"] == 2000 and record["rx"] == 1000
    assert record["dtx"] == 0 and record["drx"] == 0
    assert record["lat"] == 37.4 and record["lon"] == -122.0
    assert record["country"] == "US"


def test_an_updated_record_holds_the_deltas():
    out = io.StringIO()
    flow = _flow()
    dump_events([Updated(flow=flow, dtx=2000, drx=1000)], now=12.0, out=out)
    record = json.loads(out.getvalue())
    assert set(record) == DOCUMENTED
    assert record["dtx"] == 2000 and record["drx"] == 1000
    assert record["type"] == "updated"


def test_a_closed_record_holds_only_the_key():
    out = io.StringIO()
    dump_events([Closed(key="1.1.1.1|100|tcp")], now=12.0, out=out)
    record = json.loads(out.getvalue())
    assert set(record) == {"t", "type", "key"}
    assert record["key"] == "1.1.1.1|100|tcp"
    assert record["type"] == "closed"


def test_a_flow_without_a_process_is_written_as_null():
    out = io.StringIO()
    table = FlowTable(FakeResolver(), poll_ms=1000)
    events = table.update(
        [
            SocketRow(
                proto="tcp",
                local=("10.0.0.2", 51000),
                remote=("1.1.1.1", 443),
                state="ESTAB",
                pid=None,
                process=None,
                bytes_sent=0,
                bytes_received=0,
                key="tcp|10.0.0.2:51000|1.1.1.1:443|None",
            )
        ],
        now=0.0,
    )
    dump_events(events, now=1.0, out=out)
    record = json.loads(out.getvalue())
    assert record["process"] is None and record["pid"] is None


def test_run_dump_writes_the_events_of_every_poll(tmp_path):
    path = tmp_path / "flows.jsonl"
    cfg = Config(dump=path, poll_ms=10)
    assert run_dump(cfg, FakeResolver(), FakeCollector(polls=2)) == 0
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["type"] for record in records] == ["opened", "updated"]
    assert records[1]["dtx"] == 1000 and records[1]["drx"] == 500


def test_run_dump_needs_a_path():
    with pytest.raises(ValueError):
        run_dump(Config(), FakeResolver(), FakeCollector(polls=0))


def test_run_dump_survives_a_failing_collector(tmp_path):
    class FailingCollector:
        """A collector that fails once and then works."""

        def __init__(self):
            self.calls = 0

        def poll(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("ss is not there")
            if self.calls > 2:
                raise KeyboardInterrupt
            return []

    path = tmp_path / "flows.jsonl"
    cfg = Config(dump=path, poll_ms=10)
    assert run_dump(cfg, FakeResolver(), FailingCollector()) == 0
    assert path.exists()
