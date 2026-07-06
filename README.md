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
│  (Unidata LDM)    │────▶│  decodifica Level III     │
│  ldmd + pqact     │pipe │  (MetPy), grilla → AEQD,  │
└───────────────────┘     │  extrae fenómenos         │
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

### Componentes

1. **Contenedor LDM (Unidata Local Data Manager).** Se conecta al IDD con un `request` del feedtype `NNEXRAD` filtrado por sitios y productos. `pqact` escribe cada producto como fichero en un directorio de entrada (`FILE`); el procesador es un servicio persistente que lo consume vía watcher (inotify/watchdog). Configuración en `ldm/` (`ldmd.conf`, `pqact.conf`, `registry.xml`).
2. **Procesador Python 3.12.** Decodifica el producto Level III con **MetPy** (`Level3File`: cabecera WMO + bloques NEXRAD, paquetes radiales y raster), grilla los datos a una malla regular en proyección **AEQD centrada en el radar** (resampleo *nearest neighbor* para preservar valores calibrados), escribe el **COG calibrado** con Rasterio (valores físicos escalados, CRS + geotransform embebidos, overviews internos) y lo sube a R2. Los fenómenos (Symbology/Tabular) y VWP requieren parsing propio sobre los bloques que MetPy expone crudos; esos productos no-raster y la metadata de cada raster van a D1.
3. **Cloudflare R2.** Almacén de COGs. Sirve al viewer con CORS + HTTP range requests (el cliente solo descarga los tiles/overviews que necesita). Convención de paths propuesta: `{site}/{product_code}/{YYYY}/{MM}/{DD}/{site}_{product_code}_{YYYYMMDD_HHMMSS}.tif`.
4. **Cloudflare D1.** Base SQLite serverless (tier gratuito, misma cuenta que R2). Tablas: catálogo de radares (poblado dinámicamente desde la metadata entrante, sin radares hardcodeados), descriptores de producto, metadata de rasters (clave R2, timestamps, VCP, elevación, min/max, proyección), fenómenos (granizo, mesociclones, TVS, tracking de celdas) y perfiles VWP. El viewer accede vía binding interno de su Worker — cómo lo haga es asunto del viewer, no de este proyecto; el contrato es solo el schema en `db/`.

## Decisiones de diseño

- **Artefacto raster único: COG calibrado en AEQD centrada en el radar.** No se generan PNG. La paleta se aplica en el cliente y OpenLayers reproyecta el raster al CRS de la vista en GPU. Un solo artefacto sirve como dato y como visual; cambiar paleta/umbrales no requiere regenerar nada. AEQD preserva distancia/azimut desde la torre (mínima distorsión al resamplear datos polares) y sus parámetros salen solos de la posición del radar — no hay paralelos estándar que definir. Sin código EPSG: el viewer registra la definición proj4 por radar (`proj4.defs(...)` + `register(proj4)`).
- **MetPy como decodificador base.** `Level3File` (Unidata, misma casa que LDM/IDD) decodifica cabeceras y paquetes radiales/raster; el grillado y la escritura COG usan pyproj + Rasterio. El parsing de fenómenos (granizo, meso, TVS, celdas) y VWP sobre Symbology/Tabular es propio, construido sobre los bloques que MetPy expone.
- **D1 en vez de PostgreSQL/ClickHouse.** Escala de demo (subset de radares → miles de filas, no millones). Tier gratuito, integrado con R2. Si el proyecto pasa a producción, el schema es migrable a PostgreSQL plano (contrato tipo LAMULA-Ingest).
- **LDM como transporte, no nbtcp.** La fuente es el IDD público de Unidata, no un ORPG propio. La capa de decodificación se mantiene independiente del transporte.
- **Paquete compartido con LAMULA-Ingest.** Los aspectos comunes (parsing de fenómenos/Tabular, tipos de dominio NEXRAD) van en un paquete Python compartido entre ambos proyectos, no como copia.
- **Entrega pqact → procesador: FILE + watcher, no PIPE.** pqact escribe el producto crudo a disco y un servicio Python persistente lo consume (inotify/watchdog). Motivos: el import de MetPy/Rasterio (~1–2 s) hace inviable un proceso por producto (PIPE `-close`); el PIPE persistente concatena binarios sin framing (frágil); el fichero en disco da tolerancia a fallos, reintento y replay de crudos durante desarrollo. Los ficheros procesados se borran tras subir a R2/D1; los fallidos quedan para reproceso.
- **Malla AEQD por producto: celda = gate nativo, extensión = rango nativo.** Nada de grilla común por radar — inflaría todos los COGs a la resolución del producto más fino sin ganar dato. Grillas resultantes: N0B 3680×3680 @ 0.25 km (peor caso, bajo el cap de textura WebGL de 4096), N0G 2400×2400 @ 0.25 km, DVL 920×920 @ 1 km, EET 692×692 @ 1 km, DAA/DTA/DU3 1840×1840 @ 0.25 km. uint8/uint16 comprimido → pocos MB por COG.
- **uv para gestión de paquetes y entornos Python.** No pip/venv/poetry directos: `uv venv`, `uv pip install`, lockfile con uv. Aplica al Dockerfile del procesador y al desarrollo local.
- **pqact: un solo patrón ancho, no una entrada por producto.** Una única regla captura todos los mnemónicos × sitios del alcance hacia un mismo directorio; el procesador discrimina por nombre de fichero (grupos capturados: sitio, mnemónico, timestamp). La selección fina de qué baja del IDD vive en el `request` de `ldmd.conf` (mismo dialecto de patrón), que es donde ahorra ancho de banda; duplicar la lista de productos en `pqact.conf` sería mantenimiento doble.
- **Rotación temporal: ventana de 3 días (72 h), configurable.** Sweep periódico que borra filas D1 y objetos R2 fuera de ventana, con pase de reconciliación (huérfanos en R2 sin fila / filas apuntando a objeto inexistente). R2 lifecycle rules como red de seguridad.

## Alcance del demo

- **Sitios:** 2–4 radares configurables, propuesta inicial Florida/Caribe: `KAMX` (Miami), `KBYX` (Key West), `TJUA` (Puerto Rico). Lista en config, sin hardcodear.
- **Productos core** (verificados contra el feed real el 2026-07-04 — los códigos legacy 19/20/27/41/57-raster/78/79/80/94/99-bajas ya **no fluyen**; estos son los reemplazos vivos):

| Categoría | Producto (código / mnemónico) | Geometría nativa |
|---|---|---|
| Base | Reflectividad super-res (153 / `N0B`…) | 0.25 km × 0.5°, 460 km |
| Base | Velocidad super-res (154 / `N0G`) | 0.25 km × 0.5°, 300 km |
| Derivados | Echo tops mejorado (135 / `EET`) | 1 km × 1°, 346 km |
| Derivados | VIL digital (134 / `DVL`) | 1 km × 1°, 460 km |
| Hidrometeorología | Precip 1h (170 / `DAA`), 3h (173 / `DU3`), storm-total (172 / `DTA`) | 0.25 km × 1°, 230 km |
| Cinemática | VAD/VWP (48 / `NVW`) | vectores, no-raster |
| Fenómenos | Mesociclones (141 / `NMD`), tracking (58 / `NST`), granizo (59 / `NHI`), TVS (61 / `NTV`) | no-raster; `NHI`/`NTV` episódicos (solo con celdas activas) |

MetPy 1.7.1 `Level3File` decodifica todos los anteriores sin error (verificado con muestras reales de `KAMX`/`TJUA`).

- **Elevación única: 0.5°** para los productos radiales por elevación (`N0B`, `N0G`). Los cortes superiores del volumen (N1B/N2B/N3B/NAB/NBB…) se ignoran; los derivados de volumen (EET, DVL, precipitación) no tienen elevación que elegir.

## Estructura de carpetas (propuesta)

```
nexrad-l3-pipeline/
├── README.md
├── docker-compose.yml        # LDM + procesador
├── ldm/                      # Dockerfile y config del LDM
│   ├── ldmd.conf             # request NNEXRAD por sitios/productos
│   └── pqact.conf            # entrega al procesador
├── ingest/                   # paquete Python 3.12
│   ├── decoder/              # MetPy Level3File + parsing propio (Symbology, Tabular)
│   ├── gridding/             # polar/raster → AEQD, escritura COG
│   ├── phenomena/            # granizo, meso, TVS, celdas
│   ├── storage/              # clientes R2 (S3 API) y D1
│   └── retention/            # sweep + reconciliación
├── db/                       # schema D1 + migraciones (contrato con el viewer)
└── docs/
```

## Referencias

- LAMULA-Ingest — proyecto hermano (ingesta nbtcp desde ORPG, GeoTIFF a FTP, PostgreSQL).
- [Vesta-PostGIS](https://github.com/vladimir1284/Vesta-PostGIS) — software legado de referencia.
- [NSF Unidata LDM](https://www.unidata.ucar.edu/software/ldm/) · [IDD feedtypes](https://www.unidata.ucar.edu/projects/idd/)
- [MetPy](https://unidata.github.io/MetPy/) — `metpy.io.Level3File` para decodificación NEXRAD Level III.
- Bucket S3 público `unidata-nexrad-level3` (claves `SITE_MNEMO_YYYY_MM_DD_HH_MM_SS`, sitio sin prefijo K/T) — muestras reales para desarrollo, tests y replay sin levantar LDM.
- [Cloud-Optimized GeoTIFF](https://cogeo.org/) · [OpenLayers GeoTIFF source](https://openlayers.org/en/latest/apidoc/module-ol_source_GeoTIFF-GeoTIFF.html)
