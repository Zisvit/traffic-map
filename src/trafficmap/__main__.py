"""The command line of ``tmap``: the traffic of the machine on a map.

``tmap`` reads the sockets of the machine, turns them into flows and
shows them on a map.  Without a terminal it writes the flows to a file
instead, one JSON object per line: see :mod:`trafficmap.dump`.  With
``--snapshot`` it writes the last frame of the map as plain text, which
is what the tests and the demonstrations without a terminal need.

The option ``--verbose`` sends the log of the program to the error
stream: the failures of the collector are then diagnosable.  The log
never goes to the standard output, so it cannot corrupt the screen of
the map.
"""

import argparse
import logging
import sys

from trafficmap import __version__
from trafficmap.config import Config, load_config
from trafficmap.dump import run_dump
from trafficmap.geo import GeoResolver
from trafficmap.ui import make_collector, run, snapshot

#: The options of the program itself.  They are not settings of the
#: configuration, so they are added to its help by hand.
_PROGRAM_OPTIONS = """
options of the program itself:
  --version             show the version of the program and stop
  --verbose             send the log of the program to the error stream
"""


def _version_parser() -> argparse.ArgumentParser:
    """Return the parser that answers ``--version``.

    The parser of the configuration does not know the option, so it is
    answered before the configuration is read.

    Returns:
        The parser of the version.
    """
    parser = argparse.ArgumentParser(prog="tmap", add_help=False)
    parser.add_argument("--version", action="version", version=f"tmap {__version__}")
    return parser


def _take_verbose(argv: list[str]) -> tuple[bool, list[str]]:
    """Take the option ``--verbose`` out of the arguments.

    The option is not one of the settings of the program: it is a
    switch of the logging, so the configuration never sees it.

    Args:
        argv: The arguments of the command line.

    Returns:
        Whether the program must be verbose, and the arguments without
        the option.
    """
    verbose = False
    rest: list[str] = []
    for argument in argv:
        if argument == "--verbose":
            verbose = True
        else:
            rest.append(argument)
    return verbose, rest


def _enable_verbose() -> None:
    """Send the log of the program to the error stream.

    The map is drawn on the terminal, so the log must not go to the
    standard output: it would break the picture.  The error stream is
    the only place the messages are written to.
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Run the program.

    Args:
        argv: The arguments of the command line.  The arguments of the
            process are used when it is missing.

    Returns:
        ``0`` when the program ran as it should, ``2`` when it could
        not run.
    """
    if argv is None:
        argv = sys.argv[1:]
    verbose, arguments = _take_verbose(list(argv))
    if verbose:
        _enable_verbose()
    try:
        _version_parser().parse_known_args(arguments)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 0
    try:
        cfg = load_config(arguments)
    except SystemExit as exc:
        # The help was asked for: the options of the program itself are
        # added to the help of the configuration.
        code = exc.code
        code = code if isinstance(code, int) else 0
        if code == 0:
            print(_PROGRAM_OPTIONS)
        raise SystemExit(code)
    except ValueError as exc:
        print(f"tmap: {exc}", file=sys.stderr)
        return 2
    if cfg.dump is not None:
        return _run_dump(cfg)
    if cfg.snapshot is not None:
        return snapshot(cfg, cfg.snapshot, cfg.frames)
    return run(cfg)


def _run_dump(cfg: Config) -> int:
    """Write the flows of the machine to a file, without a terminal.

    Args:
        cfg: The configuration of the program.

    Returns:
        ``0`` when the dump was stopped, ``2`` when the databases are
        missing.
    """
    try:
        resolver = GeoResolver(cfg.city_db, cfg.asn_db, cfg.cache_dir)
    except FileNotFoundError as exc:
        print(f"tmap: {exc}", file=sys.stderr)
        print("tmap: run 'make assets' to download the databases", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"tmap: {exc}", file=sys.stderr)
        return 2
    try:
        # The mock collector is used when a recording is named, so
        # that the dump can be made without the sockets of the
        # machine.
        collector = make_collector(cfg)
    except OSError as exc:
        print(f"tmap: cannot read the recording: {exc}", file=sys.stderr)
        return 2
    _place_the_machine(resolver, cfg)
    try:
        return run_dump(cfg, resolver, collector)
    except OSError as exc:
        print(f"tmap: cannot write the dump: {exc}", file=sys.stderr)
        return 2


def _place_the_machine(resolver: GeoResolver, cfg: Config) -> None:
    """Tell the resolver where this machine is.

    The place of the machine is what chooses the point of presence of
    a network that answers from many cities: without it the traffic of
    the machine is placed in the city of the office of the network,
    which may be on another continent.  The dump shows the same places
    as the map, so it looks for the anchor as well.

    Args:
        resolver: The resolver that knows the places.
        cfg: The configuration of the program.
    """
    if cfg.anchor is not None:
        lat, lon = cfg.anchor
        resolver.set_home(lat, lon)
        return
    if cfg.no_anchor:
        return
    geo = resolver.anchor()
    if geo is not None and geo.lat is not None and geo.lon is not None:
        resolver.set_home(geo.lat, geo.lon)


if __name__ == "__main__":
    raise SystemExit(main())
