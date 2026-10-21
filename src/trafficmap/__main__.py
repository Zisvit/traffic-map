import sys

from trafficmap import __version__
from trafficmap.collector_ss import SsCollector
from trafficmap.config import Config, load_config
from trafficmap.dump import run_dump
from trafficmap.geo import GeoResolver


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if "--version" in argv:
        print(f"tmap {__version__}")
        return 0
    try:
        cfg = load_config(argv)
    except ValueError as exc:
        print(f"tmap: {exc}", file=sys.stderr)
        return 2
    if cfg.dump is not None:
        return _run_dump(cfg)
    print("not implemented", file=sys.stderr)
    return 2


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
        return run_dump(cfg, resolver, SsCollector())
    except OSError as exc:
        print(f"tmap: cannot write the dump: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
