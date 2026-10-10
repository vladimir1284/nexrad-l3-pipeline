import numpy as np
import pytest

from ingest.mosaic.quality import (
    DEFAULT_H_REF_M,
    DEFAULT_R_REF_M,
    beam_height_m,
    quality,
)


def test_a_rango_cero_el_haz_esta_a_la_altura_de_la_antena():
    h = beam_height_m(np.array([0.0], dtype=np.float32), el_deg=0.5, antenna_h_m=4.0)
    assert h[0] == pytest.approx(4.0, abs=1e-3)


def test_altura_del_haz_crece_monotona_con_el_rango():
    r = np.linspace(0.0, 460_000.0, 50, dtype=np.float32)
    h = beam_height_m(r, el_deg=0.5, antenna_h_m=0.0)
    assert np.all(np.diff(h) > 0)


def test_altura_del_haz_a_230_km_y_medio_grado():
    """Referencia de literatura: ~5 km para 0.5° a 230 km con tierra 4/3."""
    h = beam_height_m(np.array([230_000.0], dtype=np.float32), el_deg=0.5, antenna_h_m=0.0)
    assert h[0] == pytest.approx(5100.0, abs=300.0)


def test_elevacion_mayor_sube_el_haz():
    r = np.array([100_000.0], dtype=np.float32)
    assert beam_height_m(r, 1.5, 0.0)[0] > beam_height_m(r, 0.5, 0.0)[0]


def test_calidad_en_rango_unitario_y_decreciente():
    r = np.linspace(0.0, 460_000.0, 50, dtype=np.float32)
    h = beam_height_m(r, el_deg=0.5, antenna_h_m=0.0)
    q = quality(r, h)
    assert q[0] == pytest.approx(1.0, abs=1e-6)
    assert np.all((q > 0.0) & (q <= 1.0))
    assert np.all(np.diff(q) < 0)


def test_sin_elevacion_la_calidad_es_solo_rango():
    """Derivados de volumen (EET, DVL): no hay haz que pesar."""
    r = np.array([0.0, 100_000.0, 460_000.0], dtype=np.float32)
    q = quality(r, None)
    expected = np.exp(-((r.astype(np.float64) / DEFAULT_R_REF_M) ** 2))
    assert q == pytest.approx(expected, abs=1e-6)


def test_el_termino_de_altura_penaliza_de_verdad():
    """El radar que sobrepasa la tormenta tiene que perder el solape."""
    r = np.array([120_000.0], dtype=np.float32)
    bajo = quality(r, beam_height_m(r, 0.5, 0.0))
    alto = quality(r, beam_height_m(r, 3.5, 0.0))
    assert alto[0] < 0.5 * bajo[0]


def test_refs_no_positivas_son_error():
    r = np.array([1000.0], dtype=np.float32)
    with pytest.raises(ValueError, match="positivos"):
        quality(r, None, h_ref_m=0.0)
    with pytest.raises(ValueError, match="positivos"):
        quality(r, None, r_ref_m=-1.0)


def test_defaults_documentados_siguen_siendo_los_calibrados():
    """Si alguien mueve los defaults, que sea a propósito (puerta P6)."""
    assert (DEFAULT_H_REF_M, DEFAULT_R_REF_M) == (4000.0, 200_000.0)
