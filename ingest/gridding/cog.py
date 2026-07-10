"""Escritura de COG calibrado con Rasterio (driver COG).

El COG lleva niveles crudos uint8 + scale/offset embebidos (físico =
nivel·scale + offset para niveles >= 2), CRS AEQD del radar, geotransform
y overviews internos nearest — el cliente aplica paleta sobre físico.
"""

from pathlib import Path

import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin

from ingest.decoder.level3 import LEVEL_BELOW_THRESHOLD, RadialProduct
from ingest.gridding.aeqd import AeqdGrid


def write_cog(grid: AeqdGrid, prod: RadialProduct, path: str | Path) -> Path:
    path = Path(path)
    transform = from_origin(-grid.half_extent_m, grid.half_extent_m, grid.cell_m, grid.cell_m)
    profile = {
        "driver": "COG",
        "dtype": "uint8",
        "count": 1,
        "width": grid.size,
        "height": grid.size,
        "crs": CRS.from_proj4(grid.proj4),
        "transform": transform,
        "nodata": LEVEL_BELOW_THRESHOLD,
        "compress": "DEFLATE",
        "blocksize": 512,
        "overview_resampling": "NEAREST",
    }
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(grid.data, 1)
        ds.scales = (prod.scale,)
        ds.offsets = (prod.offset,)
        tags = {
            "SITE": prod.site_id,
            "PRODUCT_CODE": str(prod.spec.code),
            "PRODUCT": prod.spec.mnemonic,
            "UNIT": prod.spec.unit,
            "VOL_TIME": prod.vol_time.isoformat(),
            "VCP": str(prod.vcp),
            "RADAR_LAT": str(prod.lat),
            "RADAR_LON": str(prod.lon),
            "RADAR_HEIGHT_M": str(prod.height_m),
        }
        if prod.el_angle is not None:
            tags["EL_ANGLE"] = str(prod.el_angle)
        ds.update_tags(**tags)
    return path
