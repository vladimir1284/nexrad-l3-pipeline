"""Tests del módulo de viento GFS (ingest/wind.py).

Sin red: los GRIB2 se generan con eccodes desde el sample regular_ll y
el fetcher se inyecta. El D1 falso es SQLite real con las migraciones de
db/ — valida la sintaxis del upsert y el schema wind_grids a la vez.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from ingest.wind import (
    BBox,
    WindIngestor,
    candidate_cycles,
    decode_grib,
    encode_json,
    site_bbox,
    subset,
    union_bbox,
    wind_key,
)

MIGRATIONS = sorted((Path(__file__).parent.parent / "db" / "migrations").glob("*.sql"))

AMX_LAT, AMX_LON = 25.6111, -80.4128


class SqliteD1:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        for migration in MIGRATIONS:
            self.conn.executescript(migration.read_text())
        self.conn.execute(
            "INSERT INTO radars (site_id, icao, lat, lon, height_m, proj4,"
            " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "AMX",
                "KAMX",
                AMX_LAT,
                AMX_LON,
                20.0,
                "+proj=aeqd",
                "2026-07-18T00:00:00",
                "2026-07-18T00:00:00",
            ),
        )
        self.conn.commit()

    def execute(self, sql, params=()):
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return [dict(r) for r in cur.fetchall()]


class FakeR2:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def upload_bytes(self, data, key, content_type):
        assert content_type == "application/json"
        self.objects[key] = data

    def delete_keys(self, keys):
        self.deleted.extend(keys)
        for k in keys:
            self.objects.pop(k, None)


def make_grib(box: BBox, seed: float = 0.0, south_to_north: bool = False) -> bytes:
    """UGRD+VGRD 10 m sintéticos sobre `box`, en la convención GFS (lon 0–360).

    `south_to_north=True` imita al filtro de NOMADS, que re-empaqueta los
    subsets con jScansPositively=1 (los GFS crudos van norte→sur).
    """
    ec = pytest.importorskip("eccodes")
    n = box.nx * box.ny
    lat_first, lat_last = (box.south, box.north) if south_to_north else (box.north, box.south)
    out = b""
    for short, base in (("10u", seed), ("10v", seed + 100.0)):
        h = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
        ec.codes_set(h, "shortName", short)
        ec.codes_set(h, "Ni", box.nx)
        ec.codes_set(h, "Nj", box.ny)
        ec.codes_set(h, "jScansPositively", 1 if south_to_north else 0)
        ec.codes_set(h, "latitudeOfFirstGridPointInDegrees", lat_first)
        ec.codes_set(h, "longitudeOfFirstGridPointInDegrees", box.west % 360)
        ec.codes_set(h, "latitudeOfLastGridPointInDegrees", lat_last)
        ec.codes_set(h, "longitudeOfLastGridPointInDegrees", box.east % 360)
        ec.codes_set(h, "iDirectionIncrementInDegrees", 0.25)
        ec.codes_set(h, "jDirectionIncrementInDegrees", 0.25)
        values = base + np.arange(n, dtype=float) * 0.017
        if south_to_north:  # mismo campo físico, filas en orden de escaneo inverso
            values = values.reshape(box.ny, box.nx)[::-1].ravel()
        ec.codes_set_values(h, values)
        out += ec.codes_get_message(h)
        ec.codes_release(h)
    return out


# ------------------------------------------------------------- geometría


def test_site_bbox_alineado_y_cubre_6_grados():
    box = site_bbox(AMX_LAT, AMX_LON)
    for edge in (box.north, box.south, box.west, box.east):
        assert edge == round(edge / 0.25) * 0.25
    assert box.north >= AMX_LAT + 6 and box.south <= AMX_LAT - 6
    assert box.east >= AMX_LON + 6 and box.west <= AMX_LON - 6
    # expansión hacia fuera: a lo sumo un nodo extra por lado sobre los 49
    assert 49 <= box.nx <= 50 and 49 <= box.ny <= 50


def test_union_bbox():
    a = site_bbox(25.6111, -80.4128)  # AMX
    b = site_bbox(18.1156, -66.0781)  # JUA
    u = union_bbox([a, b])
    assert u.north == a.north and u.south == b.south
    assert u.west == a.west and u.east == b.east


def test_wind_key_ejemplo_de_la_spec():
    key = wind_key("AMX", datetime(2026, 7, 18, 12, 0, 0), datetime(2026, 7, 18, 6), 6)
    assert key == "AMX/WIND/2026/07/18/AMX_WIND_20260718_120000_c2026071806f006.json"


def test_candidate_cycles_fh_0_a_12():
    assert candidate_cycles(datetime(2026, 7, 18, 12)) == [
        (datetime(2026, 7, 18, 12), 0),
        (datetime(2026, 7, 18, 6), 6),
        (datetime(2026, 7, 18, 0), 12),
    ]
    assert candidate_cycles(datetime(2026, 7, 18, 13)) == [
        (datetime(2026, 7, 18, 12), 1),
        (datetime(2026, 7, 18, 6), 7),
    ]


# ---------------------------------------------------------- GRIB → JSON


def test_decode_subset_encode_roundtrip():
    amx = site_bbox(AMX_LAT, AMX_LON)
    jua = site_bbox(18.1156, -66.0781)
    union = union_bbox([amx, jua])

    field = decode_grib(make_grib(union))
    assert -180 <= field.lo1 < 180  # convertido del 0–360 de GFS
    assert field.la1 == union.north and field.lo1 == union.west
    assert field.u.shape == (union.ny, union.nx)

    sub = subset(field, amx)
    assert sub.u.shape == (amx.ny, amx.nx)
    # el recorte por índice conserva los valores exactos del campo grande
    row0 = round((union.north - amx.north) / 0.25)
    col0 = round((amx.west - union.west) / 0.25)
    assert np.array_equal(sub.v, field.v[row0 : row0 + amx.ny, col0 : col0 + amx.nx])

    doc = json.loads(encode_json(sub, datetime(2026, 7, 18, 6), 6))
    h = doc["header"]
    assert h == {
        "nx": amx.nx,
        "ny": amx.ny,
        "lo1": amx.west,
        "la1": amx.north,
        "dx": 0.25,
        "dy": 0.25,
        "refTime": "2026-07-18T06:00:00Z",
        "forecastHour": 6,
    }
    assert len(doc["u"]) == len(doc["v"]) == amx.nx * amx.ny
    assert all(round(x, 2) == x for x in doc["u"])  # 2 decimales


def test_decode_voltea_grillas_sur_a_norte():
    """El filtro de NOMADS re-empaqueta subsets con jScansPositively=1."""
    box = site_bbox(AMX_LAT, AMX_LON)
    norte_sur = decode_grib(make_grib(box, seed=7.0))
    sur_norte = decode_grib(make_grib(box, seed=7.0, south_to_north=True))
    assert sur_norte.la1 == norte_sur.la1 == box.north
    assert np.array_equal(sur_norte.u, norte_sur.u)
    assert np.array_equal(sur_norte.v, norte_sur.v)


# ------------------------------------------------------------- ingestor


class ScriptedNomads:
    """Fetcher inyectable: sirve GRIBs para los ciclos marcados disponibles."""

    def __init__(self):
        self.available = set()  # datetimes de ciclo publicados
        self.requests = []

    def __call__(self, cycle, fh, box):
        self.requests.append((cycle, fh))
        if cycle not in self.available:
            return None
        return make_grib(box, seed=float(cycle.hour + fh))


@pytest.fixture
def env():
    d1, r2, nomads = SqliteD1(), FakeR2(), ScriptedNomads()
    ingestor = WindIngestor(d1, r2, fetch=nomads, window_h=3, lookahead_h=2, pause_s=0)
    return d1, r2, nomads, ingestor


NOW = datetime(2026, 7, 18, 15, 30)  # → valid_times 13:00..17:00


def test_run_once_publica_ventana_completa(env):
    d1, r2, nomads, ingestor = env
    nomads.available = {datetime(2026, 7, 18, 6)}  # el 12Z aún no publicado

    stats = ingestor.run_once(now=NOW)

    assert stats == {"published": 5, "fresh": 0, "failed": 0}
    rows = d1.execute("SELECT * FROM wind_grids ORDER BY valid_time")
    assert [r["valid_time"] for r in rows] == [
        f"2026-07-18T{h}:00:00" for h in ("13", "14", "15", "16", "17")
    ]
    assert all(r["cycle_time"] == "2026-07-18T06:00:00" for r in rows)
    assert [r["forecast_hour"] for r in rows] == [7, 8, 9, 10, 11]
    assert set(r2.objects) == {r["r2_key"] for r in rows}
    assert all(r["size_bytes"] == len(r2.objects[r["r2_key"]]) for r in rows)
    # criterio de aceptación del viewer: JSON válido con u/v de nx*ny
    doc = json.loads(r2.objects[rows[0]["r2_key"]])
    assert len(doc["u"]) == doc["header"]["nx"] * doc["header"]["ny"]


def test_rerun_sin_datos_nuevos_es_noop(env):
    d1, r2, nomads, ingestor = env
    nomads.available = {datetime(2026, 7, 18, 6)}
    ingestor.run_once(now=NOW)
    uploads_antes = dict(r2.objects)
    nomads.requests.clear()

    stats = ingestor.run_once(now=NOW)

    assert stats == {"published": 0, "fresh": 5, "failed": 0}
    assert r2.objects == uploads_antes and r2.deleted == []
    # sí sondea NOMADS (¿hay ciclo más nuevo?) pero nunca el ciclo ya servido
    assert all(cycle > datetime(2026, 7, 18, 6) for cycle, _ in nomads.requests)


def test_ciclo_nuevo_reemplaza_y_borra_objetos_viejos(env):
    d1, r2, nomads, ingestor = env
    nomads.available = {datetime(2026, 7, 18, 6)}
    ingestor.run_once(now=NOW)
    viejos = {r["r2_key"] for r in d1.execute("SELECT r2_key FROM wind_grids")}

    nomads.available.add(datetime(2026, 7, 18, 12))
    stats = ingestor.run_once(now=NOW)

    assert stats == {"published": 5, "fresh": 0, "failed": 0}
    rows = d1.execute("SELECT * FROM wind_grids ORDER BY valid_time")
    assert all(r["cycle_time"] == "2026-07-18T12:00:00" for r in rows)
    assert [r["forecast_hour"] for r in rows] == [1, 2, 3, 4, 5]
    assert set(r2.deleted) == viejos
    assert set(r2.objects) == {r["r2_key"] for r in rows}
    assert "c2026071812" in rows[0]["r2_key"]


def test_upsert_no_degrada_a_ciclo_mas_viejo(env):
    """El guard del upsert: un cycle_time menor no pisa la fila."""
    d1, _r2, nomads, ingestor = env
    nomads.available = {datetime(2026, 7, 18, 12)}
    ingestor.run_once(now=NOW)

    from ingest.wind import _UPSERT_SQL

    d1.execute(
        _UPSERT_SQL,
        ["AMX", "2026-07-18T13:00:00", "2026-07-18T06:00:00", 7, "gfs0p25", "otro", 1],
    )
    row = d1.execute(
        "SELECT cycle_time FROM wind_grids WHERE valid_time = ?", ["2026-07-18T13:00:00"]
    )[0]
    assert row["cycle_time"] == "2026-07-18T12:00:00"


def test_fallo_en_un_valid_time_no_aborta_el_resto(env):
    d1, _r2, nomads, ingestor = env
    nomads.available = {datetime(2026, 7, 18, 6), datetime(2026, 7, 18, 12)}
    roto = datetime(2026, 7, 18, 12)

    def fetch(cycle, fh, box):
        if (cycle, fh) == (roto, 3):  # valid_time 15:00 revienta
            raise RuntimeError("NOMADS 503")
        return nomads(cycle, fh, box)

    ingestor._fetch = fetch
    stats = ingestor.run_once(now=NOW)

    assert stats["failed"] == 1
    assert stats["published"] == 4
    assert d1.execute("SELECT COUNT(*) AS n FROM wind_grids")[0]["n"] == 4


def test_sin_radares_no_hace_nada():
    d1 = SqliteD1()
    d1.conn.execute("DELETE FROM radars")
    d1.conn.commit()
    nomads = ScriptedNomads()
    ingestor = WindIngestor(d1, FakeR2(), fetch=nomads, pause_s=0)
    assert ingestor.run_once(now=NOW) == {"published": 0, "fresh": 0, "failed": 0}
    assert nomads.requests == []
