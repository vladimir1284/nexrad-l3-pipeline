import json

import pytest

from ingest.phenomena.parse import parse_file
from ingest.storage.publish import publish_phenomena
from tests.conftest import DATA_DIR
from tests.test_publish import SqliteD1


def test_nst_golden_amx():
    p = parse_file(DATA_DIR / "AMX_NST_2026_07_10_04_57_17")

    assert (p.site_id, p.code, p.mnemonic) == ("AMX", 58, "NST")
    assert len(p.records) == 5
    assert [r.cell_id for r in p.records] == ["A8", "Z8", "X8", "N8", "B9"]
    a8 = p.records[0]
    assert a8.kind == "storm_cell"
    # el azimut geométrico (de x/y del Symbology) debe cuadrar con el
    # AZRAN de la página tabular — dos fuentes independientes
    assert a8.attrs["azran_nm"] == [231, 112]
    assert a8.azimuth_deg == pytest.approx(231, abs=1)
    assert a8.range_km == pytest.approx(112 * 1.852, rel=0.01)
    assert a8.attrs["movement_deg"] == 130
    assert a8.attrs["movement_kt"] == 34
    # B9 es celda nueva: sin movimiento
    assert p.records[4].attrs.get("new") is True
    # posiciones dentro del alcance del radar de Miami
    for r in p.records:
        assert 22 < r.lat < 29 and -84 < r.lon < -77
        assert r.range_km < 460


def test_nmd_golden_ict():
    p = parse_file(DATA_DIR / "ICT_NMD_2026_07_10_05_07_19")

    assert (p.site_id, p.code, p.mnemonic) == ("ICT", 141, "NMD")
    assert len(p.records) == 5
    assert [r.cell_id for r in p.records] == ["286", "289", "377", "566", "807"]
    m = p.records[0]
    assert m.kind == "meso"
    assert m.attrs["radius_km"] == 2.0
    assert m.attrs["storm_id"] == "Y9"
    assert m.attrs["strength_rank"] == 6
    assert m.attrs["low_level_rv_kt"] == 31
    assert m.attrs["depth_kft"] == 20
    assert m.attrs["tvs"] is False
    assert m.attrs["msi"] == 2636
    assert m.attrs["movement_deg"] == 294
    # la fila 566 no trae movimiento: el regex no debe romperse
    assert "movement_deg" not in p.records[3].attrs
    assert m.azimuth_deg == pytest.approx(310, abs=1)


def test_productos_vacios_dan_cero_registros():
    for name in ["AMX_NMD_2026_07_10_04_57_17", "JUA_NST_2026_07_09_20_26_30"]:
        assert parse_file(DATA_DIR / name).records == []


def test_publish_phenomena_idempotente():
    d1 = SqliteD1()
    php = parse_file(DATA_DIR / "ICT_NMD_2026_07_10_05_07_19")

    n = publish_phenomena(php, d1)
    n2 = publish_phenomena(php, d1)  # republicar no duplica

    assert n == n2 == 5
    rows = d1.execute("SELECT * FROM phenomena ORDER BY id")
    assert len(rows) == 5
    assert rows[0]["kind"] == "meso"
    assert rows[0]["cell_id"] == "286"
    assert rows[0]["site_id"] == "ICT"
    assert rows[0]["vol_time"] == "2026-07-10T05:07:19"
    attrs = json.loads(rows[0]["attrs"])
    assert attrs["msi"] == 2636

    radar = d1.execute("SELECT * FROM radars")[0]
    assert radar["site_id"] == "ICT"  # catálogo poblado también por fenómenos
    producto = d1.execute("SELECT * FROM products WHERE code = 141")[0]
    assert producto["kind"] == "phenomena"


def test_publish_phenomena_producto_vacio_borra_lo_previo():
    d1 = SqliteD1()
    php = parse_file(DATA_DIR / "ICT_NMD_2026_07_10_05_07_19")
    publish_phenomena(php, d1)

    vacio = parse_file(DATA_DIR / "AMX_NMD_2026_07_10_04_57_17")
    publish_phenomena(vacio, d1)

    # el volumen de ICT sigue; el vacío de AMX no insertó nada
    assert d1.execute("SELECT COUNT(*) AS n FROM phenomena")[0]["n"] == 5
