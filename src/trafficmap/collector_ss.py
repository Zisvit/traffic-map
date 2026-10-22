"""Reading the sockets of the machine from ``ss``.

``ss`` is the only source of the traffic of the map: it knows the byte
counters of every socket and the process that owns it, and it does not
care which interface the traffic goes through.

The output of ``ss -t -u -a -i -n -p`` is a table::

    Netid State      Recv-Q Send-Q   Local Address:Port  Peer Address:Port
    tcp   ESTAB      0      0        10.0.0.2:51000       1.1.1.1:443
                                       users:(("curl",pid=100,fd=3))
    \t bbr wscale:7,7 rto:204 ... bytes_sent:1200 bytes_received:9000 ...

The header of the table is recognised by its first word and is never
read as a socket.  The lines that follow a socket line are indented
and carry the counters of the socket above them; a UDP socket usually
has none.

Only the sockets that can still carry traffic are of interest.  A
listening socket has no peer yet, a socket whose peer is a wildcard
talks to nobody, and a ``TIME-WAIT`` socket is already gone: its
counters are frozen.  Those rows are dropped.

A line that cannot be read is dropped as well and never raises: a
broken line must not stop the map.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from dataclasses import dataclass

__all__ = ["SocketRow", "SsCollector", "parse_sockets"]

log = logging.getLogger(__name__)

#: The command that asks ``ss`` for every socket with its counters.
DEFAULT_COMMAND: tuple[str, ...] = ("ss", "-t", "-u", "-a", "-i", "-n", "-p")

#: Seconds the command may take before it is given up on.
DEFAULT_TIMEOUT = 5.0

#: Addresses that stand for every address or for no address at all.
WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "*"})

#: States of a socket that carry no live flow.
SKIPPED_STATES = frozenset({"LISTEN", "TIME-WAIT"})

#: The first words of the header of the table.
HEADER_WORDS = frozenset({"netid", "state"})

#: A socket line: the protocol, the state, the two queues, the two
#: addresses and, maybe, the process that owns the socket.
_SOCKET_RE = re.compile(
    r"^(?P<proto>tcp|udp)\s+"
    r"(?P<state>\S+)\s+"
    r"\S+\s+\S+\s+"  # the receive queue and the send queue
    r"(?P<local>\S+)\s+"
    r"(?P<peer>\S+)"
    r"(?P<rest>.*)$"
)

#: The process column: ``users:(("curl",pid=100,fd=3))``.  Only the
#: first entry of the list is of interest.
_PROCESS_RE = re.compile(
    r"users:\(\((?:\"(?P<quoted>[^\"]*)\"|(?P<bare>[^,]*)),\s*pid=(?P<pid>\d+)"
)

#: The counters of the indented lines.  A value that carries a suffix
#: (``15M``) is not a plain number and is therefore left unknown.
_COUNTER_RE = re.compile(r"\bbytes_(sent|received):(\d+)\b")


@dataclass(frozen=True, slots=True)
class SocketRow:
    """One socket of the machine.

    Attributes:
        proto: The protocol of the socket, ``"tcp"`` or ``"udp"``.
        local: The address and the port of this side of the socket.
        remote: The address and the port of the other side.
        state: The state of the socket, ``"ESTAB"`` for example.
        pid: The number of the process that owns the socket, if it is
            known.
        process: The name of that process, if it is known.
        bytes_sent: The bytes the socket has sent, if they are known.
        bytes_received: The bytes the socket has received, if they are
            known.
        key: The key of the socket, unique among the sockets of one
            poll.
    """

    proto: str
    local: tuple[str, int]
    remote: tuple[str, int]
    state: str
    pid: int | None
    process: str | None
    bytes_sent: int | None
    bytes_received: int | None
    key: str


@dataclass(slots=True)
class _Fields:
    """The parts of a socket line that do not come from the counters."""

    proto: str
    state: str
    local: tuple[str, int]
    remote: tuple[str, int]
    process: str | None
    pid: int | None


def parse_sockets(text: str) -> list[SocketRow]:
    """Read the sockets of an ``ss`` output.

    Args:
        text: The output of ``ss -t -u -a -i -n -p``.

    Returns:
        The sockets that carry a flow.  The header of the table, the
        listening sockets, the sockets without a peer and the closed
        ones are left out.  A line that cannot be read is skipped
        instead of raising.

    Note:
        The header is found by its first word, so an output without a
        header (``ss -H``) and a warning line before the table keep
        their first socket.
    """
    rows: list[SocketRow] = []
    fields: _Fields | None = None
    counters: list[str] = []

    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw[0].isspace():
            # An indented line carries the counters of the socket above.
            if fields is not None:
                counters.append(raw)
            continue

        if fields is not None:
            rows.append(_build(fields, counters))
            fields = None
            counters = []

        if raw.split(None, 1)[0].lower() in HEADER_WORDS:
            # The header of the table.
            continue

        fields = _read_line(raw)

    if fields is not None:
        rows.append(_build(fields, counters))
    return rows


class SsCollector:
    """Ask ``ss`` for the sockets of the machine and read the answer.

    Args:
        command: The command to run.  It is
            :data:`DEFAULT_COMMAND` when it is missing.
        timeout: Seconds the command may take.

    Raises:
        ValueError: If the command is empty.
    """

    #: ``ss`` reads the sockets of the machine, and only them: without
    #: root they are the sockets of the user.
    sockets_only = True

    def __init__(
        self,
        command: list[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if command is None:
            self._command = list(DEFAULT_COMMAND)
        else:
            self._command = list(command)
        if not self._command:
            raise ValueError("the command must not be empty")
        self._timeout = timeout

    @property
    def command(self) -> list[str]:
        """The command the collector runs."""
        return list(self._command)

    def poll(self) -> list[SocketRow]:
        """Read the sockets of the machine once.

        Returns:
            The sockets that carry a flow.

        Raises:
            RuntimeError: If the command failed, answered with an error
                code, or did not finish in time.  The message holds the
                command and what it answered, so the program can show
                it and try again later.
        """
        try:
            result = subprocess.run(
                self._command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"{self._describe()} timed out after {self._timeout} s"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"{self._describe()} failed: {exc}") from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"{self._describe()} failed with code {result.returncode}: "
                f"{result.stderr.strip()}"
            )

        return parse_sockets(result.stdout)

    def _describe(self) -> str:
        """Return the command in a form a person can read."""
        return shlex.join(self._command)


def _read_line(line: str) -> _Fields | None:
    """Read one socket line, or return ``None`` when it is not one.

    Args:
        line: The line to read.

    Returns:
        The parts of the line, or ``None`` when the line is not a
        socket or when it carries no flow.
    """
    match = _SOCKET_RE.match(line)
    if match is None:
        log.debug("not a socket line: %r", line)
        return None

    proto = match["proto"].lower()
    state = match["state"].upper()
    local = _split_addr(match["local"])
    remote = _split_addr(match["peer"])
    if local is None or remote is None:
        log.debug("cannot read the addresses of %r", line)
        return None

    if state in SKIPPED_STATES:
        return None
    if remote[0] in WILDCARD_HOSTS:
        return None

    process, pid = _read_process(match["rest"])
    return _Fields(
        proto=proto,
        state=state,
        local=local,
        remote=remote,
        process=process,
        pid=pid,
    )


def _build(fields: _Fields, counters: list[str]) -> SocketRow:
    """Build the row of a socket from its line and its counters.

    Args:
        fields: The parts of the socket line.
        counters: The indented lines that followed it.

    Returns:
        The row of the socket.
    """
    sent, received = _read_counters(counters)
    key = (
        f"{fields.proto}|{fields.local[0]}:{fields.local[1]}|"
        f"{fields.remote[0]}:{fields.remote[1]}|{fields.pid}"
    )
    return SocketRow(
        proto=fields.proto,
        local=fields.local,
        remote=fields.remote,
        state=fields.state,
        pid=fields.pid,
        process=fields.process,
        bytes_sent=sent,
        bytes_received=received,
        key=key,
    )


def _split_addr(text: str) -> tuple[str, int] | None:
    """Split an address of a socket into its host and its port.

    The host may be an IPv4 address, an IPv6 address in brackets or a
    name with a zone on it; the zone is dropped.  A port of ``*`` means
    that there is no port.

    Args:
        text: The address, ``"1.1.1.1:443"`` for example.

    Returns:
        The host and the port, or ``None`` when the text is not an
        address.
    """
    text = text.strip()
    if not text:
        return None

    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return None
        host = text[1:end]
        tail = text[end + 1 :]
        if not tail.startswith(":"):
            return None
        port_text = tail[1:]
    else:
        index = text.rfind(":")
        if index < 0:
            return None
        host = text[:index]
        port_text = text[index + 1 :]

    zone = host.find("%")
    if zone >= 0:
        host = host[:zone]
    if not host:
        return None

    if port_text == "*":
        return host, 0
    try:
        port = int(port_text)
    except ValueError:
        return None
    if not 0 <= port <= 65535:
        return None
    return host, port


def _read_process(text: str) -> tuple[str | None, int | None]:
    """Read the process column of a socket line.

    The column holds one entry per process that shares the socket; the
    first one is the owner of the socket.

    Args:
        text: The process column.

    Returns:
        The name of the process and its number, or ``None`` for both
        when the column is empty or holds nothing readable.
    """
    match = _PROCESS_RE.search(text)
    if match is None:
        return None, None
    name = match["quoted"] if match["quoted"] is not None else match["bare"]
    return (name or None), int(match["pid"])


def _read_counters(lines: list[str]) -> tuple[int | None, int | None]:
    """Read the byte counters of the indented lines of a socket.

    Args:
        lines: The indented lines that followed the socket line.

    Returns:
        The bytes sent and the bytes received, or ``None`` for a
        counter that is not there.  A UDP socket usually has none.
    """
    sent: int | None = None
    received: int | None = None
    for line in lines:
        for name, value in _COUNTER_RE.findall(line):
            if name == "sent":
                sent = int(value)
            else:
                received = int(value)
    return sent, received
