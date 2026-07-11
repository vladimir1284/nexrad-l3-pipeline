"""Parsing de productos de fenómenos sobre los bloques que expone MetPy.

Cobertura (verificada contra el feed real 2026-07-10):

- 58/NST — tracking de celdas (SCIT): posición + ID por celda y
  trayectorias past/forecast (packets 23/24) del bloque Symbology,
  movimiento de la página tabular, dBZ máx + altura del bloque Graphic
  Alphanumeric (GAB).
- 141/NMD — mesociclones (MDA): círculo (posición + radio) + ID del
  Symbology, atributos (RV/DV, base/profundidad, flag TVS, MSI) de la
  página tabular.

Los productos NHI (granizo), NTV (TVS) y NSS (storm structure: VIL/top
por celda) **no fluyen en el bucket** (verificado barriendo junio-julio
2026 en sitios con tormentas): la señal de tornado queda cubierta por la
columna TVS del NMD; granizo y VIL/top por celda sin cobertura en el
demo. El propio NST no los trae — la tabla "STORM CELL ATTRIBUTES" de
los visores es un compuesto cliente de STI+SS+HI.

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
# GAB: fila "STORM ID" con IDs letra+dígito; fila "DBZM HGT" con pares dBZ altura-kft
_GAB_ID = re.compile(r"\b([A-Z]\d)\b")
_GAB_DBZM = re.compile(r"(\d+)\s+(\d+\.\d+)")


def _nst_gab_attrs(f: Level3File) -> dict[str, dict]:
    """dBZ máx + altura por celda del bloque Graphic Alphanumeric.

    Cada página del GAB es una tabla de hasta 6 celdas: fila STORM ID y
    fila DBZM HGT alineadas por columnas — se emparejan por posición.
    """
    out: dict[str, dict] = {}
    for page in getattr(f, "graph_pages", None) or []:
        ids: list[str] = []
        dbzm: list[tuple[str, str]] = []
        for item in page:
            text = item.get("text", "") if isinstance(item, dict) else ""
            label = text.strip()[:8]
            if label == "STORM ID":
                ids = _GAB_ID.findall(text)
            elif label == "DBZM HGT":
                dbzm = _GAB_DBZM.findall(text)
        if len(ids) == len(dbzm):
            for cid, (dbz, hgt_kft) in zip(ids, dbzm, strict=True):
                out[cid] = {"dbz_max": int(dbz), "dbz_max_height_kft": float(hgt_kft)}
    return out


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

    gab_attrs = _nst_gab_attrs(f)

    # packets 23/24 (SCIT): track[0] es la posición actual de la celda —
    # misma codificación cuarto-de-km que el packet Storm ID, la
    # asociación por igualdad exacta de coordenadas es segura
    cells: list[tuple[str, float, float]] = []
    tracks: dict[tuple[float, float], dict] = {}
    for layer in getattr(f, "sym_block", None) or []:
        for pkt in layer:
            if not isinstance(pkt, dict):
                continue
            if pkt.get("type") == "Storm ID":
                cells.append((str(pkt["id"]).strip(), float(pkt["x"]), float(pkt["y"])))
            elif "track" in pkt and pkt.get("markers"):
                markers = pkt["markers"]
                first = markers[0] if isinstance(markers, list) else markers
                key = "past" if "past storm position" in first else "forecast"
                track = [[float(x), float(y)] for x, y in pkt["track"]]
                tracks.setdefault((track[0][0], track[0][1]), {})[key] = track[1:]

    out = []
    for cid, x, y in cells:
        attrs = {**tab_attrs.get(cid, {}), **gab_attrs.get(cid, {}), **tracks.get((x, y), {})}
        out.append((cid, x, y, attrs))
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
