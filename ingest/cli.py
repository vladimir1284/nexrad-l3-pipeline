"""CLI del procesador Level III.

`l3proc process <fichero>` — decodifica un producto crudo, lo grilla a
AEQD y escribe el COG. F2 añade publicación a R2/D1.
"""

import argparse
import sys
from pathlib import Path

from ingest import __version__


def _cmd_process(args: argparse.Namespace) -> int:
    # Import diferido: MetPy/Rasterio tardan ~1-2 s y no hacen falta para --help.
    from ingest.decoder.level3 import UnsupportedProductError, decode_file
    from ingest.gridding.aeqd import grid_radial
    from ingest.gridding.cog import write_cog

    try:
        prod = decode_file(args.file)
    except UnsupportedProductError as exc:
        print(f"l3proc: {args.file}: {exc}", file=sys.stderr)
        return 2

    grid = grid_radial(prod)
    out_dir = args.output_dir or args.file.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = prod.vol_time.strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"{prod.site_id}_{prod.spec.mnemonic}_{stamp}.tif"
    write_cog(grid, prod, out)
    print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="l3proc",
        description="Procesador de productos NEXRAD Level III",
    )
    parser.add_argument("--version", action="version", version=f"l3proc {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_process = sub.add_parser("process", help="producto crudo → COG AEQD")
    p_process.add_argument("file", type=Path, help="fichero Level III crudo")
    p_process.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="directorio de salida (por defecto, el del fichero de entrada)",
    )
    p_process.set_defaults(func=_cmd_process)

    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)
