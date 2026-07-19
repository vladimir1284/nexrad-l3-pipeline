# Arquitectura

```
Radares NEXRAD → NOAA/Unidata → bucket S3 público
                 unidata-nexrad-level3 (us-east-1, anónimo)
        │
        ▼  polling (~60 s; latencia del bucket 1–5 min)
┌───────────────────┐      ┌──────────────────────────┐
│  Servicio poller  │      │   Procesador Python 3.12  │
│  (l3proc poll)    │─────▶│  (l3proc watch)           │
│  watermark por    │ FILE │  decodifica Level III     │
│  sitio×producto   │ dir  │  (MetPy), grilla → AEQD,  │
└───────────────────┘      │  extrae fenómenos         │
                           └────────────┬─────────────┘
                                        │
                      ┌─────────────────┴─────────────────┐
                      ▼                                   ▼
             Cloudflare R2                        Cloudflare D1
             COG calibrados (AEQD)                catálogo de radares,
                                                  metadata de rasters,
                                                  fenómenos, VWP
                      │                                   │
                      └────────────┬──────────────────────┘
                                   ▼
                      LAMULA-WebViewer (demo, OpenLayers)
                      ol/source/GeoTIFF + WebGLTileLayer
                      reproyecta AEQD → CRS del mapa en cliente
```

Ambos servicios salen de la **misma imagen** (`ghcr.io/vladimir1284/nexrad-l3-pipeline`); el stack de Swarm elige el comando (`poll` / `watch`) y comparten un volumen local. La ingesta de viento GFS no toca el VPS: es un Worker de Cloudflare aparte (`nexrad-l3-wind`).

## Componentes

### Servicio poller (`l3proc poll`)

Cada ciclo (60 s por defecto) lista en el bucket público las claves nuevas por sitio×producto (claves `SITE_MNEMO_YYYY_MM_DD_HH_MM_SS`, orden lexicográfico = cronológico) y las deposita en el directorio de entrada con escritura atómica (tmp + rename). Mantiene un **watermark** por par persistido en `.poll_state.json` — los reinicios no re-descargan historia — y el catch-up tras una caída se capea (`--catchup`, def. 6). Sitios y productos por flags o env (`NEXRAD_SITES`, `NEXRAD_PRODUCTS`).

!!! note "¿Por qué no LDM?"
    El diseño original usaba un contenedor LDM suscrito al IDD (`request NNEXRAD`), pero el IDD exige registro/autorización de Unidata por host. El bucket S3 es el mismo feed publicado por Unidata con 1–5 min de latencia, sin registro. La capa de decodificación es independiente del transporte: si algún día hay acceso IDD, un contenedor LDM con `pqact` escribiendo `FILE` al mismo directorio sustituye al poller sin tocar nada más. Detalle en [Decisiones](decisiones.md).

### Procesador (`l3proc watch`)

Servicio persistente con watcher (inotify/watchdog) sobre el directorio de entrada. Al arrancar consume el backlog pendiente; después reacciona a eventos:

1. Decodifica el producto con **MetPy** (`Level3File`).
2. Grilla los datos radiales/raster a malla regular en proyección **AEQD centrada en el radar** (resampleo *nearest neighbor* sobre niveles crudos).
3. Escribe el **COG calibrado** con Rasterio (niveles uint8 + scale/offset embebidos, CRS + geotransform, overviews internos) y lo sube a R2.
4. Fenómenos (parsing propio sobre Symbology/Tabular), VWP y metadata de cada raster → D1.

Procesados se borran; fallidos van a `failed/` para reproceso. Heartbeat por mtime para el healthcheck de Swarm.

### Worker de viento (`nexrad-l3-wind`)

Cron horario en Cloudflare, independiente del flujo NEXRAD y **fuera del VPS**: viento **GFS 0.25° 10 m** (u/v) para la capa de partículas animadas del viewer. Descarga subsets por el filtro GRIB de NOMADS (decenas de KB; un fichero por ciclo×forecast-hour con el bbox unión de todos los sitios, recorte local por sitio — la grilla es regular y los bordes van alineados a múltiplos de 0.25°, subset puro sin resampleo), los decodifica con un decoder GRIB2 propio en TypeScript (`workers/wind/src/grib.ts`, solo `grid_simple` — lo único que el filtro emite; OPeNDAP fue retirado por NOAA, SCN 25-81), convierte a JSON (`header` + `u`/`v` planos en m/s) y publica con bindings nativos: objeto a R2, fila a `wind_grids` en D1.

Sitios: los de la tabla `radars` (radar-agnóstico, nada hardcodeado; dominio `lat/lon ± 6°` por sitio). Ventana: `[now − 72 h, now + 2 h]` con `forecast_hour` 0–12 — ciclos cada 6 h dan valid_times horarios continuos y ~2 h de colchón si un ciclo se retrasa. El estado es D1: el upsert solo gana con `cycle_time` más nuevo, re-ejecutar sin datos nuevos no reescribe nada, y un valid_time fallido no aborta el resto (reintento natural en la corrida siguiente). Cortesía con NOMADS: requests secuenciales con pausa y presupuesto de descargas por corrida (`MAX_FETCHES`; los valid_times se recorren del más nuevo al más viejo, así lo fresco sale primero y el backfill converge en pocas corridas).

La referencia Python (`ingest/wind.py`, `l3proc wind`) implementa la misma lógica con eccodes y sirve para validación cruzada del Worker (`scripts/validate_wind_worker.py`) — no se despliega.

### Worker de rayos (`nexrad-l3-lightning`)

También fuera del VPS: descargas eléctricas del **GOES-19 GLM** (producto `GLM-L2-LCFA`, bucket público AWS, un netCDF-4 cada 20 s, nivel flash) para la capa de rayos animados del viewer. Cron minutero (+ backfill horario de 72 h): por cada **cubo de 300 s alineado a UTC** cerrado hace ≥ 90 s lista los frames del intervalo (más un frame extra — un flash que cruza frames aparece en el fichero posterior con el primer evento hacia atrás), los parsea con **h5wasm** (HDF5 en WASM, vendorizado con un parche de instanciación porque workerd prohíbe compilar wasm en runtime — detalle en `workers/lightning/README.md`), filtra calidad, recorta por sitio (gran círculo ≤ 460 km, sitios de `radars`) y publica: JSON `[lon, lat, offset_s]` a R2 cuando hay descargas + fila **siempre** a `lightning_buckets` — fila con 0 rayos = cubo cubierto sin descargas; sin fila = hueco de ingesta. Cubos fijos y no vol_times a propósito: los rayos llegan en continuo, el viewer junta los cubos que solapan cada observación. GLM es rayo total (IC+CG sin distinguir), detección ~70–90 %, posición ~8–14 km — sobra para evolución de tormenta, no para localización exacta de impactos.

### Cloudflare R2

Almacén de COGs (+ JSON de viento y rayos). Sirve al viewer con CORS + HTTP range requests (el cliente solo descarga los tiles/overviews que necesita). Convención de paths:

```
{site}/{mnemo}/{YYYY}/{MM}/{DD}/{site}_{mnemo}_{YYYYMMDD_HHMMSS}.tif
{site}/WIND/{YYYY}/{MM}/{DD}/{site}_WIND_{YYYYMMDD}_{HHMMSS}_c{ciclo}f{FFF}.json
{site}/LIGHTNING/{YYYY}/{MM}/{DD}/{site}_LTG_{YYYYMMDD}_{HHMMSS}.json
```

Las tres claves son **inmutables** (`Cache-Control: immutable`): en viento el ciclo va en el nombre — un ciclo más nuevo sube objeto nuevo y borra el anterior tras el upsert en D1; en rayos el cubo se procesa una única vez ya cerrado.

### Cloudflare D1

Base SQLite serverless (tier gratuito, misma cuenta que R2). Tablas: catálogo de radares (poblado dinámicamente desde la metadata entrante, sin radares hardcodeados), descriptores de producto, metadata de rasters (clave R2, timestamps, VCP, elevación, calibración, proyección), fenómenos (granizo, mesociclones, TVS, tracking de celdas), perfiles VWP, grillas de viento GFS (`wind_grids`) y cubos de rayos GLM (`lightning_buckets`).

El pipeline escribe a D1 desde fuera de Cloudflare vía **HTTP API REST** (`/accounts/{id}/d1/database/{id}/query` con token) — sin transacciones entre requests, por eso los upserts son idempotentes y ordenados (dimensiones → hechos). El viewer accede vía binding interno de su Worker; cómo lo haga es asunto del viewer — el contrato es solo el schema en `db/`.

## Estructura de carpetas

```
nexrad-l3-pipeline/
├── README.md
├── mkdocs.yml                # esta documentación
├── Dockerfile                # imagen única: l3proc (poll | watch)
├── docker-compose.yml        # stack Swarm: poller + procesador
├── ingest/                   # paquete Python 3.12
│   ├── decoder/              # MetPy Level3File + parsing propio (Symbology, Tabular)
│   ├── gridding/             # polar → AEQD, escritura COG
│   ├── phenomena/            # granizo, meso, TVS, celdas
│   ├── storage/              # clientes R2 (S3 API) y D1 (HTTP API)
│   ├── poller.py             # transporte: polling del bucket público
│   ├── watcher.py            # servicio procesador (FILE + watcher)
│   ├── wind.py               # referencia viento GFS (valida al Worker)
│   └── replay.py             # injector puntual para dev/tests
├── db/                       # schema D1 + migraciones (contrato con el viewer)
├── workers/                  # Workers de Cloudflare: ops (monitor+sweep), wind (GFS), lightning (GLM)
├── scripts/                  # e2e local de las puertas + validación del Worker de viento
└── docs/                     # fuentes de esta documentación
```
