#!/usr/bin/env python3
"""Aplica las migraciones de db/pg_migrations/*.sql contra Postgres.

Reemplaza `wrangler d1 migrations apply` (D1-specific). Nada de
Alembic/yoyo/Flyway — a este tamaño de schema (un puñado de ficheros) un
runner de ~20 líneas alcanza: aplica en orden lexicográfico los .sql que
todavía no estén en schema_migrations, uno por uno, cada uno en su propia
transacción.

Uso:
    uv run python db/apply_pg_migrations.py "$PG_DSN"
"""

import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "pg_migrations"


def apply_migrations(dsn: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=False) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.commit()
        done = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}
        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", [path.name])
            conn.commit()
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"uso: {sys.argv[0]} <postgres-dsn>", file=sys.stderr)
        sys.exit(1)
    applied = apply_migrations(sys.argv[1])
    if applied:
        print(f"aplicadas: {', '.join(applied)}")
    else:
        print("nada que aplicar, schema al día")
