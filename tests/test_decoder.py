import hashlib
from datetime import datetime

from ingest.decoder.level3 import decode_file

GOLDEN = {
    "AMX": {
        "lat": 25.611,
        "lon": -80.413,
        "height_m": 111.0,
        "vcp": 212,
        "vol_time": datetime(2026, 7, 6, 15, 45, 17),
        "levels_sha256": "50c8b67e6ba03d7f",
        "levels_max": 187,
        "argmax": (332, 708),
    },
    "JUA": {
        "lat": 18.116,
        "lon": -66.078,
        "height_m": 2958.0,
        "vcp": 35,
        "vol_time": datetime(2026, 7, 6, 15, 43, 47),
        "levels_sha256": "b46fbb1b34206f54",
        "levels_max": 155,
        "argmax": (464, 891),
    },
}


def test_decode_n0b_golden(site, sample_path):
    g = GOLDEN[site]
    p = decode_file(sample_path)

    assert p.site_id == site
    assert p.spec.code == 153
    assert p.spec.mnemonic == "N0B"
    assert p.lat == g["lat"]
    assert p.lon == g["lon"]
    assert p.height_m == g["height_m"]
    assert p.vcp == g["vcp"]
    assert p.el_angle == 0.5
    assert p.vol_time == g["vol_time"]

    # Geometría nativa N0B: 720 radiales × 0.5°, 1840 gates × 250 m = 460 km.
    assert p.levels.shape == (720, 1840)
    assert p.levels.dtype.name == "uint8"
    assert p.n_radials == 720
    assert p.n_gates == 1840
    assert p.max_range_m == 460_000.0
    assert p.az_start.shape == (720,)

    # Mapeo físico: thr1=-320, thr2=5 → físico = nivel·0.5 - 33 (niveles >= 2).
    assert p.scale == 0.5
    assert p.offset == -33.0

    assert hashlib.sha256(p.levels.tobytes()).hexdigest()[:16] == g["levels_sha256"]
    assert int(p.levels.max()) == g["levels_max"]
    r, c = g["argmax"]
    assert int(p.levels[r, c]) == g["levels_max"]
