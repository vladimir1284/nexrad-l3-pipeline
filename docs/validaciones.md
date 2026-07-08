# Validaciones manuales

Cada fase tiene una puerta automática (tests/CI) y, en varias, una parte **manual** que no se puede automatizar. Esta página es el checklist de esa parte manual: qué hacer, con qué comandos, y qué tiene que verse para dar la fase por buena.

Convención: ✅ = criterio de aceptación. Si algo no se cumple, la fase no se cierra.

## F1 — COG en QGIS

Los tests ya garantizan estructura (CRS, geotransform, overviews, calibración). QGIS valida lo que los tests no ven: que el raster **cae donde debe en el mundo real** y que un GIS de referencia interpreta el fichero sin ayuda.

Generar COGs de prueba desde las muestras del repo:

```bash
uv run l3proc process tests/data/AMX_N0B_2026_07_06_15_45_17 -o /tmp/cogs
uv run l3proc process tests/data/JUA_N0B_2026_07_06_15_43_47 -o /tmp/cogs
```

En QGIS:

1. Cargar un basemap de referencia: menú *XYZ Tiles → OpenStreetMap* (arrastrar al lienzo).
2. Arrastrar `AMX_N0B_20260706_154517.tif` al lienzo. QGIS debe **aceptar el CRS sin preguntar** (lo lee del fichero; aparece como proyección AEQD custom).
3. ✅ El disco del radar queda centrado sobre Miami (KAMX está al suroeste del área urbana) y el diámetro abarca ~920 km (South Florida + Bahamas + Cuba occidental). Para TJUA: centrado en Puerto Rico.
4. ✅ Los ecos coinciden con costa/geografía de forma plausible (celdas convectivas sobre tierra/mar, no desplazadas cientos de km ni rotadas).
5. Herramienta *Identify* (Ctrl+Shift+I) sobre un eco: el valor de banda 1 es el **nivel crudo** (0–255). ✅ QGIS muestra el valor físico si se activa *"aplicar scale/offset"*, o manualmente: `dBZ = nivel × 0.5 − 33` (para N0B). Un eco fuerte debe dar 45–60 dBZ, no valores absurdos.
6. ✅ Zoom out fluido: los overviews internos responden (no re-lee el raster completo a cada zoom).
7. Propiedades de capa → *Information*: ✅ CRS `+proj=aeqd +lat_0=<lat radar> +lon_0=<lon radar>`, tamaño 3680×3680, resolución 250 m, NoData 0, metadatos `SITE/PRODUCT/VOL_TIME/VCP` presentes.

## F2 — Cloudflare: setup y verificación R2↔D1

### Setup una sola vez

1. Crear el bucket R2 (dashboard o `wrangler r2 bucket create nexrad-l3`) y un **API token R2** (Object Read & Write, scoped al bucket) → `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` + endpoint `https://<account_id>.r2.cloudflarestorage.com`.
2. Crear la base D1: `wrangler d1 create nexrad-l3`. Guardar el `database_id`.
3. Copiar los `database_id` en `db/wrangler.jsonc` y aplicar migraciones **desde `db/`**: `cd db && npx wrangler d1 migrations apply nexrad-l3 --remote` (detalle en `db/README.md`).
4. Crear un **API token de cuenta** con permiso *D1 — Edit* → `CLOUDFLARE_API_TOKEN`.
5. Para el test de integración D1 en CI: **crear una segunda base `nexrad-l3-test`** (para que CI no ensucie la real), aplicarle las mismas migraciones, y añadir en GitHub los secrets `D1_TEST_DATABASE_ID` y `CLOUDFLARE_D1_API_TOKEN` (token con *D1 — Edit*; nombre distinto del `CLOUDFLARE_API_TOKEN` de Pages, que solo tiene permiso de deploy). `CLOUDFLARE_ACCOUNT_ID` ya existe del setup de docs. Sin estos secrets, los tests de integración D1 se saltan solos (CI sigue verde).

### Verificación de la puerta

Publicar una muestra real end-to-end:

```bash
export R2_ENDPOINT=... R2_BUCKET=nexrad-l3 R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=...
export CLOUDFLARE_ACCOUNT_ID=... D1_DATABASE_ID=... CLOUDFLARE_API_TOKEN=...
uv run l3proc process tests/data/AMX_N0B_2026_07_06_15_45_17 -o /tmp/cogs --publish
```

1. ✅ En el dashboard R2 (o `wrangler r2 object get`): existe `AMX/N0B/2026/07/06/AMX_N0B_20260706_154517.tif` y su tamaño coincide con el fichero local.
2. ✅ En D1 (`wrangler d1 execute nexrad-l3 --remote --command "SELECT * FROM rasters ORDER BY id DESC LIMIT 1"`): la fila apunta a esa misma clave R2, con `size_bytes` igual al tamaño del objeto y `vol_time` correcto.
3. ✅ `SELECT * FROM radars` muestra el radar AMX con lat/lon/proj4 poblados dinámicamente (sin haberlo insertado a mano).
4. ✅ Descargar el objeto R2 y abrirlo en QGIS: idéntico al COG local (la subida no corrompe).

## F3 — Replay e2e local

El injector y el script de verificación son automáticos; lo manual es **lanzarlo y leer el resultado**:

1. Arrancar el servicio watcher local apuntando a un directorio vacío.
2. Correr el injector con 20 productos recientes del bucket público.
3. ✅ El script de verificación reporta: 20 COGs en R2, 20 filas en D1, backlog vacío, 0 ficheros en el directorio de errores.
4. Matar el watcher a mitad de una tanda y relanzarlo: ✅ los ficheros pendientes se procesan al arrancar (no se pierden productos entre reinicios).

## F4 — Poller + Swarm

Setup una vez: crear los secrets de Swarm (comandos en la cabecera de `docker-compose.yml`) y tener el `.env` con las variables no-secretas (`R2_ENDPOINT`, `R2_BUCKET`, `CLOUDFLARE_ACCOUNT_ID`, `D1_DATABASE_ID`, opcional `NEXRAD_SITES`).

1. Deploy: `set -a; source .env; set +a; docker stack config -c docker-compose.yml | docker stack deploy -c - nexrad`. ✅ `docker service ls` muestra poller y procesador `1/1`; `docker ps` los marca `(healthy)` tras el primer minuto.
2. ✅ En logs del poller (`docker service logs nexrad_poller`): líneas `poll: SITE_N0B_...` entrando cada pocos minutos por sitio.
3. ✅ En logs del procesador: cada producto `→ r2://...` en ~2–3 s; los ficheros del volumen compartido desaparecen tras procesarse (backlog ~0).
4. **Puerta 24 h:** dejar el stack corriendo un día. ✅ El monitor de frescura (o consulta manual a D1: `SELECT site_id, MAX(vol_time) FROM rasters GROUP BY site_id`) confirma rasters < 30 min de antigüedad para los 3 sitios durante todo el período (los huecos de 4–10 min entre volúmenes son normales; > 30 min no).
5. Reinicio de nodo o `docker service update --force nexrad_poller`: ✅ ambos servicios vuelven solos, el watermark evita re-descargar historia y el flujo se recupera sin intervención.

## F5 — Retención y alertas

1. Inyectar (vía injector de replay o a mano) productos con `vol_time` > 72 h: ✅ el sweep los borra de R2 **y** D1 en su siguiente pasada.
2. Borrar a mano una fila D1 de un raster vigente: ✅ la reconciliación reporta el objeto R2 huérfano (métrica/log), y según política lo borra o lo re-indexa.
3. Borrar a mano un objeto R2 vigente: ✅ la reconciliación reporta la fila D1 colgante.
4. Parar el procesador (`docker service scale nexrad_processor=0`): ✅ en < ~35 min llega **alerta Telegram** de sitio en rojo.
5. Rearrancarlo: ✅ llega el mensaje de recuperación cuando el sitio vuelve a verde.

## F6 — Productos restantes y fenómenos

1. Golden tests automáticos por producto; manual: abrir en QGIS un COG de cada nuevo producto raster (N0G/EET/DVL/DAA/DU3/DTA) y repetir el checklist de F1 (ubicación, calibración plausible: velocidades ±kt, topes en km/kft, precip en mm/in según unidad declarada).
2. Buscar en el bucket público un día con tormenta activa en KAMX o TJUA (verano: casi cualquier tarde) que traiga `NHI`/`NTV`/`NMD`/`NST`.
3. ✅ Tras procesarlo: filas en `phenomena` con lat/lon dentro del área del radar y atributos coherentes (probabilidad de granizo 0–100, celdas con ID estable entre volúmenes consecutivos).
4. ✅ `vwp` poblado con perfiles de viento a alturas crecientes y direcciones 0–360.
5. Cruce visual: cargar el COG de reflectividad del mismo volumen en QGIS y los fenómenos como capa de puntos (exportar query a CSV → capa de texto delimitado). ✅ Los marcadores de granizo/meso caen sobre o junto a los núcleos de eco fuerte.
