# Arquitectura

```
NSF Unidata IDD (feedtype NNEXRAD, Level III vía NOAAPort)
        │
        ▼
┌───────────────────┐     ┌──────────────────────────┐
│  Contenedor LDM   │     │   Procesador Python 3.12  │
│  (Unidata LDM)    │────▶│  decodifica Level III     │
│  ldmd + pqact     │FILE │  (MetPy), grilla → AEQD,  │
└───────────────────┘ dir │  extrae fenómenos         │
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

## Componentes

### Contenedor LDM (Unidata Local Data Manager)

Se conecta al IDD con un `request` del feedtype `NNEXRAD` filtrado por sitios y productos. `pqact` escribe cada producto como fichero en un directorio de entrada (acción `FILE`); el procesador lo consume vía watcher. Configuración en `ldm/` (`ldmd.conf`, `pqact.conf`, `registry.xml`).

!!! warning "Gotcha pqact"
    Las líneas de continuación de `pqact.conf` exigen TAB, no espacios — con espacios la entrada se ignora en silencio.

### Procesador Python 3.12

Servicio persistente con watcher (inotify/watchdog) sobre el directorio de entrada:

1. Decodifica el producto con **MetPy** (`Level3File`).
2. Grilla los datos radiales/raster a malla regular en proyección **AEQD centrada en el radar** (resampleo *nearest neighbor*).
3. Escribe el **COG calibrado** con Rasterio (valores físicos escalados, CRS + geotransform embebidos, overviews internos) y lo sube a R2.
4. Fenómenos (parsing propio sobre Symbology/Tabular), VWP y metadata de cada raster → D1.

### Cloudflare R2

Almacén de COGs. Sirve al viewer con CORS + HTTP range requests (el cliente solo descarga los tiles/overviews que necesita). Convención de paths:

```
{site}/{product_code}/{YYYY}/{MM}/{DD}/{site}_{product_code}_{YYYYMMDD_HHMMSS}.tif
```

### Cloudflare D1

Base SQLite serverless (tier gratuito, misma cuenta que R2). Tablas: catálogo de radares (poblado dinámicamente desde la metadata entrante, sin radares hardcodeados), descriptores de producto, metadata de rasters (clave R2, timestamps, VCP, elevación, min/max, proyección), fenómenos (granizo, mesociclones, TVS, tracking de celdas) y perfiles VWP.

El pipeline escribe a D1 desde fuera de Cloudflare vía **HTTP API REST** (`/accounts/{id}/d1/database/{id}/query` con token) — atención a rate limits y batching de inserts. El viewer accede vía binding interno de su Worker; cómo lo haga es asunto del viewer — el contrato es solo el schema en `db/`.

## Estructura de carpetas

```
nexrad-l3-pipeline/
├── README.md
├── mkdocs.yml                # esta documentación
├── docker-compose.yml        # stack Swarm: LDM + procesador + monitor
├── ldm/                      # Dockerfile y config del LDM
│   ├── ldmd.conf             # request NNEXRAD por sitios/productos
│   └── pqact.conf            # entrega FILE al directorio de entrada
├── ingest/                   # paquete Python 3.12
│   ├── decoder/              # MetPy Level3File + parsing propio (Symbology, Tabular)
│   ├── gridding/             # polar/raster → AEQD, escritura COG
│   ├── phenomena/            # granizo, meso, TVS, celdas
│   ├── storage/              # clientes R2 (S3 API) y D1 (HTTP API)
│   └── retention/            # sweep + reconciliación
├── db/                       # schema D1 + migraciones (contrato con el viewer)
└── docs/                     # fuentes de esta documentación
```
