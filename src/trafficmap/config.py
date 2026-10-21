"""Configuration loading for tmap.

Precedence, highest first: command line, ``TMAP_*`` environment variables,
``config.toml``, built-in defaults. Missing config file is not an error.

Error contract: argparse usage errors (unknown options such as ``--bogus``,
missing option values) raise ``SystemExit(2)``; ``--help`` raises
``SystemExit(0)``. Invalid values raise ``ValueError``. ``__main__`` maps both
to exit code 2 later (Task 9).
"""

import argparse
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[2] / "assets"


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "trafficmap"


@dataclass(frozen=True, slots=True)
class Config:
    fps: int = 12
    poll_ms: int = 1000
    city_db: Path = ASSETS / "dbip-city-lite.mmdb"
    asn_db: Path | None = ASSETS / "dbip-asn-lite.mmdb"
    land_geojson: Path = ASSETS / "land.geojson"
    cache_dir: Path = field(default_factory=default_cache_dir)
    ascii: bool = False
    no_anchor: bool = False
    anchor: tuple[float, float] | None = None
    filter_process: str = ""
    mock: Path | None = None
    snapshot: Path | None = None
    frames: int = 0
    dump: Path | None = None


_KNOWN_KEYS = frozenset(field.name for field in fields(Config))
_INT_FIELDS = frozenset({"fps", "poll_ms", "frames"})
_INT_MINIMUMS = {"fps": 1, "poll_ms": 1, "frames": 0}
_BOOL_FIELDS = frozenset({"ascii", "no_anchor"})
_PATH_FIELDS = frozenset({"city_db", "land_geojson", "cache_dir"})
_OPTIONAL_PATH_FIELDS = frozenset({"asn_db", "mock", "snapshot", "dump"})

_TRUE_VALUES = frozenset({"1", "true", "yes"})
_FALSE_VALUES = frozenset({"0", "false", "no"})


def load_config(argv: list[str] | None = None, path: Path | None = None) -> Config:
    """Build a Config from defaults, TOML, environment, and CLI arguments."""
    config_path = (
        Path(path).expanduser() if path is not None else _default_config_path()
    )

    values: dict[str, object] = dict(_read_toml(config_path))
    values.update(_env_values())
    values.update(vars(_build_parser().parse_args(argv)))

    return Config(**{name: _convert(name, value) for name, value in values.items()})


def _default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "trafficmap" / "config.toml"


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"cannot read config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {path}: {exc}") from exc

    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        listed = ", ".join(repr(key) for key in unknown)
        raise ValueError(f"unknown key(s) in {path}: {listed}")
    return data


def _env_values() -> dict[str, object]:
    values: dict[str, object] = {}
    for spec in fields(Config):
        name = spec.name
        raw = os.environ.get(f"TMAP_{name.upper()}", "")
        if raw:
            values[name] = raw
    return values


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tmap")
    add = parser.add_argument
    add("--fps", metavar="N", default=argparse.SUPPRESS, help="UI frames per second")
    add("--poll-ms", metavar="MS", default=argparse.SUPPRESS, help="collector interval")
    add("--city-db", metavar="PATH", default=argparse.SUPPRESS, help="city MMDB")
    add("--asn-db", metavar="PATH", default=argparse.SUPPRESS, help="ASN MMDB")
    add(
        "--land-geojson", metavar="PATH", default=argparse.SUPPRESS, help="land GeoJSON"
    )
    add(
        "--cache-dir", metavar="PATH", default=argparse.SUPPRESS, help="cache directory"
    )
    add(
        "--ascii",
        action=argparse.BooleanOptionalAction,
        default=argparse.SUPPRESS,
        help="ASCII mode",
    )
    add(
        "--no-anchor",
        action="store_true",
        dest="no_anchor",
        default=argparse.SUPPRESS,
        help="do not resolve the local anchor",
    )
    add(
        "--resolve-anchor",
        action="store_false",
        dest="no_anchor",
        default=argparse.SUPPRESS,
        help="force anchor resolution",
    )
    add(
        "--anchor", metavar="LAT,LON", default=argparse.SUPPRESS, help="anchor position"
    )
    add(
        "--filter-process",
        metavar="NAME",
        default=argparse.SUPPRESS,
        help="only show this process",
    )
    add(
        "--mock", metavar="PATH", default=argparse.SUPPRESS, help="mock collector JSONL"
    )
    add("--snapshot", metavar="PATH", default=argparse.SUPPRESS, help="snapshot output")
    add("--frames", metavar="N", default=argparse.SUPPRESS, help="snapshot frame count")
    add("--dump", metavar="PATH", default=argparse.SUPPRESS, help="headless JSONL dump")
    return parser


def _convert(name: str, value: object) -> object:
    if name in _INT_FIELDS:
        return _as_int(name, value)
    if name in _BOOL_FIELDS:
        return _as_bool(name, value)
    if name in _PATH_FIELDS:
        return _as_path(name, value, optional=False)
    if name in _OPTIONAL_PATH_FIELDS:
        return _as_path(name, value, optional=True)
    if name == "anchor":
        return _as_anchor(value)
    if name == "filter_process":
        return _as_str(name, value)
    raise AssertionError(f"unhandled config field: {name}")


def _as_int(name: str, value: object) -> int:
    number: int | None = None
    if not isinstance(value, bool) and isinstance(value, (int, str)):
        try:
            number = int(value)
        except ValueError:
            pass
    if number is None:
        raise ValueError(f"{name} must be an integer, got {value!r}")

    minimum = _INT_MINIMUMS[name]
    if number < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {number}")
    return number


def _as_bool(name: str, value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_VALUES:
            return True
        if token in _FALSE_VALUES:
            return False
    raise ValueError(f"{name} must be one of 0, 1, true, false, yes, no; got {value!r}")


def _as_str(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string, got {value!r}")
    return value


def _as_path(name: str, value: object, *, optional: bool) -> Path | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"{name} must be a path, got None")
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{name} must be a path, got {value!r}")
    if isinstance(value, str) and not value.strip():
        if optional and not value:
            return None
        raise ValueError(f"{name} must not be empty")
    try:
        return Path(value).expanduser()
    except (RuntimeError, KeyError) as exc:
        raise ValueError(f"{name}: cannot expand path {value!r}: {exc}") from exc


def _as_anchor(value: object) -> tuple[float, float] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        parts: list[object] = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        raise ValueError(f"anchor must be 'LAT,LON', got {value!r}")

    if len(parts) != 2 or any(isinstance(part, bool) for part in parts):
        raise ValueError(f"anchor must be 'LAT,LON', got {value!r}")
    try:
        lat, lon = (float(part) for part in parts)
    except (TypeError, ValueError):
        raise ValueError(f"anchor must be 'LAT,LON', got {value!r}") from None

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError(f"anchor out of range: {lat},{lon}")
    return (lat, lon)
