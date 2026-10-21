import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from trafficmap.config import ASSETS, Config, load_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Keep tests independent of the developer's shell environment."""
    for name in list(os.environ):
        if name.startswith("TMAP_"):
            monkeypatch.delenv(name)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)


def test_defaults():
    cfg = load_config([], path=Path("/nonexistent/nope.toml"))
    assert cfg.fps == 12 and cfg.poll_ms == 1000


def test_default_values_for_paths():
    cfg = load_config([], path=Path("/nonexistent/nope.toml"))
    assert cfg == Config()
    assert cfg.city_db == ASSETS / "dbip-city-lite.mmdb"
    assert cfg.asn_db == ASSETS / "dbip-asn-lite.mmdb"
    assert cfg.land_geojson == ASSETS / "land.geojson"
    assert cfg.cache_dir == Path.home() / ".cache" / "trafficmap"
    assert cfg.anchor is None
    assert cfg.frames == 0


def test_cli_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TMAP_FPS", "20")
    cfg = load_config(
        ["--fps", "30", "--city-db", str(tmp_path / "c.mmdb")],
        path=tmp_path / "none.toml",
    )
    assert cfg.fps == 30
    assert cfg.city_db == tmp_path / "c.mmdb"


def test_env_used_when_cli_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("TMAP_POLL_MS", "500")
    cfg = load_config([], path=tmp_path / "none.toml")
    assert cfg.poll_ms == 500


def test_toml_used_and_cli_wins(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('fps = 7\nfilter_process = "curl"\n')
    cfg = load_config(["--fps", "9"], path=p)
    assert cfg.fps == 9 and cfg.filter_process == "curl"


def test_toml_beats_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("fps = 7\npoll_ms = 250\nascii = true\n")
    cfg = load_config([], path=p)
    assert cfg.fps == 7
    assert cfg.poll_ms == 250
    assert cfg.ascii is True


def test_env_beats_toml(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text("fps = 7\n")
    monkeypatch.setenv("TMAP_FPS", "8")
    assert load_config([], path=p).fps == 8


def test_broken_toml_raises_with_path(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("fps = [\n")
    with pytest.raises(ValueError, match="config.toml"):
        load_config([], path=p)


def test_unknown_toml_key_raises(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("fps = 12\nnope = 1\n")
    with pytest.raises(ValueError, match="nope"):
        load_config([], path=p)


def test_bad_anchor_raises(tmp_path):
    with pytest.raises(ValueError):
        load_config(["--anchor", "1,2,3"], path=tmp_path / "none.toml")


def test_anchor_out_of_range_raises(tmp_path):
    with pytest.raises(ValueError):
        load_config(["--anchor", "91,0"], path=tmp_path / "none.toml")


def test_anchor_parsed(tmp_path):
    cfg = load_config(["--anchor", "55.75,37.61"], path=tmp_path / "none.toml")
    assert cfg.anchor == (55.75, 37.61)


def test_empty_env_value_means_unset(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text("fps = 7\n")
    monkeypatch.setenv("TMAP_FPS", "")
    cfg = load_config([], path=p)
    assert cfg.fps == 7


def test_unknown_env_vars_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("TMAP_NOT_A_FIELD", "1")
    cfg = load_config([], path=tmp_path / "none.toml")
    assert cfg.fps == 12


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "Yes"])
def test_env_bool_true(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("TMAP_ASCII", raw)
    assert load_config([], path=tmp_path / "none.toml").ascii is True


@pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", "No"])
def test_env_bool_false(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("TMAP_ASCII", raw)
    assert load_config([], path=tmp_path / "none.toml").ascii is False


def test_bad_env_bool_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("TMAP_ASCII", "maybe")
    with pytest.raises(ValueError, match="ascii"):
        load_config([], path=tmp_path / "none.toml")


def test_cli_no_ascii_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TMAP_ASCII", "1")
    cfg = load_config(["--no-ascii"], path=tmp_path / "none.toml")
    assert cfg.ascii is False


def test_cli_resolve_anchor_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TMAP_NO_ANCHOR", "1")
    cfg = load_config(["--resolve-anchor"], path=tmp_path / "none.toml")
    assert cfg.no_anchor is False


@pytest.mark.parametrize("flag", ["--fps", "--poll-ms"])
def test_non_positive_values_raise(tmp_path, flag):
    with pytest.raises(ValueError):
        load_config([flag, "0"], path=tmp_path / "none.toml")


def test_negative_frames_raises(tmp_path):
    with pytest.raises(ValueError):
        load_config(["--frames", "-1"], path=tmp_path / "none.toml")


def test_optional_paths_empty_to_none(tmp_path):
    cfg = load_config(
        ["--asn-db", "", "--anchor", "", "--mock", "", "--snapshot", "", "--dump", ""],
        path=tmp_path / "none.toml",
    )
    assert cfg.asn_db is None
    assert cfg.anchor is None
    assert cfg.mock is None
    assert cfg.snapshot is None
    assert cfg.dump is None


def test_filesystem_fields_are_paths(tmp_path):
    cfg = load_config(
        ["--city-db", "c.mmdb", "--land-geojson", "l.geojson", "--cache-dir", "cache"],
        path=tmp_path / "none.toml",
    )
    assert isinstance(cfg.city_db, Path)
    assert isinstance(cfg.asn_db, Path)
    assert isinstance(cfg.land_geojson, Path)
    assert isinstance(cfg.cache_dir, Path)


def test_default_path_uses_xdg_config_home(tmp_path, monkeypatch):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "trafficmap").mkdir()
    (config_dir / "trafficmap" / "config.toml").write_text("fps = 5\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    assert load_config([]).fps == 5


def test_cache_dir_uses_xdg_cache_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    cfg = load_config([], path=tmp_path / "none.toml")
    assert cfg.cache_dir == tmp_path / "trafficmap"


def test_config_is_frozen():
    cfg = load_config([], path=Path("/nonexistent/nope.toml"))
    with pytest.raises(FrozenInstanceError):
        cfg.fps = 99  # type: ignore[misc]
