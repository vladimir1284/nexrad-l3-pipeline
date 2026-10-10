"""Campo de calidad por radar: altura del haz y volumen de muestreo.

En el solape, el píxel de un radar vale más cuanto más bajo mire (menos
sobrepaso de la tormenta) y cuanto más cerca esté (menos ensanchamiento del
haz, menor volumen de muestreo). Los dos efectos entran como gaussianas:

    h = sqrt(r² + (k·a)² + 2·r·k·a·sin φ) − k·a + h_antena     k = 4/3
    q = exp(−(h/h_ref)²) · exp(−(r/r_ref)²)

`k = 4/3` es el radio terrestre efectivo que absorbe la refracción estándar
de la atmósfera. `h` sale sobre el nivel del mar (altura de la antena
incluida); se usa como proxy de altura sobre el terreno, válido mientras el
dominio sea costero/llano — en terreno complejo habría que restar un DEM.

Los derivados de volumen (EET, DVL) no tienen elevación: ahí `el_angle` es
None, el término de altura desaparece y la calidad queda solo en rango.

`h_ref` y `r_ref` son los dos números que calibra el experto (puerta P6); se
leen del entorno para poder ajustarlos sin reconstruir la imagen.
"""

import math

import numpy as np

EARTH_RADIUS_M = 6_371_000.0
K_EFFECTIVE = 4.0 / 3.0

# Defaults de literatura, pendientes de calibración con el experto:
#  - h_ref = 4 km: a 4 km de altura el peso cae a 1/e; a 6 km (sobrepaso
#    claro de la parte útil del eco en convección) queda en ~0.10.
#  - r_ref = 200 km: a 230 km (alcance nominal de N0B) el peso queda en
#    ~0.27, que es donde el haz ya mide ~3.7 km de diámetro.
DEFAULT_H_REF_M = 4000.0
DEFAULT_R_REF_M = 200_000.0


def beam_height_m(
    range_m: np.ndarray,
    el_deg: float,
    antenna_h_m: float,
) -> np.ndarray:
    """Altura del centro del haz (msl) con tierra efectiva 4/3."""
    ka = K_EFFECTIVE * EARTH_RADIUS_M
    sin_el = math.sin(math.radians(el_deg))
    r = range_m.astype(np.float64, copy=False)
    h = np.sqrt(r * r + ka * ka + 2.0 * r * ka * sin_el) - ka + antenna_h_m
    return h.astype(np.float32)


def quality(
    range_m: np.ndarray,
    beam_h_m: np.ndarray | None,
    *,
    h_ref_m: float = DEFAULT_H_REF_M,
    r_ref_m: float = DEFAULT_R_REF_M,
) -> np.ndarray:
    """Peso en [0, 1]. `beam_h_m` None (derivados de volumen) = solo rango."""
    if h_ref_m <= 0 or r_ref_m <= 0:
        raise ValueError(f"h_ref/r_ref deben ser positivos ({h_ref_m}, {r_ref_m})")
    r = range_m.astype(np.float64, copy=False)
    q = np.exp(-((r / r_ref_m) ** 2))
    if beam_h_m is not None:
        h = beam_h_m.astype(np.float64, copy=False)
        q = q * np.exp(-((h / h_ref_m) ** 2))
    return q.astype(np.float32)
