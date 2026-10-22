from importlib.metadata import version

from trafficmap import __version__
from trafficmap.__main__ import main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"tmap {__version__}"


def test_installed_metadata_matches_package_version():
    assert version("trafficmap") == __version__
