import hashlib

import numpy as np
import pytest
from pyproj import Transformer

from ingest.decoder.level3 import (
    FLAG_LEVELS,
    LEVEL_BELOW_THRESHOLD,
    LEVEL_RANGE_FOLDED,
    decode_file,
)
from ingest.gridding.aeqd import grid_radial, radar_proj4
from ingest.mosaic.calibration import calibration_for
from ingest.mosaic.compose import (
    SourceField,
    SourceRaster,
    compose,
    project_source,
    quantize,
)
from ingest.mosaic.geometry import RadarSite, build_domain
from tests.conftest import DATA_DIR

N0B = calibration_for(153)


def _field(phys, quality, rank, site="X"):
    return SourceField(
        site_id=site,
        phys=np.array(phys, dtype=np.float32),
        quality=np.array(quality, dtype=np.float32),
        rank=np.array(rank, dtype=np.float32),
    )


# --- la regla que justifica todo el módulo ----------------------------------


def test_el_promedio_pesado_va_en_z_lineal_no_en_dbz():
    """40 y 50 dBZ con el mismo peso dan 47.4 dBZ, no 45.

    Promediar dBZ es promediar logaritmos: hunde los núcleos de convección,
    que es justo lo que el mosaico tiene que preservar.
    """
    a = _field([[40.0]], [[1.0]], [[0.0]], site="A")
    b = _field([[50.0]], [[1.0]], [[0.0]], site="B")
    out = compose([a, b], method="weighted", space="dbz")
    esperado = 10.0 * np.log10((10**4.0 + 10**5.0) / 2.0)
    assert out[0, 0] == pytest.approx(esperado, abs=1e-3)
    assert esperado == pytest.approx(47.404, abs=1e-3)
    assert out[0, 0] > 45.0


def test_en_espacio_lineal_el_promedio_es_el_aritmetico():
    """Alturas de eco o VIL no pasan por Z: ahí 40 y 50 sí dan 45."""
    a = _field([[40.0]], [[1.0]], [[0.0]])
    b = _field([[50.0]], [[1.0]], [[0.0]])
    out = compose([a, b], method="weighted", space="linear")
    assert out[0, 0] == pytest.approx(45.0, abs=1e-4)


def test_el_peso_inclina_el_resultado_hacia_el_radar_bueno():
    a = _field([[40.0]], [[0.9]], [[1000.0]])
    b = _field([[50.0]], [[0.1]], [[9000.0]])
    out = compose([a, b], method="weighted", space="dbz")
    esperado = 10.0 * np.log10((0.9 * 10**4.0 + 0.1 * 10**5.0) / 1.0)
    assert out[0, 0] == pytest.approx(esperado, abs=1e-3)


def test_lowest_beam_elige_el_haz_mas_bajo_sin_promediar():
    a = _field([[40.0]], [[0.1]], [[9000.0]])
    b = _field([[50.0]], [[0.9]], [[1000.0]])
    assert compose([a, b], method="lowest_beam")[0, 0] == pytest.approx(50.0)
    c = _field([[60.0]], [[0.5]], [[500.0]])
    assert compose([a, b, c], method="lowest_beam")[0, 0] == pytest.approx(60.0)


def test_un_solo_radar_es_identidad():
    a = _field([[33.5]], [[0.3]], [[2000.0]])
    for method in ("weighted", "lowest_beam"):
        assert compose([a], method=method, space="dbz")[0, 0] == pytest.approx(33.5, abs=1e-3)


def test_sin_cobertura_el_resultado_es_nan_y_luego_nodata():
    vacio = _field([[np.nan]], [[0.0]], [[np.inf]])
    out = compose([vacio], method="weighted")
    assert np.isnan(out[0, 0])
    assert quantize(out, scale=N0B.scale, offset=N0B.offset)[0, 0] == LEVEL_BELOW_THRESHOLD


def test_un_radar_sin_dato_no_arrastra_al_que_si_tiene():
    bueno = _field([[45.0]], [[0.5]], [[1500.0]])
    vacio = _field([[np.nan]], [[0.0]], [[np.inf]])
    assert compose([bueno, vacio], method="weighted")[0, 0] == pytest.approx(45.0, abs=1e-3)
    assert compose([bueno, vacio], method="lowest_beam")[0, 0] == pytest.approx(45.0)


def test_mallas_distintas_o_lista_vacia_son_error():
    with pytest.raises(ValueError, match="al menos un campo"):
        compose([], method="weighted")
    a = _field([[1.0]], [[1.0]], [[0.0]])
    b = _field([[1.0, 2.0]], [[1.0, 1.0]], [[0.0, 0.0]])
    with pytest.raises(ValueError, match="misma malla"):
        compose([a, b], method="weighted")
    with pytest.raises(ValueError, match="desconocido"):
        compose([a], method="mediana")  # type: ignore[arg-type]


# --- cuantización -----------------------------------------------------------


def test_quantize_ida_y_vuelta_con_la_calibracion_canonica():
    niveles = np.arange(FLAG_LEVELS, 256, dtype=np.float32)
    phys = niveles * N0B.scale + N0B.offset
    assert np.array_equal(quantize(phys, scale=N0B.scale, offset=N0B.offset), niveles)


def test_quantize_nunca_emite_range_folded():
    """El folding es del radar individual; en el compuesto no significa nada."""
    muy_bajo = np.array([[-1000.0]], dtype=np.float32)
    assert quantize(muy_bajo, scale=N0B.scale, offset=N0B.offset)[0, 0] == FLAG_LEVELS
    assert FLAG_LEVELS > LEVEL_RANGE_FOLDED


def test_quantize_recorta_por_arriba_y_valida_scale():
    alto = np.array([[1000.0]], dtype=np.float32)
    assert quantize(alto, scale=N0B.scale, offset=N0B.offset, max_level=200)[0, 0] == 200
    with pytest.raises(ValueError, match="scale"):
        quantize(alto, scale=0.0, offset=0.0)


def test_calibracion_canonica_de_n0b_es_la_rampa_estandar():
    assert (N0B.scale, N0B.offset, N0B.space) == (0.5, -33.0, "dbz")
    assert FLAG_LEVELS * N0B.scale + N0B.offset == pytest.approx(-32.0)


def test_producto_sin_calibracion_de_mosaico_revienta():
    with pytest.raises(ValueError, match="sin calibración de mosaico"):
        calibration_for(154)  # N0G: velocidad radial, no es componible


# --- proyección y golden sobre dato real ------------------------------------


@pytest.fixture(scope="module")
def amx():
    prod = decode_file(DATA_DIR / "AMX_N0B_2026_07_06_15_45_17")
    return prod, grid_radial(prod)


def _source(prod, grid, site_id, lon, levels):
    return SourceRaster(
        site_id=site_id,
        levels=levels,
        cell_m=grid.cell_m,
        half_extent_m=grid.half_extent_m,
        proj4=radar_proj4(prod.lat, lon),
        scale=prod.scale,
        offset=prod.offset,
        el_angle=prod.el_angle,
        antenna_h_m=prod.height_m,
    )


def _par_de_sitios(amx, cell_m=1500.0, delta_lon=1.5):
    """Dos radares reales solapados: AMX y una copia espejada 1.5° al este.

    El feed solo trae un N0B por sitio y los dos sitios commiteados (AMX y
    JUA) están a 1700 km, sin solape. Espejar el mismo volumen da un segundo
    campo con distribución real de niveles y un solape controlado.
    """
    prod, grid = amx
    sites = [
        RadarSite("AMX", prod.lat, prod.lon, prod.height_m),
        RadarSite("XMA", prod.lat, prod.lon + delta_lon, prod.height_m),
    ]
    dom = build_domain("TEST", sites, radius_m=grid.half_extent_m, cell_m=cell_m)
    srcs = [
        _source(prod, grid, "AMX", prod.lon, grid.data),
        _source(prod, grid, "XMA", prod.lon + delta_lon, np.fliplr(grid.data)),
    ]
    return dom, [project_source(s, dom) for s in srcs]


def test_project_source_descarta_nodata_y_range_folded(amx):
    prod, grid = amx
    levels = grid.data.copy()
    levels[:] = LEVEL_RANGE_FOLDED
    dom = build_domain(
        "SOLO",
        [RadarSite("AMX", prod.lat, prod.lon, prod.height_m)],
        radius_m=grid.half_extent_m,
        cell_m=4000.0,
    )
    campo = project_source(_source(prod, grid, "AMX", prod.lon, levels), dom)
    assert np.all(campo.quality == 0.0)
    assert np.all(np.isnan(campo.phys))


def test_project_source_pone_la_mejor_calidad_junto_al_radar(amx):
    """La calidad es máxima en el píxel donde está la antena, no en el borde."""
    prod, grid = amx
    dom, campos = _par_de_sitios(amx)
    q = campos[0].quality
    fila, col = np.unravel_index(int(np.argmax(q)), q.shape)

    tf = Transformer.from_crs("EPSG:4326", dom.proj4, always_xy=True)
    x, y = tf.transform(prod.lon, prod.lat)
    col_radar = (x + dom.half_x_m) / dom.cell_m
    fila_radar = (dom.half_y_m - y) / dom.cell_m

    # Tolerancia de 2 px: el rango viaja por nearest desde una malla de 250 m
    # a una de 1500 m, así que el mínimo no cae exactamente en el centro.
    assert abs(col - col_radar) <= 2.5
    assert abs(fila - fila_radar) <= 2.5
    assert q.max() > 0.99


def test_el_solape_existe_y_los_dos_metodos_no_coinciden_ahi(amx):
    _, campos = _par_de_sitios(amx)
    solape = (campos[0].quality > 0) & (campos[1].quality > 0)
    assert solape.sum() == 600
    a = quantize(
        compose(campos, method="weighted", space=N0B.space),
        scale=N0B.scale,
        offset=N0B.offset,
    )
    b = quantize(
        compose(campos, method="lowest_beam", space=N0B.space),
        scale=N0B.scale,
        offset=N0B.offset,
    )
    # Fuera del solape los dos métodos tienen que dar lo mismo: con un solo
    # radar aportando, promediar y elegir son la misma operación.
    assert np.array_equal(a[~solape], b[~solape])
    assert (a != b)[solape].sum() == 590


# (método, sha16, nonzero, max_level, argmax)
GOLDEN = [
    ("weighted", "ec1b99e26a5f04f5", 22342, 184, (391, 308)),
    ("lowest_beam", "a40c14ff0cdabe62", 22342, 184, (391, 308)),
]


@pytest.mark.parametrize(
    "method,sha16,nonzero,max_level,argmax", GOLDEN, ids=[g[0] for g in GOLDEN]
)
def test_mosaico_golden(amx, method, sha16, nonzero, max_level, argmax):
    dom, campos = _par_de_sitios(amx)
    assert (dom.width, dom.height) == (714, 614)
    phys = compose(campos, method=method, space=N0B.space)
    levels = quantize(phys, scale=N0B.scale, offset=N0B.offset)
    assert levels.shape == (dom.height, dom.width)
    assert hashlib.sha256(levels.tobytes()).hexdigest()[:16] == sha16
    assert int((levels > 0).sum()) == nonzero
    assert int(levels.max()) == max_level
    assert np.unravel_index(int(levels.argmax()), levels.shape) == argmax
