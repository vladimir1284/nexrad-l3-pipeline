"""Composición multi-radar sobre la malla del dominio.

Orden del cálculo, que importa:

1. Remuestreo **nearest** de los niveles crudos de cada sitio a la malla del
   dominio. Nearest y no bilinear por la misma razón que en el grillado
   polar: son datos umbralizados y la interpolación suave inventa valores.
2. El rango al radar viaja con el dato (se remuestrea igual), así la calidad
   de cada píxel del dominio es la del sample que realmente aterrizó ahí.
3. Niveles 0 (below threshold) y 1 (range folded) pesan cero. El mosaico
   nunca emite nivel 1: el folding es una propiedad del radar individual,
   no del compuesto.
4. Nivel → físico con el `scale`/`offset` **de la fila de origen**: la
   calibración es por fila de `rasters` y no hay garantía de que dos sitios
   coincidan. Componer en niveles sería un error silencioso.
5. En reflectividad, el promedio pesado va en **Z lineal** (Z = 10^(dBZ/10)),
   no en dBZ. Promediar dBZ es promediar logaritmos: hunde los núcleos.
6. Re-cuantización con la calibración canónica del producto mosaico, que es
   fija y conocida por el viewer (la paleta no cambia).

Dos reglas, seleccionables por config:

- `weighted` (default): Z̄ = Σ qᵢZᵢ / Σ qᵢ. Sin costura en el solape, suaviza
  algo los picos.
- `lowest_beam`: gana el radar que mire más bajo en ese píxel. Preserva
  gradientes; la frontera es visible pero sigue iso-altura, no una recta.
"""

from dataclasses import dataclass
from typing import Literal

import numpy as np
from rasterio.crs import CRS
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject

from ingest.decoder.level3 import FLAG_LEVELS, LEVEL_BELOW_THRESHOLD
from ingest.mosaic.geometry import DomainGrid
from ingest.mosaic.quality import DEFAULT_H_REF_M, DEFAULT_R_REF_M, beam_height_m, quality

Method = Literal["weighted", "lowest_beam"]
#: Espacio en el que se promedia. "dbz" pasa por Z lineal; "linear" promedia
#: la magnitud física tal cual (alturas de eco, VIL).
Space = Literal["dbz", "linear"]

_RANGE_NODATA = -1.0


@dataclass(frozen=True)
class SourceRaster:
    """Un COG de un sitio, ya decodificado, con lo que hace falta para componer."""

    site_id: str
    levels: np.ndarray  # (n, n) uint8, fila 0 = norte, malla AEQD centrada
    cell_m: float
    half_extent_m: float
    proj4: str
    scale: float
    offset: float
    el_angle: float | None  # None en derivados de volumen
    antenna_h_m: float

    @property
    def transform(self):
        return from_origin(-self.half_extent_m, self.half_extent_m, self.cell_m, self.cell_m)


@dataclass(frozen=True)
class SourceField:
    """Un sitio ya proyectado a la malla del dominio."""

    site_id: str
    phys: np.ndarray  # float32, NaN donde no hay dato
    quality: np.ndarray  # float32, 0 donde no hay dato
    rank: np.ndarray  # float32, menor = mejor (altura de haz, o rango)


def _source_range_m(src: SourceRaster) -> np.ndarray:
    """Rango al radar en la malla de origen: en AEQD centrada es exacto."""
    size = src.levels.shape[0]
    centers = -src.half_extent_m + (np.arange(size) + 0.5) * src.cell_m
    xx = centers[np.newaxis, :]
    yy = -centers[:, np.newaxis]
    return np.hypot(xx, yy).astype(np.float32)


def project_source(
    src: SourceRaster,
    domain: DomainGrid,
    *,
    h_ref_m: float = DEFAULT_H_REF_M,
    r_ref_m: float = DEFAULT_R_REF_M,
) -> SourceField:
    """Remuestrea un sitio a la malla del dominio y calcula su calidad ahí."""
    shape = (domain.height, domain.width)
    src_crs = CRS.from_proj4(src.proj4)
    dst_crs = domain.crs

    levels = np.full(shape, LEVEL_BELOW_THRESHOLD, dtype=np.uint8)
    reproject(
        source=src.levels,
        destination=levels,
        src_transform=src.transform,
        src_crs=src_crs,
        src_nodata=LEVEL_BELOW_THRESHOLD,
        dst_transform=domain.transform,
        dst_crs=dst_crs,
        dst_nodata=LEVEL_BELOW_THRESHOLD,
        resampling=Resampling.nearest,
    )

    range_m = np.full(shape, _RANGE_NODATA, dtype=np.float32)
    reproject(
        source=_source_range_m(src),
        destination=range_m,
        src_transform=src.transform,
        src_crs=src_crs,
        src_nodata=_RANGE_NODATA,
        dst_transform=domain.transform,
        dst_crs=dst_crs,
        dst_nodata=_RANGE_NODATA,
        resampling=Resampling.nearest,
    )

    # Nivel 1 (range folded) se descarta igual que el 0: es una propiedad del
    # radar individual y no tiene traducción en el compuesto.
    valid = (levels >= FLAG_LEVELS) & (range_m >= 0.0)

    phys = np.where(valid, levels.astype(np.float32) * src.scale + src.offset, np.nan)

    safe_range = np.where(valid, range_m, 0.0)
    if src.el_angle is None:
        beam_h = None
        rank = safe_range.astype(np.float32)
    else:
        beam_h = beam_height_m(safe_range, src.el_angle, src.antenna_h_m)
        rank = beam_h
    q = quality(safe_range, beam_h, h_ref_m=h_ref_m, r_ref_m=r_ref_m)

    return SourceField(
        site_id=src.site_id,
        phys=phys.astype(np.float32),
        quality=np.where(valid, q, 0.0).astype(np.float32),
        rank=np.where(valid, rank, np.inf).astype(np.float32),
    )


def _to_space(phys: np.ndarray, space: Space) -> np.ndarray:
    return np.power(10.0, phys / 10.0) if space == "dbz" else phys


def _from_space(value: np.ndarray, space: Space) -> np.ndarray:
    if space != "dbz":
        return value
    with np.errstate(divide="ignore", invalid="ignore"):
        return 10.0 * np.log10(value)


def compose(
    fields: list[SourceField],
    *,
    method: Method = "weighted",
    space: Space = "dbz",
) -> np.ndarray:
    """Campo físico compuesto (float32, NaN donde ningún radar aporta)."""
    if not fields:
        raise ValueError("compose necesita al menos un campo")
    shape = fields[0].phys.shape
    if any(f.phys.shape != shape for f in fields):
        raise ValueError("todos los campos deben estar en la misma malla")

    phys = np.stack([f.phys for f in fields])
    weights = np.stack([f.quality for f in fields])
    covered = np.any(weights > 0.0, axis=0)

    if method == "lowest_beam":
        rank = np.stack([f.rank for f in fields])
        pick = np.argmin(rank, axis=0)
        out = np.take_along_axis(phys, pick[np.newaxis], axis=0)[0]
    elif method == "weighted":
        values = _to_space(np.nan_to_num(phys, nan=0.0), space)
        values = np.where(weights > 0.0, values, 0.0)
        total = weights.sum(axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            mean = (values * weights).sum(axis=0) / total
        out = _from_space(mean, space)
    else:
        raise ValueError(f"método de composición desconocido: {method!r}")

    return np.where(covered, out, np.nan).astype(np.float32)


def quantize(
    phys: np.ndarray,
    *,
    scale: float,
    offset: float,
    max_level: int = 255,
) -> np.ndarray:
    """Físico → niveles del COG mosaico. Fuera de cobertura = 0 (nodata)."""
    if scale <= 0:
        raise ValueError(f"scale debe ser positivo ({scale})")
    covered = np.isfinite(phys)
    raw = np.rint((np.nan_to_num(phys, nan=0.0) - offset) / scale)
    levels = np.clip(raw, FLAG_LEVELS, max_level)
    return np.where(covered, levels, LEVEL_BELOW_THRESHOLD).astype(np.uint8)
