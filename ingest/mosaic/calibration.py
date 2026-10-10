"""Calibración canónica del producto mosaico.

El mosaico **no** hereda el `scale`/`offset` de ninguna fila de origen: esos
varían por fila de `rasters` y el viewer necesita una rampa estable para que
la paleta del producto no cambie entre slots. Aquí se fija una por código de
producto, con la misma convención que el decoder (`físico = nivel · scale +
offset`, niveles >= FLAG_LEVELS; 0 = nodata).

No hace falta un código de producto nuevo: el mosaico se identifica por
dominio y reusa el código NEXRAD, así que el catálogo del viewer
(`shared/products/<code>`) y su paleta valen tal cual.
"""

from dataclasses import dataclass

from ingest.decoder.level3 import FLAG_LEVELS
from ingest.mosaic.compose import Space


@dataclass(frozen=True)
class MosaicCalibration:
    scale: float
    offset: float
    space: Space
    unit: str


def _linear10(minimum: float, increment: float, space: Space, unit: str) -> MosaicCalibration:
    """Misma forma que `_cal_linear10` del decoder: nivel FLAG_LEVELS = mínimo."""
    return MosaicCalibration(
        scale=increment,
        offset=minimum - FLAG_LEVELS * increment,
        space=space,
        unit=unit,
    )


# N0B: rampa canónica de reflectividad super-res (−32 dBZ, paso 0.5) — el
# nivel 255 llega a 94.5 dBZ, por encima de cualquier eco meteorológico.
MOSAIC_CALIBRATION: dict[int, MosaicCalibration] = {
    153: _linear10(minimum=-32.0, increment=0.5, space="dbz", unit="dBZ"),
}


def calibration_for(product_code: int) -> MosaicCalibration:
    try:
        return MOSAIC_CALIBRATION[product_code]
    except KeyError:
        raise ValueError(
            f"producto {product_code} sin calibración de mosaico definida "
            f"(soportados: {sorted(MOSAIC_CALIBRATION)})"
        ) from None
