"""Servicio del mosaico contra SQLite real + R2 falso.

Mismo criterio que test_wind/test_lightning: el SQL de ingest/ corre contra
un motor de verdad, no contra un mock. Los COG de origen son sintéticos pero
reales (rasterio, AEQD, scale/offset embebidos), así que el camino
descarga → lectura → proyección → composición → escritura se ejercita entero.
"""

import json
import sqlite3
from datetime import datetime

import numpy as np
import pytest
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from ingest.config import MosaicConfig
from ingest.gridding.aeqd import radar_proj4
from ingest.mosaic.service import MosaicComposer
from tests.conftest import sqlite_compatible_pg_schema

SITES = {"AMX": (25.611, -80.413, 4.0), "BYX": (24.597, -81.703, 2.0)}
SRC_SIZE = 200
SRC_CELL = 2000.0  # semi-extensión 200 km
SLOT = datetime(2026, 10, 10, 3, 5)
NOW = datetime(2026, 10, 10, 3, 11)
SCALE, OFFSET = 0.5, -33.0


class SqlitePg:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(sqlite_compatible_pg_schema())
        for site_id, (lat, lon, height) in SITES.items():
            self.conn.execute(
                "INSERT INTO radars (site_id, icao, lat, lon, height_m, proj4,"
                " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (site_id, None, lat, lon, height, radar_proj4(lat, lon), "x", "x"),
            )
        self.conn.execute(
            "INSERT INTO products (code, mnemonic, unit, kind) VALUES (153, 'N0B', 'dBZ', 'raster')"
        )
        self.conn.commit()

    def execute(self, sql, params=()):
        cur = self.conn.execute(sql.replace("%s", "?"), tuple(params))
        self.conn.commit()
        return [dict(r) for r in cur.fetchall()]

    def execute_many(self, statements):
        for sql, params in statements:
            self.conn.execute(sql.replace("%s", "?"), tuple(params))
        self.conn.commit()

    def add_raster(self, site_id, vol_time, key):
        self.conn.execute(
            "INSERT INTO rasters (site_id, product_code, vol_time, r2_key, size_bytes,"
            " el_angle, vcp, value_scale, value_offset, max_level, width, height,"
            " cell_m, created_at) VALUES (?, 153, ?, ?, 1, 0.5, 215, ?, ?, 255, ?, ?, ?, 'x')",
            (site_id, vol_time.isoformat(), key, SCALE, OFFSET, SRC_SIZE, SRC_SIZE, SRC_CELL),
        )
        self.conn.commit()

    def rows(self, table):
        return [dict(r) for r in self.conn.execute(f"SELECT * FROM {table}")]


class FakeR2:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_file(self, path, key, content_type="image/tiff"):
        assert content_type == "image/tiff"
        with open(path, "rb") as fh:
            self.objects[key] = fh.read()

    def download_bytes(self, key):
        return self.objects[key]


def _source_cog(site_id: str, nivel: int) -> bytes:
    """COG AEQD sintético: disco de `nivel` centrado en el radar."""
    lat, lon, _ = SITES[site_id]
    half = SRC_SIZE * SRC_CELL / 2.0
    centros = -half + (np.arange(SRC_SIZE) + 0.5) * SRC_CELL
    rng = np.hypot(centros[np.newaxis, :], -centros[:, np.newaxis])
    data = np.where(rng <= half, nivel, 0).astype(np.uint8)
    profile = {
        "driver": "COG",
        "dtype": "uint8",
        "count": 1,
        "width": SRC_SIZE,
        "height": SRC_SIZE,
        "crs": CRS.from_proj4(radar_proj4(lat, lon)),
        "transform": from_origin(-half, half, SRC_CELL, SRC_CELL),
        "nodata": 0,
        "compress": "DEFLATE",
    }
    with MemoryFile() as mem:
        with mem.open(**profile) as ds:
            ds.write(data, 1)
            ds.scales = (SCALE,)
            ds.offsets = (OFFSET,)
        return mem.read()


def _config(**over):
    base = {
        "domains": (("GULF", ("AMX", "BYX")),),
        "products": (153,),
        "cell_m": 4000.0,
        "method": "weighted",
        "h_ref_m": 4000.0,
        "r_ref_m": 200_000.0,
        "slot_s": 300,
        "slot_tolerance_s": 150,
        "min_sites": 1,
        "lookback_s": 900,
        "max_slots": 12,
    }
    base.update(over)
    return MosaicConfig(**base)


@pytest.fixture
def mundo():
    pg, r2 = SqlitePg(), FakeR2()
    for i, (site_id, nivel) in enumerate((("AMX", 100), ("BYX", 140))):
        key = f"src/{site_id}.tif"
        r2.objects[key] = _source_cog(site_id, nivel)
        pg.add_raster(site_id, SLOT.replace(minute=7, second=10 + i * 20), key)
    return pg, r2


# --- camino feliz -----------------------------------------------------------


def test_compone_un_slot_y_deja_fila_y_objeto(mundo):
    pg, r2 = mundo
    stats = MosaicComposer(pg, r2, _config()).run_once(now=NOW)

    assert stats["composed"] == 1
    assert stats["failed"] == 0
    assert stats["sources"] == 2

    (fila,) = pg.rows("mosaic_rasters")
    assert fila["domain_id"] == "GULF"
    assert fila["product_code"] == 153
    assert fila["slot_time"] == SLOT.isoformat()
    assert fila["method"] == "weighted"
    assert fila["value_scale"] == SCALE and fila["value_offset"] == OFFSET
    assert fila["r2_key"] in r2.objects
    assert fila["size_bytes"] == len(r2.objects[fila["r2_key"]])
    assert fila["width"] > 0 and fila["height"] > 0 and fila["cell_m"] == 4000.0
    assert "+proj=aeqd" in fila["proj4"]


def test_contributing_dice_quien_aporto_y_con_cuanto_desfase(mundo):
    pg, r2 = mundo
    MosaicComposer(pg, r2, _config()).run_once(now=NOW)
    aportes = json.loads(pg.rows("mosaic_rasters")[0]["contributing"])
    assert [a["site"] for a in aportes] == ["AMX", "BYX"]
    # centro del slot = 03:07:30; AMX en :07:10, BYX en :07:30
    assert [a["lag_s"] for a in aportes] == [-20, 0]
    assert all("vol_time" in a for a in aportes)


def test_siembra_el_dominio_y_sus_sitios(mundo):
    pg, r2 = mundo
    MosaicComposer(pg, r2, _config()).run_once(now=NOW)
    (dominio,) = pg.rows("mosaic_domains")
    assert dominio["domain_id"] == "GULF"
    assert dominio["radius_m"] == pytest.approx(SRC_SIZE * SRC_CELL / 2.0)
    assert sorted(r["site_id"] for r in pg.rows("mosaic_domain_sites")) == ["AMX", "BYX"]


def test_el_cog_compuesto_es_legible_y_autodescriptivo(mundo):
    pg, r2 = mundo
    MosaicComposer(pg, r2, _config()).run_once(now=NOW)
    fila = pg.rows("mosaic_rasters")[0]
    with MemoryFile(r2.objects[fila["r2_key"]]) as mem, mem.open() as ds:
        assert ds.count == 1 and ds.dtypes[0] == "uint8"
        assert ds.scales == (SCALE,) and ds.offsets == (OFFSET,)
        assert ds.nodata == 0
        assert (ds.width, ds.height) == (fila["width"], fila["height"])
        tags = ds.tags()
        assert tags["DOMAIN"] == "GULF"
        assert tags["PRODUCT"] == "N0B"
        assert tags["METHOD"] == "weighted"
        assert tags["SLOT_TIME"] == SLOT.isoformat()
        assert [a["site"] for a in json.loads(tags["CONTRIBUTING"])] == ["AMX", "BYX"]
        datos = ds.read(1)
    assert datos.max() > 0
    # Los dos discos aportan: el compuesto cubre más que cualquiera suelto.
    assert (datos > 0).sum() > 0


# --- composición parcial ----------------------------------------------------


def test_un_radar_caido_no_apaga_el_mosaico(mundo):
    pg, r2 = mundo
    pg.conn.execute("DELETE FROM rasters WHERE site_id = 'BYX'")
    pg.conn.commit()
    stats = MosaicComposer(pg, r2, _config()).run_once(now=NOW)
    assert stats["composed"] == 1
    aportes = json.loads(pg.rows("mosaic_rasters")[0]["contributing"])
    assert [a["site"] for a in aportes] == ["AMX"]


def test_min_sites_bloquea_el_slot_incompleto(mundo):
    pg, r2 = mundo
    pg.conn.execute("DELETE FROM rasters WHERE site_id = 'BYX'")
    pg.conn.commit()
    stats = MosaicComposer(pg, r2, _config(min_sites=2)).run_once(now=NOW)
    assert stats["composed"] == 0
    assert pg.rows("mosaic_rasters") == []


def test_objeto_ausente_en_r2_no_invalida_el_slot(mundo):
    pg, r2 = mundo
    del r2.objects["src/BYX.tif"]
    stats = MosaicComposer(pg, r2, _config()).run_once(now=NOW)
    assert stats["composed"] == 1
    assert stats["failed"] == 1
    assert [a["site"] for a in json.loads(pg.rows("mosaic_rasters")[0]["contributing"])] == ["AMX"]


def test_la_tolerancia_filtra_el_volumen_lejano(mundo):
    """AMX cae a −20 s del centro, BYX justo en el centro: con 5 s solo entra BYX."""
    pg, r2 = mundo
    stats = MosaicComposer(pg, r2, _config(slot_tolerance_s=5)).run_once(now=NOW)
    assert stats["composed"] == 1
    aportes = json.loads(pg.rows("mosaic_rasters")[0]["contributing"])
    assert [a["site"] for a in aportes] == ["BYX"]


def test_sin_ningun_volumen_en_tolerancia_no_hay_fila(mundo):
    pg, r2 = mundo
    pg.conn.execute("UPDATE rasters SET vol_time = '2026-10-10T03:00:00'")
    pg.conn.commit()
    stats = MosaicComposer(pg, r2, _config(slot_tolerance_s=5)).run_once(now=NOW)
    assert stats["composed"] == 0


# --- idempotencia y reanudación --------------------------------------------


def test_recomponer_es_idempotente(mundo):
    pg, r2 = mundo
    composer = MosaicComposer(pg, r2, _config())
    composer.run_once(now=NOW)
    primera = pg.rows("mosaic_rasters")[0]["r2_key"]
    # Forzar reproceso del mismo slot: sin esto la reanudación lo salta.
    pg.conn.execute("DELETE FROM mosaic_rasters")
    pg.conn.commit()
    composer.run_once(now=NOW)
    filas = pg.rows("mosaic_rasters")
    assert len(filas) == 1 and filas[0]["r2_key"] == primera
    assert len(r2.objects) == 3  # 2 fuentes + 1 mosaico


def test_no_recompone_lo_ya_hecho(mundo):
    pg, r2 = mundo
    composer = MosaicComposer(pg, r2, _config())
    assert composer.run_once(now=NOW)["composed"] == 1
    segunda = composer.run_once(now=NOW)
    assert segunda["composed"] == 0 and segunda["slots"] == 0


def test_sin_rasters_no_dimensiona_el_dominio(mundo):
    pg, r2 = mundo
    pg.conn.execute("DELETE FROM rasters")
    pg.conn.commit()
    stats = MosaicComposer(pg, r2, _config()).run_once(now=NOW)
    assert stats == {"slots": 0, "composed": 0, "skipped": 0, "sources": 0, "failed": 0}
    assert pg.rows("mosaic_domains") == []


def test_sitio_del_dominio_sin_fila_en_radars_se_ignora(mundo):
    pg, r2 = mundo
    cfg = _config(domains=(("GULF", ("AMX", "BYX", "FANTASMA")),))
    assert MosaicComposer(pg, r2, cfg).run_once(now=NOW)["composed"] == 1
    assert sorted(r["site_id"] for r in pg.rows("mosaic_domain_sites")) == ["AMX", "BYX"]


def test_lowest_beam_da_otro_resultado_que_weighted(mundo):
    pg, r2 = mundo
    MosaicComposer(pg, r2, _config(method="weighted")).run_once(now=NOW)
    a = r2.objects[pg.rows("mosaic_rasters")[0]["r2_key"]]
    pg.conn.execute("DELETE FROM mosaic_rasters")
    pg.conn.commit()
    MosaicComposer(pg, r2, _config(method="lowest_beam")).run_once(now=NOW)
    fila = pg.rows("mosaic_rasters")[0]
    assert fila["method"] == "lowest_beam"
    with (
        MemoryFile(a) as m1,
        m1.open() as d1,
        MemoryFile(r2.objects[fila["r2_key"]]) as m2,
        m2.open() as d2,
    ):
        assert not np.array_equal(d1.read(1), d2.read(1))
