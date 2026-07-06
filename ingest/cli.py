"""CLI del procesador Level III.

F1 añade el subcomando `process <fichero>`; por ahora solo esqueleto.
"""

import argparse

from ingest import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="l3proc",
        description="Procesador de productos NEXRAD Level III",
    )
    parser.add_argument("--version", action="version", version=f"l3proc {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
