"""Claves R2 y el prefijo opcional de aislamiento (`R2_KEY_PREFIX`).

El prefijo existe para que varias instancias compartan bucket sin
pisarse (LAMULA Jobs: una instancia por job). Lo que se verifica aquí es
el contrato del que depende todo lo demás: sin prefijo las claves son
EXACTAMENTE las de siempre (producción no cambia), y con prefijo el
mosaico sigue siendo distinguible por su propio discriminante
(`{prefijo}mosaic/`), que es lo que el sweep del Worker `ops` usa para no
borrar COG compuestos cuando `api/` todavía no conoce la tabla.
"""

from datetime import datetime

from ingest.lightning import lightning_key
from ingest.storage.keys import mosaic_key, normalize_prefix, raster_key
from ingest.wind import wind_key

VOL = datetime(2026, 7, 6, 15, 45, 17)


def test_normalize_prefix():
    assert normalize_prefix(None) == ""
    assert normalize_prefix("") == ""
    assert normalize_prefix("   ") == ""
    assert normalize_prefix("jobs/42") == "jobs/42/"
    assert normalize_prefix("/jobs/42/") == "jobs/42/"
    # idempotente: da igual si el valor ya venía canónico de StorageConfig
    assert normalize_prefix(normalize_prefix("jobs/42")) == "jobs/42/"


def test_sin_prefijo_las_claves_no_cambian():
    assert raster_key("AMX", "N0B", VOL) == "AMX/N0B/2026/07/06/AMX_N0B_20260706_154517.tif"
    assert (
        mosaic_key("GULF", "N0B", VOL) == "mosaic/GULF/N0B/2026/07/06/GULF_N0B_20260706_154517.tif"
    )
    assert (
        wind_key("AMX", datetime(2026, 7, 18, 12), datetime(2026, 7, 18, 6), 6, "10m")
        == "AMX/WIND/2026/07/18/AMX_WIND_20260718_120000_c2026071806f006_10m.json"
    )
    assert (
        lightning_key("AMX", datetime(2026, 7, 20, 13, 5))
        == "AMX/LIGHTNING/2026/07/20/AMX_LTG_20260720_130500.json"
    )


def test_prefijo_antepuesto_en_las_cuatro_familias():
    assert (
        raster_key("AMX", "N0B", VOL, "jobs/42")
        == "jobs/42/AMX/N0B/2026/07/06/AMX_N0B_20260706_154517.tif"
    )
    assert (
        mosaic_key("GULF", "N0B", VOL, "jobs/42")
        == "jobs/42/mosaic/GULF/N0B/2026/07/06/GULF_N0B_20260706_154517.tif"
    )
    assert wind_key(
        "AMX", datetime(2026, 7, 18, 12), datetime(2026, 7, 18, 6), 6, "10m", "jobs/42"
    ).startswith("jobs/42/AMX/WIND/")
    assert lightning_key("AMX", datetime(2026, 7, 20, 13, 5), "jobs/42").startswith(
        "jobs/42/AMX/LIGHTNING/"
    )


def test_prefijo_sin_normalizar_da_la_misma_clave():
    assert raster_key("AMX", "N0B", VOL, "/jobs/42/") == raster_key("AMX", "N0B", VOL, "jobs/42")


def test_el_mosaico_sigue_distinguible_con_prefijo():
    # El sweep de `ops` separa COG compuestos de COG por radar por este
    # discriminante; con prefijo pasa a ser `{prefijo}mosaic/`.
    prefix = "jobs/42/"
    assert mosaic_key("GULF", "N0B", VOL, prefix).startswith(f"{prefix}mosaic/")
    assert not raster_key("AMX", "N0B", VOL, prefix).startswith(f"{prefix}mosaic/")


def test_dos_instancias_no_colisionan():
    a = raster_key("AMX", "N0B", VOL, "jobs/1")
    b = raster_key("AMX", "N0B", VOL, "jobs/2")
    assert a != b
