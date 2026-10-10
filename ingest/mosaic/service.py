"""Servicio del mosaico: slots cerrados → COG compuesto en R2 + fila en Postgres.

Mismo patrón que `ingest.lightning`/`ingest.wind`: clase con dependencias
inyectadas, `run_once()` idempotente, estado real en Postgres (no en disco),
heartbeat efímero para el healthcheck del stack.

Lee los COG de origen **de vuelta desde R2**: el procesador borra los locales
en cuanto publica, y los sitios de un slot llegan en momentos distintos, así
que no hay un punto del pipeline donde estén todos a la vez en disco.

La malla del dominio se recalcula de las filas vivas de `radars` en cada
corrida (nada hardcodeado), pero la geometría se guarda **por fila** en
`mosaic_rasters`: añadir un radar a un dominio cambia la malla y los COG
viejos tienen que seguir georreferenciándose con la suya.
"""

import json
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import numpy as np
from rasterio.io import MemoryFile

from ingest.config import MosaicConfig
from ingest.mosaic.calibration import calibration_for
from ingest.mosaic.cog import write_mosaic_cog
from ingest.mosaic.compose import SourceRaster, compose, project_source, quantize
from ingest.mosaic.geometry import DomainGrid, RadarSite, build_domain
from ingest.mosaic.slots import Contribution, closed_slots, pick_contributions, slot_center
from ingest.products import RASTER_PRODUCTS
from ingest.storage.keys import mosaic_key

log = logging.getLogger(__name__)

# Placeholders generados en vez de `= ANY(%s)`: el mismo SQL tiene que correr
# contra el SQLite de los tests rápidos, que no entiende ANY/ALL. Es la misma
# regla que el resto del repo — ejecutar el SQL real contra un motor real.
_SITES_SQL = """
SELECT site_id, lat, lon, height_m, proj4
FROM radars
WHERE site_id IN ({sites})
ORDER BY site_id
"""

# Radio nativo del producto = semi-extensión de su malla por radar. El dominio
# se dimensiona con el MAYOR de los productos configurados para que todos
# compartan una sola malla: así cambiar de producto en el viewer no cambia la
# extensión de la vista.
_RADIUS_SQL = """
SELECT max(width * cell_m / 2.0) AS radius_m
FROM rasters
WHERE product_code IN ({products}) AND site_id IN ({sites})
"""

_LAST_SLOT_SQL = """
SELECT max(slot_time) AS last_slot
FROM mosaic_rasters
WHERE domain_id = %s AND product_code = %s
"""

_CANDIDATES_SQL = """
SELECT site_id, vol_time, r2_key, value_scale, value_offset, el_angle, width, cell_m
FROM rasters
WHERE product_code = %s AND site_id IN ({sites})
  AND vol_time >= %s AND vol_time <= %s
ORDER BY site_id, vol_time
"""

_UPSERT_DOMAIN = """
INSERT INTO mosaic_domains (
    domain_id, name, proj4, width, height, cell_m, radius_m, created_at, updated_at
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (domain_id) DO UPDATE SET
    name = excluded.name,
    proj4 = excluded.proj4,
    width = excluded.width,
    height = excluded.height,
    cell_m = excluded.cell_m,
    radius_m = excluded.radius_m,
    updated_at = excluded.updated_at
"""

_UPSERT_DOMAIN_SITE = """
INSERT INTO mosaic_domain_sites (domain_id, site_id)
VALUES (%s, %s)
ON CONFLICT (domain_id, site_id) DO NOTHING
"""

_DELETE_STALE_SITES = """
DELETE FROM mosaic_domain_sites
WHERE domain_id = %s AND site_id NOT IN ({sites})
"""

_UPSERT_MOSAIC = """
INSERT INTO mosaic_rasters (
    domain_id, product_code, slot_time, slot_s, r2_key, size_bytes,
    value_scale, value_offset, max_level, proj4, width, height, cell_m,
    method, contributing, created_at
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (domain_id, product_code, slot_time) DO UPDATE SET
    r2_key = excluded.r2_key,
    size_bytes = excluded.size_bytes,
    max_level = excluded.max_level,
    proj4 = excluded.proj4,
    width = excluded.width,
    height = excluded.height,
    cell_m = excluded.cell_m,
    method = excluded.method,
    contributing = excluded.contributing,
    created_at = excluded.created_at
"""


def _marks(n: int) -> str:
    """`%s, %s, …` — lista de placeholders para un IN."""
    return ", ".join(["%s"] * n)


def _iso(moment: datetime) -> str:
    return moment.replace(tzinfo=None).isoformat(timespec="seconds")


def _parse(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


class MosaicComposer:
    def __init__(
        self,
        pg: Any,
        r2: Any,
        config: MosaicConfig,
        *,
        workdir: Path | None = None,
    ) -> None:
        self._pg = pg
        self._r2 = r2
        self._cfg = config
        self._workdir = workdir

    # -- ciclo principal ---------------------------------------------------

    def run_once(self, now: datetime | None = None) -> dict[str, int]:
        now = (now or datetime.now(UTC)).replace(tzinfo=None)
        stats = {"slots": 0, "composed": 0, "skipped": 0, "sources": 0, "failed": 0}
        for domain_id, site_ids in self._cfg.domains:
            grid = self._ensure_domain(domain_id, list(site_ids), now)
            if grid is None:
                continue
            sites = self._sites(list(site_ids))
            for product_code in self._cfg.products:
                self._run_product(grid, product_code, sites, now, stats)
        return stats

    def _run_product(
        self,
        grid: DomainGrid,
        product_code: int,
        sites: dict[str, dict],
        now: datetime,
        stats: dict[str, int],
    ) -> None:
        rows = self._pg.execute(_LAST_SLOT_SQL, [grid.domain_id, product_code])
        last = rows[0]["last_slot"] if rows and rows[0]["last_slot"] else None
        slots = closed_slots(
            now,
            self._cfg.slot_s,
            lookback_s=self._cfg.lookback_s,
            since=_parse(last) if last else None,
        )[: self._cfg.max_slots]
        for slot in slots:
            stats["slots"] += 1
            try:
                if self._compose_slot(grid, product_code, slot, sites, now, stats):
                    stats["composed"] += 1
                else:
                    stats["skipped"] += 1
            except Exception:  # noqa: BLE001 — un slot roto no tumba la corrida
                stats["failed"] += 1
                log.exception(
                    "mosaico %s/%s %s: fallo componiendo",
                    grid.domain_id,
                    product_code,
                    slot.isoformat(),
                )

    # -- dominio -----------------------------------------------------------

    def _sites(self, site_ids: list[str]) -> dict[str, dict]:
        if not site_ids:
            return {}
        sql = _SITES_SQL.format(sites=_marks(len(site_ids)))
        return {row["site_id"]: row for row in self._pg.execute(sql, list(site_ids))}

    def _ensure_domain(
        self,
        domain_id: str,
        site_ids: list[str],
        now: datetime,
    ) -> DomainGrid | None:
        sites = self._sites(site_ids)
        faltan = [s for s in site_ids if s not in sites]
        if faltan:
            log.warning("dominio %s: sitios sin fila en radars, se ignoran: %s", domain_id, faltan)
        if not sites:
            log.warning("dominio %s: ningún sitio conocido, se salta", domain_id)
            return None

        productos = list(self._cfg.products)
        sql = _RADIUS_SQL.format(products=_marks(len(productos)), sites=_marks(len(sites)))
        rows = self._pg.execute(sql, productos + sorted(sites))
        radius_m = rows[0]["radius_m"] if rows else None
        if not radius_m:
            log.warning("dominio %s: aún no hay rásters para dimensionar la malla", domain_id)
            return None

        grid = build_domain(
            domain_id,
            [
                RadarSite(r["site_id"], float(r["lat"]), float(r["lon"]), float(r["height_m"]))
                for r in sites.values()
            ],
            radius_m=float(radius_m),
            cell_m=self._cfg.cell_m,
        )
        stamp = _iso(now)
        statements = [
            (
                _UPSERT_DOMAIN,
                [
                    grid.domain_id,
                    grid.domain_id,
                    grid.proj4,
                    grid.width,
                    grid.height,
                    grid.cell_m,
                    float(radius_m),
                    stamp,
                    stamp,
                ],
            ),
            (
                _DELETE_STALE_SITES.format(sites=_marks(len(sites))),
                [grid.domain_id, *sorted(sites)],
            ),
        ]
        statements += [(_UPSERT_DOMAIN_SITE, [grid.domain_id, s]) for s in sorted(sites)]
        self._pg.execute_many(statements)
        return grid

    # -- un slot -----------------------------------------------------------

    def _compose_slot(
        self,
        grid: DomainGrid,
        product_code: int,
        slot: datetime,
        sites: dict[str, dict],
        now: datetime,
        stats: dict[str, int],
    ) -> bool:
        centro = slot_center(slot, self._cfg.slot_s)
        tol = self._cfg.slot_tolerance_s
        rows = self._pg.execute(
            _CANDIDATES_SQL.format(sites=_marks(len(sites))),
            [
                product_code,
                *sorted(sites),
                _iso(centro - timedelta(seconds=tol)),
                _iso(centro + timedelta(seconds=tol)),
            ],
        )
        por_sitio: dict[str, list[datetime]] = {s: [] for s in sites}
        indice: dict[tuple[str, str], dict] = {}
        for row in rows:
            vol = _parse(row["vol_time"])
            por_sitio.setdefault(row["site_id"], []).append(vol)
            indice[(row["site_id"], vol.isoformat())] = row

        contribuciones = pick_contributions(slot, self._cfg.slot_s, por_sitio, tolerance_s=tol)
        fuentes: list[SourceRaster] = []
        usadas: list[Contribution] = []
        for contrib in contribuciones:
            row = indice[(contrib.site_id, contrib.vol_time.isoformat())]
            try:
                fuentes.append(self._source(row, sites[contrib.site_id]))
            except Exception:  # noqa: BLE001 — un objeto ausente no invalida el slot
                stats["failed"] += 1
                log.exception("mosaico: no se pudo leer %s", row["r2_key"])
                continue
            usadas.append(contrib)

        if len(fuentes) < self._cfg.min_sites:
            log.info(
                "mosaico %s/%s %s: %d sitios (<%d), se salta",
                grid.domain_id,
                product_code,
                slot.isoformat(),
                len(fuentes),
                self._cfg.min_sites,
            )
            return False
        stats["sources"] += len(fuentes)

        cal = calibration_for(product_code)
        campos = [
            project_source(f, grid, h_ref_m=self._cfg.h_ref_m, r_ref_m=self._cfg.r_ref_m)
            for f in fuentes
        ]
        phys = compose(campos, method=self._cfg.method, space=cal.space)
        levels = quantize(phys, scale=cal.scale, offset=cal.offset)
        max_level = int(levels.max())

        mnemonic = RASTER_PRODUCTS[product_code].mnemonic
        key = mosaic_key(grid.domain_id, mnemonic, slot)
        with tempfile.TemporaryDirectory(dir=self._workdir) as tmp:
            path = Path(tmp) / Path(key).name
            write_mosaic_cog(
                levels,
                grid,
                path=path,
                product_code=product_code,
                mnemonic=mnemonic,
                slot_time=slot,
                slot_s=self._cfg.slot_s,
                calibration=cal,
                method=self._cfg.method,
                contributions=usadas,
            )
            size = path.stat().st_size
            self._r2.upload_file(path, key, content_type="image/tiff")

        self._pg.execute_many(
            [
                (
                    _UPSERT_MOSAIC,
                    [
                        grid.domain_id,
                        product_code,
                        _iso(slot),
                        self._cfg.slot_s,
                        key,
                        size,
                        cal.scale,
                        cal.offset,
                        max_level,
                        grid.proj4,
                        grid.width,
                        grid.height,
                        grid.cell_m,
                        self._cfg.method,
                        json.dumps([c.as_json() for c in usadas]),
                        _iso(now),
                    ],
                )
            ]
        )
        log.info(
            "mosaico %s/%s %s: %d sitios, %d bytes",
            grid.domain_id,
            mnemonic,
            slot.isoformat(),
            len(usadas),
            size,
        )
        return True

    def _source(self, row: dict, site: dict) -> SourceRaster:
        data = self._r2.download_bytes(row["r2_key"])
        with MemoryFile(data) as mem, mem.open() as ds:
            levels = ds.read(1)
        cell_m = float(row["cell_m"])
        return SourceRaster(
            site_id=row["site_id"],
            levels=np.ascontiguousarray(levels, dtype=np.uint8),
            cell_m=cell_m,
            half_extent_m=int(row["width"]) * cell_m / 2.0,
            proj4=site["proj4"],
            scale=float(row["value_scale"]),
            offset=float(row["value_offset"]),
            el_angle=None if row["el_angle"] is None else float(row["el_angle"]),
            antenna_h_m=float(site["height_m"]),
        )


def run_mosaic(
    composer: MosaicComposer,
    *,
    interval_s: float = 60.0,
    heartbeat: Path | None = None,
    stop: Event | None = None,
) -> None:
    """Servicio: compone slots cerrados cada `interval_s` hasta que paren."""
    stop = stop or Event()
    if heartbeat is not None:
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.touch()
    while not stop.is_set():
        try:
            stats = composer.run_once()
            log.info("mosaico: %s", stats)
        except Exception:  # noqa: BLE001 — el servicio no muere por una corrida
            log.exception("mosaico: corrida fallida")
        if heartbeat is not None:
            heartbeat.touch()
        stop.wait(interval_s)
