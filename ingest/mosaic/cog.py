"""Escritura del COG compuesto.

Mismo contrato que el COG por radar (`ingest.gridding.cog`): niveles crudos
uint8, scale/offset embebidos, CRS proj4, overviews nearest. Cambian tres
cosas:

- el CRS es el AEQD del dominio, no el de un radar;
- la calibración es la canónica del producto mosaico, no la de una fila;
- las tags llevan la procedencia (qué radares aportaron y con qué desfase),
  para que un COG suelto sea auto-descriptivo en QGIS sin consultar la base.
"""

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from ingest.decoder.level3 import LEVEL_BELOW_THRESHOLD
from ingest.mosaic.calibration import MosaicCalibration
from ingest.mosaic.geometry import DomainGrid
from ingest.mosaic.slots import Contribution


def write_mosaic_cog(
    levels: np.ndarray,
    domain: DomainGrid,
    *,
    path: str | Path,
    product_code: int,
    mnemonic: str,
    slot_time: datetime,
    slot_s: int,
    calibration: MosaicCalibration,
    method: str,
    contributions: list[Contribution],
) -> Path:
    path = Path(path)
    if levels.shape != (domain.height, domain.width):
        raise ValueError(
            f"la malla de niveles {levels.shape} no coincide con el dominio "
            f"({domain.height}, {domain.width})"
        )
    transform = from_origin(-domain.half_x_m, domain.half_y_m, domain.cell_m, domain.cell_m)
    profile = {
        "driver": "COG",
        "dtype": "uint8",
        "count": 1,
        "width": domain.width,
        "height": domain.height,
        "crs": domain.crs,
        "transform": transform,
        "nodata": LEVEL_BELOW_THRESHOLD,
        "compress": "DEFLATE",
        "blocksize": 512,
        "overview_resampling": "NEAREST",
    }
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(levels, 1)
        ds.scales = (calibration.scale,)
        ds.offsets = (calibration.offset,)
        ds.update_tags(
            DOMAIN=domain.domain_id,
            PRODUCT_CODE=str(product_code),
            PRODUCT=mnemonic,
            UNIT=calibration.unit,
            SLOT_TIME=slot_time.isoformat(),
            SLOT_S=str(slot_s),
            METHOD=method,
            SPACE=calibration.space,
            CONTRIBUTING=json.dumps([c.as_json() for c in contributions]),
        )
    return path
