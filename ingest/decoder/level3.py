"""Decodificación de productos Level III radiales digitales vía MetPy.

Contrato de salida único para todos los productos: niveles uint8 con
mapeo lineal a físico (`físico = nivel · scale + offset`, niveles ≥ 2;
0 = below threshold/nodata, 1 = range folded). Donde el nativo ya es
lineal los niveles pasan tal cual; donde no (DVL logarítmico), se
decodifica a físico y se re-encodea lineal. Así el viewer aplica una
sola fórmula por raster, venga el producto que venga.
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

IN_100TH_TO_MM = 0.254  # centésima de pulgada → mm


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
    el_angle: float | None  # None en derivados de volumen
    vol_time: datetime  # inicio del volumen, UTC naive
    levels: np.ndarray  # (n_radials, n_gates) uint8, mapeo lineal
    az_start: np.ndarray  # grados desde el norte, sentido horario
    az_end: np.ndarray
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


def _f16(halfword: int) -> float:
    """Float16 de NEXRAD (ICD 2620001): como IEEE-754 pero con bias 16.

    Verificado con DVL: con bias IEEE (15) el VIL saturaría en ~3 kg/m²;
    con bias 16 el nivel 254 da 79.5 (techo canónico) y el tramo lineal
    empalma continuo con el logarítmico en el nivel de corte.
    """
    sign = (halfword >> 15) & 0x1
    exponent = (halfword >> 10) & 0x1F
    fraction = halfword & 0x3FF
    if exponent == 0:
        return (-1.0) ** sign * 2.0 * (fraction / 1024.0)
    return (-1.0) ** sign * 2.0 ** (exponent - 16) * (1.0 + fraction / 1024.0)


def _f32(hi: int, lo: int) -> float:
    return float(np.array([(hi << 16) | lo], dtype=">u4").view(">f4")[0])


def _cal_linear10(f: Level3File, levels: np.ndarray):
    """153/154: thr1 = mínimo ×10, thr2 = incremento ×10; nativo ya lineal."""
    minimum = f.prod_desc.thr1 / 10.0
    increment = f.prod_desc.thr2 / 10.0
    if increment <= 0:
        raise UnsupportedProductError(f"incremento no positivo en thresholds ({f.prod_desc.thr2})")
    return levels, increment, minimum - FLAG_LEVELS * increment


def _cal_eet(f: Level3File, levels: np.ndarray):
    """135: bits 0-6 = topes en kft + 2; bit 7 = 'topped' (se descarta).

    El flag topped solo indica que el eco supera el tope medible; el
    valor de altura sigue siendo válido y es lo que pinta el viewer.
    """
    masked = (levels & 0x7F).astype(np.uint8)
    return masked, 1.0, -float(FLAG_LEVELS)


# Re-encode lineal del VIL: paso 0.35 kg/m² cubre 0–88.5 en niveles 2–255,
# suficiente para el rango físico del producto (~0–80) con precisión de
# sobra para paletas (que van en pasos de 5–10).
_DVL_SCALE = 0.35


def _cal_dvl(f: Level3File, levels: np.ndarray):
    """134: float16 en thresholds; lineal hasta `log_start`, log encima.

    Se decodifica a físico con la fórmula nativa y se re-encodea lineal
    para mantener el contrato único del COG.
    """
    pd = f.prod_desc
    lin_scale = _f16(pd.thr1 & 0xFFFF)
    lin_offset = _f16(pd.thr2 & 0xFFFF)
    log_start = pd.thr3
    log_scale = _f16(pd.thr4 & 0xFFFF)
    log_offset = _f16(pd.thr5 & 0xFFFF)
    if lin_scale == 0 or log_scale == 0:
        raise UnsupportedProductError("DVL: escala cero en thresholds")

    lv = levels.astype(np.float64)
    physical = np.where(
        lv < log_start,
        (lv - lin_offset) / lin_scale,
        np.exp((lv - log_offset) / log_scale),
    )
    offset = -FLAG_LEVELS * _DVL_SCALE
    reencoded = np.clip(np.rint((physical - offset) / _DVL_SCALE), FLAG_LEVELS, 255)
    out = np.where(levels < FLAG_LEVELS, levels, reencoded).astype(np.uint8)
    return out, _DVL_SCALE, offset


def _cal_dpr(f: Level3File, levels: np.ndarray):
    """170/173/172: scale/offset float32 en halfwords; físico nativo en
    centésimas de pulgada — se declara en mm sin re-encodear (lineal)."""
    pd = f.prod_desc
    scale = _f32(pd.thr1 & 0xFFFF, pd.thr2 & 0xFFFF)
    offset = _f32(pd.thr3 & 0xFFFF, pd.thr4 & 0xFFFF)
    if scale == 0:
        raise UnsupportedProductError("DPR: escala cero en thresholds")
    return levels, IN_100TH_TO_MM / scale, -IN_100TH_TO_MM * offset / scale


_CALIBRATIONS = {
    "linear10": _cal_linear10,
    "eet": _cal_eet,
    "dvl": _cal_dvl,
    "dpr": _cal_dpr,
}


def decode_file(path: str | Path) -> RadialProduct:
    f = Level3File(str(path))
    code = f.prod_desc.prod_code
    spec = RASTER_PRODUCTS.get(code)
    if spec is None:
        raise UnsupportedProductError(f"producto {code} sin spec registrado")

    packet = f.sym_block[0][0]
    if not isinstance(packet, dict) or "data" not in packet or "start_az" not in packet:
        raise UnsupportedProductError(f"producto {code} sin paquete radial digital")

    raw = np.asarray(packet["data"], dtype=np.uint8)
    levels, scale, offset = _CALIBRATIONS[spec.calibration](f, raw)

    return RadialProduct(
        site_id=f.siteID,
        lat=f.lat,
        lon=f.lon,
        height_m=float(f.height),
        spec=spec,
        vcp=f.prod_desc.vcp,
        el_angle=float(f.metadata["el_angle"]) if spec.has_elevation else None,
        vol_time=f.metadata["vol_time"],
        levels=levels,
        az_start=np.asarray(packet["start_az"], dtype=np.float64),
        az_end=np.asarray(packet["end_az"], dtype=np.float64),
        scale=scale,
        offset=offset,
    )
