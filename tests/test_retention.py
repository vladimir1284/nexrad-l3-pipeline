from datetime import UTC, datetime

from ingest.retention.sweep import reconcile, sweep
from tests.test_publish import SqliteD1

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)


class FakeR2Store:
    """R2 mínimo respaldado por un dict clave→tamaño."""

    def __init__(self, keys: dict[str, int] | None = None):
        self.store = dict(keys or {})

    def list_keys(self, prefix=""):
        return sorted(k for k in self.store if k.startswith(prefix))

    def delete_keys(self, keys):
        for k in keys:
            self.store.pop(k, None)

    def head(self, key):
        return {"ContentLength": self.store[key]} if key in self.store else None


def _seed_raster(d1, site, vol_iso, key):
    d1.execute(
        "INSERT OR IGNORE INTO radars VALUES (?, NULL, 0, 0, 0, 'p4', ?, ?)",
        [site, vol_iso, vol_iso],
    )
    d1.execute("INSERT OR IGNORE INTO products VALUES (153, 'N0B', 'dBZ', 'raster')")
    d1.execute(
        "INSERT INTO rasters (site_id, product_code, vol_time, r2_key, size_bytes,"
        " value_scale, value_offset, width, height, cell_m, created_at)"
        " VALUES (?, 153, ?, ?, 100, 0.5, -33, 10, 10, 250, ?)",
        [site, vol_iso, key, vol_iso],
    )


def test_sweep_borra_solo_fuera_de_ventana():
    d1, r2 = SqliteD1(), FakeR2Store()
    vieja = "2026-07-06T11:00:00"  # 73 h antes de NOW
    fresca = "2026-07-09T11:00:00"  # 1 h antes
    for vol, key in ((vieja, "AMX/old.tif"), (fresca, "AMX/new.tif")):
        _seed_raster(d1, "AMX", vol, key)
        r2.store[key] = 100

    report = sweep(d1, r2, window_hours=72, now=NOW)

    assert report.rasters_deleted == 1
    assert "AMX/old.tif" not in r2.store
    assert "AMX/new.tif" in r2.store
    rows = d1.execute("SELECT r2_key FROM rasters")
    assert [r["r2_key"] for r in rows] == ["AMX/new.tif"]


def test_sweep_barre_phenomena_y_vwp():
    d1, r2 = SqliteD1(), FakeR2Store()
    _seed_raster(d1, "AMX", "2026-07-09T11:00:00", "AMX/new.tif")
    d1.execute(
        "INSERT INTO phenomena (site_id, product_code, vol_time, kind, lat, lon, created_at)"
        " VALUES ('AMX', 153, '2026-07-05T00:00:00', 'hail', 25.0, -80.0, '2026-07-05T00:00:00')"
    )
    d1.execute(
        "INSERT INTO vwp (site_id, vol_time, height_ft, wind_dir_deg, wind_speed_kt, created_at)"
        " VALUES ('AMX', '2026-07-05T00:00:00', 1000, 180, 20, '2026-07-05T00:00:00')"
    )

    report = sweep(d1, r2, window_hours=72, now=NOW)

    assert report.phenomena_deleted == 1
    assert report.vwp_deleted == 1
    assert d1.execute("SELECT COUNT(*) AS n FROM phenomena")[0]["n"] == 0
    assert d1.execute("SELECT COUNT(*) AS n FROM vwp")[0]["n"] == 0


def test_reconcile_detecta_huerfanos_y_colgantes():
    d1 = SqliteD1()
    _seed_raster(d1, "AMX", "2026-07-09T11:00:00", "AMX/con-objeto.tif")
    _seed_raster(d1, "AMX", "2026-07-09T11:05:00", "AMX/sin-objeto.tif")
    r2 = FakeR2Store({"AMX/con-objeto.tif": 100, "AMX/huerfano.tif": 50})

    report = reconcile(d1, r2)

    assert report.r2_orphans == ["AMX/huerfano.tif"]
    assert report.dangling_rows == ["AMX/sin-objeto.tif"]
    # sin fix, nada cambia
    assert "AMX/huerfano.tif" in r2.store
    assert d1.execute("SELECT COUNT(*) AS n FROM rasters")[0]["n"] == 2


def test_reconcile_fix_limpia():
    d1 = SqliteD1()
    _seed_raster(d1, "AMX", "2026-07-09T11:00:00", "AMX/con-objeto.tif")
    _seed_raster(d1, "AMX", "2026-07-09T11:05:00", "AMX/sin-objeto.tif")
    r2 = FakeR2Store({"AMX/con-objeto.tif": 100, "AMX/huerfano.tif": 50})

    reconcile(d1, r2, fix=True)
    followup = reconcile(d1, r2)

    assert followup.r2_orphans == []
    assert followup.dangling_rows == []
    assert list(r2.store) == ["AMX/con-objeto.tif"]
    rows = d1.execute("SELECT r2_key FROM rasters")
    assert [r["r2_key"] for r in rows] == ["AMX/con-objeto.tif"]
