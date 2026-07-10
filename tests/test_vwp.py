from ingest.phenomena.vwp import parse_vwp_file
from ingest.storage.publish import publish_vwp
from tests.conftest import DATA_DIR
from tests.test_publish import SqliteD1

SAMPLE = DATA_DIR / "AMX_NVW_2026_07_10_04_57_17"


def test_parse_nvw_golden():
    v = parse_vwp_file(SAMPLE)

    assert v.site_id == "AMX"
    assert v.vol_time.isoformat() == "2026-07-10T04:57:17"
    assert len(v.levels) == 13
    first = v.levels[0]
    assert first.height_ft == 400
    assert first.wind_dir_deg == 91.0
    assert first.wind_speed_kt == 8.0
    assert first.rms_kt == 5.8
    # alturas crecientes y direcciones válidas
    alturas = [lv.height_ft for lv in v.levels]
    assert alturas == sorted(alturas)
    assert all(0 <= lv.wind_dir_deg <= 360 for lv in v.levels)
    assert v.levels[-1].height_ft == 6000


def test_publish_vwp_idempotente():
    d1 = SqliteD1()
    v = parse_vwp_file(SAMPLE)

    n = publish_vwp(v, d1)
    n2 = publish_vwp(v, d1)

    assert n == n2 == 13
    rows = d1.execute("SELECT * FROM vwp ORDER BY height_ft")
    assert len(rows) == 13
    assert rows[0]["height_ft"] == 400
    assert rows[0]["wind_dir_deg"] == 91.0
    assert rows[0]["wind_speed_kt"] == 8.0
    assert d1.execute("SELECT kind FROM products WHERE code = 48")[0]["kind"] == "vwp"
    assert d1.execute("SELECT site_id FROM radars")[0]["site_id"] == "AMX"


def test_watcher_enruta_nvw(tmp_path):
    import json
    import shutil

    from ingest.watcher import ProductProcessor, run_watcher

    d = tmp_path / "incoming"
    d.mkdir()
    shutil.copy(SAMPLE, d / SAMPLE.name)

    stats = run_watcher(d, ProductProcessor(output_dir=tmp_path / "out"), once=True)

    assert stats.processed == 1
    levels = json.loads((tmp_path / "out" / "AMX_NVW_20260710_045717.json").read_text())
    assert len(levels) == 13
    assert levels[0]["height_ft"] == 400
