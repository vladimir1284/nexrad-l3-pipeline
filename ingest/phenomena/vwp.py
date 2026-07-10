"""Perfil de viento VAD (48/NVW) desde la página tabular.

El Symbology del NVW es puro dibujo (ejes y barbas del gráfico VWP);
los datos limpios viven en la página "VAD Algorithm Output": una fila
por altitud con U/V/W, dirección, velocidad y RMS. ALT viene en
centenares de pies (unidad nativa del producto).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from metpy.io import Level3File

from ingest.decoder.level3 import UnsupportedProductError

VWP_CODE = 48
VWP_MNEMONIC = "NVW"

# "    004    -4.3     0.1     NA    091   008   5.8  ..."
_ROW = re.compile(
    r"^\s+(\d{3})\s+(-?\d+\.\d+|NA)\s+(-?\d+\.\d+|NA)\s+(-?\d+\.\d+|NA)"
    r"\s+(\d{3})\s+(\d{3})\s+(\d+\.\d+)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class VwpLevel:
    height_ft: int
    wind_dir_deg: float
    wind_speed_kt: float
    rms_kt: float | None


@dataclass(frozen=True)
class VwpProduct:
    site_id: str
    lat: float
    lon: float
    height_m: float
    vol_time: datetime
    levels: list[VwpLevel] = field(default_factory=list)


def parse_vwp_file(path: str | Path) -> VwpProduct:
    f = Level3File(str(path))
    if f.prod_desc.prod_code != VWP_CODE:
        raise UnsupportedProductError(f"producto {f.prod_desc.prod_code} no es NVW")

    levels = []
    for page in getattr(f, "tab_pages", None) or []:
        if "VAD Algorithm Output" not in page:
            continue
        for m in _ROW.finditer(page):
            alt, _u, _v, _w, direction, speed, rms = m.groups()
            levels.append(
                VwpLevel(
                    height_ft=int(alt) * 100,
                    wind_dir_deg=float(direction),
                    wind_speed_kt=float(speed),
                    rms_kt=float(rms),
                )
            )

    return VwpProduct(
        site_id=f.siteID,
        lat=f.lat,
        lon=f.lon,
        height_m=float(f.height),
        vol_time=f.metadata["vol_time"],
        levels=levels,
    )
