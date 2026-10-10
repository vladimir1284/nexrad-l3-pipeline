from datetime import datetime, timedelta

import pytest

from ingest.mosaic.slots import (
    Contribution,
    closed_slots,
    pick_contributions,
    slot_center,
    slot_start,
)

SLOT = 300


def t(h, m, s=0):
    return datetime(2026, 10, 10, h, m, s)


def test_slot_start_alinea_a_la_epoca_no_a_la_hora_en_curso():
    assert slot_start(t(3, 8, 18), SLOT) == t(3, 5)
    assert slot_start(t(3, 5, 0), SLOT) == t(3, 5)
    assert slot_start(t(3, 9, 59), SLOT) == t(3, 5)
    assert slot_start(t(3, 10, 0), SLOT) == t(3, 10)


def test_slot_center_esta_a_la_mitad():
    assert slot_center(t(3, 5), SLOT) == t(3, 7, 30)


def test_slot_s_no_positivo_es_error():
    with pytest.raises(ValueError, match="positivo"):
        slot_start(t(3, 5), 0)


def test_closed_slots_no_incluye_el_slot_en_curso():
    """Componer un slot abierto daría un COG que cambia bajo los pies."""
    slots = closed_slots(t(3, 8, 18), SLOT, lookback_s=900)
    assert slots == [t(2, 50), t(2, 55), t(3, 0)]
    assert t(3, 5) not in slots


def test_closed_slots_reanuda_despues_del_ultimo_hecho():
    slots = closed_slots(t(3, 20, 1), SLOT, lookback_s=3600, since=t(3, 5))
    assert slots == [t(3, 10), t(3, 15)]


def test_closed_slots_vacio_si_since_esta_al_dia():
    assert closed_slots(t(3, 8, 18), SLOT, lookback_s=3600, since=t(3, 5)) == []


def test_pick_elige_el_volumen_mas_cercano_al_centro():
    vols = {"AMX": [t(3, 5, 10), t(3, 7, 20), t(3, 9, 40)]}
    (c,) = pick_contributions(t(3, 5), SLOT, vols, tolerance_s=150)
    assert c.site_id == "AMX"
    assert c.vol_time == t(3, 7, 20)
    assert c.lag_s == -10  # 10 s por delante del centro


def test_lag_positivo_cuando_el_volumen_va_por_detras():
    vols = {"AMX": [t(3, 8, 0)]}
    (c,) = pick_contributions(t(3, 5), SLOT, vols, tolerance_s=150)
    assert c.lag_s == 30


def test_fuera_de_tolerancia_el_sitio_no_aporta():
    vols = {"AMX": [t(3, 5, 0)]}  # 150 s antes del centro
    assert pick_contributions(t(3, 5), SLOT, vols, tolerance_s=149) == []
    assert len(pick_contributions(t(3, 5), SLOT, vols, tolerance_s=150)) == 1


def test_se_compone_con_los_sitios_que_haya():
    """Un radar caído no apaga el mosaico: aporta quien puede."""
    vols = {
        "AMX": [t(3, 7, 0)],
        "BYX": [t(3, 7, 45)],
        "HDC": [],  # sin volumen en la ventana
        "EOX": [t(2, 50, 0)],  # demasiado viejo
    }
    sitios = [c.site_id for c in pick_contributions(t(3, 5), SLOT, vols, tolerance_s=150)]
    assert sitios == ["AMX", "BYX"]


def test_orden_de_salida_determinista_por_site_id():
    """El r2_key del slot no puede depender del orden de la consulta."""
    vols = {"HDC": [t(3, 7)], "AMX": [t(3, 7)], "BYX": [t(3, 7)]}
    assert [c.site_id for c in pick_contributions(t(3, 5), SLOT, vols, tolerance_s=150)] == [
        "AMX",
        "BYX",
        "HDC",
    ]


def test_empate_exacto_gana_el_mas_reciente():
    centro = slot_center(t(3, 5), SLOT)
    vols = {"AMX": [centro - timedelta(seconds=40), centro + timedelta(seconds=40)]}
    (c,) = pick_contributions(t(3, 5), SLOT, vols, tolerance_s=150)
    assert c.vol_time == centro + timedelta(seconds=40)


def test_contribution_serializa_lo_que_espera_el_viewer():
    c = Contribution("AMX", t(3, 7, 20), -10)
    assert c.as_json() == {
        "site": "AMX",
        "vol_time": "2026-10-10T03:07:20",
        "lag_s": -10,
    }
