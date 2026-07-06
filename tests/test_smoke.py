from ingest import __version__
from ingest.cli import main


def test_version_presente():
    assert __version__


def test_cli_sin_argumentos_sale_bien():
    assert main([]) == 0
