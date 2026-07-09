from datetime import UTC, datetime

from ingest.monitor import check_site
from tests.test_publish import SqliteD1
from tests.test_retention import FakeR2Store, _seed_raster

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


def test_sitio_fresco_ok():
    d1 = SqliteD1()
    _seed_raster(d1, "AMX", "2026-07-09T11:50:00", "AMX/x.tif")
    r2 = FakeR2Store({"AMX/x.tif": 100})

    s = check_site(d1, r2, "AMX", max_age_min=30, now=NOW)

    assert s.fresh
    assert s.reason == "ok"
    assert round(s.age_min) == 10


def test_sitio_sin_datos():
    s = check_site(SqliteD1(), FakeR2Store(), "AMX", now=NOW)
    assert not s.fresh
    assert s.reason == "sin datos"


def test_sitio_viejo():
    d1 = SqliteD1()
    _seed_raster(d1, "AMX", "2026-07-09T10:00:00", "AMX/x.tif")
    s = check_site(d1, FakeR2Store({"AMX/x.tif": 100}), "AMX", max_age_min=30, now=NOW)
    assert not s.fresh
    assert "viejo" in s.reason


def test_fila_sin_objeto_r2_no_es_fresco():
    d1 = SqliteD1()
    _seed_raster(d1, "AMX", "2026-07-09T11:50:00", "AMX/x.tif")
    s = check_site(d1, FakeR2Store(), "AMX", max_age_min=30, now=NOW)
    assert not s.fresh
    assert s.reason == "falta objeto R2"


def test_usa_el_raster_mas_reciente():
    d1 = SqliteD1()
    _seed_raster(d1, "AMX", "2026-07-09T09:00:00", "AMX/viejo.tif")
    _seed_raster(d1, "AMX", "2026-07-09T11:55:00", "AMX/nuevo.tif")
    r2 = FakeR2Store({"AMX/nuevo.tif": 100})  # el viejo ni existe en R2

    s = check_site(d1, r2, "AMX", max_age_min=30, now=NOW)

    assert s.fresh
