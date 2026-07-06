#!/usr/bin/env python3
"""Puerta F3 — e2e local contra R2 y D1 reales.

Arranca el watcher como subproceso, inyecta N productos reales del bucket
público, espera a que el backlog se vacíe y verifica: objetos en R2,
filas en D1, backlog vacío, fallidos preservados (deben ser 0).

Requiere las variables de entorno de ingest.config (véase .env) y las
migraciones aplicadas en la base D1. Uso:

    set -a; source .env; set +a
    uv run python scripts/e2e_local.py [--count 10] [--site AMX --site JUA]
"""

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest.config import StorageConfig  # noqa: E402
from ingest.replay import inject  # noqa: E402
from ingest.storage.d1 import D1Client  # noqa: E402
from ingest.storage.keys import raster_key  # noqa: E402
from ingest.storage.r2 import R2Client  # noqa: E402


def key_to_r2_key(bucket_key: str) -> str:
    """AMX_N0B_2026_07_06_15_45_17 → clave R2 esperada (vol_time = ts de la clave)."""
    from datetime import datetime

    site, mnemo, *ts = bucket_key.split("_")
    vol = datetime(*map(int, ts))
    return raster_key(site, mnemo, vol)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", action="append", default=None, help="repetible (def. AMX, JUA)")
    parser.add_argument("--count", type=int, default=10, help="productos por sitio (def. 10)")
    parser.add_argument("--timeout", type=int, default=300, help="espera máx. en s (def. 300)")
    args = parser.parse_args()
    sites = args.site or ["AMX", "JUA"]

    cfg = StorageConfig.from_env()  # falla rápido si falta config
    input_dir = Path(tempfile.mkdtemp(prefix="l3proc-e2e-"))
    heartbeat = input_dir / ".heartbeat"
    print(f"directorio de entrada: {input_dir}")

    watcher = subprocess.Popen(
        [sys.executable, "-m", "ingest.cli", "watch", str(input_dir)],
        env=os.environ,
    )
    try:
        deadline = time.monotonic() + 30
        while not heartbeat.exists():
            if watcher.poll() is not None:
                print("FALLO: el watcher murió al arrancar", file=sys.stderr)
                return 1
            if time.monotonic() > deadline:
                print("FALLO: watcher sin heartbeat en 30 s", file=sys.stderr)
                return 1
            time.sleep(0.5)

        injected = inject(input_dir, sites, ["N0B"], args.count)
        expected = len(injected)
        print(f"inyectados {expected} productos ({', '.join(sites)})")

        def backlog() -> list[str]:
            return [
                p.name for p in input_dir.iterdir() if p.is_file() and not p.name.startswith(".")
            ]

        deadline = time.monotonic() + args.timeout
        while backlog() and time.monotonic() < deadline:
            if watcher.poll() is not None:
                print("FALLO: el watcher murió procesando", file=sys.stderr)
                return 1
            time.sleep(2)
    finally:
        watcher.send_signal(signal.SIGTERM)
        watcher.wait(timeout=30)

    failures = []
    rest = backlog()
    if rest:
        failures.append(f"backlog no vacío: {rest}")

    failed_dir = input_dir / "failed"
    failed = [p.name for p in failed_dir.iterdir()] if failed_dir.exists() else []
    if failed:
        failures.append(f"{len(failed)} fallidos: {failed}")

    r2 = R2Client(cfg.r2_endpoint, cfg.r2_bucket, cfg.r2_access_key_id, cfg.r2_secret_access_key)
    with D1Client(cfg.cf_account_id, cfg.d1_database_id, cfg.cf_api_token) as d1:
        for bucket_key in injected:
            r2_key = key_to_r2_key(bucket_key)
            meta = r2.head(r2_key)
            if meta is None:
                failures.append(f"falta en R2: {r2_key}")
                continue
            rows = d1.execute("SELECT size_bytes FROM rasters WHERE r2_key = ?", [r2_key])
            if not rows:
                failures.append(f"falta fila D1: {r2_key}")
            elif rows[0]["size_bytes"] != meta["ContentLength"]:
                failures.append(
                    f"tamaño no coincide {r2_key}: D1={rows[0]['size_bytes']} "
                    f"R2={meta['ContentLength']}"
                )

    if failures:
        print("\nPUERTA F3: ROJA", file=sys.stderr)
        for f in failures:
            print(f"  ✗ {f}", file=sys.stderr)
        return 1
    print(f"\nPUERTA F3: VERDE — {expected} COGs en R2, {expected} filas D1, backlog vacío")
    return 0


if __name__ == "__main__":
    sys.exit(main())
