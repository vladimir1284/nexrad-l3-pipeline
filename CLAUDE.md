# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Estado del repo

Implementación completa (F0–F6) siguiendo `docs/plan-implementacion.md`: núcleo decode→grid→COG para 7 productos raster, fenómenos NST/NMD y VWP a D1, storage R2/D1, watcher + replay, poller S3, stack Swarm en producción (VPS con Portainer), retención + reconciliación + monitor con Telegram. Además: capa de **viento GFS 0.25° 10 m + niveles de altura** (spec de superficie acordada jul-2026, migración `0003_wind_grids.sql`; fase 2 con terna "steering flow" 850/700/500 hPa, migración `0005_wind_levels.sql` del 2026-07-20 — PK con `level`, `l3proc wind` sigue por defecto en solo `10m` hasta confirmar con el viewer el naming de `level` y su query, opt-in vía `--levels`/`WIND_LEVELS`) y capa de **rayos GLM** (GOES-19 `GLM-L2-LCFA`, cubos de 300 s, contrato acordado 2026-07-19, migración `0004_lightning_buckets.sql`); sweep/reconciliación/monitor de ops cubren ambas tablas; contratos en `db/README.md`. **Ingesta de ambas movida a servicios Docker el 2026-07-20** (`wind`/`lightning` en `docker-compose.yml`, sobre `ingest/wind.py`/`ingest/lightning.py`, `l3proc wind`/`l3proc lightning`): vivían en los Workers `nexrad-l3-wind`/`nexrad-l3-lightning`, pero la cuenta está en **plan Free de Cloudflare Workers**, que no permite subir `limits.cpu_ms` — el parse HDF5 de GLM (~60 ms/frame) excedía el default en ~69% de las invocaciones (confirmado con GraphQL Analytics), y el fix de config vivía sin commitear en el repo. `ingest/lightning.py` es un puerto fiel de la lógica del Worker (`workers/lightning/src/`) usando `h5py` en vez de h5wasm vendorizado; ambos Workers quedan en el repo marcados deprecados (referencia/rollback), no desplegados. `ops` (`workers/ops/`) no cambió: monitorea/hace sweep por SQL D1 + API R2, sin importarle quién escribió las filas. El monitor de ops alerta por Telegram por tres capas por sitio: rasters, viento (`SITE:wind`, fresco = cobertura futura ≥ `WIND_MIN_LEAD_H`) y rayos (`SITE:ltg`, fresco = último cubo < `LTG_MAX_AGE_MIN`); wind/lightning se auto-activan cuando su tabla tiene filas. **Pendiente**: validar `l3proc lightning`/`l3proc wind` contra D1/R2 real de producción (requiere confirmación explícita — escriben en tablas que ya sirve el viewer) y luego deshabilitar los crons de los dos Workers. Pendiente además: puertas de operación (24 h de frescura F4, prueba Telegram F5) y validaciones QGIS del usuario para F6. La extensión de `phenomena.attrs` para el viewer está implementada (past/forecast de packets 23/24 + dBZ máx/altura del GAB); VIL/top/granizo por celda quedaron **fuera de alcance** — viven en SS (62)/HI (59), que el bucket no distribuye (verificado 2026-07-10; claves y recorte en `db/README.md`). Validaciones manuales por fase en `docs/validaciones.md`.

## Comandos

```bash
uv sync                                            # entorno (Python pinado a 3.12)
uv run ruff check . && uv run ruff format .        # lint + format (CI exige ambos limpios)
uv run pytest                                      # suite completa; integración se salta sin credenciales
uv run l3proc process <crudo> [-o dir] [--publish] # un producto → COG (→ R2/D1)
uv run l3proc watch <dir> [--once|--no-publish]    # servicio procesador
uv run l3proc poll <dir> --site AMX [--interval 60] # servicio poller del bucket público
uv run l3proc replay <dir> --site AMX -n 5         # inyectar productos reales puntuales
uv run l3proc wind --once                          # servicio viento GFS (autoritativo; una corrida y salir)
uv run l3proc lightning --once                     # servicio rayos GLM (autoritativo; una corrida y salir)
uv run python scripts/validate_wind_worker.py      # validación cruzada histórica Worker↔Python (obsoleto tras el corte)
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
3. **R2**: paths `{site}/{mnemo}/{YYYY}/{MM}/{DD}/{site}_{mnemo}_{YYYYMMDD_HHMMSS}.tif`; viento `{site}/WIND/{YYYY}/{MM}/{DD}/{site}_WIND_{ts}_c{ciclo}f{FFF}.json` (inmutable, el ciclo en el nombre).
4. **D1**: SQLite serverless vía HTTP API (sin transacciones entre requests). Tablas: `radars` (catálogo dinámico, **sin radares hardcodeados**, columna `proj4` que el viewer registra tal cual), `products`, `rasters` (calibración `value_scale`/`value_offset`), `phenomena`, `vwp`, `wind_grids`, `lightning_buckets`. Schema en `db/migrations/` = contrato con el viewer; wrangler config en `db/wrangler.jsonc` (aplicar desde `db/`).
5. **Wind** (servicio Docker `wind`, `ingest/wind.py`, `l3proc wind`, cron interno horario): GFS 0.25° 10 m vía filtro GRIB de NOMADS (OPeNDAP retirado — SCN 25-81) — un GRIB por (ciclo, fh 0–12) con bbox unión de los sitios de `radars` (± 6°, bordes a múltiplos de 0.25°), decode GRIB2 con eccodes (`grid_simple`; ojo: el filtro re-empaqueta subsets **sur→norte**, se normaliza a norte→sur), recorte local por sitio (subset puro), JSON u/v (m/s, 2 decimales, row-major desde NO) a R2 + upsert a `wind_grids` que solo gana con `cycle_time` mayor; borra el objeto reemplazado tras el upsert. Estado = D1; idempotente; sin cap de fetches (ventana completa cada corrida, barato tras el primer pase). Vivió en el Worker `nexrad-l3-wind` (`workers/wind/`, deprecado, conservado como referencia).
6. **Lightning** (servicio Docker `lightning`, `ingest/lightning.py`, `l3proc lightning`, cron interno cada 60 s): rayos GLM GOES-19 (`GLM-L2-LCFA`, bucket S3 público `noaa-goes19`) → cubos de 300 s por sitio, radio 460 km, `flash_quality_flag == 0`; fila SIEMPRE a `lightning_buckets` (incluso 0 rayos), objeto JSON a R2 solo si hay rayos. Un único barrido por corrida sobre la ventana de 72 h (sin split minutero/backfill: sin presupuesto de CPU/subrequests que lo justifique en Docker). Parse HDF5 con `h5py` — puerto fiel del Worker `nexrad-l3-lightning` (`workers/lightning/`, deprecado, conservado como referencia; sus gotchas de formato GLM siguen documentados ahí).

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

Docker Swarm de **nodo único**, imagen única (`Dockerfile`, entrypoint `l3proc`) desde ghcr. Stack en `docker-compose.yml`: servicios `poller` + `processor` + `wind` + `lightning`, los dos últimos sin volumen compartido (heartbeat efímero, estado real en D1), secrets de Swarm (R2 keys, token CF) vía convención `*_FILE` de `ingest/config.py`, healthchecks `l3proc health`. Deploy: `docker stack config -c docker-compose.yml | docker stack deploy -c - nexrad` (stack deploy no interpola `${VARS}`). Monitor de frescura (Telegram) y sweep de retención corren fuera del VPS, en el Worker de Cloudflare `nexrad-l3-ops` (`workers/ops/`, dos crons, estado en tabla D1 `ops_monitor_state`) — un monitor dentro del VPS no alerta cuando el VPS muere; `ops` no le importa que wind/lightning ahora escriban desde Docker en vez de un Worker. Los Workers `nexrad-l3-wind`/`nexrad-l3-lightning` quedan deprecados (ver sus README) — pendiente deshabilitar sus crons en Cloudflare una vez validados los servicios Docker contra producción.

## Alcance del demo

- Sitios: 2–4 radares **configurables** (`NEXRAD_SITES`, propuesta: AMX, BYX, JUA — ids de 3 chars del feed, sin prefijo K/T). El mapeo a ICAO completo (KAMX…) queda pendiente de config (los ficheros del bucket no traen header WMO).
- Productos (**códigos legacy 19/20/27/41/78/79/80/94 retirados del feed** — verificado 2026-07-04; **59/`NHI` y 61/`NTV` tampoco fluyen** — barrido jun-jul 2026 con tormentas activas = 0 claves; la señal TVS viaja en la columna TVS del NMD): raster 153/`N0B`, 154/`N0G`, 135/`EET`, 134/`DVL`, 170/`DAA`, 173/`DU3`, 172/`DTA`; fenómenos 141/`NMD`, 58/`NST`; perfil 48/`NVW`. Registro de specs y calibraciones en `ingest/products.py` (gate width por convención ICD; calibración por estrategia: linear10/eet/dvl/dpr — DVL usa float16 NEXRAD con bias 16, NO IEEE). El watcher enruta por contenido: raster → fenómenos → vwp.
- Muestras reales commiteadas en `tests/data/` (golden tests deterministas sin red). Goldens: sha256 de niveles/malla + argmax + counts.
