"""Ingesta de viento GFS 0.25° (u/v) para la capa de partículas del viewer.

Fuente: filtro de NOMADS (subsets de decenas de KB, no el GRIB global).
Un fichero por (ciclo, forecast hour, nivel) con el bbox unión de todos
los sitios; el recorte por sitio es local — la grilla GFS es regular
0.25° y los bordes van alineados a múltiplos de 0.25°, así que el subset
es puro índice, sin resampleo. El JSON por sitio va a R2 (clave
inmutable, el ciclo en el nombre) y la fila a D1 con upsert que solo
gana si el ``cycle_time`` nuevo es mayor. El estado vive en D1: no hay
watermark local y re-ejecutar sin datos nuevos no reescribe nada.

Fase 2 (niveles de altura, acordada 2026-07-20): además de la
superficie 10 m se ingesta la terna "steering flow" 850/700/500 hPa. El
viewer tiene selector de altura pero muestra un nivel a la vez — el
contrato sigue siendo una fila por lookup (PK con ``level``), no hace
falta empaquetar los 3 niveles juntos.

No cubre radares que crucen el antimeridiano (lon ±180): el bbox se
calcula en el dominio [-180, 180) sin envolver.
"""

import json
import logging
import math
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import numpy as np

log = logging.getLogger(__name__)

GRID_STEP = 0.25  # grados; grilla GFS 0p25
HALF_SPAN_DEG = 6.0  # dominio por sitio: radar ± 6°
FH_MAX = 12  # forecast hour máximo aceptado (ciclos cada 6 h → ~2 h de colchón)
CYCLE_STEP_H = 6  # ciclos GFS: 00/06/12/18Z
MODEL = "gfs0p25"
NOMADS_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"


@dataclass(frozen=True)
class Level:
    """Nivel de viento: nombre del contrato (PK/r2_key) + claves GRIB/NOMADS."""

    name: str  # '10m' | '850hPa' | '700hPa' | '500hPa'
    nomads_lev: str  # parámetro lev_* del filtro NOMADS
    u_short: str  # shortName GRIB del componente u en ese nivel
    v_short: str  # shortName GRIB del componente v en ese nivel


LEVEL_10M = Level("10m", "lev_10_m_above_ground", "10u", "10v")
# terna "steering flow": mismo shortName u/v en todos los niveles isobáricos
LEVELS: tuple[Level, ...] = (
    LEVEL_10M,
    Level("850hPa", "lev_850_mb", "u", "v"),
    Level("700hPa", "lev_700_mb", "u", "v"),
    Level("500hPa", "lev_500_mb", "u", "v"),
)
LEVELS_BY_NAME: dict[str, Level] = {level.name: level for level in LEVELS}


def resolve_levels(names: Iterable[str]) -> tuple[Level, ...]:
    """Nombres de nivel (CLI/env, coma-separados ya partidos) → Level, en orden."""
    out = []
    for name in names:
        try:
            out.append(LEVELS_BY_NAME[name])
        except KeyError:
            raise ValueError(
                f"nivel de viento desconocido: {name!r} (válidos: {sorted(LEVELS_BY_NAME)})"
            ) from None
    return tuple(out)


# tipo del fetcher inyectable: (ciclo, fh, bbox, nivel) → GRIB2 crudo o None
# si ese fichero aún no está publicado en NOMADS
FetchFn = Callable[[datetime, int, "BBox", Level], bytes | None]

_UPSERT_SQL = """
INSERT INTO wind_grids
    (site_id, valid_time, level, cycle_time, forecast_hour, model, r2_key, size_bytes)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (site_id, valid_time, level) DO UPDATE SET
    cycle_time = excluded.cycle_time,
    forecast_hour = excluded.forecast_hour,
    model = excluded.model,
    r2_key = excluded.r2_key,
    size_bytes = excluded.size_bytes
WHERE excluded.cycle_time > wind_grids.cycle_time
"""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _ceil_hour(dt: datetime) -> datetime:
    floor = _floor_hour(dt)
    return floor if floor == dt else floor + timedelta(hours=1)


# ------------------------------------------------------------------ dominio


@dataclass(frozen=True)
class BBox:
    """Caja lat/lon con bordes alineados a la grilla GFS (múltiplos de 0.25°)."""

    north: float
    south: float
    west: float
    east: float

    @property
    def nx(self) -> int:
        return round((self.east - self.west) / GRID_STEP) + 1

    @property
    def ny(self) -> int:
        return round((self.north - self.south) / GRID_STEP) + 1


def site_bbox(lat: float, lon: float) -> BBox:
    """radar ± 6°, expandido hacia fuera hasta múltiplos de 0.25°.

    Los nodos coinciden exactamente con la grilla GFS → subset puro.
    """
    return BBox(
        north=math.ceil((lat + HALF_SPAN_DEG) / GRID_STEP) * GRID_STEP,
        south=math.floor((lat - HALF_SPAN_DEG) / GRID_STEP) * GRID_STEP,
        west=math.floor((lon - HALF_SPAN_DEG) / GRID_STEP) * GRID_STEP,
        east=math.ceil((lon + HALF_SPAN_DEG) / GRID_STEP) * GRID_STEP,
    )


def union_bbox(boxes: Iterable[BBox]) -> BBox:
    boxes = list(boxes)
    return BBox(
        north=max(b.north for b in boxes),
        south=min(b.south for b in boxes),
        west=min(b.west for b in boxes),
        east=max(b.east for b in boxes),
    )


def candidate_cycles(valid_time: datetime) -> list[tuple[datetime, int]]:
    """(ciclo, fh) con fh en 0..FH_MAX, del ciclo más nuevo al más viejo."""
    cycle = valid_time.replace(
        hour=(valid_time.hour // CYCLE_STEP_H) * CYCLE_STEP_H,
        minute=0,
        second=0,
        microsecond=0,
    )
    out: list[tuple[datetime, int]] = []
    while (fh := round((valid_time - cycle).total_seconds() / 3600)) <= FH_MAX:
        out.append((cycle, fh))
        cycle -= timedelta(hours=CYCLE_STEP_H)
    return out


def wind_key(site_id: str, valid_time: datetime, cycle_time: datetime, fh: int, level: str) -> str:
    """{SITE}/WIND/{Y}/{M}/{D}/{SITE}_WIND_{ts}_c{ciclo}f{FFF}_{level}.json (inmutable).

    El nivel va en el nombre (no solo en la fila D1): niveles distintos del
    mismo (site, valid_time, cycle, fh) son objetos R2 distintos.
    """
    stamp = valid_time.strftime("%Y%m%d_%H%M%S")
    return (
        f"{site_id}/WIND/{valid_time:%Y/%m/%d}/"
        f"{site_id}_WIND_{stamp}_c{cycle_time:%Y%m%d%H}f{fh:03d}_{level}.json"
    )


# -------------------------------------------------------------- GRIB → JSON


@dataclass(frozen=True)
class WindField:
    """u/v 10 m en grilla regular, row-major desde la esquina NO.

    ``la1`` = latitud norte, ``lo1`` = longitud oeste en [-180, 180);
    filas norte→sur, columnas oeste→este (convención GRIB de GFS).
    """

    la1: float
    lo1: float
    dx: float
    dy: float
    u: np.ndarray  # (ny, nx) m/s
    v: np.ndarray


class WindDecodeError(Exception):
    pass


def _split_grib(data: bytes) -> Iterable[bytes]:
    """Mensajes individuales de un GRIB2 concatenado (longitud en la sección 0)."""
    offset = 0
    while offset < len(data):
        if data[offset : offset + 4] != b"GRIB":
            raise WindDecodeError(f"basura en offset {offset}: no empieza con GRIB")
        length = int.from_bytes(data[offset + 8 : offset + 16], "big")
        yield data[offset : offset + length]
        offset += length


def decode_grib(data: bytes, level: Level = LEVEL_10M) -> WindField:
    """GRIB2 de NOMADS (UGRD+VGRD del nivel dado) → WindField. No reordena filas."""
    import eccodes as ec  # diferido: carga la lib C, no hace falta para --help

    fields: dict[str, np.ndarray] = {}
    meta: tuple[float, float, float, float] | None = None
    for msg in _split_grib(data):
        handle = ec.codes_new_from_message(msg)
        try:
            short = ec.codes_get(handle, "shortName")
            if short not in (level.u_short, level.v_short):
                continue
            if ec.codes_get(handle, "iScansNegatively") != 0:
                raise WindDecodeError("grilla este→oeste inesperada (iScansNegatively=1)")
            ni = ec.codes_get(handle, "Ni")
            nj = ec.codes_get(handle, "Nj")
            la1 = float(ec.codes_get(handle, "latitudeOfFirstGridPointInDegrees"))
            lo1 = float(ec.codes_get(handle, "longitudeOfFirstGridPointInDegrees"))
            dx = float(ec.codes_get(handle, "iDirectionIncrementInDegrees"))
            dy = float(ec.codes_get(handle, "jDirectionIncrementInDegrees"))
            values = ec.codes_get_values(handle).reshape(nj, ni)
            # Los ficheros GFS crudos van norte→sur, pero el filtro de NOMADS
            # re-empaqueta el subset sur→norte (jScansPositively=1, verificado
            # 2026-07-18). El contrato es norte→sur — voltear filas.
            if ec.codes_get(handle, "jScansPositively") == 1:
                values = values[::-1]
                la1 = float(ec.codes_get(handle, "latitudeOfLastGridPointInDegrees"))
            if lo1 >= 180.0:  # GFS usa 0–360; el contrato pide [-180, 180)
                lo1 -= 360.0
            grid = (la1, lo1, dx, dy)
            if meta is None:
                meta = grid
            elif meta != grid:
                raise WindDecodeError(f"grillas u/v distintas: {meta} vs {grid}")
            fields[short] = values
        finally:
            ec.codes_release(handle)
    if meta is None or set(fields) != {level.u_short, level.v_short}:
        raise WindDecodeError(f"faltan mensajes u/v de {level.name} (presentes: {sorted(fields)})")
    la1, lo1, dx, dy = meta
    return WindField(
        la1=la1, lo1=lo1, dx=dx, dy=dy, u=fields[level.u_short], v=fields[level.v_short]
    )


def _index(offset_deg: float, step: float, what: str) -> int:
    idx = offset_deg / step
    if abs(idx - round(idx)) > 1e-6:
        raise WindDecodeError(f"{what} no alineado a la grilla (offset {offset_deg}°)")
    return round(idx)


def subset(field: WindField, box: BBox) -> WindField:
    """Recorte por índice a un bbox alineado; el bbox debe caber en el campo."""
    row0 = _index(field.la1 - box.north, field.dy, "borde norte")
    col0 = _index(box.west - field.lo1, field.dx, "borde oeste")
    ny, nx = box.ny, box.nx
    rows, cols = field.u.shape
    if row0 < 0 or col0 < 0 or row0 + ny > rows or col0 + nx > cols:
        raise WindDecodeError(f"bbox {box} fuera del campo descargado ({rows}×{cols})")
    return WindField(
        la1=box.north,
        lo1=box.west,
        dx=field.dx,
        dy=field.dy,
        u=field.u[row0 : row0 + ny, col0 : col0 + nx],
        v=field.v[row0 : row0 + ny, col0 : col0 + nx],
    )


def encode_json(field: WindField, cycle_time: datetime, fh: int) -> bytes:
    """Formato del contrato: header + u/v planos en m/s a 2 decimales."""
    ny, nx = field.u.shape
    doc = {
        "header": {
            "nx": nx,
            "ny": ny,
            "lo1": field.lo1,
            "la1": field.la1,
            "dx": field.dx,
            "dy": field.dy,
            "refTime": _iso(cycle_time) + "Z",
            "forecastHour": fh,
        },
        "u": [round(float(x), 2) for x in field.u.ravel()],
        "v": [round(float(x), 2) for x in field.v.ravel()],
    }
    return json.dumps(doc, separators=(",", ":")).encode()


# ---------------------------------------------------------------- ingestor


class WindIngestor:
    """Una corrida = ventana [now − window, now + lookahead] al día en R2+D1.

    Idempotente y parcial-tolerante: un valid_time que falle no aborta el
    resto; el reintento es natural en la corrida siguiente.
    """

    def __init__(
        self,
        d1: Any,
        r2: Any,
        *,
        fetch: FetchFn | None = None,
        levels: Sequence[Level] = LEVELS,
        window_h: float = 72.0,
        lookahead_h: float = 2.0,
        pause_s: float = 2.0,
    ) -> None:
        self._d1 = d1
        self._r2 = r2
        self._fetch = fetch or self._fetch_nomads
        self._levels = levels
        self._window = timedelta(hours=window_h)
        self._lookahead = timedelta(hours=lookahead_h)
        self._pause_s = pause_s
        self._http: httpx.Client | None = None
        # cache por corrida: (ciclo, fh, nivel) → campo decodificado o None (no publicado)
        self._cache: dict[tuple[datetime, int, str], WindField | None] = {}

    def _fetch_nomads(self, cycle: datetime, fh: int, box: BBox, level: Level) -> bytes | None:
        if self._http is None:
            self._http = httpx.Client(
                timeout=60.0,
                follow_redirects=True,
                headers={"User-Agent": "nexrad-l3-pipeline/wind"},
            )
        # cortesía con NOMADS: secuencial con pausa (bloquean IPs > ~120 hits/min)
        time.sleep(self._pause_s)
        resp = self._http.get(
            NOMADS_FILTER,
            params={
                "dir": f"/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos",
                "file": f"gfs.t{cycle:%H}z.pgrb2.0p25.f{fh:03d}",
                "var_UGRD": "on",
                "var_VGRD": "on",
                level.nomads_lev: "on",
                "subregion": "",
                "toplat": box.north,
                "bottomlat": box.south,
                "leftlon": box.west % 360,  # el filtro trabaja en 0–360
                "rightlon": box.east % 360,
            },
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        if not resp.content.startswith(b"GRIB"):
            # el filtro a veces responde 200 con HTML de "data file is not present"
            log.debug("wind: respuesta no-GRIB para %s f%03d %s", _iso(cycle), fh, level.name)
            return None
        return resp.content

    def _field(self, cycle: datetime, fh: int, box: BBox, level: Level) -> WindField | None:
        key = (cycle, fh, level.name)
        if key not in self._cache:
            data = self._fetch(cycle, fh, box, level)
            self._cache[key] = decode_grib(data, level) if data is not None else None
        return self._cache[key]

    def run_once(self, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(UTC).replace(tzinfo=None)
        self._cache.clear()  # la disponibilidad en NOMADS cambia entre corridas
        stats = {"published": 0, "fresh": 0, "failed": 0}

        sites = self._d1.execute("SELECT site_id, lat, lon FROM radars ORDER BY site_id")
        if not sites:
            log.info("wind: sin radares en D1 todavía — nada que hacer")
            return stats
        boxes = {row["site_id"]: site_bbox(row["lat"], row["lon"]) for row in sites}
        union = union_bbox(boxes.values())

        existing: dict[tuple[str, str, str], tuple[str, str]] = {}
        rows = self._d1.execute(
            "SELECT site_id, valid_time, level, cycle_time, r2_key FROM wind_grids"
        )
        for row in rows:
            existing[(row["site_id"], row["valid_time"], row["level"])] = (
                row["cycle_time"],
                row["r2_key"],
            )

        vt = _ceil_hour(now - self._window)
        last = _floor_hour(now + self._lookahead)
        while vt <= last:
            try:
                n = self._ingest_valid_time(vt, boxes, union, existing)
                stats["published"] += n
                if n == 0:
                    stats["fresh"] += 1
            except Exception:
                log.exception("wind: fallo en valid_time %s (reintento en próxima corrida)", vt)
                stats["failed"] += 1
            vt += timedelta(hours=1)

        log.info(
            "wind: publicados=%d al_día=%d fallidos=%d",
            stats["published"],
            stats["fresh"],
            stats["failed"],
        )
        return stats

    def _ingest_valid_time(
        self,
        vt: datetime,
        boxes: dict[str, BBox],
        union: BBox,
        existing: dict[tuple[str, str, str], tuple[str, str]],
    ) -> int:
        vt_s = _iso(vt)
        return sum(
            self._ingest_level(vt, vt_s, level, boxes, union, existing) for level in self._levels
        )

    def _ingest_level(
        self,
        vt: datetime,
        vt_s: str,
        level: Level,
        boxes: dict[str, BBox],
        union: BBox,
        existing: dict[tuple[str, str, str], tuple[str, str]],
    ) -> int:
        for cycle, fh in candidate_cycles(vt):
            cycle_s = _iso(cycle)
            wanting = [
                site
                for site in boxes
                if (row := existing.get((site, vt_s, level.name))) is None or row[0] < cycle_s
            ]
            if not wanting:
                # todos tienen un ciclo >= que cualquier candidato restante
                return 0
            field = self._field(cycle, fh, union, level)
            if field is None:
                continue  # ciclo aún no publicado en NOMADS → probar uno más viejo
            for site in wanting:
                self._publish(site, boxes[site], vt, cycle, fh, level, field, existing)
            return len(wanting)
        return 0

    def _publish(
        self,
        site: str,
        box: BBox,
        vt: datetime,
        cycle: datetime,
        fh: int,
        level: Level,
        field: WindField,
        existing: dict[tuple[str, str, str], tuple[str, str]],
    ) -> None:
        body = encode_json(subset(field, box), cycle, fh)
        key = wind_key(site, vt, cycle, fh, level.name)
        vt_s, cycle_s = _iso(vt), _iso(cycle)
        ekey = (site, vt_s, level.name)
        old = existing.get(ekey)
        # orden: R2 → D1 → borrar el reemplazado. Si D1 falla, el objeto
        # nuevo queda huérfano y lo recoge la reconciliación del Worker.
        self._r2.upload_bytes(body, key, content_type="application/json")
        self._d1.execute(_UPSERT_SQL, [site, vt_s, level.name, cycle_s, fh, MODEL, key, len(body)])
        if old is not None and old[1] != key:
            self._r2.delete_keys([old[1]])
        existing[ekey] = (cycle_s, key)


def run_wind(
    ingestor: WindIngestor,
    *,
    interval_s: float = 3600.0,
    heartbeat: Path | None = None,
    stop: Event | None = None,
) -> None:
    """Servicio: corrida horaria con heartbeat para `l3proc health`."""
    stop = stop or Event()
    if heartbeat is not None:
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.touch()  # healthcheck verde desde el arranque, no tras la 1.ª corrida
    log.info("wind: cada %.0f s, ventana %s", interval_s, ingestor._window)
    while not stop.is_set():
        try:
            ingestor.run_once()
        except Exception:
            log.exception("wind: corrida fallida (se reintenta)")
        if heartbeat is not None:
            heartbeat.touch()
        stop.wait(interval_s)
