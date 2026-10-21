"""Tests of the parser of ``ss`` and of the collector.

The parser is fed with a recorded output of ``ss``, and the collector
with commands that pretend to be ``ss``.
"""

import shlex
from pathlib import Path

import pytest

from trafficmap.collector_ss import SocketRow, SsCollector, parse_sockets

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ss_tinup.txt"

#: The header of the table, as ``ss`` prints it.
HEADER = (
    "Netid State      Recv-Q Send-Q     "
    "Local Address:Port     Peer Address:Port Process\n"
)


@pytest.fixture
def fixture_text() -> str:
    """The recorded output of ``ss``."""
    return FIXTURE.read_text(encoding="utf-8")


def test_parses_ipv4_with_process(fixture_text):
    rows = parse_sockets(fixture_text)
    r = next(r for r in rows if r.remote[1] == 443 and r.process == "curl")
    assert r.bytes_sent == 1200 and r.bytes_received == 9000
    assert r.proto == "tcp" and r.pid == 100


def test_parses_ipv6(fixture_text):
    r = next(r for r in parse_sockets(fixture_text) if ":" in r.remote[0])
    assert r.remote == ("2001:db8::1", 443)


def test_skips_listen_and_wildcard(fixture_text):
    rows = parse_sockets(fixture_text)
    assert all(r.state != "LISTEN" for r in rows)
    assert all(r.remote[0] not in {"0.0.0.0", "::", "*"} for r in rows)


def test_header_is_not_a_socket(fixture_text):
    rows = parse_sockets(fixture_text)
    assert len(rows) == 5
    assert all(r.proto in {"tcp", "udp"} for r in rows)
    assert all(r.state not in {"Recv-Q", "Send-Q"} for r in rows)


def test_udp_row_has_no_counters(fixture_text):
    rows = parse_sockets(fixture_text)
    udp = [r for r in rows if r.proto == "udp"]
    assert udp
    assert all(r.bytes_sent is None for r in udp)
    assert all(r.bytes_received is None for r in udp)


def test_row_without_process_has_no_pid(fixture_text):
    rows = parse_sockets(fixture_text)
    r = next(r for r in rows if r.remote == ("199.165.136.101", 443))
    assert r.pid is None and r.process is None
    assert r.bytes_sent == 1094493 and r.bytes_received == 135460


def test_zone_is_stripped(fixture_text):
    rows = parse_sockets(fixture_text)
    r = next(r for r in rows if r.proto == "udp")
    assert r.local == ("192.168.170.189", 68)
    assert r.remote == ("192.168.170.1", 67)


def test_key_format(fixture_text):
    rows = parse_sockets(fixture_text)
    r = next(r for r in rows if r.process == "curl")
    assert r.key == "tcp|10.0.0.2:51000|1.1.1.1:443|100"
    without = next(r for r in rows if r.process is None and r.proto == "tcp")
    assert without.key == "tcp|192.168.170.189:50376|199.165.136.101:443|None"


def test_garbage_line_is_skipped(fixture_text):
    text = fixture_text + "this is not a socket line\n\tand not counters either\n"
    rows = parse_sockets(text)
    assert len(rows) == len(parse_sockets(fixture_text))


def test_time_wait_is_skipped(fixture_text):
    rows = parse_sockets(fixture_text)
    assert all(r.state != "TIME-WAIT" for r in rows)
    assert all(r.remote != ("127.0.0.1", 10808) for r in rows)


def test_first_process_entry_wins():
    text = (
        HEADER + "tcp   ESTAB      0      0   10.0.0.2:51000   1.1.1.1:443"
        '   users:(("first",pid=1,fd=2),("second",pid=3,fd=4))\n'
    )
    r = parse_sockets(text)[0]
    assert r.process == "first" and r.pid == 1


def test_process_name_with_spaces():
    text = (
        HEADER + "tcp   ESTAB      0      0   10.0.0.2:51000   1.1.1.1:443"
        '   users:(("My App",pid=42,fd=7))\n'
    )
    r = parse_sockets(text)[0]
    assert r.process == "My App" and r.pid == 42


def test_collector_raises_on_error():
    collector = SsCollector(command=["sh", "-c", "exit 3"])
    with pytest.raises(RuntimeError) as info:
        collector.poll()
    message = str(info.value)
    assert "exit 3" in message and "3" in message


def test_collector_reads_the_fixture():
    command = ["sh", "-c", f"cat {shlex.quote(str(FIXTURE))}"]
    rows = SsCollector(command=command).poll()
    assert rows == parse_sockets(FIXTURE.read_text(encoding="utf-8"))


def test_collector_uses_the_default_command():
    assert SsCollector().command == ["ss", "-t", "-u", "-a", "-i", "-n", "-p"]


def test_collector_rejects_an_empty_command():
    with pytest.raises(ValueError):
        SsCollector(command=[])


def test_row_is_frozen():
    row = SocketRow(
        proto="tcp",
        local=("10.0.0.2", 51000),
        remote=("1.1.1.1", 443),
        state="ESTAB",
        pid=100,
        process="curl",
        bytes_sent=1200,
        bytes_received=9000,
        key="tcp|10.0.0.2:51000|1.1.1.1:443|100",
    )
    with pytest.raises(Exception):
        row.pid = 101
