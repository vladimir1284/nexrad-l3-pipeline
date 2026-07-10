"""CLI del procesador Level III.

`l3proc process <fichero>` — decodifica un producto crudo, lo grilla a
AEQD y escribe el COG. F2 añade publicación a R2/D1.
"""

import argparse
import logging
import signal
import sys
from pathlib import Path
from threading import Event

from ingest import __version__


def _cmd_process(args: argparse.Namespace) -> int:
    # Import diferido: MetPy/Rasterio tardan ~1-2 s y no hacen falta para --help.
    from ingest.decoder.level3 import UnsupportedProductError, decode_file
    from ingest.gridding.aeqd import grid_radial
    from ingest.gridding.cog import write_cog

    try:
        prod = decode_file(args.file)
    except UnsupportedProductError as exc:
        print(f"l3proc: {args.file}: {exc}", file=sys.stderr)
        return 2

    grid = grid_radial(prod)
    out_dir = args.output_dir or args.file.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = prod.vol_time.strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"{prod.site_id}_{prod.spec.mnemonic}_{stamp}.tif"
    write_cog(grid, prod, out)
    print(out)

    if args.publish:
        from ingest.config import ConfigError, StorageConfig
        from ingest.storage.d1 import D1Client
        from ingest.storage.publish import publish_cog
        from ingest.storage.r2 import R2Client

        try:
            cfg = StorageConfig.from_env()
        except ConfigError as exc:
            print(f"l3proc: {exc}", file=sys.stderr)
            return 3
        r2 = R2Client(
            cfg.r2_endpoint, cfg.r2_bucket, cfg.r2_access_key_id, cfg.r2_secret_access_key
        )
        with D1Client(cfg.cf_account_id, cfg.d1_database_id, cfg.cf_api_token) as d1:
            result = publish_cog(out, prod, grid, r2, d1)
        print(f"r2://{cfg.r2_bucket}/{result.r2_key} ({result.size_bytes} bytes)")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    from ingest.config import ConfigError
    from ingest.watcher import ProductProcessor, build_publisher, run_watcher

    if args.no_publish:
        processor = ProductProcessor(output_dir=args.output_dir or args.dir / "cogs")
    else:
        try:
            processor = ProductProcessor(publisher=build_publisher())
        except ConfigError as exc:
            print(f"l3proc: {exc}", file=sys.stderr)
            return 3

    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    heartbeat = args.heartbeat if args.heartbeat else args.dir / ".heartbeat"
    stats = run_watcher(args.dir, processor, heartbeat=heartbeat, once=args.once, stop=stop)
    print(f"procesados={stats.processed} fallidos={stats.failed}")
    return 0 if stats.failed == 0 else 1


def _cmd_poll(args: argparse.Namespace) -> int:
    import os

    from ingest.poller import PollConfig, run_poller
    from ingest.products import all_mnemonics

    env_sites = [s.strip() for s in os.environ.get("NEXRAD_SITES", "").split(",") if s.strip()]
    sites = args.site or env_sites
    if not sites:
        print("l3proc: faltan sitios (--site o NEXRAD_SITES)", file=sys.stderr)
        return 3
    env_products = [
        p.strip() for p in os.environ.get("NEXRAD_PRODUCTS", "").split(",") if p.strip()
    ]
    mnemonics = args.product or env_products or all_mnemonics()
    cfg = PollConfig(
        input_dir=args.dir,
        sites=sites,
        mnemonics=mnemonics,
        interval_s=args.interval,
        catchup=args.catchup,
    )
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    heartbeat = args.heartbeat if args.heartbeat else args.dir / ".poll_heartbeat"
    run_poller(cfg, heartbeat=heartbeat, stop=stop)
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    import time

    try:
        age = time.time() - args.heartbeat.stat().st_mtime
    except OSError:
        print("unhealthy: sin heartbeat", file=sys.stderr)
        return 1
    if age > args.max_age:
        print(f"unhealthy: heartbeat de hace {age:.0f} s (máx {args.max_age})", file=sys.stderr)
        return 1
    if args.dir is not None:
        backlog = sum(1 for p in args.dir.iterdir() if p.is_file() and not p.name.startswith("."))
        if backlog > args.max_backlog:
            print(f"unhealthy: backlog {backlog} (máx {args.max_backlog})", file=sys.stderr)
            return 1
    print("healthy")
    return 0


def _storage_clients():
    from ingest.config import StorageConfig
    from ingest.storage.d1 import D1Client
    from ingest.storage.r2 import R2Client

    cfg = StorageConfig.from_env()
    r2 = R2Client(cfg.r2_endpoint, cfg.r2_bucket, cfg.r2_access_key_id, cfg.r2_secret_access_key)
    d1 = D1Client(cfg.cf_account_id, cfg.d1_database_id, cfg.cf_api_token)
    return r2, d1


def _cmd_sweep(args: argparse.Namespace) -> int:
    from ingest.config import ConfigError
    from ingest.retention.sweep import reconcile, sweep

    try:
        r2, d1 = _storage_clients()
    except ConfigError as exc:
        print(f"l3proc: {exc}", file=sys.stderr)
        return 3

    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    with d1:
        while True:
            sweep(d1, r2, window_hours=args.window_hours)
            report = reconcile(d1, r2, fix=args.fix)
            if args.once:
                ok = not report.r2_orphans and not report.dangling_rows
                return 0 if (ok or args.fix) else 1
            if args.heartbeat:
                args.heartbeat.touch()
            if stop.wait(args.interval):
                return 0


def _cmd_monitor(args: argparse.Namespace) -> int:
    import os

    from ingest.config import ConfigError, _env
    from ingest.monitor import TelegramNotifier, run_monitor

    sites = args.site or [
        s.strip() for s in os.environ.get("NEXRAD_SITES", "").split(",") if s.strip()
    ]
    if not sites:
        print("l3proc: faltan sitios (--site o NEXRAD_SITES)", file=sys.stderr)
        return 3
    try:
        r2, d1 = _storage_clients()
    except ConfigError as exc:
        print(f"l3proc: {exc}", file=sys.stderr)
        return 3

    bot_token = _env("TELEGRAM_BOT_TOKEN", default="")
    chat_id = _env("TELEGRAM_CHAT_ID", default="")
    notifier = TelegramNotifier(bot_token, chat_id) if bot_token and chat_id else None
    if notifier is None:
        print("l3proc: sin credenciales Telegram — transiciones solo al log", file=sys.stderr)

    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    with d1:
        run_monitor(
            d1,
            r2,
            sites,
            notifier=notifier,
            max_age_min=args.max_age,
            interval_s=args.interval,
            heartbeat=args.heartbeat,
            stop=stop,
        )
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from ingest.products import all_mnemonics
    from ingest.replay import inject

    mnemonics = args.product or all_mnemonics()
    injected = inject(args.dir, args.site, mnemonics, args.count)
    print(f"inyectados={len(injected)}")
    return 0 if injected else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(
        prog="l3proc",
        description="Procesador de productos NEXRAD Level III",
    )
    parser.add_argument("--version", action="version", version=f"l3proc {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_process = sub.add_parser("process", help="producto crudo → COG AEQD")
    p_process.add_argument("file", type=Path, help="fichero Level III crudo")
    p_process.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="directorio de salida (por defecto, el del fichero de entrada)",
    )
    p_process.add_argument(
        "--publish",
        action="store_true",
        help="subir el COG a R2 y registrar metadata en D1 (config por entorno)",
    )
    p_process.set_defaults(func=_cmd_process)

    p_watch = sub.add_parser("watch", help="servicio: vigila el directorio de entrada")
    p_watch.add_argument("dir", type=Path, help="directorio de entrada (pqact FILE)")
    p_watch.add_argument(
        "--once", action="store_true", help="procesar el backlog y salir (sin inotify)"
    )
    p_watch.add_argument(
        "--no-publish",
        action="store_true",
        help="no publicar: conservar los COG en --output-dir (dev)",
    )
    p_watch.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="directorio de COGs con --no-publish (por defecto <dir>/cogs)",
    )
    p_watch.add_argument(
        "--heartbeat",
        type=Path,
        default=None,
        help="fichero de heartbeat (por defecto <dir>/.heartbeat)",
    )
    p_watch.set_defaults(func=_cmd_watch)

    p_poll = sub.add_parser("poll", help="servicio: polling continuo del bucket público")
    p_poll.add_argument("dir", type=Path, help="directorio de entrada del watcher")
    p_poll.add_argument(
        "--site",
        action="append",
        default=None,
        help="sitio sin prefijo K/T; repetible (o env NEXRAD_SITES=AMX,BYX,JUA)",
    )
    p_poll.add_argument(
        "--product", action="append", default=None, help="mnemónico; por defecto los registrados"
    )
    p_poll.add_argument(
        "--interval", type=float, default=60.0, help="segundos entre ciclos (def. 60)"
    )
    p_poll.add_argument(
        "--catchup", type=int, default=6, help="máx. claves por par al ponerse al día (def. 6)"
    )
    p_poll.add_argument(
        "--heartbeat",
        type=Path,
        default=None,
        help="fichero heartbeat (def. <dir>/.poll_heartbeat)",
    )
    p_poll.set_defaults(func=_cmd_poll)

    p_health = sub.add_parser("health", help="healthcheck: edad de heartbeat y backlog")
    p_health.add_argument(
        "--heartbeat", type=Path, required=True, help="fichero de heartbeat a comprobar"
    )
    p_health.add_argument(
        "--max-age", type=float, default=300, help="edad máx. del heartbeat en s (def. 300)"
    )
    p_health.add_argument(
        "--dir", type=Path, default=None, help="si se da, comprobar backlog del directorio"
    )
    p_health.add_argument(
        "--max-backlog", type=int, default=200, help="ficheros máx. en backlog (def. 200)"
    )
    p_health.set_defaults(func=_cmd_health)

    p_sweep = sub.add_parser("sweep", help="servicio: retención 72 h + reconciliación R2↔D1")
    p_sweep.add_argument(
        "--window-hours", type=float, default=72.0, help="ventana de retención (def. 72)"
    )
    p_sweep.add_argument(
        "--interval", type=float, default=3600.0, help="segundos entre pasadas (def. 3600)"
    )
    p_sweep.add_argument(
        "--once", action="store_true", help="una pasada y salir (exit 1 si hay huérfanos sin --fix)"
    )
    p_sweep.add_argument(
        "--fix", action="store_true", help="borrar huérfanos R2 y filas colgantes al reconciliar"
    )
    p_sweep.add_argument("--heartbeat", type=Path, default=None, help="fichero de heartbeat")
    p_sweep.set_defaults(func=_cmd_sweep)

    p_monitor = sub.add_parser("monitor", help="servicio: frescura E2E + alertas Telegram")
    p_monitor.add_argument(
        "--site", action="append", default=None, help="repetible (o env NEXRAD_SITES)"
    )
    p_monitor.add_argument(
        "--max-age", type=float, default=30.0, help="frescura máx. en minutos (def. 30)"
    )
    p_monitor.add_argument(
        "--interval", type=float, default=300.0, help="segundos entre ciclos (def. 300)"
    )
    p_monitor.add_argument("--heartbeat", type=Path, default=None, help="fichero de heartbeat")
    p_monitor.set_defaults(func=_cmd_monitor)

    p_replay = sub.add_parser("replay", help="inyecta productos recientes del bucket público")
    p_replay.add_argument("dir", type=Path, help="directorio de entrada del watcher")
    p_replay.add_argument(
        "--site",
        action="append",
        required=True,
        help="sitio sin prefijo K/T (AMX, JUA…); repetible",
    )
    p_replay.add_argument(
        "--product",
        action="append",
        default=None,
        help="mnemónico (N0B…); repetible; por defecto todos los registrados",
    )
    p_replay.add_argument(
        "-n", "--count", type=int, default=5, help="productos por sitio×producto (def. 5)"
    )
    p_replay.set_defaults(func=_cmd_replay)

    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
