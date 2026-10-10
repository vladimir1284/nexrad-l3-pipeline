"""Contraste cruzado de la composición contra wradlib.

wradlib implementa el método de referencia (`wradlib.comp.compose_weighted`),
pero arrastra dask/netCDF4/xradar y sus helpers de calidad asumen volúmenes
polares, no L3 ya grillado. Por eso aquí está solo como dependencia de
desarrollo: la implementación de producción es numpy propio y este test es la
prueba de que coincide con la referencia.

Diferencia de convención, a propósito: wradlib marca "sin dato" con NaN en la
rejilla de radar; nosotros lo marcamos con calidad 0, que además permite
distinguir "no hay dato" de "hay dato pero no vale nada".
"""

import numpy as np
import pytest

from ingest.mosaic.compose import SourceField, compose

wradlib_comp = pytest.importorskip("wradlib.comp", reason="wradlib es dependencia de dev")

RNG = np.random.default_rng(20261010)
SHAPE = (64, 48)


def _campos(n: int) -> tuple[list[SourceField], list[np.ndarray], list[np.ndarray]]:
    """n radares con huecos aleatorios; devuelve los dos formatos a comparar."""
    campos, grids, qualities = [], [], []
    for i in range(n):
        phys = RNG.uniform(-30.0, 70.0, SHAPE).astype(np.float32)
        q = RNG.uniform(0.0, 1.0, SHAPE).astype(np.float32)
        hueco = RNG.random(SHAPE) < 0.3
        phys[hueco] = np.nan
        q[hueco] = 0.0
        campos.append(
            SourceField(
                site_id=f"S{i}",
                phys=phys,
                quality=q,
                rank=RNG.uniform(0.0, 9000.0, SHAPE).astype(np.float32),
            )
        )
        grids.append(np.where(hueco, np.nan, phys).astype(np.float64))
        qualities.append(np.where(hueco, np.nan, q).astype(np.float64))
    return campos, grids, qualities


@pytest.mark.parametrize("n", [2, 3, 4])
def test_weighted_lineal_coincide_con_wradlib(n):
    campos, grids, qualities = _campos(n)
    nuestro = compose(campos, method="weighted", space="linear")
    suyo = wradlib_comp.compose_weighted(grids, [q.copy() for q in qualities])
    np.testing.assert_allclose(nuestro, suyo.astype(np.float32), rtol=1e-5, atol=1e-4)


def test_weighted_en_dbz_es_wradlib_sobre_z_lineal():
    """Nuestra rama 'dbz' = wradlib sobre Z + vuelta a dBZ, no sobre dBZ."""
    campos, grids, qualities = _campos(3)
    nuestro = compose(campos, method="weighted", space="dbz")
    z = [np.power(10.0, g / 10.0) for g in grids]
    suyo = 10.0 * np.log10(wradlib_comp.compose_weighted(z, [q.copy() for q in qualities]))
    np.testing.assert_allclose(nuestro, suyo.astype(np.float32), rtol=1e-5, atol=1e-4)


def test_componer_en_dbz_directamente_daria_otra_cosa():
    """Red de seguridad: si alguien quita el paso a Z, este test cae."""
    campos, grids, qualities = _campos(3)
    en_z = compose(campos, method="weighted", space="dbz")
    en_dbz = wradlib_comp.compose_weighted(grids, [q.copy() for q in qualities])
    cubierto = np.isfinite(en_z) & np.isfinite(en_dbz)
    assert cubierto.sum() > 0
    # El promedio en Z siempre queda por encima del promedio en dBZ (Jensen).
    assert np.all(en_z[cubierto] >= en_dbz[cubierto] - 1e-4)
    assert np.mean(en_z[cubierto] - en_dbz[cubierto]) > 1.0
