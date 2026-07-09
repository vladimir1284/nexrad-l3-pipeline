"""Retención: ventana temporal (72 h por defecto) + reconciliación R2↔D1.

Orden del sweep pensado para cortes a mitad: primero se borran los
objetos R2, después las filas D1. Si el borrado R2 falla, la fila
sobrevive y el siguiente sweep reintenta; si el proceso muere entre
ambos pasos, la fila colgante la detecta (y limpia) la reconciliación.

La reconciliación es métrica además de limpieza: huérfanos R2 (objeto
sin fila) y filas colgantes (fila sin objeto) indican cortes o bugs en
la cadena de publicación — se reportan siempre, se borran solo con
`fix=True`.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ingest.storage.d1 import D1Client
from ingest.storage.r2 import R2Client

log = logging.getLogger("l3proc")

_CHUNK = 50  # filas por DELETE ... IN (...) — cómodo bajo los límites de D1


@dataclass
class SweepReport:
    rasters_deleted: int = 0
    phenomena_deleted: int = 0
    vwp_deleted: int = 0


@dataclass
class ReconcileReport:
    r2_orphans: list[str] = field(default_factory=list)  # objeto sin fila D1
    dangling_rows: list[str] = field(default_factory=list)  # fila D1 sin objeto
    fixed: bool = False


def _cutoff_iso(window_hours: float, now: datetime | None) -> str:
    now = now or datetime.now(UTC)
    cutoff = now.replace(tzinfo=None) - timedelta(hours=window_hours)
    return cutoff.isoformat(timespec="seconds")


def sweep(
    d1: D1Client,
    r2: R2Client,
    *,
    window_hours: float = 72.0,
    now: datetime | None = None,
) -> SweepReport:
    """Borra todo lo anterior a la ventana: objetos R2 + filas D1."""
    cutoff = _cutoff_iso(window_hours, now)
    report = SweepReport()

    rows = d1.execute("SELECT r2_key FROM rasters WHERE vol_time < ?", [cutoff])
    keys = [r["r2_key"] for r in rows]
    if keys:
        r2.delete_keys(keys)
        for i in range(0, len(keys), _CHUNK):
            chunk = keys[i : i + _CHUNK]
            marks = ",".join("?" * len(chunk))
            d1.execute(f"DELETE FROM rasters WHERE r2_key IN ({marks})", chunk)
        report.rasters_deleted = len(keys)

    for table, attr in (("phenomena", "phenomena_deleted"), ("vwp", "vwp_deleted")):
        n = d1.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE vol_time < ?", [cutoff])[0]["n"]
        if n:
            d1.execute(f"DELETE FROM {table} WHERE vol_time < ?", [cutoff])
            setattr(report, attr, n)

    log.info(
        "sweep: cutoff=%s rasters=%d phenomena=%d vwp=%d",
        cutoff,
        report.rasters_deleted,
        report.phenomena_deleted,
        report.vwp_deleted,
    )
    return report


def reconcile(d1: D1Client, r2: R2Client, *, fix: bool = False) -> ReconcileReport:
    """Compara el bucket con la tabla rasters y reporta discrepancias.

    Con `fix=True` además borra: huérfanos R2 y filas colgantes.
    """
    report = ReconcileReport(fixed=fix)
    in_r2 = set(r2.list_keys())
    in_d1 = {r["r2_key"] for r in d1.execute("SELECT r2_key FROM rasters")}

    report.r2_orphans = sorted(in_r2 - in_d1)
    report.dangling_rows = sorted(in_d1 - in_r2)

    if report.r2_orphans or report.dangling_rows:
        log.warning(
            "reconcile: %d huérfanos R2, %d filas colgantes%s",
            len(report.r2_orphans),
            len(report.dangling_rows),
            " (corrigiendo)" if fix else "",
        )
    else:
        log.info("reconcile: consistente (%d objetos)", len(in_r2))

    if fix:
        if report.r2_orphans:
            r2.delete_keys(report.r2_orphans)
        for i in range(0, len(report.dangling_rows), _CHUNK):
            chunk = report.dangling_rows[i : i + _CHUNK]
            marks = ",".join("?" * len(chunk))
            d1.execute(f"DELETE FROM rasters WHERE r2_key IN ({marks})", chunk)

    return report
