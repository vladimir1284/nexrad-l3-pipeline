"""Poller continuo del bucket público `unidata-nexrad-level3`.

Sustituye al LDM como transporte (sin registro con Unidata): cada ciclo
lista las claves nuevas por sitio×producto y las deposita en el
directorio de entrada con escritura atómica — el watcher las consume
por la misma ruta que cualquier otro transporte.

Estado: watermark (última clave bajada) por par, persistido en un JSON
oculto dentro del directorio de entrada — sobrevive reinicios sin
re-descargar el día entero. El catch-up tras una caída larga se capea a
las `catchup` claves más recientes (lo demás ya lo cubrirá la retención
de 72 h del bucket… y no tiene sentido inundar el demo con historia).
"""

import json
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event

from ingest.replay import BUCKET, _anonymous_s3, latest_keys

log = logging.getLogger("l3proc")

STATE_FILE = ".poll_state.json"


@dataclass(frozen=True)
class PollConfig:
    input_dir: Path
    sites: list[str]
    mnemonics: list[str]
    interval_s: float = 60.0
    catchup: int = 6  # máx. claves a bajar de golpe por par


def _load_state(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict[str, str]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=0, sort_keys=True))
    tmp.rename(path)


def _download_atomic(s3, key: str, input_dir: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=input_dir, prefix=f".{key}.", delete=False) as tmp:
        s3.download_fileobj(BUCKET, key, tmp)
    Path(tmp.name).rename(input_dir / key)


def poll_cycle(
    cfg: PollConfig, state: dict[str, str], *, s3=None, now: datetime | None = None
) -> int:
    """Un ciclo: baja lo nuevo de cada par sitio×producto. Devuelve
    cuántos productos depositó. Errores por par no tumban el ciclo."""
    s3 = s3 or _anonymous_s3()
    downloaded = 0
    for site in cfg.sites:
        for mnemo in cfg.mnemonics:
            pair = f"{site}_{mnemo}"
            try:
                recent = latest_keys(site, mnemo, cfg.catchup, s3=s3, now=now)
                fresh = [k for k in recent if k > state.get(pair, "")]
                for key in fresh:
                    _download_atomic(s3, key, cfg.input_dir)
                    state[pair] = key
                    downloaded += 1
                    log.info("poll: %s", key)
            except Exception:
                log.exception("poll: fallo en %s (se reintenta el próximo ciclo)", pair)
    return downloaded


def run_poller(
    cfg: PollConfig, *, heartbeat: Path | None = None, stop: Event | None = None
) -> None:
    stop = stop or Event()
    cfg.input_dir.mkdir(parents=True, exist_ok=True)
    state_path = cfg.input_dir / STATE_FILE
    state = _load_state(state_path)
    log.info(
        "poller: %s × %s cada %.0f s → %s",
        ",".join(cfg.sites),
        ",".join(cfg.mnemonics),
        cfg.interval_s,
        cfg.input_dir,
    )
    while not stop.is_set():
        n = poll_cycle(cfg, state)
        if n:
            _save_state(state_path, state)
        if heartbeat is not None:
            heartbeat.touch()
        stop.wait(cfg.interval_s)
