from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent / "data"

# Muestras reales del bucket público unidata-nexrad-level3, commiteadas
# para que CI no dependa de la red. Goldens capturados con MetPy 1.7.x.
SAMPLES = {
    "AMX": DATA_DIR / "AMX_N0B_2026_07_06_15_45_17",
    "JUA": DATA_DIR / "JUA_N0B_2026_07_06_15_43_47",
}


@pytest.fixture(params=sorted(SAMPLES))
def site(request):
    return request.param


@pytest.fixture
def sample_path(site):
    return SAMPLES[site]
