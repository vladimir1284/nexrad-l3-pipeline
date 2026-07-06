import hashlib

import numpy as np

from ingest.decoder.level3 import decode_file
from ingest.gridding.aeqd import grid_radial

GOLDEN = {
    "AMX": {
        "grid_sha256": "830df53b9bf1f53a",
        "argmax": (2059, 2512),
        "max": 187,
        "nonzero": 411053,
        "proj4_frag": "+proj=aeqd +lat_0=25.611 +lon_0=-80.413",
    },
    "JUA": {
        "grid_sha256": "3fd4c2d11992452c",
        "argmax": (2725, 1930),
        "max": 155,
        "nonzero": 291579,
        "proj4_frag": "+proj=aeqd +lat_0=18.116 +lon_0=-66.078",
    },
}


def test_grid_n0b_golden(site, sample_path):
    g = GOLDEN[site]
    prod = decode_file(sample_path)
    grid = grid_radial(prod)

    # Celda = gate nativo, extensión = rango nativo: 3680×3680 @ 250 m.
    assert grid.size == 3680
    assert grid.data.shape == (3680, 3680)
    assert grid.data.dtype.name == "uint8"
    assert grid.cell_m == 250.0
    assert grid.half_extent_m == 460_000.0
    assert g["proj4_frag"] in grid.proj4

    assert hashlib.sha256(grid.data.tobytes()).hexdigest()[:16] == g["grid_sha256"]
    assert int(grid.data.max()) == g["max"]
    assert int((grid.data > 1).sum()) == g["nonzero"]
    r, c = g["argmax"]
    assert int(grid.data[r, c]) == g["max"]


def test_grid_pixel_matches_polar(site, sample_path):
    # El pixel del máximo debe coincidir con el valor polar del
    # (radial, gate) más cercano, calculado por una vía independiente.
    prod = decode_file(sample_path)
    grid = grid_radial(prod)
    r, c = np.unravel_index(np.argmax(grid.data), grid.data.shape)

    x = -grid.half_extent_m + (c + 0.5) * grid.cell_m
    y = grid.half_extent_m - (r + 0.5) * grid.cell_m
    rng = float(np.hypot(x, y))
    az = float(np.degrees(np.arctan2(x, y)) % 360)

    az_center = (prod.az_start + prod.az_end) / 2 % 360
    ridx = int(np.argmin(np.abs(az_center - az)))
    gidx = int((rng / prod.spec.gate_width_m - prod.first_gate) / prod.gate_scale)

    assert int(prod.levels[ridx, gidx]) == int(grid.data[r, c])
