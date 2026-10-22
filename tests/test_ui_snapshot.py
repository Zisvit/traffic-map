"""Tests of the interface and of the snapshot.

Nothing here starts a terminal: the snapshot runs the whole pipeline
without curses, and the interface is asked for its answer when there is
no terminal at all.
"""

import logging
import sys
from pathlib import Path

import pytest

from trafficmap.__main__ import main
from trafficmap.anim import Animator
from trafficmap.collector_mock import MockCollector
from trafficmap.config import Config
from trafficmap.flows import FlowEvent, Opened
from trafficmap.ui import (
    MIN_COLS,
    MIN_ROWS,
    SNAPSHOT_COLS,
    SNAPSHOT_ROWS,
    STATUS_ROWS,
    run,
)

#: The recording the tests replay.
FIXTURE = Path(__file__).parent / "fixtures" / "mock.jsonl"

#: The number of lines of a snapshot: the map and the status bar.
EXPECTED_LINES = SNAPSHOT_ROWS
EXPECTED_MAP_LINES = SNAPSHOT_ROWS - STATUS_ROWS

#: The first and the last character of the Braille block.
BRAILLE_FIRST = "⠀"
BRAILLE_LAST = "⣿"


def snapshot_path(tmp_path: Path, name: str = "frame.txt") -> Path:
    """Return the path of a snapshot of the recording."""
    return tmp_path / name


def make_snapshot(
    tmp_path: Path, *extra: str, frames: str = "10", name: str = "frame.txt"
) -> str:
    """Make a snapshot of the recording and return its text.

    Args:
        tmp_path: The directory the snapshot is written to.
        extra: Other arguments of the command line.
        frames: The number of frames to render.
        name: The name of the file the snapshot is written to.

    Returns:
        The text of the snapshot.
    """
    out = snapshot_path(tmp_path, name)
    code = main(
        [
            "--mock",
            str(FIXTURE),
            "--snapshot",
            str(out),
            "--frames",
            frames,
            "--no-anchor",
            *extra,
        ]
    )
    assert code == 0
    return out.read_text(encoding="utf-8")


def test_snapshot_contains_map_and_stats(tmp_path):
    out = snapshot_path(tmp_path)
    rc = main(
        [
            "--mock",
            str(FIXTURE),
            "--snapshot",
            str(out),
            "--frames",
            "10",
            "--no-anchor",
        ]
    )
    assert rc == 0
    text = out.read_text()
    assert any("⠀" <= ch <= "⣿" for ch in text)
    assert "flows" in text and "MB/s" in text


def test_snapshot_has_the_map_and_the_status_bar(tmp_path):
    text = make_snapshot(tmp_path)
    lines = text.splitlines()
    assert len(lines) == EXPECTED_LINES
    assert EXPECTED_LINES == EXPECTED_MAP_LINES + STATUS_ROWS
    # The three last lines are the status bar.
    assert "flows" in lines[-3]
    assert "firefox:443" in lines[-2]
    assert "q quit" in lines[-1] or "no root" in lines[-1]


def test_snapshot_shows_the_land(tmp_path):
    text = make_snapshot(tmp_path)
    map_lines = text.splitlines()[:EXPECTED_MAP_LINES]
    filled = sum(1 for line in map_lines for char in line if char != "⠀")
    assert filled > 100


def test_snapshot_counts_the_local_peer(tmp_path):
    """The private address is counted, but it is not on the map."""
    text = make_snapshot(tmp_path)
    assert "local 1" in text
    assert "10.0.0.5" not in text


def test_snapshot_counts_the_unknown_address(tmp_path):
    """The address the database does not know is counted."""
    text = make_snapshot(tmp_path)
    assert "unknown 1" in text


def test_snapshot_shows_the_rates(tmp_path):
    text = make_snapshot(tmp_path)
    assert "↑ 1.2 MB/s" in text
    assert "↓ 9.1 MB/s" in text


def test_the_snapshot_shows_the_rate_of_every_flow(tmp_path):
    """The busiest flows show their rate, not the bytes they carried."""
    text = make_snapshot(tmp_path)
    line = text.splitlines()[-2]
    assert "firefox:443 ↓9.0 MB/s" in line
    # The rate is not an amount of bytes.
    assert "9.0M" not in line


def test_the_verbose_option_sends_the_log_to_the_error_stream(monkeypatch, tmp_path):
    """The verbose option turns the log on, and the log goes to stderr."""
    calls = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: calls.append(kwargs))
    code = main(
        [
            "--verbose",
            "--mock",
            str(FIXTURE),
            "--snapshot",
            str(snapshot_path(tmp_path)),
            "--frames",
            "5",
            "--no-anchor",
        ]
    )
    assert code == 0
    assert calls
    assert calls[0]["level"] == logging.INFO
    # The map is on the terminal: the log must not go to the standard
    # output.
    assert calls[0]["stream"] is sys.stderr


def test_snapshot_without_frames_still_writes_a_frame(tmp_path):
    out = snapshot_path(tmp_path)
    rc = main(
        [
            "--mock",
            str(FIXTURE),
            "--snapshot",
            str(out),
            "--frames",
            "0",
            "--no-anchor",
        ]
    )
    assert rc == 0
    assert out.exists()


def test_the_ascii_snapshot_has_no_braille_character(tmp_path):
    """The ASCII mode draws the map with plain ASCII characters."""
    text = make_snapshot(tmp_path, "--ascii")
    assert "#" in text, "the land is not on the map"
    assert "*" in text, "the peers are not on the map"
    braille = [char for char in text if BRAILLE_FIRST <= char <= BRAILLE_LAST]
    assert braille == [], f"the ASCII frame holds Braille characters: {braille!r}"


def test_the_ascii_snapshot_has_the_same_shape(tmp_path):
    """The ASCII map is as big as the Braille one."""
    braille = make_snapshot(tmp_path, name="braille.txt")
    ascii_ = make_snapshot(tmp_path, "--ascii", name="ascii.txt")
    assert len(braille.splitlines()) == len(ascii_.splitlines())
    assert len(ascii_.splitlines()) == EXPECTED_LINES
    assert any(BRAILLE_FIRST <= char <= BRAILLE_LAST for char in braille)
    assert not any(BRAILLE_FIRST <= char <= BRAILLE_LAST for char in ascii_)


def test_the_ascii_snapshot_shows_the_same_status_bar(tmp_path):
    """The status bar does not depend on the way the map is drawn."""
    braille = make_snapshot(tmp_path, name="braille.txt")
    ascii_ = make_snapshot(tmp_path, "--ascii", name="ascii.txt")
    assert braille.splitlines()[-3:] == ascii_.splitlines()[-3:]


def test_the_recording_does_not_warn_about_the_privileges(tmp_path):
    """A recording is not the sockets of the user: no banner belongs to it."""
    text = make_snapshot(tmp_path)
    assert "no root" not in text


def test_the_snapshot_handles_every_event_once(monkeypatch, tmp_path):
    """A frame does not repeat the events of the poll before it."""
    handled: list[FlowEvent] = []
    original = Animator.handle_event

    def counting(self, event, now):
        """Remember the event and let the animator have it."""
        handled.append(event)
        original(self, event, now)

    monkeypatch.setattr(Animator, "handle_event", counting)
    out = snapshot_path(tmp_path)
    rc = main(
        [
            "--mock",
            str(FIXTURE),
            "--snapshot",
            str(out),
            "--frames",
            "30",
            "--no-anchor",
        ]
    )
    assert rc == 0
    opened = [event.flow.key for event in handled if isinstance(event, Opened)]
    assert opened
    # Every flow of the recording is opened once, and the frames that
    # follow the poll do not open it again.
    assert len(opened) == len(set(opened))


def test_snapshot_to_a_missing_directory_fails(tmp_path, capsys):
    out = tmp_path / "missing" / "frame.txt"
    rc = main(
        [
            "--mock",
            str(FIXTURE),
            "--snapshot",
            str(out),
            "--frames",
            "5",
            "--no-anchor",
        ]
    )
    assert rc == 2
    assert "cannot write the snapshot" in capsys.readouterr().err


def test_the_interface_needs_a_terminal(capsys):
    assert run(Config()) == 2
    assert "TUI requires a terminal" in capsys.readouterr().err


def test_the_command_line_needs_a_terminal(capsys):
    assert main(["--mock", str(FIXTURE)]) == 2
    assert "TUI requires a terminal" in capsys.readouterr().err


def test_a_missing_recording_fails(tmp_path, capsys):
    rc = main(
        [
            "--mock",
            str(tmp_path / "nope.jsonl"),
            "--snapshot",
            str(snapshot_path(tmp_path)),
            "--frames",
            "5",
            "--no-anchor",
        ]
    )
    assert rc == 2
    assert "recording" in capsys.readouterr().err


def test_the_version_is_printed(capsys):
    from trafficmap import __version__

    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"tmap {__version__}"


def test_the_minimum_size_of_the_terminal():
    assert (MIN_COLS, MIN_ROWS) == (80, 24)
    assert SNAPSHOT_COLS >= MIN_COLS
    assert SNAPSHOT_ROWS >= MIN_ROWS


class TestMockCollector:
    """Tests of the collector that replays a recording."""

    def test_the_ticks_are_read(self):
        collector = MockCollector(FIXTURE)
        assert collector.ticks == 6
        assert collector.current_time == 0.0

    def test_the_poll_answers_with_the_sockets_of_the_tick(self):
        collector = MockCollector(FIXTURE)
        rows = collector.poll()
        assert len(rows) == 6
        assert {row.remote[0] for row in rows} == {
            "1.1.1.1",
            "142.250.185.78",
            "203.0.113.7",
            "9.9.9.9",
            "10.0.0.5",
            "8.8.8.8",
        }

    def test_the_counters_grow(self):
        collector = MockCollector(FIXTURE)
        first = {row.key: row for row in collector.poll()}
        second = {row.key: row for row in collector.poll()}
        for key, row in first.items():
            if row.bytes_sent is None:
                continue
            assert second[key].bytes_sent > row.bytes_sent

    def test_the_tick_moves_on(self):
        collector = MockCollector(FIXTURE)
        assert collector.current_time == 0.0
        collector.poll()
        assert collector.current_time == 1.0
        collector.poll()
        assert collector.current_time == 2.0

    def test_the_last_tick_repeats(self):
        collector = MockCollector(FIXTURE)
        for _ in range(10):
            collector.poll()
        assert collector.current_time == 5.0
        assert len(collector.poll()) == 6

    def test_a_socket_without_a_process_is_read(self):
        collector = MockCollector(FIXTURE)
        rows = [row for row in collector.poll() if row.remote[0] == "8.8.8.8"]
        assert rows[0].process is None
        assert rows[0].pid is None

    def test_a_socket_without_counters_is_read(self):
        collector = MockCollector(FIXTURE)
        rows = [row for row in collector.poll() if row.proto == "udp"]
        assert rows[0].bytes_sent is None
        assert rows[0].bytes_received is None

    def test_a_broken_line_is_skipped(self, tmp_path):
        path = tmp_path / "broken.jsonl"
        path.write_text(
            "not json\n"
            '{"t": 1.0, "sockets": []}\n'
            '{"t": "soon", "sockets": []}\n'
            '{"t": 2.0, "sockets": [{"proto": "tcp"}]}\n',
            encoding="utf-8",
        )
        collector = MockCollector(path)
        # The two lines that are a tick are read, the two broken ones
        # are left out.
        assert collector.ticks == 2
        assert collector.current_time == 1.0
        assert collector.poll() == []
        assert collector.poll() == []

    def test_a_broken_socket_is_skipped(self, tmp_path):
        path = tmp_path / "broken.jsonl"
        path.write_text(
            '{"t": 1.0, "sockets": [{"proto": "tcp"},'
            ' {"proto": "tcp", "local": ["10.0.0.2", 1],'
            ' "remote": ["1.1.1.1", 443], "state": "ESTAB"}]}\n',
            encoding="utf-8",
        )
        collector = MockCollector(path)
        rows = collector.poll()
        assert len(rows) == 1
        assert rows[0].remote == ("1.1.1.1", 443)

    def test_a_missing_recording_is_refused(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            MockCollector(tmp_path / "nope.jsonl")
