# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Estado del repo

Implementación completa (F0–F6) siguiendo `docs/plan-implementacion.md`: núcleo decode→grid→COG para 7 productos raster, fenómenos NST/NMD y VWP a D1, storage R2/D1, watcher + replay, poller S3, stack Swarm en producción (VPS con Portainer), retención + reconciliación + monitor con Telegram. Pendiente: puertas de operación (24 h de frescura F4, prueba Telegram F5), validaciones QGIS del usuario para F6, y **extensión de `phenomena.attrs` acordada con el viewer** (packets 23/24 SCIT + tabular STORM CELL ATTRIBUTES de NST → past/forecast, VIL, dBZ máx, top, POH/POSH/granizo; spec y claves en `db/README.md`; prerequisito de la fase F4 de lamula-webviewer). Validaciones manuales por fase en `docs/validaciones.md`.

## Comandos

```bash
uv sync                                            # entorno (Python pinado a 3.12)
uv run ruff check . && uv run ruff format .        # lint + format (CI exige ambos limpios)
uv run pytest                                      # suite completa; integración se salta sin credenciales
uv run l3proc process <crudo> [-o dir] [--publish] # un producto → COG (→ R2/D1)
uv run l3proc watch <dir> [--once|--no-publish]    # servicio procesador
uv run l3proc poll <dir> --site AMX [--interval 60] # servicio poller del bucket público
uv run l3proc replay <dir> --site AMX -n 5         # inyectar productos reales puntuales
uv run python scripts/e2e_local.py                 # puerta F3 (necesita credenciales en env)
uvx --with mkdocs-material mkdocs serve            # preview docs en :8000
uvx --with mkdocs-material mkdocs build --strict   # build docs (lo que corre CI)
```

Tests de integración: S3 corre contra MinIO en CI (docker run en el job); D1 real gated por secrets (`D1_TEST_DATABASE_ID`, `CLOUDFLARE_D1_API_TOKEN`) — sin ellos se saltan. Test de red opcional del bucket: `REPLAY_NETWORK_TEST=1`.

Docs se despliegan solos a Cloudflare Pages (proyecto `nexrad-l3-docs`) al tocar `docs/**` o `mkdocs.yml` en `main`. CI también construye y empuja la imagen única a `ghcr.io/vladimir1284/nexrad-l3-pipeline` en cada push a main.

## Qué es

Pipeline **headless** de ingesta de productos NEXRAD Level III desde el espejo S3 público del feed de Unidata (`unidata-nexrad-level3`, anónimo, latencia 1–5 min). Genera COGs (Cloud-Optimized GeoTIFF) calibrados en proyección AEQD centrada en cada radar y los sube a Cloudflare R2; metadatos, fenómenos (granizo, mesociclones, TVS, tracking de celdas) y perfiles VWP van a Cloudflare D1. Este proyecto **no renderiza ni visualiza nada** — el consumidor es LAMULA-WebViewer (proyecto aparte, OpenLayers), que lee los COG por HTTP range requests y consulta D1 vía binding interno de su Worker (asunto del viewer; el contrato con él es solo el schema en `db/`).

Es el hermano "cloud/demo" de **LAMULA-Ingest**: misma lógica de decodificación NEXRAD (bloques PDB / Symbology / Tabular), distinta fuente (S3 público en vez de nbtcp desde ORPG) y distinto destino (R2 + D1 en vez de FTP + PostgreSQL).

## Arquitectura (flujo de datos)

1. **Poller** (`ingest/poller.py`, `l3proc poll`): cada ~60 s lista claves nuevas por sitio×producto en el bucket (claves `SITE_MNEMO_YYYY_MM_DD_HH_MM_SS`, orden lexicográfico = cronológico) y las deposita en el directorio de entrada con escritura atómica (tmp+rename). Watermark por par persistido en `.poll_state.json`; catch-up capeado (`--catchup`, def. 6). Sitios/productos por flags o env `NEXRAD_SITES`/`NEXRAD_PRODUCTS`.
2. **Procesador** (`ingest/watcher.py`, `l3proc watch`): watcher inotify/watchdog; al arrancar consume backlog, luego eventos `on_closed`/`on_moved`. Decodifica con MetPy (`Level3File`), grilla polar → malla AEQD centrada en el radar (nearest neighbor sobre niveles crudos uint8), escribe COG con Rasterio (scale/offset embebidos, CRS proj4 AEQD, overviews) y publica: objeto a R2, metadata a D1 (upserts idempotentes, dimensiones→hechos). Procesados se borran; fallidos a `failed/`. Heartbeat por mtime → `l3proc health` (HEALTHCHECK del stack).
3. **R2**: paths `{site}/{mnemo}/{YYYY}/{MM}/{DD}/{site}_{mnemo}_{YYYYMMDD_HHMMSS}.tif`.
4. **D1**: SQLite serverless vía HTTP API (sin transacciones entre requests). Tablas: `radars` (catálogo dinámico, **sin radares hardcodeados**, columna `proj4` que el viewer registra tal cual), `products`, `rasters` (calibración `value_scale`/`value_offset`), `phenomena`, `vwp`. Schema en `db/migrations/` = contrato con el viewer; wrangler config en `db/wrangler.jsonc` (aplicar desde `db/`).

## Decisiones de diseño (no re-litigar sin motivo)

- **Un solo artefacto raster: COG calibrado en AEQD centrada en el radar.** Nada de PNGs. Paleta y reproyección en el cliente (GPU). Sin EPSG — el viewer registra proj4 por radar. Niveles crudos uint8 + scale/offset embebidos (`físico = nivel·scale + offset`, niveles ≥ 2; 0 = below threshold/nodata, 1 = range folded).
- **Polling S3 público, no LDM/IDD** (revisada 2026-07-06: el IDD exige registro con Unidata; el bucket es el mismo feed sin registro). Decodificación independiente del transporte: un LDM con `pqact FILE` al mismo directorio encajaría sin tocar el resto.
- **Entrega poller → procesador: FILE + watcher.** Fichero en disco = tolerancia a fallos + replay; escritura atómica tmp+rename. Un solo directorio de entrada; el procesador decodifica contenido, no discrimina por nombre.
- **MetPy como decodificador base** (radial/raster). Parsing de fenómenos y VWP sobre Symbology/Tabular es propio — MetPy solo expone los bloques crudos.
- **Resampleo nearest neighbor**, no bilinear — datos calibrados con umbrales, interpolación suave inventa valores.
- **Malla AEQD por producto: celda = gate nativo, extensión = rango nativo.** N0B 3680×3680 @ 0.25 km (peor caso, bajo cap textura WebGL 4096).
- **uv para paquetes y entornos Python** — no pip/venv/poetry directos. Python pinado a 3.12 (`.python-version`).
- **D1, no PostgreSQL/ClickHouse** — escala de demo. Schema migrable a PostgreSQL.
- **Elevación única: 0.5°** (`N0B`, `N0G`). Derivados de volumen (EET, DVL, precip) no tienen elevación que elegir.
- **Retención: 3 días (72 h), configurable.** Sweep D1+R2 con reconciliación de huérfanos (F5).
- **Aspectos comunes con LAMULA-Ingest** (fenómenos/Tabular, tipos de dominio) → paquete Python compartido, no copia.

## Despliegue

Docker Swarm de **nodo único**, imagen única (`Dockerfile`, entrypoint `l3proc`) desde ghcr. Stack en `docker-compose.yml`: servicios `poller` + `processor` con volumen compartido, secrets de Swarm (R2 keys, token CF) vía convención `*_FILE` de `ingest/config.py`, healthchecks `l3proc health`. Deploy: `docker stack config -c docker-compose.yml | docker stack deploy -c - nexrad` (stack deploy no interpola `${VARS}`). Monitor de frescura (Telegram) y sweep de retención corren fuera del VPS, en el Worker de Cloudflare `nexrad-l3-ops` (`workers/ops/`, dos crons, estado en tabla D1 `ops_monitor_state`) — un monitor dentro del VPS no alerta cuando el VPS muere.

## Alcance del demo

- Sitios: 2–4 radares **configurables** (`NEXRAD_SITES`, propuesta: AMX, BYX, JUA — ids de 3 chars del feed, sin prefijo K/T). El mapeo a ICAO completo (KAMX…) queda pendiente de config (los ficheros del bucket no traen header WMO).
- Productos (**códigos legacy 19/20/27/41/78/79/80/94 retirados del feed** — verificado 2026-07-04; **59/`NHI` y 61/`NTV` tampoco fluyen** — barrido jun-jul 2026 con tormentas activas = 0 claves; la señal TVS viaja en la columna TVS del NMD): raster 153/`N0B`, 154/`N0G`, 135/`EET`, 134/`DVL`, 170/`DAA`, 173/`DU3`, 172/`DTA`; fenómenos 141/`NMD`, 58/`NST`; perfil 48/`NVW`. Registro de specs y calibraciones en `ingest/products.py` (gate width por convención ICD; calibración por estrategia: linear10/eet/dvl/dpr — DVL usa float16 NEXRAD con bias 16, NO IEEE). El watcher enruta por contenido: raster → fenómenos → vwp.
- Muestras reales commiteadas en `tests/data/` (golden tests deterministas sin red). Goldens: sha256 de niveles/malla + argmax + counts.
