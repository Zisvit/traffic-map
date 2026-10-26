"""Tests of the asset fetcher.

The script is not part of the package, so it is loaded from its path.
Nothing here touches the network: the downloads are stubbed out.
"""

import gzip
import http.client
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_assets.py"


def _load_script():
    """Import the fetcher script as a module."""
    spec = importlib.util.spec_from_file_location("fetch_assets", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # The dataclass decorator looks the module up in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fetch_assets = _load_script()


def test_month_labels_count_back_three_months():
    assert fetch_assets.month_labels(date(2026, 10, 21)) == [
        "2026-10",
        "2026-09",
        "2026-08",
        "2026-07",
    ]


def test_month_labels_cross_the_year_boundary():
    assert fetch_assets.month_labels(date(2026, 1, 15)) == [
        "2026-01",
        "2025-12",
        "2025-11",
        "2025-10",
    ]


def test_the_assets_include_the_map_and_its_labels():
    """The zoom draws borders and place names, so the fetcher brings them too."""
    names = {asset.name for asset in fetch_assets.ASSETS}
    assert {
        "land.geojson",
        "borders.geojson",
        "countries.geojson",
        "cities.geojson",
    } <= names


def test_validate_geojson_accepts_a_feature_collection():
    fetch_assets.validate_geojson(b'{"type": "FeatureCollection", "features": []}')


def test_validate_geojson_rejects_broken_json():
    with pytest.raises(fetch_assets.AssetError):
        fetch_assets.validate_geojson(b"not json at all")


def test_validate_geojson_rejects_json_without_features():
    with pytest.raises(fetch_assets.AssetError):
        fetch_assets.validate_geojson(b'{"type": "Something"}')


def test_validate_mmdb_accepts_the_metadata_marker():
    # A MaxMind DB ends with the metadata section, the marker is in front
    # of it.  A file that starts with the marker is accepted as well.
    fetch_assets.validate_mmdb(b"\x00\x01" + fetch_assets.MMDB_MARKER + b"meta")
    fetch_assets.validate_mmdb(fetch_assets.MMDB_MARKER + b"meta")


def test_validate_mmdb_rejects_other_content():
    with pytest.raises(fetch_assets.AssetError):
        fetch_assets.validate_mmdb(b"<html>404 not found</html>")


def test_decompress_unpacks_a_gzip_stream():
    payload = gzip.compress(b"hello")
    assert fetch_assets.decompress(payload) == b"hello"


def test_decompress_passes_plain_data_through():
    assert fetch_assets.decompress(b"plain") == b"plain"


def test_decompress_rejects_a_broken_stream():
    with pytest.raises(fetch_assets.AssetError):
        fetch_assets.decompress(b"\x1f\x8b broken")


def test_download_falls_back_to_the_previous_month(monkeypatch):
    """A 404 on the current month is retried with an older one."""
    asset = fetch_assets.Asset(
        name="city.mmdb",
        url="https://example.invalid/city-{ym}.mmdb.gz",
        validate=fetch_assets.validate_mmdb,
        compressed=True,
        monthly=True,
    )
    asked: list[str] = []

    def fake_fetch(url: str) -> bytes:
        asked.append(url)
        if url.endswith("2026-10.mmdb.gz"):
            raise fetch_assets.AssetError("HTTP 404", status=404)
        return gzip.compress(b"payload")

    monkeypatch.setattr(fetch_assets, "fetch", fake_fetch)
    monkeypatch.setattr(
        fetch_assets, "month_labels", lambda *a, **k: ["2026-10", "2026-09"]
    )
    url, payload = fetch_assets.download(asset)
    assert asked == [
        "https://example.invalid/city-2026-10.mmdb.gz",
        "https://example.invalid/city-2026-09.mmdb.gz",
    ]
    assert url.endswith("2026-09.mmdb.gz")
    assert payload == gzip.compress(b"payload")


def test_download_gives_up_after_three_months_back(monkeypatch):
    asset = fetch_assets.Asset(
        name="city.mmdb",
        url="https://example.invalid/city-{ym}.mmdb.gz",
        validate=fetch_assets.validate_mmdb,
        monthly=True,
    )

    def fake_fetch(url: str) -> bytes:
        raise fetch_assets.AssetError("HTTP 404", status=404)

    monkeypatch.setattr(fetch_assets, "fetch", fake_fetch)
    with pytest.raises(fetch_assets.AssetError, match="no publication found"):
        fetch_assets.download(asset)


def test_download_does_not_hide_other_errors(monkeypatch):
    asset = fetch_assets.Asset(
        name="city.mmdb",
        url="https://example.invalid/city-{ym}.mmdb.gz",
        validate=fetch_assets.validate_mmdb,
        monthly=True,
    )

    def fake_fetch(url: str) -> bytes:
        raise fetch_assets.AssetError("HTTP 500", status=500)

    monkeypatch.setattr(fetch_assets, "fetch", fake_fetch)
    with pytest.raises(fetch_assets.AssetError, match="HTTP 500"):
        fetch_assets.download(asset)


def test_fetch_asset_keeps_a_valid_file(tmp_path, monkeypatch):
    asset = fetch_assets.Asset(
        name="land.geojson",
        url="https://example.invalid/land.geojson",
        validate=fetch_assets.validate_geojson,
    )
    dest = tmp_path / "land.geojson"
    dest.write_bytes(b'{"features": []}')

    def fake_fetch(url: str) -> bytes:
        raise AssertionError("a valid file must not be downloaded again")

    monkeypatch.setattr(fetch_assets, "fetch", fake_fetch)
    assert "kept" in fetch_assets.fetch_asset(asset, dest)
    assert dest.read_bytes() == b'{"features": []}'


def test_fetch_asset_replaces_an_invalid_file(tmp_path, monkeypatch):
    asset = fetch_assets.Asset(
        name="land.geojson",
        url="https://example.invalid/land.geojson",
        validate=fetch_assets.validate_geojson,
    )
    dest = tmp_path / "land.geojson"
    dest.write_bytes(b"broken")

    def fake_fetch(url: str) -> bytes:
        return b'{"features": []}'

    monkeypatch.setattr(fetch_assets, "fetch", fake_fetch)
    fetch_assets.fetch_asset(asset, dest)
    assert json.loads(dest.read_bytes()) == {"features": []}


def test_main_reports_a_failure(tmp_path, monkeypatch, capsys):
    """A failing asset makes the script exit with 1 and a clean message."""
    asset = fetch_assets.Asset(
        name="land.geojson",
        url="https://example.invalid/land.geojson",
        validate=fetch_assets.validate_geojson,
    )

    def fake_fetch(url: str) -> bytes:
        raise fetch_assets.AssetError("cannot fetch", status=None)

    monkeypatch.setattr(fetch_assets, "fetch", fake_fetch)
    monkeypatch.setattr(fetch_assets, "ASSETS", (asset,))
    assert fetch_assets.main(["--assets-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "cannot fetch" in captured.err
    assert "land.geojson" in captured.err
    # The failure is reported, not raised: no traceback reaches the user.
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_main_succeeds_when_the_assets_are_present(tmp_path, monkeypatch):
    """Assets that are already there and valid are kept, the exit code is 0."""
    asset = fetch_assets.Asset(
        name="land.geojson",
        url="https://example.invalid/land.geojson",
        validate=fetch_assets.validate_geojson,
    )
    (tmp_path / "land.geojson").write_bytes(b'{"features": []}')

    def fake_fetch(url: str) -> bytes:
        raise AssertionError("a valid file must not be downloaded again")

    monkeypatch.setattr(fetch_assets, "fetch", fake_fetch)
    monkeypatch.setattr(fetch_assets, "ASSETS", (asset,))
    assert fetch_assets.main(["--assets-dir", str(tmp_path)]) == 0


def test_main_keeps_going_after_a_failed_asset(tmp_path, monkeypatch):
    """One broken asset does not stop the other ones."""
    broken = fetch_assets.Asset(
        name="broken.geojson",
        url="https://example.invalid/broken.geojson",
        validate=fetch_assets.validate_geojson,
    )
    good = fetch_assets.Asset(
        name="good.geojson",
        url="https://example.invalid/good.geojson",
        validate=fetch_assets.validate_geojson,
    )

    def fake_fetch(url: str) -> bytes:
        if url.endswith("broken.geojson"):
            raise fetch_assets.AssetError("cannot fetch", status=None)
        return b'{"features": []}'

    monkeypatch.setattr(fetch_assets, "fetch", fake_fetch)
    monkeypatch.setattr(fetch_assets, "ASSETS", (broken, good))
    assert fetch_assets.main(["--assets-dir", str(tmp_path)]) == 1
    assert (tmp_path / "good.geojson").read_bytes() == b'{"features": []}'


def test_fetch_turns_an_incomplete_read_into_an_asset_error(monkeypatch):
    """A truncated download does not escape as an HTTPException."""

    class BrokenResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self) -> bytes:
            raise http.client.IncompleteRead(b"12345", 10)

    monkeypatch.setattr(
        fetch_assets.urllib.request, "urlopen", lambda *a, **k: BrokenResponse()
    )
    url = "https://example.invalid/land.geojson"
    with pytest.raises(fetch_assets.AssetError) as excinfo:
        fetch_assets.fetch(url)
    message = str(excinfo.value)
    assert url in message
    assert "incomplete" in message.lower()


def test_fetch_asset_reports_a_failed_write(tmp_path, monkeypatch):
    """A file that cannot be written does not escape as an OSError."""
    asset = fetch_assets.Asset(
        name="land.geojson",
        url="https://example.invalid/land.geojson",
        validate=fetch_assets.validate_geojson,
    )
    dest = tmp_path / "land.geojson"

    def fake_fetch(url: str) -> bytes:
        return b'{"features": []}'

    def failing_write(target: Path, data: bytes) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fetch_assets, "fetch", fake_fetch)
    monkeypatch.setattr(fetch_assets, "_write_atomic", failing_write)
    with pytest.raises(fetch_assets.AssetError) as excinfo:
        fetch_assets.fetch_asset(asset, dest)
    assert str(dest) in str(excinfo.value)


def test_fetch_asset_leaves_no_temporary_file(tmp_path, monkeypatch):
    """The asset is written atomically, no temporary file is left behind."""
    asset = fetch_assets.Asset(
        name="land.geojson",
        url="https://example.invalid/land.geojson",
        validate=fetch_assets.validate_geojson,
    )
    dest = tmp_path / "land.geojson"

    def fake_fetch(url: str) -> bytes:
        return b'{"features": []}'

    monkeypatch.setattr(fetch_assets, "fetch", fake_fetch)
    fetch_assets.fetch_asset(asset, dest)
    assert dest.read_bytes() == b'{"features": []}'
    assert list(tmp_path.iterdir()) == [dest]


@pytest.mark.parametrize("status", [403, 404, 410])
def test_download_falls_back_on_a_missing_publication(monkeypatch, status):
    """A month that is not published yet is looked up in an older one."""
    asset = fetch_assets.Asset(
        name="city.mmdb",
        url="https://example.invalid/city-{ym}.mmdb.gz",
        validate=fetch_assets.validate_mmdb,
        compressed=True,
        monthly=True,
    )
    asked: list[str] = []

    def fake_fetch(url: str) -> bytes:
        asked.append(url)
        if url.endswith("2026-10.mmdb.gz"):
            raise fetch_assets.AssetError(f"HTTP {status}", status=status)
        return gzip.compress(b"payload")

    monkeypatch.setattr(fetch_assets, "fetch", fake_fetch)
    monkeypatch.setattr(
        fetch_assets, "month_labels", lambda *a, **k: ["2026-10", "2026-09"]
    )
    url, payload = fetch_assets.download(asset)
    assert asked == [
        "https://example.invalid/city-2026-10.mmdb.gz",
        "https://example.invalid/city-2026-09.mmdb.gz",
    ]
    assert url.endswith("2026-09.mmdb.gz")
    assert payload == gzip.compress(b"payload")
