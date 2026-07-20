# db/ — schema D1 (contrato con el viewer)

Migraciones SQL en `migrations/`, formato wrangler (`NNNN_nombre.sql`, orden lexicográfico, cada una corre una sola vez).

Wrangler exige un fichero de config para resolver la base: es `wrangler.jsonc` **de este directorio** (no hay config en la raíz del repo — el deploy de docs no la necesita). Setup una vez: crear las bases y copiar sus IDs en `wrangler.jsonc`:

```bash
npx wrangler d1 create nexrad-l3        # anota database_id
npx wrangler d1 create nexrad-l3-test   # ídem (o: npx wrangler d1 list)
```

Aplicar migraciones (desde `db/`):

```bash
cd db
npx wrangler d1 migrations apply nexrad-l3 --remote        # base real
npx wrangler d1 migrations apply nexrad-l3-test --remote   # base de CI
```

Reglas:

- **Nunca editar una migración ya aplicada** — siempre una nueva.
- El schema es el **contrato con LAMULA-WebViewer**: cambios incompatibles (renombrar/borrar columnas o tablas) se coordinan con el viewer antes de aplicarse.
- Timestamps `TEXT` ISO-8601 UTC sin sufijo de zona. Comparables lexicográficamente (los índices de retención dependen de eso).
- Calibración de rasters: `físico = nivel · value_scale + value_offset` para niveles ≥ 2; nivel 0 = below threshold (nodata), 1 = range folded.
- Diseñado para ser migrable a PostgreSQL (tipos y constraints estándar; `AUTOINCREMENT` → `BIGSERIAL` sería el único cambio mecánico).

## Claves de `phenomena.attrs` (parte del contrato)

`attrs` es JSON y por tanto extensible sin migración, pero **sus claves son contrato con el viewer** igual que las columnas: renombrar o cambiar unidades de una clave existente se coordina como cualquier cambio incompatible. Referencia cruzada: página "Contrato de datos" de la doc de [lamula-webviewer](https://github.com/vladimir1284/lamula-webviewer).

Estado del parser (`ingest/phenomena/parse.py`), prerequisito de la fase F4 del viewer:

| `kind` | Clave | Estado | Contenido |
|---|---|---|---|
| `storm_cell` | `azran_nm` | ✅ | `[az_deg, range_nm]` posición radar-céntrica del tabular |
| `storm_cell` | `movement_deg`, `movement_kt` | ✅ | vector de movimiento |
| `storm_cell` | `new` | ✅ | celda nueva en este volumen |
| `storm_cell` | `past`, `forecast` | ✅ | arrays `[[x_km, y_km], …]` de los packets 23/24 del symbology (SCIT); sin la posición actual, que ya es la del registro. Celdas nuevas no traen ninguno; puede venir uno solo de los dos |
| `storm_cell` | `dbz_max`, `dbz_max_height_kft` | ✅ | reflectividad máxima de la celda (dBZ) y su altura (kft), del bloque Graphic Alphanumeric del NST (fila `DBZM HGT`). El GAB pagina de a 6 celdas y puede listar menos celdas que el symbology — la clave falta en las que se quedan fuera |
| `meso` | `radius_km` + atributos del tabular NMD | ✅ | atributos del mesociclón; la columna TVS del NMD es la señal TVS |

**Fuera de alcance — datos que no distribuye el feed** (acordado 2026-07: se recorta la extensión original; coordinado con el viewer): VIL por celda, echo top por celda (`vil_kg_m2`, `top_kft`) y granizo (`poh_pct`, `posh_pct`, `hail_size_in`). Viven en los productos SS (62) y HI (59), que **no fluyen en el bucket de Unidata** (sondeo 0 claves con tormentas activas, 2026-07-10); el NST no los trae — la tabla "STORM CELL ATTRIBUTES" de los visores es un compuesto cliente de STI+SS+HI. VIL y echo top sí existen como rasters de grilla (DVL, EET).

Para los charts de tendencia del viewer (series temporales por `cell_id` cross-volumen): el `cell_id` del RPG se guarda tal cual (estable entre volúmenes).

## `wind_grids` — viento GFS 10 m + niveles de altura (parte del contrato)

Spec de superficie acordada con el viewer jul-2026 (migración `0003_wind_grids.sql`; en el viewer el DDL vivía en `tests/contract/proposed/wind.sql` hasta el merge aquí). Fuente GFS 0.25° vía el filtro GRIB de NOMADS; la ingesta es el servicio Docker `wind` (`ingest/wind.py`, `l3proc wind`, ver `docker-compose.yml`) — vivió en el Worker `nexrad-l3-wind` hasta que el plan Free de Cloudflare Workers forzó mover también rayos a Docker (ver contrato de `lightning_buckets` abajo); el código del Worker queda en `workers/wind/` marcado deprecado, por si hace falta rollback.

**Fase 2 (niveles de altura, 2026-07-20, migración `0005_wind_levels.sql`):** terna "steering flow" 850/700/500 hPa, para contrastar con el movimiento de celdas. El viewer tiene selector de altura pero **muestra un nivel a la vez** — no simultáneo — así que el contrato sigue siendo una fila por lookup, solo que ahora con `level` en la PK. **Pendiente de confirmar con el viewer** antes de que consuma niveles distintos de `10m`: el nombre exacto de los valores de `level` (`850hPa` etc.) y que su query agregue el filtro `level = ?`. Hasta esa confirmación, `l3proc wind` sigue por defecto ingiriendo solo `10m` (opt-in vía `--levels`/`WIND_LEVELS`, ver `ingest/wind.py`).

- Una fila por `(site_id, valid_time, level)`; valid_times **horarios** en la ventana de 72 h (huecos solo si NOMADS falló > 12 h). La PK cubre el lookup del viewer (`WHERE site_id = ? AND level = ? AND valid_time >= ? AND valid_time < ?`).
- `level`: `10m` (superficie, dato original) | `850hPa` | `700hPa` | `500hPa`. Filas viejas (pre fase 2) fueron backfilleadas a `10m` en la migración.
- `r2_key`: `{SITE}/WIND/{YYYY}/{MM}/{DD}/{SITE}_WIND_{YYYYMMDD}_{HHMMSS}_c{YYYYMMDDHH}f{FFF}_{level}.json` — **inmutable**, ciclo y nivel van en el nombre (niveles distintos del mismo valid_time son objetos R2 distintos). Un ciclo más nuevo (upsert gana solo si `cycle_time` es mayor, por nivel) sube objeto nuevo y borra el anterior tras el upsert.
- Formato del JSON (contrato con el viewer, igual en todos los niveles): `{"header": {nx, ny, lo1, la1, dx, dy, refTime, forecastHour}, "u": […], "v": […]}` — u/v en m/s a 2 decimales, longitud `nx*ny`, row-major desde la esquina NO (filas norte→sur, columnas oeste→este), `lo1` en [-180, 180). Dominio por sitio: `radars.lat/lon ± 6°` expandido a múltiplos de 0.25° (nodos = grilla GFS, subset puro) — igual para superficie y niveles isobáricos.
- `forecast_hour` 0–12 con ciclos cada 6 h → continuidad horaria y ~2 h de colchón si un ciclo se retrasa.
- Retención: mismo sweep de 72 h que los rasters (Worker `nexrad-l3-ops`, por `valid_time`); la reconciliación R2↔D1 cubre `rasters` **y** `wind_grids`. `ops` no cambió con la migración a Docker ni con los niveles: lee/escribe por SQL D1 + API R2, sin importarle quién escribió las filas ni cuántos niveles haya por valid_time.
- CORS: los JSON se leen con `fetch` directo desde el navegador — misma allowlist que los COGs (van en el mismo bucket, ya cubierto). Pendiente de verificar: si el dominio del bucket no comprime JSON en el edge, subir con `content-encoding: gzip` (hoy se sube plano).

## `lightning_buckets` — descargas eléctricas GLM (parte del contrato)

Spec acordada con el viewer 2026-07-19 (migración `0004_lightning_buckets.sql`; el DDL propuesto vivía en `tests/contract/proposed/` del viewer). Fuente: **GOES-19 GLM `GLM-L2-LCFA`** (bucket público `noaa-goes19`, un netCDF-4 cada 20 s, nivel flash). Sin contrato comercial de rayos (confirmado jul-2026) — si algún día lo hubiera, cambia solo el adaptador de fuente; este contrato queda igual. La ingesta es el servicio Docker `lightning` (`ingest/lightning.py`, `l3proc lightning`, ver `docker-compose.yml`), un único barrido por vuelta (sin split minutero/backfill) sobre la ventana completa de 72 h. Vivió en el Worker `nexrad-l3-lightning` hasta confirmar (2026-07-20) que el plan Free de Cloudflare Workers no permite subir `limits.cpu_ms` y el parse HDF5 de GLM (~60 ms/frame) excede el default en la mayoría de invocaciones (confirmado con GraphQL Analytics: ~69% `exceededResources`); el código del Worker queda en `workers/lightning/` marcado deprecado — el spike h5wasm-sobre-workerd de 2026-07-19 (vendorizado, wasm como módulo + hook `instantiateWasm`; `jsfive` descartado por perder scale/offset) documenta gotchas del formato GLM igual de válidos para el port a `h5py`.

- Cubos fijos de **300 s alineados a UTC** (`:00/:05/:10…`), desacoplados del VCP a propósito — el viewer junta los cubos que solapan la ventana de la observación (lookup: `WHERE site_id = ? AND bucket_start >= ? AND bucket_start < ?`, cubierto por la PK).
- **Fila SIEMPRE al cerrar el cubo, incluso con 0 rayos** (`strike_count = 0`, `r2_key NULL`, sin objeto R2): fila presente = cubo cubierto sin descargas; fila ausente = hueco de ingesta.
- `r2_key`: `{SITE}/LIGHTNING/{YYYY}/{MM}/{DD}/{SITE}_LTG_{YYYYMMDD}_{HHMMSS}.json` (fecha/hora = `bucket_start`) — **inmutable**: el cubo se procesa una única vez, cerrado + ≥ 90 s de margen de latencia GLM.
- Formato JSON: `{"site", "bucket_start", "bucket_s", "strikes": [[lon, lat, offset_s], …]}` — lon/lat en grados a 3 decimales, lon en [-180, 180); `offset_s` = segundos desde `bucket_start`, 1 decimal, ascendente, en `[0, bucket_s)`. Atributos futuros irían en una posición extra opcional (parser del viewer tolerante, como `attrs`).
- Recorte por sitio: gran círculo ≤ **460 km** del radar (extensión N0B; acordado 2026-07-19). Un flash puede aparecer en el fichero de varios sitios — correcto, ficheros independientes.
- Filtro de calidad: `flash_quality_flag == 0` — **provisional, sin criterio experto aún**; cambiar el umbral es cambio de ingesta, no de contrato.
- Frontera de cubos (regla de ingesta): flashes asignados por el tiempo real del primer evento; se lista un frame GLM extra (`s = bucket_end` inclusive) porque un flash que cruza frames aparece en el fichero posterior con el primer evento hacia atrás; lo que caiga fuera de `[0, bucket_s)` se descarta. Ojo: el cubo `:55` mete el frame extra en el prefijo de la hora siguiente (dos LIST).
- Retención: 72 h en `nexrad-l3-ops` (sweep por `bucket_start`, no por `r2_key` — hay filas sin objeto; la reconciliación ignora `r2_key NULL`). **Orden de deploy/arranque: `ops` antes que la primera corrida real de ingesta** (Worker o el servicio Docker) — si no, la reconciliación borra los primeros JSON como huérfanos (pasada la gracia de 1 h). `ops` no cambió con la migración a Docker.
- CORS: mismo bucket que COGs/viento, allowlist ya cubierta.
