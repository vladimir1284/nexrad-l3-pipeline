import hashlib
from datetime import datetime

import pytest

from ingest.decoder.level3 import decode_file
from tests.conftest import DATA_DIR

# Goldens capturados con MetPy 1.7.x sobre muestras reales del bucket.
# físico = nivel·scale + offset (niveles >= 2). scale/offset de DAA/DU3
# vienen de float32 del PDB → se comparan con tolerancia.
GOLDEN = [
    # (fichero, mnemo, shape, scale, offset, unit, el, vol_time, sha16, lv_max)
    (
        "AMX_N0B_2026_07_06_15_45_17",
        "N0B",
        (720, 1840),
        0.5,
        -33.0,
        "dBZ",
        0.5,
        datetime(2026, 7, 6, 15, 45, 17),
        "50c8b67e6ba03d7f",
        187,
    ),
    (
        "JUA_N0B_2026_07_06_15_43_47",
        "N0B",
        (720, 1840),
        0.5,
        -33.0,
        "dBZ",
        0.5,
        datetime(2026, 7, 6, 15, 43, 47),
        "b46fbb1b34206f54",
        155,
    ),
    (
        "AMX_N0G_2026_07_10_05_03_18",
        "N0G",
        (720, 1200),
        0.5,
        -64.5,
        "kt",
        0.5,
        datetime(2026, 7, 10, 5, 3, 18),
        "87a516dff2b1cbc8",
        197,
    ),
    (
        "AMX_EET_2026_07_10_04_57_17",
        "EET",
        (360, 346),
        1.0,
        -2.0,
        "kft",
        None,
        datetime(2026, 7, 10, 4, 57, 17),
        "92effa42cac4469a",
        40,
    ),
    (
        "AMX_DVL_2026_07_10_04_57_17",
        "DVL",
        (360, 460),
        0.35,
        -0.7,
        "kg/m2",
        None,
        datetime(2026, 7, 10, 4, 57, 17),
        "9952ecb684e9d767",
        88,
    ),
    (
        "AMX_DAA_2026_07_10_04_57_17",
        "DAA",
        (360, 920),
        0.0734,
        -0.048,
        "mm",
        None,
        datetime(2026, 7, 10, 4, 57, 17),
        "ccbf4e62f7bc4a0a",
        255,
    ),
    (
        "AMX_DU3_2026_07_10_04_09_39",
        "DU3",
        (360, 920),
        0.0932,
        -0.0678,
        "mm",
        None,
        datetime(2026, 7, 10, 4, 9, 39),
        "409a5cc68ad1be11",
        255,
    ),
    (
        "AMX_DTA_2026_07_10_04_57_17",
        "DTA",
        (360, 920),
        0.254,
        0.0,
        "mm",
        None,
        datetime(2026, 7, 10, 4, 57, 17),
        "9bc638e73542444a",
        93,
    ),
]


@pytest.mark.parametrize(
    "fname,mnemo,shape,scale,offset,unit,el,vol,sha16,lv_max",
    GOLDEN,
    ids=[g[1] + "-" + g[0][:3] for g in GOLDEN],
)
def test_decode_golden(fname, mnemo, shape, scale, offset, unit, el, vol, sha16, lv_max):
    p = decode_file(DATA_DIR / fname)

    assert p.spec.mnemonic == mnemo
    assert p.site_id == fname[:3]
    assert p.levels.shape == shape
    assert p.levels.dtype.name == "uint8"
    assert p.scale == pytest.approx(scale, rel=1e-3)
    assert p.offset == pytest.approx(offset, rel=1e-3, abs=1e-3)
    assert p.spec.unit == unit
    assert p.el_angle == el
    assert p.vol_time == vol
    assert hashlib.sha256(p.levels.tobytes()).hexdigest()[:16] == sha16
    assert int(p.levels.max()) == lv_max
    assert p.az_start.shape == (shape[0],)
    assert p.max_range_m == shape[1] * p.spec.gate_width_m


def test_fisica_plausible_por_producto():
    """El máximo físico de cada muestra cae en el rango del producto."""
    rangos = {
        "N0B": (0, 95),  # dBZ
        "N0G": (-100, 100),  # kt
        "EET": (0, 70),  # kft
        "DVL": (0, 80),  # kg/m²
        "DAA": (0, 100),  # mm en 1 h
        "DU3": (0, 300),
        "DTA": (0, 500),
    }
    for g in GOLDEN:
        p = decode_file(DATA_DIR / g[0])
        datos = p.levels[p.levels >= 2]
        assert datos.size, g[0]
        fis = float(datos.max()) * p.scale + p.offset
        lo, hi = rangos[p.spec.mnemonic]
        assert lo <= fis <= hi, f"{g[0]}: {fis} fuera de [{lo},{hi}]"
