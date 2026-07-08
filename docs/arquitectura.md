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

Ambos servicios salen de la **misma imagen** (`ghcr.io/vladimir1284/nexrad-l3-pipeline`); el stack de Swarm elige el comando (`poll` / `watch`) y comparten un volumen local.

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

### Cloudflare R2

Almacén de COGs. Sirve al viewer con CORS + HTTP range requests (el cliente solo descarga los tiles/overviews que necesita). Convención de paths:

```
{site}/{mnemo}/{YYYY}/{MM}/{DD}/{site}_{mnemo}_{YYYYMMDD_HHMMSS}.tif
```

### Cloudflare D1

Base SQLite serverless (tier gratuito, misma cuenta que R2). Tablas: catálogo de radares (poblado dinámicamente desde la metadata entrante, sin radares hardcodeados), descriptores de producto, metadata de rasters (clave R2, timestamps, VCP, elevación, calibración, proyección), fenómenos (granizo, mesociclones, TVS, tracking de celdas) y perfiles VWP.

El pipeline escribe a D1 desde fuera de Cloudflare vía **HTTP API REST** (`/accounts/{id}/d1/database/{id}/query` con token) — sin transacciones entre requests, por eso los upserts son idempotentes y ordenados (dimensiones → hechos). El viewer accede vía binding interno de su Worker; cómo lo haga es asunto del viewer — el contrato es solo el schema en `db/`.

## Estructura de carpetas

```
nexrad-l3-pipeline/
├── README.md
├── mkdocs.yml                # esta documentación
├── Dockerfile                # imagen única: l3proc (poll | watch)
├── docker-compose.yml        # stack Swarm: poller + procesador (+ monitor en F5)
├── ingest/                   # paquete Python 3.12
│   ├── decoder/              # MetPy Level3File + parsing propio (Symbology, Tabular)
│   ├── gridding/             # polar → AEQD, escritura COG
│   ├── phenomena/            # granizo, meso, TVS, celdas
│   ├── storage/              # clientes R2 (S3 API) y D1 (HTTP API)
│   ├── retention/            # sweep + reconciliación
│   ├── poller.py             # transporte: polling del bucket público
│   ├── watcher.py            # servicio procesador (FILE + watcher)
│   └── replay.py             # injector puntual para dev/tests
├── db/                       # schema D1 + migraciones (contrato con el viewer)
├── scripts/                  # e2e local de las puertas
└── docs/                     # fuentes de esta documentación
```
