# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Estado del repo

Proyecto greenfield: por ahora solo existe README.md (el documento de diseño). No hay código, ni docker-compose, ni comandos de build/test todavía. Al implementar, seguir la estructura de carpetas propuesta en el README (`ldm/`, `ingest/` con subpaquetes `decoder/`, `gridding/`, `phenomena/`, `storage/`, `retention/`, y `db/`).

## Qué es

Pipeline **headless** de ingesta de productos NEXRAD Level III desde el feed IDD de NSF Unidata (feedtype `NNEXRAD`). Genera COGs (Cloud-Optimized GeoTIFF) calibrados en proyección AEQD centrada en cada radar y los sube a Cloudflare R2; metadatos, fenómenos (granizo, mesociclones, TVS, tracking de celdas) y perfiles VWP van a Cloudflare D1. Este proyecto **no renderiza ni visualiza nada** — el consumidor es LAMULA-WebViewer (proyecto aparte, OpenLayers), que lee los COG por HTTP range requests y consulta D1 vía Worker.

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
- **Entrega pqact → procesador: FILE + watcher, no PIPE.** Import de MetPy/Rasterio (~1–2 s) hace inviable proceso-por-producto; PIPE persistente concatena binarios sin framing. Fichero en disco = tolerancia a fallos + replay de crudos en desarrollo. Procesados se borran tras subir; fallidos quedan para reproceso.
- **pqact: un solo patrón ancho.** Una regla captura todos los mnemónicos × sitios hacia un directorio; el procesador discrimina por nombre de fichero. El filtro fino (qué productos bajan) vive en el `request` de `ldmd.conf` — no duplicar la lista de productos en `pqact.conf`.
- **D1, no PostgreSQL/ClickHouse** — escala de demo (miles de filas). Schema diseñado para ser migrable a PostgreSQL si pasa a producción.
- **Decodificación independiente del transporte** — el parsing propio de fenómenos/Tabular es el candidato a compartir con LAMULA-Ingest (nbtcp).
- **Retención por ventana temporal** (propuesta 24–72 h): sweep que borra filas D1 + objetos R2 fuera de ventana, con reconciliación de huérfanos en ambas direcciones. R2 lifecycle rules como red de seguridad.

## Alcance del demo

- Sitios: 2–4 radares **configurables** (propuesta: `KAMX`, `KBYX`, `TJUA`). Lista en config, nunca hardcodeada.
- Productos: reflectividad (19, 20, 94), velocidad (27, 99), echo tops (41), VIL (57), precipitación (78, 79, 80), VAD/VWP (48), fenómenos desde Symbology/Tabular.

## Pendiente de definir (decisiones abiertas — preguntar antes de asumir)

- Resolución de malla AEQD y extensión (radio 230 vs 460 km según producto).
- Tabla código→mnemónico definitiva para el `request` de `ldmd.conf` (legacy vs digital para 94/99: N0Q/N0B, N0U/N0G según build del ORPG) — verificar contra tabla NWS vigente.
- Ventana de retención exacta.
- Acceso del viewer a D1: Worker REST vs binding directo.
- Parsing de fenómenos/Tabular compartido con LAMULA-Ingest: paquete compartido vs copia.
- Cobertura real de MetPy por código de producto (verificar 19/20/94/27/99/41/57/78/79/80/48 contra `Level3File`).
