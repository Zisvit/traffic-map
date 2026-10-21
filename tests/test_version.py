from trafficmap import __version__
from trafficmap.__main__ import main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"tmap {__version__}"
