"""Tests of the collector of the packets.

The packets are made here, written to a file of packets, and read back
by the real ``tshark``: reading a file needs no privileges, so these
tests run wherever ``tshark`` is installed.
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import struct
import subprocess
import sys

import pytest

from trafficmap.collector_pcap import (
    FIELDS,
    Burst,
    Packet,
    PcapCollector,
    default_interface,
    local_addresses,
    parse_packet_line,
)

#: The second of the first packet of the file of the tests.
BASE_TS = 1_712_345_678

#: The IPv4 address of the machine in the tests.
LOCAL = "192.168.1.10"

#: The IPv6 address of the machine in the tests.
LOCAL6 = "2001:db8::10"

#: The addresses of the machine in the tests.
ADDRESSES = {LOCAL, LOCAL6}

#: The program that reads the packets, when it is installed.
TSHARK = shutil.which("tshark")

needs_tshark = pytest.mark.skipif(TSHARK is None, reason="tshark is not installed")


# -- the packets of the tests ------------------------------------------


def _checksum(data: bytes) -> int:
    """Return the checksum of a header of the IP.

    Args:
        data: The bytes of the header.

    Returns:
        The one's complement of the sum of the words of the header.
    """
    if len(data) % 2:
        data += b"\x00"
    total = sum(
        (data[index] << 8) | data[index + 1] for index in range(0, len(data), 2)
    )
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def _ipv4(src: str, dst: str, proto: int, payload: bytes) -> bytes:
    """Return an IPv4 packet.

    Args:
        src: The address the packet comes from.
        dst: The address the packet goes to.
        proto: The number of the protocol of the payload.
        payload: The bytes of the payload.

    Returns:
        The packet, header included.
    """
    header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(payload),
        0x1234,
        0x4000,
        64,
        proto,
        0,
        ipaddress.IPv4Address(src).packed,
        ipaddress.IPv4Address(dst).packed,
    )
    header = header[:10] + struct.pack("!H", _checksum(header)) + header[12:]
    return header + payload


def _ipv6(src: str, dst: str, nxt: int, payload: bytes) -> bytes:
    """Return an IPv6 packet without extension headers.

    Args:
        src: The address the packet comes from.
        dst: The address the packet goes to.
        nxt: The number of the protocol of the payload.
        payload: The bytes of the payload.

    Returns:
        The packet, header included.
    """
    header = (
        struct.pack("!IHBB", 6 << 28, len(payload), nxt, 64)
        + ipaddress.IPv6Address(src).packed
        + ipaddress.IPv6Address(dst).packed
    )
    return header + payload


def _tcp(sport: int, dport: int, payload: bytes = b"") -> bytes:
    """Return a TCP segment."""
    return (
        struct.pack("!HHIIBBHHH", sport, dport, 1, 2, 5 << 4, 0x18, 65535, 0, 0)
        + payload
    )


def _udp(sport: int, dport: int, payload: bytes = b"") -> bytes:
    """Return a UDP datagram."""
    return struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload


def _frame(payload: bytes, ether_type: int = 0x0800) -> bytes:
    """Return an Ethernet frame around a packet."""
    return (
        bytes.fromhex("00112233445566778899aabb")
        + struct.pack("!H", ether_type)
        + payload
    )


def _pcap(packets: list[bytes]) -> bytes:
    """Return a file of packets of the classic format.

    Args:
        packets: The frames to write.

    Returns:
        The bytes of the file, one record per frame.
    """
    out = struct.pack("!IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for index, packet in enumerate(packets):
        out += struct.pack("!IIII", BASE_TS + index, 0, len(packet), len(packet))
        out += packet
    return out


def _packets() -> list[bytes]:
    """Return the frames of the tests.

    Returns:
        Five frames: a request and an answer of TCP, a query of UDP,
        a packet of TCP over IPv6 and a packet of the machine to
        itself.
    """
    return [
        _frame(_ipv4(LOCAL, "1.1.1.1", 6, _tcp(51000, 443, b"x" * 20))),
        _frame(_ipv4("1.1.1.1", LOCAL, 6, _tcp(443, 51000, b"y" * 100))),
        _frame(_ipv4(LOCAL, "8.8.8.8", 17, _udp(53000, 53, b"z" * 30))),
        _frame(_ipv6(LOCAL6, "2001:db8::1", 6, _tcp(52000, 443, b"w" * 50)), 0x86DD),
        _frame(_ipv4(LOCAL, LOCAL, 6, _tcp(40000, 40001, b"q" * 10))),
    ]


@pytest.fixture
def pcap_file(tmp_path):
    """The file of packets of the tests."""
    path = tmp_path / "traffic.pcap"
    path.write_bytes(_pcap(_packets()))
    return path


# -- the lines of the table --------------------------------------------


def test_reads_a_line_of_tcp():
    line = "1712345678.5,10.0.0.2,1.1.1.1,,,6,,60,,74,51000,443,,"
    assert parse_packet_line(line) == Packet(
        ts=1712345678.5,
        src="10.0.0.2",
        dst="1.1.1.1",
        proto="tcp",
        src_port=51000,
        dst_port=443,
        size=60,
    )


def test_reads_a_line_of_udp():
    line = "1712345678.5,10.0.0.2,8.8.8.8,,,17,,58,,86,,,53000,53"
    packet = parse_packet_line(line)
    assert packet is not None
    assert (packet.proto, packet.src_port, packet.dst_port, packet.size) == (
        "udp",
        53000,
        53,
        58,
    )


def test_reads_a_line_of_ipv6():
    line = "1712345678.5,,,2001:db8::10,2001:db8::1,,6,,110,,52000,443,,"
    packet = parse_packet_line(line)
    assert packet is not None
    assert packet.src == LOCAL6 and packet.dst == "2001:db8::1"
    assert packet.proto == "tcp"
    # The size of an IPv6 packet is its header and what follows it.
    assert packet.size == 40 + 110


def test_a_line_of_another_protocol_is_other():
    # The fields are the ones of FIELDS: an IPv4 packet of ICMP has no
    # ports, and its size is not in its header but in its frame.
    line = ",".join(
        ["1712345678.5", "10.0.0.2", "1.1.1.1", "", "", "1", "", "", "", "74"]
        + [""] * 4
    )
    packet = parse_packet_line(line)
    assert packet is not None
    assert packet.proto == "other"
    assert packet.src_port == 0 and packet.dst_port == 0
    assert packet.size == 74


def test_a_broken_line_is_not_a_packet():
    assert parse_packet_line("") is None
    assert parse_packet_line("not,a,packet") is None
    assert parse_packet_line(",,,,,,,,,,,,,") is None
    assert parse_packet_line("abc,10.0.0.2,1.1.1.1,,,6,,60,,74,1,2,,") is None


def test_the_fields_of_the_table_are_the_ones_of_the_parser():
    # Every column the parser reads has a name in the table it asks
    # for: a field that is added to one of the two lists must be added
    # to the other one as well.
    assert len(FIELDS) == 14
    assert FIELDS[0] == "frame.time_epoch"
    assert FIELDS[-2:] == ("udp.srcport", "udp.dstport")


# -- the real tshark ----------------------------------------------------


@needs_tshark
def test_the_real_tshark_reads_the_file(pcap_file):
    collector = PcapCollector(pcap_file=pcap_file, addresses=ADDRESSES)
    try:
        result = subprocess.run(
            collector.command,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    finally:
        collector.close()

    packets = [parse_packet_line(line) for line in result.stdout.splitlines()]
    packets = [packet for packet in packets if packet is not None]
    assert len(packets) == 5

    outgoing = next(packet for packet in packets if packet.src == LOCAL)
    assert (
        outgoing.dst,
        outgoing.proto,
        outgoing.src_port,
        outgoing.dst_port,
        outgoing.size,
    ) == ("1.1.1.1", "tcp", 51000, 443, 60)

    incoming = next(packet for packet in packets if packet.src == "1.1.1.1")
    assert (incoming.dst, incoming.dst_port, incoming.size) == (LOCAL, 51000, 140)

    query = next(packet for packet in packets if packet.proto == "udp")
    assert (query.src, query.src_port, query.dst, query.dst_port) == (
        LOCAL,
        53000,
        "8.8.8.8",
        53,
    )
    assert query.size == 58

    over_v6 = next(packet for packet in packets if ":" in packet.src)
    assert (over_v6.proto, over_v6.size) == ("tcp", 110)


# -- the collector ------------------------------------------------------


@needs_tshark
def test_the_collector_turns_the_packets_into_sockets(pcap_file):
    collector = PcapCollector(pcap_file=pcap_file, addresses=ADDRESSES)
    try:
        rows = collector.poll()
        by_remote = {row.remote: row for row in rows}

        assert set(by_remote) == {
            ("1.1.1.1", 443),
            ("8.8.8.8", 53),
            ("2001:db8::1", 443),
        }
        # The packet of the machine to itself is not on the map.
        assert ("192.168.1.10", 40001) not in by_remote

        flow = by_remote[("1.1.1.1", 443)]
        assert flow.local == (LOCAL, 51000)
        assert (flow.bytes_sent, flow.bytes_received) == (60, 140)
        assert flow.proto == "tcp" and flow.state == "ESTAB"

        query = by_remote[("8.8.8.8", 53)]
        assert query.proto == "udp"
        assert (query.bytes_sent, query.bytes_received) == (58, 0)

        over_v6 = by_remote[("2001:db8::1", 443)]
        assert over_v6.local == (LOCAL6, 52000)
        assert (over_v6.bytes_sent, over_v6.bytes_received) == (110, 0)

        # The counters and the keys do not move between two polls.
        keys = {row.key for row in rows}
        again = collector.poll()
        assert {row.key for row in again} == keys
        assert {row.key: row.bytes_sent for row in again} == {
            row.key: row.bytes_sent for row in rows
        }
    finally:
        collector.close()


@needs_tshark
def test_the_bursts_hold_the_traffic_of_the_peers(pcap_file):
    collector = PcapCollector(pcap_file=pcap_file, addresses=ADDRESSES)
    try:
        collector.poll()
        bursts = collector.drain_bursts()
        assert [burst.remote for burst in bursts] == [
            "1.1.1.1",
            "2001:db8::1",
            "8.8.8.8",
        ]
        assert bursts[0] == Burst(
            remote="1.1.1.1",
            bytes=200,
            packets=2,
            proto="tcp",
            age=bursts[0].age,
        )
        assert all(burst.age >= 0 for burst in bursts)
        # Draining clears the bursts.
        assert collector.drain_bursts() == []
    finally:
        collector.close()


@needs_tshark
def test_the_process_of_the_port_is_taken(pcap_file):
    def lookup() -> dict[int, tuple[int | None, str | None]]:
        return {51000: (1234, "curl")}

    collector = PcapCollector(
        pcap_file=pcap_file, addresses=ADDRESSES, process_lookup=lookup
    )
    try:
        rows = collector.poll()
        flow = next(row for row in rows if row.remote == ("1.1.1.1", 443))
        assert (flow.pid, flow.process) == (1234, "curl")
        assert flow.key.endswith("|1234")
    finally:
        collector.close()


def test_a_missing_tshark_is_reported():
    with pytest.raises(RuntimeError, match="tshark"):
        PcapCollector(command=["trafficmap-no-such-program"], addresses=ADDRESSES)


def test_the_capture_without_privileges_says_what_to_do():
    script = (
        "import sys; sys.stderr.write('tshark: Couldn\\'t run dumpcap in child "
        "process: Permission denied\\n'); sys.exit(1)"
    )
    collector = PcapCollector(
        command=[sys.executable, "-c", script], addresses=ADDRESSES
    )
    try:
        with pytest.raises(RuntimeError) as error:
            collector.poll()
    finally:
        collector.close()
    assert "setcap" in str(error.value)
    assert collector.error_label == "capture error"


# -- the addresses and the interface ------------------------------------


def test_the_addresses_of_the_machine(monkeypatch):
    output = json.dumps(
        [
            {"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}]},
            {"ifname": "wlan0", "addr_info": [{"local": "192.168.1.10"}]},
        ]
    )
    monkeypatch.setattr("trafficmap.collector_pcap._run", lambda command: output)
    assert local_addresses() == {"127.0.0.1", "::1", "192.168.1.10"}


def test_the_addresses_of_the_machine_when_ip_fails(monkeypatch):
    monkeypatch.setattr("trafficmap.collector_pcap._run", lambda command: None)
    assert local_addresses() == set()


def test_the_interface_of_the_default_route(monkeypatch):
    output = json.dumps([{"dst": "default", "dev": "singbox_tun"}])
    monkeypatch.setattr("trafficmap.collector_pcap._run", lambda command: output)
    assert default_interface() == "singbox_tun"


def test_the_interface_of_the_default_route_from_the_table(monkeypatch):
    answers = {
        ("ip", "-j", "route", "get", "1.1.1.1"): None,
        ("ip", "route", "show", "default"): "default via 10.0.0.1 dev wlan0",
    }

    def run(command):
        return answers.get(tuple(command))

    monkeypatch.setattr("trafficmap.collector_pcap._run", run)
    assert default_interface() == "wlan0"
