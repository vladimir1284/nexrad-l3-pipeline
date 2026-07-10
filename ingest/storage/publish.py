"""Publicación de un COG procesado: objeto a R2 + metadata a D1.

Orden de statements pensado para cortes a mitad: primero dimensiones
(radar, producto — upserts idempotentes), después el hecho (raster).
Republicar el mismo volumen es idempotente (upsert por clave natural).
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ingest.decoder.level3 import RadialProduct
from ingest.gridding.aeqd import AeqdGrid, radar_proj4
from ingest.phenomena.parse import PhenomenaProduct
from ingest.storage.d1 import D1Client
from ingest.storage.keys import raster_key
from ingest.storage.r2 import R2Client

UPSERT_RADAR = """
INSERT INTO radars (site_id, icao, lat, lon, height_m, proj4, first_seen_at, last_seen_at)
VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
ON CONFLICT (site_id) DO UPDATE SET
    lat = excluded.lat,
    lon = excluded.lon,
    height_m = excluded.height_m,
    proj4 = excluded.proj4,
    last_seen_at = excluded.last_seen_at
"""

UPSERT_PRODUCT = """
INSERT INTO products (code, mnemonic, unit, kind)
VALUES (?, ?, ?, ?)
ON CONFLICT (code) DO UPDATE SET
    mnemonic = excluded.mnemonic,
    unit = excluded.unit,
    kind = excluded.kind
"""

UPSERT_RASTER = """
INSERT INTO rasters (
    site_id, product_code, vol_time, r2_key, size_bytes, el_angle, vcp,
    value_scale, value_offset, max_level, width, height, cell_m, created_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (site_id, product_code, vol_time) DO UPDATE SET
    r2_key = excluded.r2_key,
    size_bytes = excluded.size_bytes,
    max_level = excluded.max_level,
    created_at = excluded.created_at
"""


@dataclass(frozen=True)
class PublishResult:
    r2_key: str
    size_bytes: int


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def publish_cog(
    cog_path: str | Path,
    prod: RadialProduct,
    grid: AeqdGrid,
    r2: R2Client,
    d1: D1Client,
) -> PublishResult:
    cog_path = Path(cog_path)
    key = raster_key(prod.site_id, prod.spec.mnemonic, prod.vol_time)
    size = cog_path.stat().st_size
    now = utcnow_iso()
    vol_iso = prod.vol_time.isoformat(timespec="seconds")

    r2.upload_file(cog_path, key)

    d1.execute_many(
        [
            (
                UPSERT_RADAR,
                [prod.site_id, prod.lat, prod.lon, prod.height_m, grid.proj4, now, now],
            ),
            (
                UPSERT_PRODUCT,
                [prod.spec.code, prod.spec.mnemonic, prod.spec.unit, "raster"],
            ),
            (
                UPSERT_RASTER,
                [
                    prod.site_id,
                    prod.spec.code,
                    vol_iso,
                    key,
                    size,
                    prod.el_angle,
                    prod.vcp,
                    prod.scale,
                    prod.offset,
                    int(prod.levels.max()),
                    grid.size,
                    grid.size,
                    grid.cell_m,
                    now,
                ],
            ),
        ]
    )
    return PublishResult(r2_key=key, size_bytes=size)


INSERT_PHENOMENON = """
INSERT INTO phenomena (
    site_id, product_code, vol_time, kind, cell_id, lat, lon,
    azimuth_deg, range_km, attrs, created_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def publish_phenomena(php: PhenomenaProduct, d1: D1Client) -> int:
    """Publica los fenómenos de un producto. Idempotente por
    (sitio, producto, volumen): borra y reinserta — no hay clave natural
    por registro. Devuelve cuántos registros insertó."""
    now = utcnow_iso()
    vol_iso = php.vol_time.isoformat(timespec="seconds")

    statements = [
        (
            UPSERT_RADAR,
            [php.site_id, php.lat, php.lon, php.height_m, radar_proj4(php.lat, php.lon), now, now],
        ),
        (UPSERT_PRODUCT, [php.code, php.mnemonic, None, "phenomena"]),
        (
            "DELETE FROM phenomena WHERE site_id = ? AND product_code = ? AND vol_time = ?",
            [php.site_id, php.code, vol_iso],
        ),
    ]
    statements += [
        (
            INSERT_PHENOMENON,
            [
                php.site_id,
                php.code,
                vol_iso,
                r.kind,
                r.cell_id,
                r.lat,
                r.lon,
                r.azimuth_deg,
                r.range_km,
                json.dumps(r.attrs, separators=(",", ":")),
                now,
            ],
        )
        for r in php.records
    ]
    d1.execute_many(statements)
    return len(php.records)


INSERT_VWP = """
INSERT INTO vwp (site_id, vol_time, height_ft, wind_dir_deg, wind_speed_kt, rms_kt, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (site_id, vol_time, height_ft) DO UPDATE SET
    wind_dir_deg = excluded.wind_dir_deg,
    wind_speed_kt = excluded.wind_speed_kt,
    rms_kt = excluded.rms_kt
"""


def publish_vwp(vwp, d1: D1Client) -> int:
    """Publica un perfil VAD. Idempotente por volumen (delete + upsert)."""
    from ingest.phenomena.vwp import VWP_CODE, VWP_MNEMONIC

    now = utcnow_iso()
    vol_iso = vwp.vol_time.isoformat(timespec="seconds")
    statements = [
        (
            UPSERT_RADAR,
            [vwp.site_id, vwp.lat, vwp.lon, vwp.height_m, radar_proj4(vwp.lat, vwp.lon), now, now],
        ),
        (UPSERT_PRODUCT, [VWP_CODE, VWP_MNEMONIC, "kt", "vwp"]),
        ("DELETE FROM vwp WHERE site_id = ? AND vol_time = ?", [vwp.site_id, vol_iso]),
    ]
    statements += [
        (
            INSERT_VWP,
            [vwp.site_id, vol_iso, lv.height_ft, lv.wind_dir_deg, lv.wind_speed_kt, lv.rms_kt, now],
        )
        for lv in vwp.levels
    ]
    d1.execute_many(statements)
    return len(vwp.levels)
