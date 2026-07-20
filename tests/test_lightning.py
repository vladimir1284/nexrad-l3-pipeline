"""Tests del módulo de rayos GLM (ingest/lightning.py).

Sin red: los ficheros GLM-L2-LCFA sintéticos se generan con h5py replicando
el layout real (`make_glm_file`); el D1 falso es SQLite real con las
migraciones de db/ — valida la sintaxis del INSERT y el schema
lightning_buckets a la vez. Un fichero real commiteado
(`tests/data/GLM_LCFA_2026_07_20_13_00_00.nc`) cubre el parse contra el
formato real vía golden test.
"""

import io
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import h5py
import numpy as np
import pytest

from ingest.lightning import (
    BUCKET_S,
    FRAMES_PER_BUCKET,
    Flash,
    LightningIngestor,
    eligible_bucket_starts,
    encode_bucket_json,
    frames_for_bucket,
    glm_hour_prefixes,
    glm_key_start_epoch,
    haversine_km,
    lightning_key,
    parse_glm,
    parse_s3_list_keys,
    parse_units_base,
    strikes_for_site,
)

MIGRATIONS = sorted((Path(__file__).parent.parent / "db" / "migrations").glob("*.sql"))
GLM_SAMPLE = Path(__file__).parent / "data" / "GLM_LCFA_2026_07_20_13_00_00.nc"

AMX_LAT, AMX_LON = 25.6111, -80.4128
JUA_LAT, JUA_LON = 18.1156, -66.0781


class SqliteD1:
    def __init__(self, sites=(("AMX", AMX_LAT, AMX_LON),)):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        for migration in MIGRATIONS:
            self.conn.executescript(migration.read_text())
        for site_id, lat, lon in sites:
            self.conn.execute(
                "INSERT INTO radars (site_id, icao, lat, lon, height_m, proj4,"
                " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    site_id,
                    f"K{site_id}",
                    lat,
                    lon,
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

    def upload_bytes(self, data, key, content_type):
        assert content_type == "application/json"
        self.objects[key] = data


def make_glm_file(
    records: list[tuple[float, float, int, int]],
    *,
    base: datetime,
    scale: float = 0.00038148,
    offset: float = -5.0,
) -> bytes:
    """Fichero GLM-L2-LCFA sintético (formato real, ver spike 2026-07-20).

    `records`: (lon, lat, raw_uint16_offset, quality_flag). El raw se
    guarda con el bit pattern de un uint16 reinterpretado como int16 —
    igual que los ficheros reales cuando el valor supera 32767 — para
    ejercitar la máscara `_Unsigned` en el parse.
    """
    lons = np.array([r[0] for r in records], dtype=np.float32)
    lats = np.array([r[1] for r in records], dtype=np.float32)
    raw_u16 = np.array([r[2] % 65536 for r in records], dtype=np.uint16)
    raw_i16 = raw_u16.view(np.int16)
    qf = np.array([r[3] for r in records], dtype=np.int16)

    buf = io.BytesIO()
    with h5py.File(buf, "w") as f:
        f.create_dataset("flash_lon", data=lons)
        f.create_dataset("flash_lat", data=lats)
        toff = f.create_dataset("flash_time_offset_of_first_event", data=raw_i16)
        toff.attrs["scale_factor"] = np.array([scale], dtype=np.float32)
        toff.attrs["add_offset"] = np.array([offset], dtype=np.float32)
        toff.attrs["units"] = np.bytes_(f"seconds since {base:%Y-%m-%d %H:%M:%S}.000".encode())
        toff.attrs["_Unsigned"] = np.bytes_(b"true")
        f.create_dataset("flash_quality_flag", data=qf)
    return buf.getvalue()


def raw_for_offset(offset_s: float, scale: float = 0.00038148, add_offset: float = -5.0) -> int:
    """uint16 crudo que produce `offset_s` tras `raw*scale + add_offset`."""
    return round((offset_s - add_offset) / scale)


# --------------------------------------------------------------- geometría


def test_eligible_bucket_starts_ventana_y_margen():
    now = datetime(2026, 7, 20, 13, 10, 0)
    starts = eligible_bucket_starts(now, window_s=900, margin_s=90)
    # 13:10:00 - 90s margen - 300s bucket = 13:04:30 -> piso de cubo 13:00:00
    assert starts[0] == datetime(2026, 7, 20, 13, 0, 0)
    assert starts == sorted(starts, reverse=True)
    assert all(
        (starts[i] - starts[i + 1]).total_seconds() == BUCKET_S for i in range(len(starts) - 1)
    )


def test_lightning_key_formato():
    key = lightning_key("AMX", datetime(2026, 7, 20, 13, 5, 0))
    assert key == "AMX/LIGHTNING/2026/07/20/AMX_LTG_20260720_130500.json"


def test_glm_hour_prefixes_un_prefijo_normal():
    prefixes = glm_hour_prefixes(datetime(2026, 7, 20, 13, 0, 0))
    assert prefixes == ["GLM-L2-LCFA/2026/201/13/"]


def test_glm_hour_prefixes_dos_prefijos_en_cubo_55():
    prefixes = glm_hour_prefixes(datetime(2026, 7, 20, 13, 55, 0))
    assert prefixes == ["GLM-L2-LCFA/2026/201/13/", "GLM-L2-LCFA/2026/201/14/"]


def test_glm_key_start_epoch():
    from ingest.lightning import _epoch

    key = "GLM-L2-LCFA/2026/201/13/OR_GLM-L2-LCFA_G19_s20262011300000_e20262011300200_c1.nc"
    assert glm_key_start_epoch(key) == _epoch(datetime(2026, 7, 20, 13, 0, 0))


def test_glm_key_start_epoch_no_matchea():
    assert glm_key_start_epoch("basura.nc") is None


def test_parse_s3_list_keys():
    xml = "<a><Key>x1.nc</Key><Key>x2.nc</Key><IsTruncated>false</IsTruncated></a>"
    keys, truncated = parse_s3_list_keys(xml)
    assert keys == ["x1.nc", "x2.nc"]
    assert truncated is False


def test_frames_for_bucket_incluye_frame_extra_en_bucket_end():
    start = datetime(2026, 7, 20, 13, 0, 0)
    keys = [
        "..._s20262011259400_e1_c1.nc",  # 12:59:40, fuera (< start)
        "..._s20262011300000_e1_c1.nc",  # 13:00:00, dentro
        "..._s20262011305000_e1_c1.nc",  # 13:05:00 = start+300, inclusive
        "..._s20262011305200_e1_c1.nc",  # 13:05:20, fuera
    ]
    frames = frames_for_bucket(keys, start)
    assert frames == keys[1:3]


def test_haversine_km_mismo_punto_es_cero():
    assert haversine_km(AMX_LAT, AMX_LON, AMX_LAT, AMX_LON) == 0.0


def test_strikes_for_site_filtra_radio_y_ventana_y_ordena():
    from ingest.lightning import _epoch

    start = datetime(2026, 7, 20, 13, 0, 0)
    cerca = Flash(lon=AMX_LON + 0.01, lat=AMX_LAT, epoch_s=_epoch(start) + 100.04)
    lejos = Flash(lon=AMX_LON + 20, lat=AMX_LAT, epoch_s=_epoch(start) + 10)
    fuera_ventana = Flash(lon=AMX_LON, lat=AMX_LAT, epoch_s=_epoch(start) + BUCKET_S)  # exclusivo
    strikes = strikes_for_site([lejos, cerca, fuera_ventana], AMX_LAT, AMX_LON, 460.0, start)
    assert len(strikes) == 1
    assert strikes[0][2] == 100.0  # 1 decimal, redondeado


def test_strikes_for_site_clava_offset_bajo_bucket_s():
    from ingest.lightning import _epoch

    start = datetime(2026, 7, 20, 13, 0, 0)
    casi_al_borde = Flash(lon=AMX_LON, lat=AMX_LAT, epoch_s=_epoch(start) + BUCKET_S - 0.04)
    strikes = strikes_for_site([casi_al_borde], AMX_LAT, AMX_LON, 460.0, start)
    assert strikes[0][2] == BUCKET_S - 0.1


def test_parse_units_base():
    from ingest.lightning import _epoch

    base = parse_units_base("seconds since 2026-07-20 13:00:00.000")
    assert base == _epoch(datetime(2026, 7, 20, 13, 0, 0))


def test_encode_bucket_json():
    import json

    body = encode_bucket_json("AMX", datetime(2026, 7, 20, 13, 0, 0), [(-80.1, 25.6, 1.0)])
    doc = json.loads(body)
    assert doc == {
        "site": "AMX",
        "bucket_start": "2026-07-20T13:00:00",
        "bucket_s": 300,
        "strikes": [[-80.1, 25.6, 1.0]],
    }


# ------------------------------------------------------------- parse HDF5


def test_parse_glm_fichero_real_golden():
    data = GLM_SAMPLE.read_bytes()
    flashes = parse_glm(data)
    assert len(flashes) == 148  # solo flash_quality_flag == 0
    # rango de tiempo dentro del fichero (nominal 13:00:00-13:00:02, +/- margen)
    assert all(1784552390 < f.epoch_s < 1784552430 for f in flashes)


def test_parse_glm_mascara_unsigned_valores_grandes():
    """Un raw uint16 > 32767 se lee como int16 negativo — hay que reinterpretar."""
    base = datetime(2026, 1, 1, 0, 0, 0)
    raw = raw_for_offset(15.0)  # ~39337 con scale/offset reales, > 32767
    assert raw > 32767
    data = make_glm_file([(-80.0, 25.0, raw, 0)], base=base)
    flashes = parse_glm(data)
    assert len(flashes) == 1
    from ingest.lightning import _epoch

    assert flashes[0].epoch_s == pytest.approx(_epoch(base) + 15.0, abs=0.01)


def test_parse_glm_filtra_quality_flag():
    base = datetime(2026, 1, 1, 0, 0, 0)
    data = make_glm_file(
        [(-80.0, 25.0, raw_for_offset(1.0), 0), (-80.0, 25.0, raw_for_offset(2.0), 3)],
        base=base,
    )
    flashes = parse_glm(data)
    assert len(flashes) == 1


# ------------------------------------------------------------- ingestor


class ScriptedGlm:
    """Fetcher inyectable: listado S3 + contenido de fichero por clave."""

    def __init__(self):
        self.listings: dict[str, list[str]] = {}
        self.files: dict[str, bytes] = {}
        self.list_calls: list[str] = []
        self.fetch_calls: list[str] = []

    def add_frame(self, key: str, records: list[tuple[float, float, int, int]], base: datetime):
        self.files[key] = make_glm_file(records, base=base)
        for prefix, keys in self.listings.items():
            if key.startswith(prefix):
                keys.append(key)

    def register_prefix(self, prefix: str):
        self.listings.setdefault(prefix, [])

    def list_prefix(self, prefix: str) -> list[str]:
        self.list_calls.append(prefix)
        return list(self.listings.get(prefix, []))

    def fetch_file(self, key: str) -> bytes | None:
        self.fetch_calls.append(key)
        return self.files.get(key)


def frame_key(prefix: str, dt: datetime) -> str:
    return f"{prefix}OR_GLM-L2-LCFA_G19_s{dt:%Y}{dt.timetuple().tm_yday:03d}{dt:%H%M%S}0_e1_c1.nc"


def full_frames(glm: ScriptedGlm, start: datetime, near_site: tuple[float, float] | None):
    """Puebla los FRAMES_PER_BUCKET frames de un cubo; uno con un flash si `near_site`."""
    prefix = glm_hour_prefixes(start)[0]
    glm.register_prefix(prefix)
    for i in range(FRAMES_PER_BUCKET):
        t = start + timedelta(seconds=20 * i)
        records = []
        if near_site is not None and i == 0:
            lat, lon = near_site
            records = [(lon, lat, raw_for_offset(5.0), 0)]
        glm.add_frame(frame_key(prefix, t), records, base=t)


# NOW está alineado a la grilla de 300 s con margen 90 s (default de
# LightningIngestor): el cubo elegible más nuevo es exactamente 13:00:00
# (13:00:00 + 300 + 90 = 13:05:30 <= NOW; 13:05:00 + 300 + 90 = 13:10:30 > NOW).
NOW = datetime(2026, 7, 20, 13, 10, 0)
BUCKET_A = datetime(2026, 7, 20, 13, 0, 0)  # único candidato con window_h=BUCKET_A_WINDOW_H
BUCKET_B = datetime(2026, 7, 20, 12, 55, 0)  # segundo candidato más viejo
BUCKET_A_WINDOW_H = 600 / 3600  # oldest == newest == BUCKET_A (ver derivación arriba)
TWO_BUCKETS_WINDOW_H = 900 / 3600  # oldest == BUCKET_B, newest == BUCKET_A


@pytest.fixture
def env():
    d1, r2, glm = SqliteD1(), FakeR2(), ScriptedGlm()
    ingestor = LightningIngestor(
        d1,
        r2,
        list_prefix=glm.list_prefix,
        fetch_file=glm.fetch_file,
        window_h=BUCKET_A_WINDOW_H,
        margin_s=90,
    )
    return d1, r2, glm, ingestor


def test_run_once_fila_siempre_incluso_sin_rayos(env):
    d1, r2, glm, ingestor = env
    full_frames(glm, BUCKET_A, near_site=None)

    stats = ingestor.run_once(now=NOW)

    rows = d1.execute("SELECT * FROM lightning_buckets")
    assert len(rows) == 1
    assert rows[0]["strike_count"] == 0
    assert rows[0]["r2_key"] is None
    assert rows[0]["size_bytes"] is None
    assert r2.objects == {}
    assert stats["rows"] == 1


def test_run_once_publica_objeto_con_rayos(env):
    d1, r2, glm, ingestor = env
    full_frames(glm, BUCKET_A, near_site=(AMX_LAT, AMX_LON))

    stats = ingestor.run_once(now=NOW)

    rows = d1.execute("SELECT * FROM lightning_buckets")
    assert rows[0]["strike_count"] == 1
    assert rows[0]["r2_key"] is not None
    assert rows[0]["r2_key"] in r2.objects
    assert stats["objects"] == 1


def test_rerun_no_repite_cubos_ya_ingeridos(env):
    d1, r2, glm, ingestor = env
    full_frames(glm, BUCKET_A, near_site=None)
    ingestor.run_once(now=NOW)
    glm.list_calls.clear()
    glm.fetch_calls.clear()

    stats = ingestor.run_once(now=NOW)

    assert stats["rows"] == 0
    assert glm.list_calls == []  # ni siquiera vuelve a listar: ya no hay target
    assert glm.fetch_calls == []


def test_cubo_incompleto_y_fresco_se_difiere(env):
    d1, r2, glm, ingestor = env
    prefix = glm_hour_prefixes(BUCKET_A)[0]
    glm.register_prefix(prefix)
    glm.add_frame(frame_key(prefix, BUCKET_A), [], base=BUCKET_A)  # solo 1/16 frames

    stats = ingestor.run_once(now=NOW)

    assert stats["deferred"] == 1
    assert d1.execute("SELECT COUNT(*) AS n FROM lightning_buckets")[0]["n"] == 0


def test_cubo_incompleto_pero_viejo_se_ingiere_igual(env):
    d1, r2, glm, ingestor = env
    prefix = glm_hour_prefixes(BUCKET_A)[0]
    glm.register_prefix(prefix)
    glm.add_frame(frame_key(prefix, BUCKET_A), [], base=BUCKET_A)  # solo 1/16 frames

    # "now" 2h después de BUCKET_A: su edad (>= DEFER_INCOMPLETE_S) fuerza la
    # ingesta pese a estar incompleto. La ventana amplia trae de paso otros
    # candidatos sin datos que sí son recientes y se difieren legítimamente
    # (comportamiento correcto, no se afirma sobre el total global) — se
    # verifica solo la fila de BUCKET_A.
    muy_tarde = BUCKET_A + timedelta(hours=2)
    ingestor._window_s = (muy_tarde - BUCKET_A).total_seconds()
    ingestor.run_once(now=muy_tarde)

    row = d1.execute(
        "SELECT * FROM lightning_buckets WHERE bucket_start = ?", ["2026-07-20T13:00:00"]
    )[0]
    assert row["strike_count"] == 0


def test_sin_radares_no_hace_nada():
    d1 = SqliteD1(sites=())
    glm = ScriptedGlm()
    ingestor = LightningIngestor(
        d1, FakeR2(), list_prefix=glm.list_prefix, fetch_file=glm.fetch_file
    )
    assert ingestor.run_once(now=NOW) == {
        "buckets": 0,
        "rows": 0,
        "objects": 0,
        "deferred": 0,
        "failed": 0,
    }
    assert glm.list_calls == []


def test_fallo_en_un_cubo_no_aborta_el_resto(env):
    d1, r2, glm, ingestor = env
    ingestor._window_s = TWO_BUCKETS_WINDOW_H * 3600
    full_frames(glm, BUCKET_A, near_site=None)
    full_frames(glm, BUCKET_B, near_site=None)

    real_list = glm.list_prefix
    roto_prefix = glm_hour_prefixes(BUCKET_B)[0]

    def flaky_list(prefix):
        if prefix == roto_prefix:
            raise RuntimeError("S3 500")
        return real_list(prefix)

    ingestor._list_prefix = flaky_list
    stats = ingestor.run_once(now=NOW)

    assert stats["failed"] == 1
    rows = d1.execute("SELECT bucket_start FROM lightning_buckets")
    assert {r["bucket_start"] for r in rows} == {"2026-07-20T13:00:00"}
