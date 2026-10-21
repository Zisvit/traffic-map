"""The command line of ``tmap``: the traffic of the machine on a map.

``tmap`` reads the sockets of the machine, turns them into flows and
shows them on a map.  Without a terminal it writes the flows to a file
instead, one JSON object per line: see :mod:`trafficmap.dump`.  With
``--snapshot`` it writes the last frame of the map as plain text, which
is what the tests and the demonstrations without a terminal need.
"""

import argparse
import sys

from trafficmap import __version__
from trafficmap.config import Config, load_config
from trafficmap.dump import run_dump
from trafficmap.geo import GeoResolver
from trafficmap.ui import make_collector, run, snapshot


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
    try:
        _version_parser().parse_known_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 0
    try:
        cfg = load_config(argv)
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
        return run_dump(cfg, resolver, make_collector(cfg))
    except OSError as exc:
        print(f"tmap: cannot write the dump: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
