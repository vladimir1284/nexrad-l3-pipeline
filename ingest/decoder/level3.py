"""Decodificación de productos Level III radiales digitales vía MetPy.

Expone los niveles crudos (uint8) sin mapear a físico: el mapeo lineal
(nivel → valor físico) viaja como scale/offset y se embebe en el COG,
de modo que el resampleo nearest neighbor opera sobre niveles exactos.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from metpy.io import Level3File

from ingest.products import RASTER_PRODUCTS, ProductSpec

# Niveles reservados en productos digitales (ICD 2620001): no son datos.
LEVEL_BELOW_THRESHOLD = 0
LEVEL_RANGE_FOLDED = 1
FLAG_LEVELS = 2  # los niveles físicos empiezan en 2


class UnsupportedProductError(Exception):
    """Producto sin ProductSpec registrado o sin paquete radial digital."""


@dataclass(frozen=True)
class RadialProduct:
    site_id: str  # identificador de 3 caracteres del feed (AMX, JUA…)
    lat: float
    lon: float
    height_m: float
    spec: ProductSpec
    vcp: int
    el_angle: float
    vol_time: datetime  # inicio del volumen, UTC naive
    levels: np.ndarray  # (n_radials, n_gates) uint8, niveles crudos
    az_start: np.ndarray  # grados desde el norte, sentido horario
    az_end: np.ndarray
    first_gate: float  # offset del primer gate, en unidades de gate
    gate_scale: float  # factor de escala de índice de gate
    scale: float  # físico = nivel * scale + offset (solo niveles >= 2)
    offset: float

    @property
    def n_radials(self) -> int:
        return self.levels.shape[0]

    @property
    def n_gates(self) -> int:
        return self.levels.shape[1]

    @property
    def max_range_m(self) -> float:
        return self.n_gates * self.spec.gate_width_m


def decode_file(path: str | Path) -> RadialProduct:
    f = Level3File(str(path))
    code = f.prod_desc.prod_code
    spec = RASTER_PRODUCTS.get(code)
    if spec is None:
        raise UnsupportedProductError(f"producto {code} sin spec registrado")

    packet = f.sym_block[0][0]
    if not isinstance(packet, dict) or "data" not in packet or "start_az" not in packet:
        raise UnsupportedProductError(f"producto {code} sin paquete radial digital")

    levels = np.asarray(packet["data"], dtype=np.uint8)

    # Productos digitales lineales (153/154): thr1 = mínimo ×10, thr2 = incremento ×10.
    # físico = (nivel - 2) * inc + min  →  scale = inc, offset = min - 2·inc
    minimum = f.prod_desc.thr1 / 10.0
    increment = f.prod_desc.thr2 / 10.0
    if increment <= 0:
        raise UnsupportedProductError(
            f"producto {code}: incremento no positivo en thresholds ({f.prod_desc.thr2})"
        )

    return RadialProduct(
        site_id=f.siteID,
        lat=f.lat,
        lon=f.lon,
        height_m=float(f.height),
        spec=spec,
        vcp=f.prod_desc.vcp,
        el_angle=float(f.metadata["el_angle"]),
        vol_time=f.metadata["vol_time"],
        levels=levels,
        az_start=np.asarray(packet["start_az"], dtype=np.float64),
        az_end=np.asarray(packet["end_az"], dtype=np.float64),
        first_gate=float(packet["first"]),
        gate_scale=float(packet["gate_scale"]),
        scale=increment,
        offset=minimum - FLAG_LEVELS * increment,
    )
