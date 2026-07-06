# nexrad-l3-pipeline

Pipeline de ingesta de productos NEXRAD Level III desde el feed IDD de NSF Unidata, con generación de artefactos geoespaciales (Cloud-Optimized GeoTIFF) en Cloudflare R2 y metadatos/fenómenos en Cloudflare D1.

**Propósito:** montar un demo de nuestro visualizador web de productos de radar. El visualizador (proyecto aparte, basado en OpenLayers) consume directamente los COG desde R2 y consulta D1; este proyecto no renderiza ni visualiza nada — es headless.

Es el hermano "cloud/demo" de **LAMULA-Ingest**: misma lógica de dominio NEXRAD (decodificación de bloques PDB / Symbology / Tabular, extracción de fenómenos), pero cambia la fuente (LDM/IDD en vez de nbtcp desde un ORPG) y el destino (R2 + D1 en vez de FTP + PostgreSQL).

## Arquitectura

```
NSF Unidata IDD (feedtype NNEXRAD, Level III vía NOAAPort)
        │
        ▼
┌───────────────────┐     ┌──────────────────────────┐
│  Contenedor LDM   │     │   Procesador Python 3.12  │
│  (Unidata LDM)    │────▶│  decodifica Level III,    │
│  ldmd + pqact     │pipe │  grilla polar → LCC,      │
└───────────────────┘     │  extrae fenómenos         │
                          └────────────┬─────────────┘
                                       │
                     ┌─────────────────┴─────────────────┐
                     ▼                                   ▼
            Cloudflare R2                        Cloudflare D1
            COG calibrados (LCC)                 catálogo de radares,
                                                 metadata de rasters,
                                                 fenómenos, VWP
                     │                                   │
                     └────────────┬──────────────────────┘
                                  ▼
                     LAMULA-WebViewer (demo, OpenLayers)
                     ol/source/GeoTIFF + WebGLTileLayer
                     reproyecta LCC → CRS del mapa en cliente
```

### Componentes

1. **Contenedor LDM (Unidata Local Data Manager).** Se conecta al IDD con un `request` del feedtype `NNEXRAD` filtrado por sitios y productos. `pqact` entrega cada producto al procesador Python (PIPE o FILE + watcher). Configuración en `ldm/` (`ldmd.conf`, `pqact.conf`, `registry.xml`).
2. **Procesador Python 3.12.** Decodifica el producto Level III (cabecera WMO + bloques NEXRAD: PDB, Symbology con paquetes binarios por código, Tabular Alphanumeric), grilla los datos polares a una malla regular en proyección **LCC**, escribe el **COG calibrado** (valores físicos escalados, CRS + geotransform embebidos, overviews internos) y lo sube a R2. Los productos no-raster (VWP, fenómenos) y la metadata de cada raster van a D1.
3. **Cloudflare R2.** Almacén de COGs. Sirve al viewer con CORS + HTTP range requests (el cliente solo descarga los tiles/overviews que necesita). Convención de paths propuesta: `{site}/{product_code}/{YYYY}/{MM}/{DD}/{site}_{product_code}_{YYYYMMDD_HHMMSS}.tif`.
4. **Cloudflare D1.** Base SQLite serverless (tier gratuito, misma cuenta que R2). Tablas: catálogo de radares (poblado dinámicamente desde la metadata entrante, sin radares hardcodeados), descriptores de producto, metadata de rasters (clave R2, timestamps, VCP, elevación, min/max, proyección), fenómenos (granizo, mesociclones, TVS, tracking de celdas) y perfiles VWP. El acceso del viewer es vía Worker/REST de Cloudflare.

## Decisiones de diseño

- **Artefacto raster único: COG calibrado en LCC nativa.** No se generan PNG. La paleta se aplica en el cliente y OpenLayers reproyecta el raster al CRS de la vista en GPU. Un solo artefacto sirve como dato y como visual; cambiar paleta/umbrales no requiere regenerar nada. Si la proyección LCC no tiene código EPSG, el viewer registra la definición proj4 (`proj4.defs(...)` + `register(proj4)`).
- **D1 en vez de PostgreSQL/ClickHouse.** Escala de demo (subset de radares → miles de filas, no millones). Tier gratuito, integrado con R2. Si el proyecto pasa a producción, el schema es migrable a PostgreSQL plano (contrato tipo LAMULA-Ingest).
- **LDM como transporte, no nbtcp.** La fuente es el IDD público de Unidata, no un ORPG propio. La capa de decodificación se mantiene independiente del transporte para poder reutilizar los decodificadores con LAMULA-Ingest.
- **Rotación temporal.** Ventana de retención configurable: sweep periódico que borra filas D1 y objetos R2 fuera de ventana, con pase de reconciliación (huérfanos en R2 sin fila / filas apuntando a objeto inexistente). R2 lifecycle rules como red de seguridad.

## Alcance del demo

- **Sitios:** 2–4 radares configurables, propuesta inicial Florida/Caribe: `KAMX` (Miami), `KBYX` (Key West), `TJUA` (Puerto Rico). Lista en config, sin hardcodear.
- **Productos core:**

| Categoría | Productos (códigos) |
|---|---|
| Base | Reflectividad (19, 20, 94), Velocidad (27, 99) |
| Derivados | Echo Tops (41), VIL (57) |
| Hidrometeorología | Precipitación 1h / 3h / storm-total (78, 79, 80) |
| Cinemática | VAD/VWP (48) |
| Fenómenos | Granizo, mesociclones, TVS, tracking de celdas (desde Symbology/Tabular) |

## Estructura de carpetas (propuesta)

```
nexrad-l3-pipeline/
├── README.md
├── docker-compose.yml        # LDM + procesador
├── ldm/                      # Dockerfile y config del LDM
│   ├── ldmd.conf             # request NNEXRAD por sitios/productos
│   └── pqact.conf            # entrega al procesador
├── ingest/                   # paquete Python 3.12
│   ├── decoder/              # bloques NEXRAD (PDB, Symbology, Tabular)
│   ├── gridding/             # polar → LCC, escritura COG
│   ├── phenomena/            # granizo, meso, TVS, celdas
│   ├── storage/              # clientes R2 (S3 API) y D1
│   └── retention/            # sweep + reconciliación
├── db/                       # schema D1 + migraciones (contrato con el viewer)
└── docs/
```

## Pendiente de definir

- Parámetros exactos de la proyección LCC (lat_1, lat_2, lon_0) y resolución de la malla.
- Patrones `pqact` definitivos por cabecera WMO (`SDUS[2357]x .... /pXXXyyy`).
- Ventana de retención del demo (propuesta: 24–72 h).
- Mecanismo de acceso del viewer a D1 (Worker REST vs binding directo).
- Reutilización de decodificadores con LAMULA-Ingest (paquete compartido vs copia).

## Referencias

- LAMULA-Ingest — proyecto hermano (ingesta nbtcp desde ORPG, GeoTIFF a FTP, PostgreSQL).
- [Vesta-PostGIS](https://github.com/vladimir1284/Vesta-PostGIS) — software legado de referencia.
- [NSF Unidata LDM](https://www.unidata.ucar.edu/software/ldm/) · [IDD feedtypes](https://www.unidata.ucar.edu/projects/idd/)
- [Cloud-Optimized GeoTIFF](https://cogeo.org/) · [OpenLayers GeoTIFF source](https://openlayers.org/en/latest/apidoc/module-ol_source_GeoTIFF-GeoTIFF.html)
