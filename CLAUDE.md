# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Estado del repo

Proyecto greenfield: por ahora solo existe README.md (el documento de diseño). No hay código, ni docker-compose, ni comandos de build/test todavía. Al implementar, seguir la estructura de carpetas propuesta en el README (`ldm/`, `ingest/` con subpaquetes `decoder/`, `gridding/`, `phenomena/`, `storage/`, `retention/`, y `db/`).

## Qué es

Pipeline **headless** de ingesta de productos NEXRAD Level III desde el feed IDD de NSF Unidata (feedtype `NNEXRAD`). Genera COGs (Cloud-Optimized GeoTIFF) calibrados en proyección AEQD centrada en cada radar y los sube a Cloudflare R2; metadatos, fenómenos (granizo, mesociclones, TVS, tracking de celdas) y perfiles VWP van a Cloudflare D1. Este proyecto **no renderiza ni visualiza nada** — el consumidor es LAMULA-WebViewer (proyecto aparte, OpenLayers), que lee los COG por HTTP range requests y consulta D1 vía binding interno de su Worker (asunto del viewer; el contrato con él es solo el schema en `db/`).

Es el hermano "cloud/demo" de **LAMULA-Ingest**: misma lógica de decodificación NEXRAD (bloques PDB / Symbology / Tabular), distinta fuente (LDM/IDD en vez de nbtcp desde ORPG) y distinto destino (R2 + D1 en vez de FTP + PostgreSQL).

## Arquitectura (flujo de datos)

1. **Contenedor LDM** (Unidata Local Data Manager): `ldmd` se suscribe al IDD con `request NNEXRAD` filtrado por sitios/productos; `pqact` escribe cada producto como fichero en directorio de entrada (acción `FILE`). Config en `ldm/` (`ldmd.conf`, `pqact.conf`, `registry.xml`). Gotcha: las líneas de continuación de `pqact.conf` exigen TAB, no espacios — con espacios la entrada se ignora en silencio.
2. **Procesador Python 3.12**: servicio persistente con watcher (inotify/watchdog) sobre el directorio de entrada; decodifica con MetPy (`Level3File`), grilla datos polares/raster → malla regular AEQD centrada en el radar (nearest neighbor, preserva valores calibrados), escribe COG con Rasterio (valores físicos escalados, CRS + geotransform embebidos, overviews internos), sube a R2. Productos no-raster y metadata → D1.
3. **R2**: paths `{site}/{product_code}/{YYYY}/{MM}/{DD}/{site}_{product_code}_{YYYYMMDD_HHMMSS}.tif`.
4. **D1**: SQLite serverless. Tablas: catálogo de radares (poblado dinámicamente, **sin radares hardcodeados**), descriptores de producto, metadata de rasters, fenómenos, VWP. El schema en `db/` es el contrato con el viewer.

## Decisiones de diseño (no re-litigar sin motivo)

- **Un solo artefacto raster: COG calibrado en AEQD centrada en el radar.** Nada de PNGs. Paleta y reproyección se aplican en el cliente (GPU). Cambiar paleta/umbrales no regenera nada. AEQD: parámetros derivan de la posición del radar (sin paralelos que definir), mínima distorsión al resamplear polar. Sin EPSG — el viewer registra la definición proj4 por radar.
- **MetPy como decodificador base** (radial/raster). Grillado con pyproj, escritura con Rasterio (driver COG). Parsing de fenómenos y VWP sobre Symbology/Tabular es propio — MetPy solo expone los bloques crudos.
- **Resampleo nearest neighbor**, no bilinear — datos calibrados con umbrales, interpolación suave inventa valores.
- **Malla AEQD por producto: celda = gate nativo, extensión = rango nativo.** Sin grilla común por radar. Peor caso N0B: 3680×3680 @ 0.25 km (bajo cap textura WebGL 4096). Ver tabla de geometrías en README.
- **uv para paquetes y entornos Python** — no pip/venv/poetry directos. Aplica a Dockerfile y desarrollo local.
- **Entrega pqact → procesador: FILE + watcher, no PIPE.** Import de MetPy/Rasterio (~1–2 s) hace inviable proceso-por-producto; PIPE persistente concatena binarios sin framing. Fichero en disco = tolerancia a fallos + replay de crudos en desarrollo. Procesados se borran tras subir; fallidos quedan para reproceso.
- **pqact: un solo patrón ancho.** Una regla captura todos los mnemónicos × sitios hacia un directorio; el procesador discrimina por nombre de fichero. El filtro fino (qué productos bajan) vive en el `request` de `ldmd.conf` — no duplicar la lista de productos en `pqact.conf`.
- **D1, no PostgreSQL/ClickHouse** — escala de demo (miles de filas). Schema diseñado para ser migrable a PostgreSQL si pasa a producción.
- **Decodificación independiente del transporte.** Los aspectos comunes con LAMULA-Ingest (parsing de fenómenos/Tabular, tipos de dominio NEXRAD) van en **paquete Python compartido**, no copia.
- **Elevación única: 0.5°** (`N0B`, `N0G`). Cortes superiores del volumen se ignoran; los derivados de volumen (EET, DVL, precip) no tienen elevación que elegir.
- **Retención: 3 días (72 h), configurable.** Sweep que borra filas D1 + objetos R2 fuera de ventana, con reconciliación de huérfanos en ambas direcciones. R2 lifecycle rules como red de seguridad.

## Alcance del demo

- Sitios: 2–4 radares **configurables** (propuesta: `KAMX`, `KBYX`, `TJUA`). Lista en config, nunca hardcodeada.
- Productos (**códigos legacy 19/20/27/41/78/79/80/94 retirados del feed** — verificado 2026-07-04; el 99 solo fluye en cortes altos, fuera de alcance por la decisión de elevación única): reflectividad super-res (153/`N0B`), velocidad super-res (154/`N0G`), echo tops (135/`EET`), VIL digital (134/`DVL`), precipitación 1h/3h/storm-total (170/`DAA`, 173/`DU3`, 172/`DTA`), VAD/VWP (48/`NVW`), fenómenos: meso (141/`NMD`), tracking (58/`NST`), granizo (59/`NHI`), TVS (61/`NTV`) — `NHI`/`NTV` episódicos. Geometrías nativas en tabla del README. MetPy 1.7.1 decodifica todos (verificado con muestras reales).
- Muestras para dev/tests/replay sin LDM: bucket S3 público `unidata-nexrad-level3`, claves `SITE_MNEMO_YYYY_MM_DD_HH_MM_SS` (sitio sin prefijo K/T), acceso anónimo (botocore `UNSIGNED`).
