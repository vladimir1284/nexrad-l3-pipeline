import math

import pytest
from pyproj import Transformer

from ingest.mosaic.geometry import MAX_TEXTURE_PX, RadarSite, build_domain

# Posiciones aproximadas del dominio GULF del demo (AMX, BYX, HDC, EOX). Solo
# se usan para ejercitar la geometría: en producción salen de la tabla radars.
GULF = [
    RadarSite("AMX", 25.611, -80.413, 4.0),
    RadarSite("BYX", 24.597, -81.703, 2.0),
    RadarSite("HDC", 30.519, -90.407, 12.0),
    RadarSite("EOX", 31.460, -85.459, 54.0),
]
N0B_RADIUS_M = 460_000.0


def test_domain_cubre_todos_los_sitios_con_su_radio():
    dom = build_domain("GULF", GULF, radius_m=N0B_RADIUS_M, cell_m=1000.0)
    tf = Transformer.from_crs("EPSG:4326", dom.proj4, always_xy=True)
    for site in GULF:
        x, y = tf.transform(site.lon, site.lat)
        assert abs(x) + N0B_RADIUS_M <= dom.half_x_m + dom.cell_m
        assert abs(y) + N0B_RADIUS_M <= dom.half_y_m + dom.cell_m


def test_domain_es_par_y_centrado():
    dom = build_domain("GULF", GULF, radius_m=N0B_RADIUS_M, cell_m=1000.0)
    assert dom.width % 2 == 0 and dom.height % 2 == 0
    # Misma convención que AeqdGrid: origen en (-half_x, +half_y).
    assert dom.transform.c == pytest.approx(-dom.half_x_m)
    assert dom.transform.f == pytest.approx(dom.half_y_m)
    assert dom.transform.a == pytest.approx(dom.cell_m)
    assert dom.transform.e == pytest.approx(-dom.cell_m)


def test_domain_gulf_entra_en_el_cap_de_textura():
    dom = build_domain("GULF", GULF, radius_m=N0B_RADIUS_M, cell_m=1000.0)
    assert max(dom.width, dom.height) <= MAX_TEXTURE_PX


def test_celda_demasiado_fina_revienta_en_vez_de_colar_una_malla_irrenderizable():
    with pytest.raises(ValueError, match="cap de textura"):
        build_domain("GULF", GULF, radius_m=N0B_RADIUS_M, cell_m=250.0)


def test_dominio_de_un_solo_sitio_equivale_a_su_propia_malla():
    site = GULF[0]
    dom = build_domain("AMX", [site], radius_m=N0B_RADIUS_M, cell_m=250.0)
    assert dom.width == dom.height == 2 * int(N0B_RADIUS_M / 250.0)
    assert f"+lat_0={site.lat}" in dom.proj4
    assert f"+lon_0={site.lon}" in dom.proj4


def test_centro_es_el_punto_medio_del_bbox():
    dom = build_domain("GULF", GULF, radius_m=N0B_RADIUS_M, cell_m=1000.0)
    lat0 = (min(s.lat for s in GULF) + max(s.lat for s in GULF)) / 2
    lon0 = (min(s.lon for s in GULF) + max(s.lon for s in GULF)) / 2
    assert f"+lat_0={lat0}" in dom.proj4
    assert f"+lon_0={lon0}" in dom.proj4


def test_sin_sitios_o_celda_no_positiva_es_error():
    with pytest.raises(ValueError, match="al menos un sitio"):
        build_domain("VACIO", [], radius_m=N0B_RADIUS_M, cell_m=1000.0)
    with pytest.raises(ValueError, match="positivo"):
        build_domain("GULF", GULF, radius_m=N0B_RADIUS_M, cell_m=0.0)


def test_malla_mas_fina_da_mas_pixeles_para_la_misma_extension():
    coarse = build_domain("GULF", GULF, radius_m=N0B_RADIUS_M, cell_m=2000.0)
    fine = build_domain("GULF", GULF, radius_m=N0B_RADIUS_M, cell_m=1000.0)
    assert fine.width == pytest.approx(2 * coarse.width, abs=2)
    assert math.isclose(fine.half_x_m, coarse.half_x_m, rel_tol=1e-3)
