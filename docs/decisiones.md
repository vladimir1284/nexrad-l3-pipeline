# Decisiones de diseño

Decisiones cerradas. No re-litigar sin motivo nuevo.

## Raster

- **Artefacto raster único: COG calibrado en AEQD centrada en el radar.** No se generan PNG. La paleta se aplica en el cliente y OpenLayers reproyecta el raster al CRS de la vista en GPU. Un solo artefacto sirve como dato y como visual; cambiar paleta/umbrales no requiere regenerar nada. AEQD preserva distancia/azimut desde la torre (mínima distorsión al resamplear datos polares) y sus parámetros salen solos de la posición del radar — no hay paralelos estándar que definir. Sin código EPSG: el viewer registra la definición proj4 por radar.
- **Malla AEQD por producto: celda = gate nativo, extensión = rango nativo.** Nada de grilla común por radar — inflaría todos los COGs a la resolución del producto más fino sin ganar dato. Peor caso: N0B 3680×3680 @ 0.25 km, bajo el cap de textura WebGL de 4096. Ver [Productos](productos.md).
- **Resampleo nearest neighbor**, no bilinear — datos calibrados con umbrales; la interpolación suave inventa valores entre categorías.
- **Elevación única: 0.5°** para los productos radiales por elevación (`N0B`, `N0G`). Los cortes superiores del volumen se ignoran; los derivados de volumen (EET, DVL, precipitación) no tienen elevación que elegir.

## Decodificación

- **MetPy como decodificador base.** `Level3File` (Unidata, misma casa que el feed) decodifica cabeceras y paquetes radiales/raster; el grillado usa pyproj y la escritura Rasterio (driver COG). El parsing de fenómenos (granizo, meso, TVS, celdas) y VWP sobre Symbology/Tabular es propio — MetPy solo expone los bloques crudos.
- **Paquete compartido con LAMULA-Ingest.** Los aspectos comunes (parsing de fenómenos/Tabular, tipos de dominio NEXRAD) van en un paquete Python compartido entre ambos proyectos, no como copia. La capa de decodificación se mantiene independiente del transporte (polling S3 aquí, nbtcp allá).

## Transporte y entrega

- **Polling del bucket S3 público, no LDM/IDD** *(revisada 2026-07-06; sustituye a "LDM como transporte")*. El IDD exige registro/autorización de Unidata por host; el bucket `unidata-nexrad-level3` es el mismo feed, público y anónimo, con 1–5 min de latencia — irrelevante frente al umbral de frescura de 30 min. El poller (`l3proc poll`) lista claves nuevas por sitio×producto cada ~60 s con watermark persistido y catch-up capeado. Si algún día hay acceso IDD, un contenedor LDM (`pqact` con acción `FILE` al mismo directorio) sustituye al poller sin tocar el resto — la decodificación es independiente del transporte.
- **Entrega poller → procesador: FILE + watcher, no acoplamiento directo.** El fichero en disco da tolerancia a fallos, reintento, replay de crudos durante desarrollo, y mantiene el contrato que cualquier transporte alternativo (LDM, nbtcp) sabe cumplir. Escritura atómica (tmp + rename en el mismo filesystem) para que el watcher nunca vea productos a medias. Procesados se borran tras subir a R2/D1; fallidos quedan en `failed/` para reproceso.
- **Un solo directorio de entrada para todos los sitios×productos.** El procesador no discrimina por nombre: decodifica el contenido. La selección de qué baja vive en la config del poller (`NEXRAD_SITES`/`NEXRAD_PRODUCTS`).

## Almacenamiento

- **D1 en vez de PostgreSQL/ClickHouse.** Escala de demo (subset de radares → miles de filas, no millones). Tier gratuito, integrado con R2. Si el proyecto pasa a producción, el schema es migrable a PostgreSQL plano (contrato tipo LAMULA-Ingest).
- **Retención: 3 días (72 h), configurable.** Sweep periódico que borra filas D1 y objetos R2 fuera de ventana, con pase de reconciliación (huérfanos en R2 sin fila / filas apuntando a objeto inexistente). R2 lifecycle rules como red de seguridad.

## Tooling y despliegue

- **uv para gestión de paquetes y entornos Python.** No pip/venv/poetry directos: `uv venv`, `uv pip install`, lockfile con uv. Aplica al Dockerfile del procesador y al desarrollo local.
- **Despliegue en Docker Swarm** (nodo único). Stack file con `deploy:`, secrets de Swarm para credenciales R2/D1, healthchecks nativos. Ver [Plan de implementación](plan-implementacion.md).
- **CI en GitHub Actions**, imágenes en `ghcr.io`. Swarm hace pull del registry, nunca build local.
- **Alertas por Telegram** (bot existente) desde el monitor de frescura.
