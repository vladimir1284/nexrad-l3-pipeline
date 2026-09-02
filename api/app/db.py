"""Pool de conexiones Postgres, una única instancia por proceso.

Config por variables de entorno con la misma convención `<NOMBRE>_FILE`
(Docker secrets) que ingest/config.py — duplicada aquí, no importada,
porque api/ es un despliegue Python independiente del pipeline (deps
propias, sin rasterio/eccodes/h5py).
"""

import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def _env(name: str) -> str:
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        return open(file_path).read().strip()
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(f"falta la variable {name} (o {name}_FILE)")
    return value


def _dsn() -> str:
    host = _env("PG_HOST")
    port = os.environ.get("PG_PORT", "5432")
    db = _env("PG_DB")
    user = _env("PG_USER")
    password = _env("PG_PASSWORD")
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(_dsn(), min_size=1, max_size=5, kwargs={"row_factory": dict_row})
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
