import hashlib

import numpy as np
import pytest

from ingest.decoder.level3 import decode_file
from ingest.gridding.aeqd import grid_radial
from tests.conftest import DATA_DIR

# (fichero, grid_size, cell_m, sha16, argmax, nonzero>1)
GOLDEN = [
    ("AMX_N0B_2026_07_06_15_45_17", 3680, 250.0, "c5e1035ddb849ab7", (2059, 2513), 411848),
    ("JUA_N0B_2026_07_06_15_43_47", 3680, 250.0, "9a0354315ede27eb", (2726, 1929), 292108),
    ("AMX_N0G_2026_07_10_05_03_18", 2400, 250.0, "f3e82cf2b566ffe4", (1131, 1211), 102247),
    ("AMX_EET_2026_07_10_04_57_17", 692, 1000.0, "b44ec07b381e2066", (488, 198), 5900),
    ("AMX_DVL_2026_07_10_04_57_17", 920, 1000.0, "fdc8461f222b5eaa", (589, 295), 14686),
    ("AMX_DAA_2026_07_10_04_57_17", 1840, 250.0, "e410c6384b7062c3", (1455, 325), 154936),
    ("AMX_DU3_2026_07_10_04_09_39", 1840, 250.0, "1b77d62c42c9f80f", (995, 886), 189119),
    ("AMX_DTA_2026_07_10_04_57_17", 1840, 250.0, "61e9cc628c1b411e", (995, 886), 169293),
]


@pytest.mark.parametrize(
    "fname,size,cell,sha16,argmax,nonzero",
    GOLDEN,
    ids=[g[0].split("_")[1] + "-" + g[0][:3] for g in GOLDEN],
)
def test_grid_golden(fname, size, cell, sha16, argmax, nonzero):
    prod = decode_file(DATA_DIR / fname)
    grid = grid_radial(prod)

    # Celda = gate nativo, extensión = rango nativo.
    assert grid.size == size == 2 * prod.n_gates
    assert grid.data.shape == (size, size)
    assert grid.data.dtype.name == "uint8"
    assert grid.cell_m == cell == prod.spec.gate_width_m
    assert grid.half_extent_m == prod.max_range_m
    assert f"+proj=aeqd +lat_0={prod.lat} +lon_0={prod.lon}" in grid.proj4

    assert hashlib.sha256(grid.data.tobytes()).hexdigest()[:16] == sha16
    assert int((grid.data > 1).sum()) == nonzero
    r, c = argmax
    assert int(grid.data[r, c]) == int(grid.data.max()) == int(prod.levels.max())


@pytest.mark.parametrize("fname", [g[0] for g in GOLDEN[:3]])
def test_grid_pixel_matches_polar(fname):
    """El pixel del máximo coincide con el valor polar del (radial, gate)
    más cercano, calculado por una vía independiente."""
    prod = decode_file(DATA_DIR / fname)
    grid = grid_radial(prod)
    r, c = np.unravel_index(np.argmax(grid.data), grid.data.shape)

    x = -grid.half_extent_m + (c + 0.5) * grid.cell_m
    y = grid.half_extent_m - (r + 0.5) * grid.cell_m
    rng = float(np.hypot(x, y))
    az = float(np.degrees(np.arctan2(x, y)) % 360)

    az_center = (prod.az_start + prod.az_end) / 2 % 360
    ridx = int(np.argmin(np.abs(az_center - az)))
    gidx = int(rng / prod.spec.gate_width_m)

    assert int(prod.levels[ridx, gidx]) == int(grid.data[r, c])
