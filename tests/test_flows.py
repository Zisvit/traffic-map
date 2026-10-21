"""Tests of the flow table.

No database and no network: the resolver is a stub that answers with a
fixed place for every address, and the sockets are built by hand.
"""

import pytest

from trafficmap.collector_ss import SocketRow
from trafficmap.flows import Closed, FlowTable, Opened, Updated
from trafficmap.geo import Geo

#: The places the stub resolver knows.
PLACES = {
    "1.1.1.1": Geo("1.1.1.1", 37.4, -122.0, "US", "Mountain View", 15169, "Example"),
    "8.8.8.8": Geo("8.8.8.8", 55.75, 37.61, "RU", "Moscow", 12345, "Example"),
}


class FakeResolver:
    """A resolver that answers with a fixed place.

    Args:
        places: The places of the addresses that are known.
    """

    def __init__(self, places=None):
        self.places = dict(PLACES if places is None else places)
        self.asked: list[str] = []

    def lookup(self, ip):
        """Return the place of an address.

        Args:
            ip: The address to look up.

        Returns:
            The place of the address, or a place without coordinates
            when the address is not known.
        """
        self.asked.append(ip)
        return self.places.get(ip, Geo(ip, None, None, "--", None, None, None))


@pytest.fixture
def resolver():
    """A resolver that knows two addresses."""
    return FakeResolver()


def row(
    remote_ip="1.1.1.1",
    port=443,
    proto="tcp",
    pid=100,
    process="curl",
    local_port=51000,
    sent=None,
    received=None,
    state="ESTAB",
):
    """Build a socket of a poll.

    Args:
        remote_ip: The address of the other side.
        port: The port of the other side.
        proto: The protocol of the socket.
        pid: The number of the process that owns the socket.
        process: The name of that process.
        local_port: The port of this side, which makes the socket
            unique.
        sent: The bytes the socket has sent.
        received: The bytes the socket has received.
        state: The state of the socket.

    Returns:
        The socket.
    """
    return SocketRow(
        proto=proto,
        local=("10.0.0.2", local_port),
        remote=(remote_ip, port),
        state=state,
        pid=pid,
        process=process,
        bytes_sent=sent,
        bytes_received=received,
        key=f"{proto}|10.0.0.2:{local_port}|{remote_ip}:{port}|{pid}",
    )


def test_the_first_poll_opens_a_flow(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    events = table.update([row(sent=1000, received=500)], now=0.0)
    assert len(events) == 1
    opened = events[0]
    assert isinstance(opened, Opened)
    assert opened.flow.key == "1.1.1.1|100|tcp"
    assert opened.flow.tx == 1000 and opened.flow.rx == 500
    assert opened.flow.tx_rate == 0.0 and opened.flow.rx_rate == 0.0
    assert opened.flow.process == "curl" and opened.flow.pid == 100
    assert opened.flow.proto == "tcp" and opened.flow.port == 443
    assert opened.flow.state == "ESTAB"
    assert opened.flow.remote.country == "US"


def test_the_second_poll_reports_the_growth(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(sent=1000, received=500)], now=0.0)
    events = table.update([row(sent=3000, received=1500)], now=1.0)
    updated = next(e for e in events if isinstance(e, Updated))
    assert updated.dtx == 2000 and updated.drx == 1000
    assert updated.flow.tx_rate == pytest.approx(2000)
    assert updated.flow.rx_rate == pytest.approx(1000)
    assert updated.flow.tx == 3000 and updated.flow.rx == 1500


def test_a_new_socket_carries_no_delta(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(local_port=51000, sent=1000)], now=0.0)
    events = table.update(
        [row(local_port=51000, sent=1000), row(local_port=51001, sent=5000)],
        now=1.0,
    )
    # The new socket only brings a baseline, so nothing changed.
    assert not any(isinstance(e, Updated) for e in events)
    assert table.flows()[0].tx == 6000


def test_a_quiet_poll_says_nothing(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(sent=1000)], now=0.0)
    assert table.update([row(sent=1000)], now=1.0) == []


def test_a_counter_reset_never_goes_negative(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(sent=5000)], now=0.0)
    events = table.update([row(sent=100)], now=1.0)
    assert all(getattr(e, "dtx", 0) >= 0 for e in events)
    updated = next(e for e in events if isinstance(e, Updated))
    assert updated.dtx == 100


def test_the_rate_is_smoothed(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(sent=0)], now=0.0)
    table.update([row(sent=1000)], now=1.0)
    table.update([row(sent=3000)], now=2.0)
    flow = table.flows()[0]
    assert flow.tx_rate == pytest.approx(1500)


def test_a_quiet_flow_loses_half_of_its_rate(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(sent=0)], now=0.0)
    table.update([row(sent=1000)], now=1.0)
    table.update([row(sent=1000)], now=2.0)
    assert table.flows()[0].tx_rate == pytest.approx(500)


def test_a_flow_closes_after_three_idle_polls(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    key = table.update([row()], now=0.0)[0].flow.key
    table.update([], now=1.0)
    table.update([], now=2.0)
    table.update([], now=3.0)
    events = table.update([], now=4.5)
    assert any(isinstance(e, Closed) and e.key == key for e in events)
    assert table.flows() == []


def test_a_flow_that_is_alive_is_not_closed(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(sent=0)], now=0.0)
    events = table.update([row(sent=0)], now=2.0)
    assert not any(isinstance(e, Closed) for e in events)


def test_a_local_peer_is_counted_and_not_mapped(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(remote_ip="10.0.0.5")], now=0.0)
    assert table.stats().local == 1
    assert table.flows() == []
    assert "10.0.0.5" not in resolver.asked


def test_a_public_peer_without_a_place_is_unknown(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(remote_ip="203.0.113.7")], now=0.0)
    assert table.stats().unknown == 1
    assert table.flows() == []


def test_the_local_and_the_unknown_peers_are_counted_once(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update(
        [
            row(remote_ip="10.0.0.5", local_port=51000),
            row(remote_ip="10.0.0.5", local_port=51001),
            row(remote_ip="203.0.113.7", local_port=51002),
            row(remote_ip="203.0.113.8", local_port=51003),
        ],
        now=0.0,
    )
    stats = table.stats()
    assert stats.local == 1 and stats.unknown == 2


def test_two_sockets_of_one_process_make_one_flow(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    first = row(local_port=51000, sent=1000, received=0)
    second = row(local_port=51001, sent=2000, received=0)
    table.update([first, second], now=0.0)
    events = table.update(
        [
            row(local_port=51000, sent=3000, received=0),
            row(local_port=51001, sent=4000, received=0),
        ],
        now=1.0,
    )
    updated = [e for e in events if isinstance(e, Updated)]
    assert len(updated) == 1
    assert updated[0].dtx == 2000 + 2000
    assert len(table.flows()) == 1
    # The counters of the two sockets are added up.
    assert table.flows()[0].tx == 3000 + 4000


def test_the_deltas_of_the_sockets_are_summed(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update(
        [row(local_port=51000, sent=1000), row(local_port=51001, sent=2000)], now=0.0
    )
    events = table.update(
        [row(local_port=51000, sent=1500), row(local_port=51001, sent=2500)], now=1.0
    )
    updated = next(e for e in events if isinstance(e, Updated))
    assert updated.dtx == 500 + 500


def test_the_top_is_sorted_by_rate(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update(
        [row(remote_ip="1.1.1.1", sent=0), row(remote_ip="8.8.8.8", port=53, sent=0)],
        now=0.0,
    )
    table.update(
        [
            row(remote_ip="1.1.1.1", sent=100),
            row(remote_ip="8.8.8.8", port=53, sent=5000),
        ],
        now=1.0,
    )
    top = table.stats().top
    assert [label for label, _ in top] == ["curl:53", "curl:443"]
    assert top[0][1] == pytest.approx(5000)
    assert top[1][1] == pytest.approx(100)


def test_a_flow_without_a_process_is_unknown_in_the_top(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(process=None, sent=0)], now=0.0)
    table.update([row(process=None, sent=1000)], now=1.0)
    assert table.stats().top[0][0] == "unknown:443"


def test_the_top_joins_the_flows_of_one_process_and_port(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update(
        [row(remote_ip="1.1.1.1", sent=0), row(remote_ip="8.8.8.8", sent=0)], now=0.0
    )
    table.update(
        [row(remote_ip="1.1.1.1", sent=100), row(remote_ip="8.8.8.8", sent=100)],
        now=1.0,
    )
    top = table.stats().top
    assert len(top) == 1
    assert top[0][0] == "curl:443" and top[0][1] == pytest.approx(200)


def test_the_top_holds_five_entries_at_most(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    rows = [row(pid=100 + n, process=f"p{n}", sent=0) for n in range(8)]
    table.update(rows, now=0.0)
    table.update(rows, now=1.0)
    assert len(table.stats().top) == 5


def test_the_stats_count_the_flows_and_the_peers(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update(
        [
            row(remote_ip="1.1.1.1", local_port=51000, sent=0),
            row(remote_ip="1.1.1.1", local_port=51001, sent=0),
            row(remote_ip="8.8.8.8", local_port=51002, sent=0),
        ],
        now=0.0,
    )
    stats = table.stats()
    assert stats.flows == 2
    assert stats.peers == 2
    assert stats.countries == {"US": 1, "RU": 1}


def test_a_flow_that_never_carried_a_byte_leaves_the_map(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(sent=0, received=0)], now=0.0)
    assert len(table.flows()) == 1
    table.update([row(sent=0, received=0)], now=6.0)
    assert table.flows() == []


def test_a_flow_that_carried_a_byte_stays(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(sent=10, received=0)], now=0.0)
    table.update([row(sent=10, received=0)], now=6.0)
    assert len(table.flows()) == 1


def test_the_flows_are_copies(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(sent=100)], now=0.0)
    flow = table.flows()[0]
    flow.tx = 999999
    assert table.flows()[0].tx == 100


def test_the_table_refuses_a_poll_of_no_length(resolver):
    with pytest.raises(ValueError):
        FlowTable(resolver, poll_ms=0)


def test_the_rates_are_per_second(resolver):
    table = FlowTable(resolver, poll_ms=500)
    table.update([row(sent=0)], now=0.0)
    table.update([row(sent=1000)], now=0.5)
    assert table.flows()[0].tx_rate == pytest.approx(2000)


def test_the_stats_hold_the_sum_of_the_rates(resolver):
    table = FlowTable(resolver, poll_ms=1000)
    table.update([row(sent=0), row(remote_ip="8.8.8.8", sent=0)], now=0.0)
    table.update([row(sent=1000), row(remote_ip="8.8.8.8", sent=3000)], now=1.0)
    stats = table.stats()
    assert stats.tx_rate == pytest.approx(4000)
    assert stats.rx_rate == pytest.approx(0)
