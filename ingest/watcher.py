"""Servicio persistente: watcher sobre el directorio de entrada del LDM.

Flujo por fichero: decode → grid AEQD → COG → publish (R2+D1) → borrar
el crudo. Fallos: el crudo se mueve a `failed/` (subdirectorio del de
entrada) para reproceso manual. Heartbeat por mtime de fichero para el
healthcheck del contenedor (F4).

Un solo hilo consumidor: inotify sobre un directorio local no se
paraleliza, y el orden de llegada ya es el orden del feed.
"""

import logging
import queue
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ingest.decoder.level3 import RadialProduct, decode_file
from ingest.gridding.aeqd import AeqdGrid, grid_radial
from ingest.gridding.cog import write_cog
from ingest.storage.publish import PublishResult

log = logging.getLogger("l3proc")

# callable(cog_path, prod, grid) -> PublishResult
Publisher = Callable[[Path, RadialProduct, AeqdGrid], PublishResult]


def build_publisher() -> Publisher:
    """Publisher real R2+D1 con configuración del entorno."""
    from ingest.config import StorageConfig
    from ingest.storage.d1 import D1Client
    from ingest.storage.publish import publish_cog
    from ingest.storage.r2 import R2Client

    cfg = StorageConfig.from_env()
    r2 = R2Client(cfg.r2_endpoint, cfg.r2_bucket, cfg.r2_access_key_id, cfg.r2_secret_access_key)
    d1 = D1Client(cfg.cf_account_id, cfg.d1_database_id, cfg.cf_api_token)

    def publish(cog_path: Path, prod: RadialProduct, grid: AeqdGrid) -> PublishResult:
        return publish_cog(cog_path, prod, grid, r2, d1)

    return publish


class ProductProcessor:
    """Procesa un producto crudo. Con publisher, el COG es efímero;
    sin publisher (dev), el COG queda en output_dir."""

    def __init__(self, publisher: Publisher | None = None, output_dir: Path | None = None):
        if publisher is None and output_dir is None:
            raise ValueError("sin publisher hace falta output_dir para conservar los COG")
        self._publisher = publisher
        self._output_dir = output_dir

    def process(self, raw: Path) -> None:
        t0 = time.monotonic()
        prod = decode_file(raw)
        grid = grid_radial(prod)
        stamp = prod.vol_time.strftime("%Y%m%d_%H%M%S")
        name = f"{prod.site_id}_{prod.spec.mnemonic}_{stamp}.tif"

        if self._publisher is None:
            out_dir = self._output_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            cog = write_cog(grid, prod, out_dir / name)
            dest = str(cog)
        else:
            with tempfile.TemporaryDirectory(prefix="l3proc-") as tmp:
                cog = write_cog(grid, prod, Path(tmp) / name)
                result = self._publisher(cog, prod, grid)
                dest = f"r2://{result.r2_key}"

        log.info("%s → %s (%.2f s)", raw.name, dest, time.monotonic() - t0)


@dataclass
class WatchStats:
    processed: int = 0
    failed: int = 0
    failed_files: list[str] = field(default_factory=list)


class _EnqueueHandler(FileSystemEventHandler):
    """Encola ficheros al cerrarse la escritura (pqact) o al renombrarse
    dentro del directorio (escritura atómica del injector)."""

    def __init__(self, q: queue.Queue, input_dir: Path):
        self._q = q
        self._input_dir = input_dir

    def _enqueue(self, path_str: str) -> None:
        path = Path(path_str)
        if path.parent != self._input_dir or path.name.startswith("."):
            return
        self._q.put(path)

    def on_closed(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(event.dest_path)


def _backlog(input_dir: Path) -> list[Path]:
    files = [p for p in input_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def run_watcher(
    input_dir: Path,
    processor: ProductProcessor,
    *,
    heartbeat: Path | None = None,
    once: bool = False,
    stop: Event | None = None,
) -> WatchStats:
    """Consume el backlog y (salvo `once`) queda vigilando el directorio.

    `stop` permite parada limpia desde un manejador de señal o un test.
    """
    input_dir = input_dir.resolve()
    failed_dir = input_dir / "failed"
    stop = stop or Event()
    stats = WatchStats()
    q: queue.Queue[Path] = queue.Queue()

    def touch_heartbeat() -> None:
        if heartbeat is not None:
            heartbeat.touch()

    def handle(path: Path) -> None:
        if not path.exists():  # evento duplicado de un fichero ya procesado
            return
        try:
            processor.process(path)
            path.unlink(missing_ok=True)
            stats.processed += 1
        except Exception:
            log.exception("fallo procesando %s — movido a %s", path.name, failed_dir)
            failed_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), failed_dir / path.name)
            stats.failed += 1
            stats.failed_files.append(path.name)
        touch_heartbeat()

    observer = None
    if not once:
        observer = Observer()
        observer.schedule(_EnqueueHandler(q, input_dir), str(input_dir), recursive=False)
        observer.start()

    # Heartbeat inmediato: vivo = heartbeat. Si el primer producto se
    # atascara (red), el healthcheck no debe matar el arranque en bucle.
    touch_heartbeat()

    # Backlog primero: lo pendiente de antes de arrancar no genera eventos.
    pending = _backlog(input_dir)
    log.info("watcher: %s (%d en backlog)", input_dir, len(pending))
    for path in pending:
        handle(path)
    touch_heartbeat()

    if once:
        return stats

    try:
        while not stop.is_set():
            try:
                handle(q.get(timeout=5.0))
            except queue.Empty:
                touch_heartbeat()
    finally:
        observer.stop()
        observer.join()
    return stats
