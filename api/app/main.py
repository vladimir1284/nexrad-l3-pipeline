"""API HTTP mínima para workers/ops — ver api/README.md.

Cada endpoint espeja una query puntual de workers/ops/src/index.ts; no
hay passthrough SQL genérico (deliberado, ver plan de migración: acota
la superficie expuesta en internet a un puñado de rutas fijas).
"""

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI

from app.auth import require_ops_token
from app.db import close_pool, get_pool
from app.models import (
    DeleteDanglingRequest,
    DeleteDanglingResult,
    ExpiredR2Keys,
    LightningCheck,
    MonitorStateRow,
    MonitorStateUpsert,
    PurgeRequest,
    PurgeResult,
    RasterCheck,
    ReconcileKeys,
    WindCheck,
)

# tabla → columna temporal por la que se filtra el sweep de retención;
# fijo en el código (no viene del cliente) — mismo rol que KEYED_TABLES
# en workers/ops/src/index.ts.
_KEYED_TABLES: dict[str, str] = {"rasters": "vol_time", "wind_grids": "valid_time"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_pool()  # falla rápido en el arranque si PG_DSN es inválido
    yield
    close_pool()


app = FastAPI(title="nexrad-l3-api", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/checks/raster", dependencies=[Depends(require_ops_token)])
def check_raster(site: str) -> RasterCheck | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT vol_time, r2_key FROM rasters"
            " WHERE site_id = %s AND product_code = 153"
            " ORDER BY vol_time DESC LIMIT 1",
            [site],
        ).fetchone()
    return RasterCheck(**row) if row else None


@app.get("/v1/checks/wind", dependencies=[Depends(require_ops_token)])
def check_wind(site: str) -> WindCheck | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT valid_time, r2_key FROM wind_grids"
            " WHERE site_id = %s ORDER BY valid_time DESC LIMIT 1",
            [site],
        ).fetchone()
    return WindCheck(**row) if row else None


@app.get("/v1/checks/lightning", dependencies=[Depends(require_ops_token)])
def check_lightning(site: str) -> LightningCheck | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT bucket_start, r2_key FROM lightning_buckets"
            " WHERE site_id = %s ORDER BY bucket_start DESC LIMIT 1",
            [site],
        ).fetchone()
    return LightningCheck(**row) if row else None


@app.get("/v1/layers/{layer}/active", dependencies=[Depends(require_ops_token)])
def layer_active(layer: Literal["wind", "lightning"]) -> bool:
    table = "wind_grids" if layer == "wind" else "lightning_buckets"
    with get_pool().connection() as conn:
        return conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None


@app.get("/v1/monitor-state", dependencies=[Depends(require_ops_token)])
def get_monitor_state() -> list[MonitorStateRow]:
    with get_pool().connection() as conn:
        rows = conn.execute("SELECT site_id, fresh FROM ops_monitor_state").fetchall()
    return [MonitorStateRow(**r) for r in rows]


@app.post("/v1/monitor-state", dependencies=[Depends(require_ops_token)])
def upsert_monitor_state(rows: list[MonitorStateUpsert]) -> dict[str, int]:
    with get_pool().connection() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO ops_monitor_state (site_id, fresh, reason, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (site_id) DO UPDATE SET
                    fresh = excluded.fresh, reason = excluded.reason, updated_at = excluded.updated_at
                """,
                [r.site_id, r.fresh, r.reason, r.updated_at],
            )
    return {"upserted": len(rows)}


@app.get("/v1/sweep/expired-r2-keys", dependencies=[Depends(require_ops_token)])
def sweep_expired_r2_keys(cutoff: str) -> ExpiredR2Keys:
    with get_pool().connection() as conn:
        keyed = {
            table: [
                r["r2_key"]
                for r in conn.execute(
                    f"SELECT r2_key FROM {table} WHERE {time_col} < %s",
                    [cutoff],
                ).fetchall()
            ]
            for table, time_col in _KEYED_TABLES.items()
        }
        lightning = [
            r["r2_key"]
            for r in conn.execute(
                "SELECT r2_key FROM lightning_buckets"
                " WHERE bucket_start < %s AND r2_key IS NOT NULL",
                [cutoff],
            ).fetchall()
        ]
    return ExpiredR2Keys(
        rasters=keyed["rasters"], wind_grids=keyed["wind_grids"], lightning_buckets=lightning
    )


@app.post("/v1/sweep/purge", dependencies=[Depends(require_ops_token)])
def sweep_purge(body: PurgeRequest) -> PurgeResult:
    """Borra filas vencidas — llamar SOLO después de que el Worker ya
    borró los objetos R2 correspondientes (ver GET expired-r2-keys)."""
    cutoff = body.cutoff
    with get_pool().connection() as conn:
        counts = {}
        for table, time_col in (
            ("rasters", "vol_time"),
            ("wind_grids", "valid_time"),
            ("lightning_buckets", "bucket_start"),
            ("phenomena", "vol_time"),
            ("vwp", "vol_time"),
        ):
            cur = conn.execute(f"DELETE FROM {table} WHERE {time_col} < %s", [cutoff])
            counts[table] = cur.rowcount
    return PurgeResult(**counts)


@app.get("/v1/reconcile/keys", dependencies=[Depends(require_ops_token)])
def reconcile_keys() -> ReconcileKeys:
    with get_pool().connection() as conn:
        out = {
            table: [
                r["r2_key"]
                for r in conn.execute(
                    f"SELECT r2_key FROM {table} WHERE r2_key IS NOT NULL"
                ).fetchall()
            ]
            for table in ("rasters", "wind_grids", "lightning_buckets")
        }
    return ReconcileKeys(**out)


@app.post("/v1/reconcile/delete-dangling", dependencies=[Depends(require_ops_token)])
def reconcile_delete_dangling(body: DeleteDanglingRequest) -> DeleteDanglingResult:
    if not body.keys:
        return DeleteDanglingResult(deleted=0)
    with get_pool().connection() as conn:
        cur = conn.execute(
            f"DELETE FROM {body.table} WHERE r2_key = ANY(%s)",
            [body.keys],
        )
    return DeleteDanglingResult(deleted=cur.rowcount)
