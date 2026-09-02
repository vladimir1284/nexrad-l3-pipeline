from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent / "data"
PG_SCHEMA = Path(__file__).parent.parent / "db" / "pg_migrations" / "0001_init.sql"


def sqlite_compatible_pg_schema() -> str:
    """El schema real de db/pg_migrations/, traducido para correr en SQLite.

    Los tests rápidos de publish/wind/lightning ejecutan el SQL real de
    ingest/ (upserts, delete+reinsert) contra un motor real en vez de un
    mock — SQLite alcanza para eso (misma sintaxis ON CONFLICT que
    Postgres) salvo por `BIGSERIAL`, que SQLite no entiende; se traduce a
    su equivalente `INTEGER PRIMARY KEY AUTOINCREMENT`. La validación de
    lo que SQLite no puede cubrir (tipos reales, FKs enforced) vive en
    tests/test_storage_integration.py contra un Postgres real (gated).
    """
    return PG_SCHEMA.read_text().replace(
        "BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT"
    )


# Muestras reales del bucket público unidata-nexrad-level3, commiteadas
# para que CI no dependa de la red. Goldens capturados con MetPy 1.7.x.
SAMPLES = {
    "AMX": DATA_DIR / "AMX_N0B_2026_07_06_15_45_17",
    "JUA": DATA_DIR / "JUA_N0B_2026_07_06_15_43_47",
}


@pytest.fixture(params=sorted(SAMPLES))
def site(request):
    return request.param


@pytest.fixture
def sample_path(site):
    return SAMPLES[site]
