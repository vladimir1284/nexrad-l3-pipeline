"""Publisher contra un D1 falso respaldado por SQLite real.

Ejecutar los statements reales contra SQLite valida la sintaxis de los
upserts (D1 es SQLite) y el schema de db/migrations/ a la vez.
"""

import sqlite3
from pathlib import Path

import pytest

from ingest.decoder.level3 import decode_file
from ingest.gridding.aeqd import grid_radial
from ingest.gridding.cog import write_cog
from ingest.storage.publish import publish_cog

MIGRATION = Path(__file__).parent.parent / "db" / "migrations" / "0001_init.sql"


class SqliteD1:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(MIGRATION.read_text())

    def execute(self, sql, params=()):
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return [dict(r) for r in cur.fetchall()]

    def execute_many(self, statements):
        for sql, params in statements:
            self.execute(sql, params)


class FakeR2:
    def __init__(self):
        self.uploads = []

    def upload_file(self, path, key, content_type="image/tiff"):
        self.uploads.append((Path(path), key, content_type))


@pytest.fixture(scope="module")
def pipeline_amx(tmp_path_factory):
    prod = decode_file(Path(__file__).parent / "data" / "AMX_N0B_2026_07_06_15_45_17")
    grid = grid_radial(prod)
    cog = write_cog(grid, prod, tmp_path_factory.mktemp("cog") / "amx.tif")
    return prod, grid, cog


def test_publish_sube_y_registra(pipeline_amx):
    prod, grid, cog = pipeline_amx
    r2, d1 = FakeR2(), SqliteD1()

    result = publish_cog(cog, prod, grid, r2, d1)

    assert result.r2_key == "AMX/N0B/2026/07/06/AMX_N0B_20260706_154517.tif"
    assert result.size_bytes == cog.stat().st_size
    assert r2.uploads == [(cog, result.r2_key, "image/tiff")]

    radar = d1.execute("SELECT * FROM radars")[0]
    assert radar["site_id"] == "AMX"
    assert radar["lat"] == 25.611
    assert "+proj=aeqd +lat_0=25.611" in radar["proj4"]

    producto = d1.execute("SELECT * FROM products")[0]
    assert producto["code"] == 153
    assert producto["mnemonic"] == "N0B"
    assert producto["kind"] == "raster"

    raster = d1.execute("SELECT * FROM rasters")[0]
    assert raster["r2_key"] == result.r2_key
    assert raster["size_bytes"] == result.size_bytes
    assert raster["vol_time"] == "2026-07-06T15:45:17"
    assert raster["value_scale"] == 0.5
    assert raster["value_offset"] == -33.0
    assert raster["max_level"] == 187
    assert raster["width"] == raster["height"] == 3680
    assert raster["cell_m"] == 250.0


def test_publish_es_idempotente(pipeline_amx):
    prod, grid, cog = pipeline_amx
    r2, d1 = FakeR2(), SqliteD1()

    publish_cog(cog, prod, grid, r2, d1)
    first_seen = d1.execute("SELECT first_seen_at FROM radars")[0]["first_seen_at"]
    publish_cog(cog, prod, grid, r2, d1)

    assert len(d1.execute("SELECT * FROM rasters")) == 1
    assert len(d1.execute("SELECT * FROM radars")) == 1
    assert len(d1.execute("SELECT * FROM products")) == 1
    # first_seen_at se conserva; last_seen_at avanza con cada publicación.
    assert d1.execute("SELECT first_seen_at FROM radars")[0]["first_seen_at"] == first_seen
