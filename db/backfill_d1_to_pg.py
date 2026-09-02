#!/usr/bin/env python3
"""Backfill único D1 → Postgres, para el cutover de producción.

Lee TODAS las filas de D1 (vía D1Client, de solo lectura acá) e inserta
en Postgres en orden de dependencia de FK: radars/products primero
(dimensiones), después rasters/phenomena/vwp/wind_grids/lightning_buckets
(hechos). Postgres debe tener el schema ya aplicado
(db/apply_pg_migrations.py) y estar vacío — no hace upsert, corre una
sola vez.

Uso:
    uv run python db/backfill_d1_to_pg.py \
        --d1-account "$CLOUDFLARE_ACCOUNT_ID" --d1-database "$D1_DATABASE_ID" \
        --d1-token "$CLOUDFLARE_API_TOKEN" \
        --pg-dsn "host=... port=5432 dbname=... user=... password=..."
"""

import argparse
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from ingest.storage.d1 import D1Client  # noqa: E402
from ingest.storage.pg import PgClient  # noqa: E402

# Orden de dependencia: dimensiones antes que hechos.
TABLES = [
    "radars",
    "products",
    "rasters",
    "phenomena",
    "vwp",
    "wind_grids",
    "lightning_buckets",
]


def copy_table(d1: D1Client, pg: PgClient, table: str) -> int:
    rows = d1.execute(f"SELECT * FROM {table}")  # noqa: S608 — TABLES es una lista fija, no input externo
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ", ".join("%s" for _ in cols)
    col_list = ", ".join(cols)
    insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"  # noqa: S608
    pg.execute_many([(insert_sql, [row[c] for c in cols]) for row in rows])
    return len(rows)


def verify_counts(d1: D1Client, pg: PgClient) -> bool:
    ok = True
    for table in TABLES:
        d1_n = d1.execute(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]  # noqa: S608
        pg_n = pg.execute(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]  # noqa: S608
        status = "OK" if d1_n == pg_n else "MISMATCH"
        print(f"  {table}: D1={d1_n} Postgres={pg_n} [{status}]")
        ok = ok and d1_n == pg_n
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--d1-account", required=True)
    parser.add_argument("--d1-database", required=True)
    parser.add_argument("--d1-token", required=True)
    parser.add_argument("--pg-dsn", required=True)
    args = parser.parse_args()

    with (
        D1Client(args.d1_account, args.d1_database, args.d1_token) as d1,
        PgClient(args.pg_dsn) as pg,
    ):
        for table in TABLES:
            n = copy_table(d1, pg, table)
            print(f"{table}: {n} filas copiadas")

        print("\nVerificación de conteos:")
        if not verify_counts(d1, pg):
            print("\n✗ hay tablas con conteos distintos — no continuar el cutover", file=sys.stderr)
            return 1
        print("\n✓ conteos coinciden en las 7 tablas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
