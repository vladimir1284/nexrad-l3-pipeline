"""Parsing de productos de fenómenos sobre los bloques que expone MetPy.

Cobertura (verificada contra el feed real 2026-07-10):

- 58/NST — tracking de celdas (SCIT): posición + ID por celda del bloque
  Symbology, movimiento/posiciones previstas de la página tabular.
- 141/NMD — mesociclones (MDA): círculo (posición + radio) + ID del
  Symbology, atributos (RV/DV, base/profundidad, flag TVS, MSI) de la
  página tabular.

Los productos NHI (granizo) y NTV (TVS) **no fluyen en el bucket**
(verificado barriendo junio-julio 2026 en sitios con tormentas): la
señal de tornado queda cubierta por la columna TVS del NMD; granizo sin
cobertura en el demo.

Posiciones: MetPy entrega x/y en km radar-céntricos (este/norte) — son
coordenadas AEQD, la conversión a lat/lon es la inversa exacta de la
proyección de los COG.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from metpy.io import Level3File
from pyproj import Transformer

from ingest.decoder.level3 import UnsupportedProductError
from ingest.gridding.aeqd import radar_proj4
from ingest.products import PHENOMENA_PRODUCTS


@dataclass(frozen=True)
class PhenomenonRecord:
    kind: str
    cell_id: str
    lat: float
    lon: float
    azimuth_deg: float
    range_km: float
    attrs: dict


@dataclass(frozen=True)
class PhenomenaProduct:
    site_id: str
    lat: float
    lon: float
    height_m: float
    code: int
    mnemonic: str
    vol_time: datetime
    records: list[PhenomenonRecord] = field(default_factory=list)


def _polar(x_km: float, y_km: float) -> tuple[float, float]:
    az = float(np.degrees(np.arctan2(x_km, y_km)) % 360)
    return az, float(np.hypot(x_km, y_km))


# ── NST (58) ────────────────────────────────────────────────────────────
# Fila tabular: "  A8     231/112   130/ 34   ..." (movimiento o NEW)
_NST_ROW = re.compile(r"^\s*(\w\d)\s+(\d+)/\s*(\d+)\s+(?:(\d+)/\s*(\d+)|NEW)", re.MULTILINE)


def _parse_nst(f: Level3File) -> list[tuple[str, float, float, dict]]:
    tab_attrs: dict[str, dict] = {}
    for page in getattr(f, "tab_pages", None) or []:
        for m in _NST_ROW.finditer(page):
            cid, az, rng, mv_deg, mv_kt = m.groups()
            attrs = {"azran_nm": [int(az), int(rng)]}
            if mv_deg is not None:
                attrs["movement_deg"] = int(mv_deg)
                attrs["movement_kt"] = int(mv_kt)
            else:
                attrs["new"] = True
            tab_attrs[cid] = attrs

    out = []
    for layer in getattr(f, "sym_block", None) or []:
        for pkt in layer:
            if isinstance(pkt, dict) and pkt.get("type") == "Storm ID":
                cid = str(pkt["id"]).strip()
                out.append((cid, float(pkt["x"]), float(pkt["y"]), tab_attrs.get(cid, {})))
    return out


# ── NMD (141) ───────────────────────────────────────────────────────────
# Fila tabular MDA:
#  CIRC  AZRAN  SR STM  RV  DV  BASE  DEPTH STMREL%  MAXRV(kft kts) TVS  MOTION  MSI
_NMD_ROW = re.compile(
    r"^\s*(\d+)\s+(\d+)/\s*(\d+)\s+(\d+)\s+(\w\d)\s+(\d+)\s+(-?\d+)\s+[<>]?\s*(\d+)"
    r"\s+[<>]?\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([YN])(?:\s+(\d+)/\s*(\d+))?\s+(\d+)\s*$",
    re.MULTILINE,
)


def _parse_nmd(f: Level3File) -> list[tuple[str, float, float, dict]]:
    tab_attrs: dict[str, dict] = {}
    for page in getattr(f, "tab_pages", None) or []:
        for m in _NMD_ROW.finditer(page):
            (
                cid,
                az,
                rng,
                sr,
                stm,
                rv,
                dv,
                base,
                depth,
                stmrel,
                maxrv_kft,
                maxrv_kt,
                tvs,
                mv_deg,
                mv_kt,
                msi,
            ) = m.groups()
            attrs = {
                "azran_nm": [int(az), int(rng)],
                "strength_rank": int(sr),
                "storm_id": stm,
                "low_level_rv_kt": int(rv),
                "low_level_dv_kt": int(dv),
                "base_kft": int(base),
                "depth_kft": int(depth),
                "depth_stmrel_pct": int(stmrel),
                "max_rv_kft": int(maxrv_kft),
                "max_rv_kt": int(maxrv_kt),
                "tvs": tvs == "Y",
                "msi": int(msi),
            }
            if mv_deg is not None:
                attrs["movement_deg"] = int(mv_deg)
                attrs["movement_kt"] = int(mv_kt)
            tab_attrs[cid] = attrs

    circles: list[tuple[float, float, float]] = []
    labels: dict[tuple[float, float], str] = {}
    for layer in getattr(f, "sym_block", None) or []:
        for pkt in layer:
            if not isinstance(pkt, dict):
                continue
            if pkt.get("type") == "MDA":
                circles.append((float(pkt["x"]), float(pkt["y"]), float(pkt["radius"])))
            elif "text" in pkt and "x" in pkt:
                labels[(float(pkt["x"]), float(pkt["y"]))] = str(pkt["text"]).strip()

    out = []
    for x, y, radius in circles:
        cid = labels.get((x, y), "")
        attrs = {"radius_km": radius, **tab_attrs.get(cid, {})}
        out.append((cid, x, y, attrs))
    return out


_PARSERS = {58: _parse_nst, 141: _parse_nmd}


def parse_file(path: str | Path) -> PhenomenaProduct:
    f = Level3File(str(path))
    code = f.prod_desc.prod_code
    if code not in PHENOMENA_PRODUCTS:
        raise UnsupportedProductError(f"producto {code} no es de fenómenos soportados")
    mnemonic, kind = PHENOMENA_PRODUCTS[code]

    to_wgs84 = Transformer.from_crs(radar_proj4(f.lat, f.lon), "EPSG:4326", always_xy=True)
    records = []
    for cell_id, x_km, y_km, attrs in _PARSERS[code](f):
        lon, lat = to_wgs84.transform(x_km * 1000.0, y_km * 1000.0)
        az, rng = _polar(x_km, y_km)
        records.append(
            PhenomenonRecord(
                kind=kind,
                cell_id=cell_id,
                lat=round(float(lat), 5),
                lon=round(float(lon), 5),
                azimuth_deg=round(az, 1),
                range_km=round(rng, 2),
                attrs=attrs,
            )
        )

    return PhenomenaProduct(
        site_id=f.siteID,
        lat=f.lat,
        lon=f.lon,
        height_m=float(f.height),
        code=code,
        mnemonic=mnemonic,
        vol_time=f.metadata["vol_time"],
        records=records,
    )
