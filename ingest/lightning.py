"""Ingesta de rayos GLM (GOES-19 GLM-L2-LCFA) para la capa de rayos del viewer.

Puerto del Worker `nexrad-l3-lightning` (`workers/lightning/src/`), que dejó
de ser viable en el plan Free de Cloudflare Workers (el parse HDF5 de GLM,
~60 ms/frame, excede el cap de CPU sin poder subir `limits.cpu_ms`). Sin la
restricción de presupuesto de CPU/subrequests por invocación que forzaba el
split minutero/backfill-horario del Worker, aquí una sola pasada por cada
vuelta del loop cubre toda la ventana (`window_h`): barata cuando no hay
nada nuevo (un SELECT + diff contra D1), cara solo para los cubos que de
verdad faltan.

Fuente: listado S3 del bucket público `noaa-goes19`
(`GLM-L2-LCFA/{YYYY}/{DDD}/{HH}/`), ficheros netCDF-4/HDF5 cada 20 s a nivel
flash. Cubos fijos de 300 s alineados a UTC; fila SIEMPRE a
`lightning_buckets` al cerrar un cubo (incluso con 0 rayos). Contrato
completo en `db/README.md`.
"""

import io
import json
import logging
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import numpy as np

log = logging.getLogger(__name__)

BUCKET_S = 300  # contrato: cubos fijos alineados a UTC
FRAME_S = 20  # cadencia de ficheros GLM-L2-LCFA
FRAMES_PER_BUCKET = BUCKET_S // FRAME_S + 1  # +1: frame extra en bucket_end, ver frames_for_bucket
SOURCE = "glm-goes19"
EARTH_RADIUS_KM = 6371.0
DEFER_INCOMPLETE_S = 3600.0  # cubo con <16 frames se difiere hasta que tenga esta edad
GLM_BASE = "https://noaa-goes19.s3.amazonaws.com"

# fetcher inyectable: prefijo S3 → claves listadas
ListFn = Callable[[str], list[str]]
# fetcher inyectable: clave S3 → bytes del fichero, o None si no existe (404)
FetchFileFn = Callable[[str], bytes | None]

Strike = tuple[float, float, float]  # lon (3dec), lat (3dec), offset_s (1dec) desde bucket_start

_INSERT_SQL = """
INSERT OR IGNORE INTO lightning_buckets
    (site_id, bucket_start, bucket_s, strike_count, r2_key, size_bytes, source)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_KEY_START_RE = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)_")
_KEY_TAG_RE = re.compile(r"<Key>([^<]+)</Key>")
_UNITS_RE = re.compile(r"seconds since (\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(\.\d+)?")


class LightningDecodeError(Exception):
    pass


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _epoch(dt: datetime) -> float:
    """Epoch UTC de un datetime naive (convención del schema: siempre UTC)."""
    return dt.replace(tzinfo=UTC).timestamp()


def _from_epoch(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, tz=UTC).replace(tzinfo=None)


# ------------------------------------------------------------------ dominio


def eligible_bucket_starts(now: datetime, window_s: float, margin_s: float) -> list[datetime]:
    """Inicios de cubo elegibles en [now − window_s, now], del más nuevo al más viejo.

    Elegible = cerrado hace >= margin_s (latencia GLM).
    """
    now_s = _epoch(now)
    newest = math.floor((now_s - margin_s - BUCKET_S) / BUCKET_S) * BUCKET_S
    oldest = math.ceil((now_s - window_s) / BUCKET_S) * BUCKET_S
    out = []
    t = newest
    while t >= oldest:
        out.append(_from_epoch(t))
        t -= BUCKET_S
    return out


def lightning_key(site: str, bucket_start: datetime) -> str:
    """{SITE}/LIGHTNING/{Y}/{M}/{D}/{SITE}_LTG_{YYYYMMDD}_{HHMMSS}.json (inmutable)."""
    stamp = bucket_start.strftime("%Y%m%d_%H%M%S")
    return f"{site}/LIGHTNING/{bucket_start:%Y/%m/%d}/{site}_LTG_{stamp}.json"


def _day_of_year(dt: datetime) -> int:
    return dt.timetuple().tm_yday


def glm_hour_prefixes(bucket_start: datetime, product: str = "GLM-L2-LCFA") -> list[str]:
    """Prefijos horarios S3 que cubren los frames [start, start + BUCKET_S].

    Dos prefijos cuando el frame extra cae en la hora siguiente (cubos :55).
    """
    out: list[str] = []
    for t in (bucket_start, bucket_start + timedelta(seconds=BUCKET_S)):
        prefix = f"{product}/{t.year}/{_day_of_year(t):03d}/{t.hour:02d}/"
        if prefix not in out:
            out.append(prefix)
    return out


def glm_key_start_epoch(key: str) -> float | None:
    """Epoch (s UTC) del campo s del nombre LCFA (..._sYYYYDDDHHMMSSt_...)."""
    m = _KEY_START_RE.search(key)
    if not m:
        return None
    y, doy, hh, mi, ss, tenth = m.groups()
    base = datetime(int(y), 1, 1, tzinfo=UTC).timestamp()
    return (
        base + (int(doy) - 1) * 86_400 + int(hh) * 3600 + int(mi) * 60 + int(ss) + int(tenth) / 10
    )


def parse_s3_list_keys(xml: str) -> tuple[list[str], bool]:
    """Claves <Key>…</Key> de un listado REST de S3 (list-type=2)."""
    keys = _KEY_TAG_RE.findall(xml)
    truncated = "<IsTruncated>true</IsTruncated>" in xml
    return keys, truncated


def frames_for_bucket(keys: Iterable[str], bucket_start: datetime) -> list[str]:
    """Frames del cubo: s en [start, start + BUCKET_S], extremo superior inclusive."""
    start_s = _epoch(bucket_start)
    out = []
    for k in keys:
        s = glm_key_start_epoch(k)
        if s is not None and start_s <= s <= start_s + BUCKET_S:
            out.append(k)
    return out


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rad = math.pi / 180
    d_lat = (lat2 - lat1) * rad
    d_lon = (lon2 - lon1) * rad
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin(d_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def strikes_for_site(
    flashes: Iterable["Flash"],
    site_lat: float,
    site_lon: float,
    radius_km: float,
    bucket_start: datetime,
) -> list[Strike]:
    """Strikes de un sitio: flashes del cubo a <= radius_km, [lon, lat, offset_s] ascendente.

    El redondeo a 1 decimal puede tocar el techo (299.96 -> 300.0): se clava
    a BUCKET_S - 0.1 para mantener offset en [0, bucket_s).
    """
    start_s = _epoch(bucket_start)
    out: list[Strike] = []
    for f in flashes:
        if f.epoch_s < start_s or f.epoch_s >= start_s + BUCKET_S:
            continue
        if haversine_km(site_lat, site_lon, f.lat, f.lon) > radius_km:
            continue
        offset = min(round(f.epoch_s - start_s, 1), BUCKET_S - 0.1)
        out.append((round(f.lon, 3), round(f.lat, 3), offset))
    out.sort(key=lambda s: s[2])
    return out


def parse_units_base(units: str) -> float:
    """Epoch (s) de un atributo `units` GLM "seconds since YYYY-MM-DD HH:MM:SS(.mmm)"."""
    m = _UNITS_RE.search(units)
    if not m:
        raise LightningDecodeError(f'units de tiempo GLM no reconocidas: "{units}"')
    y, mo, d, hh, mi, ss, frac = m.groups()
    base = datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss), tzinfo=UTC).timestamp()
    return base + (float(frac) if frac else 0.0)


def encode_bucket_json(site: str, bucket_start: datetime, strikes: list[Strike]) -> bytes:
    """JSON del contrato: {site, bucket_start, bucket_s, strikes: [[lon,lat,offset_s],...]}."""
    doc = {
        "site": site,
        "bucket_start": _iso(bucket_start),
        "bucket_s": BUCKET_S,
        "strikes": [list(s) for s in strikes],
    }
    return json.dumps(doc, separators=(",", ":")).encode()


# -------------------------------------------------------------- HDF5 → Flash


@dataclass(frozen=True)
class Flash:
    lon: float
    lat: float
    epoch_s: float


def _attr_float(value: Any) -> float:
    """Atributos HDF5 llegan como array de 1 elemento o escalar (h5py, según fichero)."""
    if isinstance(value, np.ndarray):
        return float(value.reshape(-1)[0])
    return float(value)


def _attr_str(value: Any) -> str:
    if isinstance(value, np.ndarray):
        value = value.reshape(-1)[0]
    if isinstance(value, bytes | np.bytes_):
        return value.decode()
    return str(value)


def parse_glm(data: bytes) -> list[Flash]:
    """Fichero GLM-L2-LCFA (netCDF-4/HDF5) → flashes con flash_quality_flag == 0.

    Los offsets de tiempo son uint16 empaquetados (atributo `_Unsigned` sobre
    int16 crudo, inherente al modelo netCDF4-classic — no una particularidad
    de ninguna librería de parseo): hay que reinterpretar el bit pattern como
    uint16 antes de aplicar scale/offset, verificado contra un fichero real
    2026-07-20 (valores negativos de `flash_time_offset_of_first_event` sin
    la reinterpretación).
    """
    import h5py  # diferido: no hace falta para --help

    with h5py.File(io.BytesIO(data), "r") as f:
        lat = np.asarray(f["flash_lat"][:], dtype=np.float64)
        lon = np.asarray(f["flash_lon"][:], dtype=np.float64)
        toff_ds = f["flash_time_offset_of_first_event"]
        toff_raw = toff_ds[:]
        scale = _attr_float(toff_ds.attrs["scale_factor"])
        offset = _attr_float(toff_ds.attrs["add_offset"])
        base = parse_units_base(_attr_str(toff_ds.attrs["units"]))
        qf = f["flash_quality_flag"][:]

    epochs = base + toff_raw.astype(np.uint16).astype(np.float64) * scale + offset
    return [
        Flash(lon=float(lon[i]), lat=float(lat[i]), epoch_s=float(epochs[i]))
        for i in range(len(lat))
        if qf[i] == 0
    ]


# ---------------------------------------------------------------- ingestor


class LightningIngestor:
    """Una vuelta = ventana [now − window, now] barrida en R2+D1.

    Sin cap artificial de cubos por corrida (a diferencia del Worker: aquí
    no hay presupuesto de CPU/subrequests por invocación) — `max_buckets` es
    solo un cinturón de seguridad, no una restricción real.
    """

    def __init__(
        self,
        d1: Any,
        r2: Any,
        *,
        list_prefix: ListFn | None = None,
        fetch_file: FetchFileFn | None = None,
        base_url: str = GLM_BASE,
        window_h: float = 72.0,
        margin_s: float = 90.0,
        radius_km: float = 460.0,
        max_buckets: int = 200,
    ) -> None:
        self._d1 = d1
        self._r2 = r2
        self._base = base_url.rstrip("/")
        self._list_prefix = list_prefix or self._list_prefix_s3
        self._fetch_file = fetch_file or self._fetch_file_s3
        self._window_s = window_h * 3600.0
        self._margin_s = margin_s
        self._radius_km = radius_km
        self._max_buckets = max_buckets
        self._http: httpx.Client | None = None
        # cache por corrida: el listado S3 y los flashes ya parseados se
        # comparten entre cubos vecinos (frame frontera de uno es el mismo
        # fichero que abre el siguiente)
        self._list_cache: dict[str, list[str]] = {}
        self._file_cache: dict[str, list[Flash]] = {}

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                timeout=30.0, headers={"User-Agent": "nexrad-l3-pipeline/lightning"}
            )
        return self._http

    def _list_prefix_s3(self, prefix: str) -> list[str]:
        resp = self._client().get(
            f"{self._base}/", params={"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        )
        resp.raise_for_status()
        keys, truncated = parse_s3_list_keys(resp.text)
        if truncated:
            log.warning("lightning: listado truncado en %s (inesperado)", prefix)
        return keys

    def _fetch_file_s3(self, key: str) -> bytes | None:
        resp = self._client().get(f"{self._base}/{key}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    def _list(self, prefix: str) -> list[str]:
        if prefix not in self._list_cache:
            self._list_cache[prefix] = self._list_prefix(prefix)
        return self._list_cache[prefix]

    def _flashes(self, key: str) -> list[Flash]:
        if key not in self._file_cache:
            data = self._fetch_file(key)
            self._file_cache[key] = parse_glm(data) if data is not None else []
        return self._file_cache[key]

    def run_once(self, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(UTC).replace(tzinfo=None)
        self._list_cache.clear()
        self._file_cache.clear()
        stats = {"buckets": 0, "rows": 0, "objects": 0, "deferred": 0, "failed": 0}

        sites = self._d1.execute("SELECT site_id, lat, lon FROM radars ORDER BY site_id")
        if not sites:
            log.info("lightning: sin radares en D1 todavía — nada que hacer")
            return stats

        candidates = eligible_bucket_starts(now, self._window_s, self._margin_s)
        if not candidates:
            return stats
        oldest = _iso(candidates[-1])
        existing = self._d1.execute(
            "SELECT site_id, bucket_start FROM lightning_buckets WHERE bucket_start >= ?",
            [oldest],
        )
        have = {(row["site_id"], row["bucket_start"]) for row in existing}

        targets = [
            b for b in candidates if any((s["site_id"], _iso(b)) not in have for s in sites)
        ][: self._max_buckets]

        for bucket in targets:
            try:
                n = self._ingest_bucket(bucket, sites, have, now, stats)
                stats["rows"] += n
                if n:
                    stats["buckets"] += 1
            except Exception:
                log.exception(
                    "lightning: fallo en cubo %s (reintento próxima corrida)", _iso(bucket)
                )
                stats["failed"] += 1

        log.info(
            "lightning: cubos=%d filas=%d objetos=%d diferidos=%d fallidos=%d",
            stats["buckets"],
            stats["rows"],
            stats["objects"],
            stats["deferred"],
            stats["failed"],
        )
        return stats

    def _ingest_bucket(
        self,
        start: datetime,
        sites: list[dict],
        have: set[tuple[str, str]],
        now: datetime,
        stats: dict[str, int],
    ) -> int:
        start_iso = _iso(start)
        pending = [s for s in sites if (s["site_id"], start_iso) not in have]
        if not pending:
            return 0

        keys: list[str] = []
        for prefix in glm_hour_prefixes(start):
            keys.extend(self._list(prefix))
        frames = frames_for_bucket(keys, start)

        age_s = (now - start).total_seconds() - BUCKET_S
        if len(frames) < FRAMES_PER_BUCKET and age_s < DEFER_INCOMPLETE_S:
            stats["deferred"] += 1
            log.info(
                "lightning: %s con %d/%d frames — se difiere",
                start_iso,
                len(frames),
                FRAMES_PER_BUCKET,
            )
            return 0
        if len(frames) < FRAMES_PER_BUCKET:
            log.warning(
                "lightning: %s incompleto (%d/%d frames), se ingiere igual",
                start_iso,
                len(frames),
                FRAMES_PER_BUCKET,
            )

        flashes: list[Flash] = []
        for key in frames:
            flashes.extend(self._flashes(key))

        for site in pending:
            strikes = strikes_for_site(flashes, site["lat"], site["lon"], self._radius_km, start)
            r2_key: str | None = None
            size: int | None = None
            if strikes:
                r2_key = lightning_key(site["site_id"], start)
                body = encode_bucket_json(site["site_id"], start, strikes)
                size = len(body)
                # orden: R2 → D1, igual que el Worker — si D1 falla, el
                # objeto queda huérfano y lo recoge la reconciliación de ops
                self._r2.upload_bytes(body, r2_key, content_type="application/json")
                stats["objects"] += 1
            self._d1.execute(
                _INSERT_SQL,
                [site["site_id"], start_iso, BUCKET_S, len(strikes), r2_key, size, SOURCE],
            )
            have.add((site["site_id"], start_iso))
        return len(pending)


def run_lightning(
    ingestor: LightningIngestor,
    *,
    interval_s: float = 60.0,
    heartbeat: Path | None = None,
    stop: Event | None = None,
) -> None:
    """Servicio: barrido periódico con heartbeat para `l3proc health`."""
    stop = stop or Event()
    if heartbeat is not None:
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.touch()  # healthcheck verde desde el arranque, no tras la 1.ª corrida
    log.info("lightning: cada %.0f s, ventana %.0f h", interval_s, ingestor._window_s / 3600)
    while not stop.is_set():
        try:
            ingestor.run_once()
        except Exception:
            log.exception("lightning: corrida fallida (se reintenta)")
        if heartbeat is not None:
            heartbeat.touch()
        stop.wait(interval_s)
