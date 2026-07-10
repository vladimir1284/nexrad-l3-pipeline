"""Grillado polar → malla regular AEQD centrada en el radar.

Nearest neighbor sobre niveles crudos: cada pixel toma el nivel del
(radial, gate) más cercano. Celda = gate nativo, extensión = rango nativo,
así el peor caso (N0B) queda en 3680×3680 bajo el cap de textura WebGL.

La malla se construye directamente en metros radar-céntricos: en AEQD
centrada en el radar, distancia y azimut desde el origen son exactos,
no hay reproyección que hacer.
"""

from dataclasses import dataclass

import numpy as np

from ingest.decoder.level3 import LEVEL_BELOW_THRESHOLD, RadialProduct


@dataclass(frozen=True)
class AeqdGrid:
    data: np.ndarray  # (size, size) uint8, fila 0 = norte
    size: int
    cell_m: float
    half_extent_m: float  # extensión desde el centro; origen = (-half, +half)
    proj4: str


def radar_proj4(lat: float, lon: float) -> str:
    return f"+proj=aeqd +lat_0={lat} +lon_0={lon} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"


def grid_radial(prod: RadialProduct) -> AeqdGrid:
    n_radials = prod.n_radials
    gate_w = prod.spec.gate_width_m
    half = prod.max_range_m
    size = 2 * prod.n_gates
    cell = gate_w

    # Los radiales llegan en orden de escaneo empezando en un azimut arbitrario:
    # se indexan por bin de azimut uniforme (ancho 360/n_radials).
    az_width = 360.0 / n_radials
    bins = np.round(prod.az_start / az_width).astype(np.intp) % n_radials
    if np.unique(bins).size != n_radials:
        raise ValueError("radiales no cubren el círculo con paso uniforme")
    radial_for_bin = np.empty(n_radials, dtype=np.intp)
    radial_for_bin[bins] = np.arange(n_radials)

    # Centros de pixel: x este, y norte; fila 0 arriba (norte).
    centers = -half + (np.arange(size) + 0.5) * cell
    xx = centers[np.newaxis, :]
    yy = -centers[:, np.newaxis]

    rng = np.hypot(xx, yy)
    az = np.degrees(np.arctan2(xx, yy)) % 360.0  # 0 = norte, horario

    pix_bin = np.floor_divide(az, az_width).astype(np.intp) % n_radials
    rad_idx = radial_for_bin[pix_bin]
    # Índice de gate por geometría nominal del producto (el gate_scale del
    # paquete es un artefacto inconsistente entre productos y se ignora).
    gate_idx = np.floor(rng / gate_w).astype(np.intp)

    valid = gate_idx < prod.n_gates
    data = np.full((size, size), LEVEL_BELOW_THRESHOLD, dtype=np.uint8)
    data[valid] = prod.levels[rad_idx[valid], gate_idx[valid]]

    return AeqdGrid(
        data=data,
        size=size,
        cell_m=cell,
        half_extent_m=half,
        proj4=radar_proj4(prod.lat, prod.lon),
    )
