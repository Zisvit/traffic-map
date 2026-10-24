"""The traffic of the machine, read from the packets of the network.

``ss`` knows the sockets of the machine and their byte counters, but
it does not know the UDP traffic that does not open a socket, and it
misses every connection that is shorter than a second.  The packets
themselves know everything: every byte of every connection is in
them.

``tshark`` is the program that reads the packets and prints them as a
table.  It is asked for one line per packet::

    1712345678.123456,192.168.1.10,1.1.1.1,,,6,,60,,74,51000,443,,

The line holds the time of the packet, the addresses of its two ends,
the protocol and the ports.  A field that does not apply to the packet
is empty: an IPv6 packet has no ``ip.src``, a UDP packet has no
``tcp.srcport``, and only an IPv4 packet has an ``ip.len``.

The direction
-------------
A packet is ours when one of its two ends is one of our own
addresses.  A packet that leaves the machine is *outgoing*, a packet
that comes to it is *incoming*, a packet that goes from one of our
addresses to another one is *local* (it is counted, but it has no
place on the map), and a packet that has none of our addresses is
*transit*: it crosses the machine without being its traffic, and it
is ignored.

The counters
------------
Every five-tuple has its own counters of bytes, and they are never
put back to zero: the table of the flows reads the difference between
two polls, and a counter that starts again at zero looks like a burst
of new traffic.  A five-tuple that carries nothing for a minute is
dropped from the table: its last counters are not flushed as a
delta, the flow simply stops.

The privileges
--------------
Reading the packets of the machine needs privileges: without them
``tshark`` cannot open the interface.  The error says what to do about
it, so that the user does not have to look it up.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from trafficmap.collector_ss import SocketRow

__all__ = [
    "Burst",
    "Packet",
    "PcapCollector",
    "default_interface",
    "local_addresses",
    "parse_packet_line",
]

log = logging.getLogger(__name__)

#: The fields of the table of ``tshark``, in the order they are asked
#: for.  The order is the order of the columns of the line.
FIELDS: tuple[str, ...] = (
    "frame.time_epoch",
    "ip.src",
    "ip.dst",
    "ipv6.src",
    "ipv6.dst",
    "ip.proto",
    "ipv6.nxt",
    "ip.len",
    "ipv6.plen",
    "frame.len",
    "tcp.srcport",
    "tcp.dstport",
    "udp.srcport",
    "udp.dstport",
)

#: The number of the protocol of TCP in the header of an IP packet.
PROTO_TCP = 6

#: The number of the protocol of UDP in the header of an IP packet.
PROTO_UDP = 17

#: The length of the header of an IPv6 packet, in bytes.
IPV6_HEADER = 40

#: The addresses of the loopback: they are always ours.
LOOPBACK: tuple[str, ...] = ("127.0.0.1", "::1")

#: The message that tells how to install the program that captures.
MISSING_TSHARK = (
    "tshark is required for packet capture (install wireshark-common or tshark)"
)

#: The message that tells how to capture the packets of the machine.
NO_PRIVILEGES = (
    "capture needs privileges: run as root, or once: "
    "sudo setcap cap_net_raw,cap_net_admin=eip $(command -v dumpcap)"
)

#: The words of an error that mean that the capture was not allowed.
PERMISSION_WORDS: tuple[str, ...] = (
    "denied",
    "access",
    "permission",
    "Отказано в доступе",
)

#: Seconds a five-tuple may be idle before it is dropped.
IDLE_TIMEOUT = 60.0

#: Seconds the capture may take to fail when it starts.
START_GRACE = 0.5

#: The number of lines of the error of ``tshark`` that are kept.
STDERR_TAIL = 20

#: The greatest number of packets that are kept for the bursts.
MAX_EVENTS = 100_000

#: The number of the columns of the line of a packet.
_TS = 0
_IP_SRC = 1
_IP_DST = 2
_IPV6_SRC = 3
_IPV6_DST = 4
_IP_PROTO = 5
_IPV6_NXT = 6
_IP_LEN = 7
_IPV6_PLEN = 8
_FRAME_LEN = 9
_TCP_SRC = 10
_TCP_DST = 11
_UDP_SRC = 12
_UDP_DST = 13


@dataclass(frozen=True, slots=True)
class Packet:
    """One packet of the network.

    Attributes:
        ts: The time of the packet, in seconds since the epoch.
        src: The address the packet comes from.
        dst: The address the packet goes to.
        proto: The protocol of the packet, ``"tcp"``, ``"udp"`` or
            ``"other"``.
        src_port: The port the packet comes from.
        dst_port: The port the packet goes to.
        size: The size of the IP packet, in bytes.
    """

    ts: float
    src: str
    dst: str
    proto: str
    src_port: int
    dst_port: int
    size: int


@dataclass(frozen=True, slots=True)
class Burst:
    """The traffic of one peer since the last poll.

    Attributes:
        remote: The address of the peer.
        bytes: The bytes the peer sent or received since the last
            poll.
        packets: The number of the packets since the last poll.
        proto: The protocol of the traffic, ``"tcp"`` or ``"udp"``.
        age: The seconds that passed since the newest packet of the
            burst.  The map draws the burst at the moment it happened,
            and not at the moment it was read.
    """

    remote: str
    bytes: int
    packets: int
    proto: str
    age: float = 0.0


@dataclass(slots=True)
class _Flow:
    """The counters of one five-tuple.

    Attributes:
        proto: The protocol of the packets.
        local: The address and the port of our side.
        remote: The address and the port of the other side.
        sent: The bytes we sent since the flow was seen.
        received: The bytes we received since the flow was seen.
        pid: The number of the process that owns the socket, when it
            is known.  It is kept once it is known: the key of the row
            must not change from one poll to the next.
        process: The name of that process.
        last_seen: The moment the flow carried a packet, in seconds.
    """

    proto: str
    local: tuple[str, int]
    remote: tuple[str, int]
    sent: int = 0
    received: int = 0
    pid: int | None = None
    process: str | None = None
    last_seen: float = 0.0


def parse_packet_line(line: str) -> Packet | None:
    """Read one line of the table of ``tshark``.

    The line is a CSV line whose fields are the ones of
    :data:`FIELDS`, in the same order.  A field that does not apply to
    the packet is empty.

    Args:
        line: The line of the table.

    Returns:
        The packet of the line, or ``None`` when the line does not
        hold one.  A line that cannot be read never raises: a broken
        line must not stop the map.
    """
    parts = line.split(",")
    if len(parts) < len(FIELDS):
        return None

    ts = _as_float(parts[_TS])
    if ts is None:
        return None

    src = parts[_IP_SRC].strip() or parts[_IPV6_SRC].strip()
    dst = parts[_IP_DST].strip() or parts[_IPV6_DST].strip()
    if not src or not dst:
        return None

    proto = _read_proto(parts)
    if proto == "tcp":
        src_port = _as_int(parts[_TCP_SRC]) or 0
        dst_port = _as_int(parts[_TCP_DST]) or 0
    elif proto == "udp":
        src_port = _as_int(parts[_UDP_SRC]) or 0
        dst_port = _as_int(parts[_UDP_DST]) or 0
    else:
        src_port = 0
        dst_port = 0

    return Packet(
        ts=ts,
        src=src,
        dst=dst,
        proto=proto,
        src_port=src_port,
        dst_port=dst_port,
        size=_read_size(parts),
    )


def _protocol_number(parts: list[str]) -> int | None:
    """Return the number of the protocol of a packet, if it has one.

    Args:
        parts: The fields of the line of the packet.

    Returns:
        The number of the protocol of the header of the packet, or
        ``None`` when the packet has no such number.  An IPv6 packet
        keeps it in ``ipv6.nxt`` instead of ``ip.proto``.
    """
    number = _as_int(parts[_IP_PROTO])
    if number is None:
        number = _as_int(parts[_IPV6_NXT])
    return number


def _read_size(parts: list[str]) -> int:
    """Return the size of a packet, in bytes.

    Args:
        parts: The fields of the line of the packet.

    Returns:
        The size of the IP packet.  An IPv4 packet holds it in its own
        header, an IPv6 packet is the length of its header and the
        length of what follows it.  The length of the frame is the
        last resort: it holds the headers of the link layer as well,
        but a packet of an unknown size is worse than a packet that is
        a little too big.
    """
    size = _as_int(parts[_IP_LEN])
    if size is not None:
        return size
    payload = _as_int(parts[_IPV6_PLEN])
    if payload is not None:
        return IPV6_HEADER + payload
    return _as_int(parts[_FRAME_LEN]) or 0


def _read_proto(parts: list[str]) -> str:
    """Return the protocol of a packet.

    Args:
        parts: The fields of the line of the packet.

    Returns:
        ``"tcp"``, ``"udp"`` or ``"other"``.  The number of the
        protocol of the header of an IP packet is used when there is
        one; an IPv6 packet keeps it in another field, and the ports
        are the last hint of its protocol.
    """
    number = _protocol_number(parts)
    if number == PROTO_TCP:
        return "tcp"
    if number == PROTO_UDP:
        return "udp"
    if number is None:
        if parts[_TCP_SRC].strip() or parts[_TCP_DST].strip():
            return "tcp"
        if parts[_UDP_SRC].strip() or parts[_UDP_DST].strip():
            return "udp"
    return "other"


def _as_float(text: str) -> float | None:
    """Return a number of a field of the table, or ``None``."""
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_int(text: str) -> int | None:
    """Return a whole number of a field of the table, or ``None``."""
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def local_addresses() -> set[str]:
    """Return the addresses of this machine.

    Every address of every interface is collected, the addresses of
    the loopback included.

    Returns:
        The addresses of the machine.  The set is empty when they
        cannot be read: the packets cannot be attributed then, and
        every one of them is transit traffic.
    """
    result = _run(["ip", "-j", "addr", "show"])
    if result is None:
        return set()
    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        log.debug("the output of ip -j addr show is not JSON")
        return set()
    if not isinstance(data, list) or not data:
        return set()

    addresses = set(LOOPBACK)
    for interface in data:
        if not isinstance(interface, dict):
            continue
        for info in interface.get("addr_info", []):
            if not isinstance(info, dict):
                continue
            local = info.get("local")
            if isinstance(local, str) and local:
                addresses.add(local)
    return addresses


def default_interface() -> str:
    """Return the interface that carries the traffic of the machine.

    It is the interface of the route to the internet, the one the
    packets of the machine go through.

    Returns:
        The name of the interface, or an empty string when it cannot
        be found.
    """
    result = _run(["ip", "-j", "route", "get", "1.1.1.1"])
    if result is not None:
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            for route in data:
                if isinstance(route, dict):
                    device = route.get("dev")
                    if isinstance(device, str) and device:
                        return device

    result = _run(["ip", "route", "show", "default"])
    if result is not None:
        words = result.split()
        for index, word in enumerate(words):
            if word == "dev" and index + 1 < len(words):
                return words[index + 1]
    return ""


def _run(command: list[str]) -> str | None:
    """Run a command and return what it answered.

    Args:
        command: The command to run.

    Returns:
        The output of the command, or ``None`` when it failed.
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("cannot run %s: %s", " ".join(command), exc)
        return None
    if result.returncode != 0:
        log.debug("%s failed: %s", " ".join(command), result.stderr.strip())
        return None
    return result.stdout


class PcapCollector:
    """The traffic of the machine, read from the packets of the network.

    The packets are read by ``tshark``, in a thread of its own, and
    are turned into the same rows of sockets as the collector of
    ``ss``: the table of the flows does not know the difference.

    Args:
        interface: The interface to capture on.  An empty name means
            the interface of the route to the internet.
        pcap_file: The file of packets to read instead of capturing
            the live traffic.  Reading a file needs no privileges.
        command: The command that reads the packets.  It is the
            command of ``tshark`` when it is missing.
        process_lookup: The function that tells the process of a
            port, ``{port: (pid, name)}``.  The packets do not know
            the process that sent them: the table of the sockets of
            the machine does.
        addresses: The addresses of this machine.  They are read from
            the kernel when they are missing.

    Raises:
        ValueError: If an interface and a file are both given.
        RuntimeError: If the program that captures the packets is not
            installed, or if the addresses of the machine cannot be
            read.
    """

    #: The words that tell the user that the capture is broken.
    error_label = "capture error"

    #: The collector reads every packet of the machine, not the
    #: sockets of the user: privileges do not change what it sees.
    sockets_only = False

    def __init__(
        self,
        interface: str = "",
        pcap_file: str | Path | None = None,
        command: list[str] | None = None,
        process_lookup: Callable[[], dict[int, tuple[int | None, str | None]]]
        | None = None,
        addresses: Iterable[str] | None = None,
    ) -> None:
        if interface and pcap_file is not None:
            raise ValueError("give an interface or a file of packets, not both")

        self._interface = interface
        self._pcap_file = Path(pcap_file) if pcap_file is not None else None
        self._process_lookup = process_lookup

        if addresses is None:
            found = local_addresses()
            if not found:
                raise RuntimeError(
                    "cannot read the addresses of the machine (ip -j addr show): "
                    "the packets cannot be attributed to it"
                )
            self._addresses = found
        else:
            self._addresses = {str(address) for address in addresses}

        self._command = list(command) if command is not None else self._build_command()
        if shutil.which(self._command[0]) is None:
            raise RuntimeError(MISSING_TSHARK)

        self._flows: dict[tuple[str, tuple[str, int], tuple[str, int]], _Flow] = {}
        self._packets: "queue.Queue[Packet]" = queue.Queue()
        self._events: deque[tuple[float, str, int, str]] = deque()
        self._local_packets = 0
        self._transit_packets = 0
        self._errors: deque[str] = deque(maxlen=STDERR_TAIL)
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._error_reader: threading.Thread | None = None
        self._stderr_done = threading.Event()

    # -- the command ---------------------------------------------------

    def _build_command(self) -> list[str]:
        """Return the command that reads the packets.

        Returns:
            The command of ``tshark`` that prints one line per packet.
        """
        command = ["tshark", "-n", "-l", "-T", "fields"]
        for field in FIELDS:
            command.extend(["-e", field])
        command.extend(["-E", "separator=,", "-E", "occurrence=f"])
        if self._pcap_file is not None:
            command.extend(["-r", str(self._pcap_file)])
        else:
            command.extend(["-i", self._interface or default_interface()])
        return command

    @property
    def command(self) -> list[str]:
        """The command the collector runs."""
        return list(self._command)

    @property
    def interface(self) -> str:
        """The interface the collector captures on."""
        return self._interface

    @property
    def addresses(self) -> set[str]:
        """The addresses of this machine."""
        return set(self._addresses)

    # -- the life of the capture ---------------------------------------

    def _start(self) -> None:
        """Start the program that reads the packets.

        Raises:
            RuntimeError: If the capture cannot start: the program is
                not allowed to read the packets of the machine.
        """
        log.debug("starting the capture: %s", " ".join(self._command))
        self._process = subprocess.Popen(
            self._command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            text=True,
            errors="replace",
        )
        self._reader = threading.Thread(
            target=self._read_packets, name="trafficmap-pcap", daemon=True
        )
        self._reader.start()
        self._error_reader = threading.Thread(
            target=self._read_errors, name="trafficmap-pcap-errors", daemon=True
        )
        self._error_reader.start()

        # A capture that cannot start dies at once: the program waits a
        # little, so that the error is known before the first poll.
        deadline = time.monotonic() + START_GRACE
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                break
            time.sleep(0.02)
        code = self._process.poll()
        if code is not None and code != 0:
            raise self._failure()

    def _read_packets(self) -> None:
        """Read the lines of the table and keep the packets of them."""
        process = self._process
        if process is None or process.stdout is None:  # pragma: no cover
            return
        for line in process.stdout:
            packet = parse_packet_line(line)
            if packet is not None:
                self._packets.put(packet)

    def _read_errors(self) -> None:
        """Read what the capture writes on its error stream."""
        process = self._process
        if process is None or process.stderr is None:  # pragma: no cover
            self._stderr_done.set()
            return
        for line in process.stderr:
            text = line.strip()
            if text:
                self._errors.append(text)
        self._stderr_done.set()

    def _failure(self) -> RuntimeError:
        """Return the error of a capture that stopped.

        Returns:
            The error: the one of the privileges when the capture was
            not allowed, the one of the last words of the error stream
            otherwise.
        """
        self._stderr_done.wait(timeout=1.0)
        text = " ".join(self._errors)
        if _is_permission_error(text):
            return RuntimeError(NO_PRIVILEGES)
        return RuntimeError(f"packet capture stopped: {text}")

    def _check_alive(self) -> None:
        """Raise the error of a capture that stopped.

        Raises:
            RuntimeError: If the capture stopped with an error.
        """
        process = self._process
        if process is None:
            return
        code = process.poll()
        if code is None or code == 0:
            return
        raise self._failure()

    # -- the polls -----------------------------------------------------

    def poll(self) -> list[SocketRow]:
        """Read the packets of the last second and answer with the sockets.

        Returns:
            One row per five-tuple that carried a packet.  The rows
            hold the same counters from one poll to the next, so that
            the table of the flows reads the traffic of the second
            that just passed.

        Raises:
            RuntimeError: If the capture stopped, or if it could not
                start.
        """
        if self._process is None:
            self._start()
        self._check_alive()
        self._read_all()
        self._drain_finished()
        self._evict_idle()
        return self._rows()

    def _read_all(self) -> None:
        """Take the packets of the queue and count them."""
        while True:
            try:
                packet = self._packets.get_nowait()
            except queue.Empty:
                break
            self._account(packet)

    def _drain_finished(self) -> None:
        """Take the last packets of a capture that has ended.

        The reading of a file of packets ends at once: its packets are
        read by the thread of the reader, and the poll that starts the
        capture may find the queue empty.  The thread is joined, so
        that the packets of the file are in the rows of the first poll.
        """
        process = self._process
        if process is None or process.poll() is None:
            return
        reader = self._reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.0)
        self._read_all()

    def _account(self, packet: Packet) -> None:
        """Count one packet in the flow it belongs to.

        Args:
            packet: The packet.
        """
        src_ours = packet.src in self._addresses
        dst_ours = packet.dst in self._addresses
        if src_ours and dst_ours:
            # The traffic of the machine to itself: it is counted, but
            # it has no place on the map.
            self._local_packets += 1
            return
        if src_ours:
            local = (packet.src, packet.src_port)
            remote = (packet.dst, packet.dst_port)
            outgoing = True
        elif dst_ours:
            local = (packet.dst, packet.dst_port)
            remote = (packet.src, packet.src_port)
            outgoing = False
        else:
            # The packet crosses the machine: it is not its traffic.
            self._transit_packets += 1
            return

        if packet.proto not in ("tcp", "udp"):
            return

        key = (packet.proto, local, remote)
        flow = self._flows.get(key)
        if flow is None:
            flow = _Flow(proto=packet.proto, local=local, remote=remote)
            self._flows[key] = flow
        if outgoing:
            flow.sent += packet.size
        else:
            flow.received += packet.size
        flow.last_seen = time.monotonic()

        self._events.append((packet.ts, remote[0], packet.size, packet.proto))
        while len(self._events) > MAX_EVENTS:
            self._events.popleft()

    def _evict_idle(self) -> None:
        """Drop the five-tuples that carry nothing any more."""
        now = time.monotonic()
        for key in list(self._flows):
            if now - self._flows[key].last_seen > IDLE_TIMEOUT:
                del self._flows[key]

    def _rows(self) -> list[SocketRow]:
        """Return the rows of the five-tuples that are alive.

        Returns:
            One row per five-tuple.  The key of a row is the five-tuple
            itself: the process of a socket is not known when its first
            packet arrives, and the key of the row must not change when
            it becomes known.
        """
        ports = self._lookup()
        rows: list[SocketRow] = []
        for flow in self._flows.values():
            pid, process = ports.get(flow.local[1], (None, None))
            if flow.pid is None and pid is not None:
                # The process of the socket was not known when the
                # flow appeared: it is taken now, and the key of the
                # row changes once.
                flow.pid = pid
                flow.process = process
            elif pid is not None and pid == flow.pid:
                flow.process = process
            rows.append(
                SocketRow(
                    proto=flow.proto,
                    local=flow.local,
                    remote=flow.remote,
                    state="ESTAB",
                    pid=flow.pid,
                    process=flow.process,
                    bytes_sent=flow.sent,
                    bytes_received=flow.received,
                    key=(
                        f"{flow.proto}|{flow.local[0]}:{flow.local[1]}|"
                        f"{flow.remote[0]}:{flow.remote[1]}"
                    ),
                )
            )
        return rows

    def _lookup(self) -> dict[int, tuple[int | None, str | None]]:
        """Return the processes of the machine, by port.

        Returns:
            The table of the processes.  It is empty when the table of
            the sockets cannot be read: the map keeps working.
        """
        if self._process_lookup is None:
            return {}
        try:
            return self._process_lookup() or {}
        except Exception as exc:  # noqa: BLE001 - the map must survive
            log.debug("cannot read the processes: %s", exc)
            return {}

    def drain_bursts(self) -> list[Burst]:
        """Return the traffic of the packets since the last call.

        A burst is what a peer sent or received since the last drain.
        The traffic of the whole time between two polls is kept: a
        packet that was sent at the beginning of a second must not be
        lost because the window of the bursts is shorter than the
        time between two polls.

        The age of a burst says how long ago its newest packet was
        sent: the map draws the burst at the moment it happened, and
        not at the moment it was read.

        Returns:
            The bursts of the peers, the busiest first.
        """
        if not self._events:
            return []
        now = time.time()
        totals: dict[tuple[str, str], list[int]] = {}
        newest: dict[tuple[str, str], float] = {}
        for ts, remote, size, proto in self._events:
            counter = totals.setdefault((remote, proto), [0, 0])
            counter[0] += size
            counter[1] += 1
            if ts > newest.get((remote, proto), 0.0):
                newest[(remote, proto)] = ts
        self._events.clear()

        bursts = [
            Burst(
                remote=remote,
                bytes=counter[0],
                packets=counter[1],
                proto=proto,
                age=max(0.0, now - newest[(remote, proto)]),
            )
            for (remote, proto), counter in totals.items()
            if counter[0] > 0
        ]
        bursts.sort(key=lambda burst: (-burst.bytes, burst.remote))
        return bursts

    def close(self) -> None:
        """Stop the capture and wait for its threads."""
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:  # pragma: no cover - rare
                process.kill()
        for thread in (self._reader, self._error_reader):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
        self._process = None
        self._reader = None
        self._error_reader = None

    def __enter__(self) -> "PcapCollector":
        """Return the collector itself."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Stop the capture."""
        self.close()


def _is_permission_error(text: str) -> bool:
    """Tell whether an error of the capture is one of the privileges.

    Args:
        text: The words of the error.

    Returns:
        ``True`` when the error says that the capture was not allowed.
    """
    lowered = text.lower()
    return any(word.lower() in lowered for word in PERMISSION_WORDS)
