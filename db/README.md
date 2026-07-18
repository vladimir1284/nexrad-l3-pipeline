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

## `wind_grids` — viento GFS 10 m (parte del contrato)

Spec acordada con el viewer jul-2026 (migración `0003_wind_grids.sql`; en el viewer el DDL vivía en `tests/contract/proposed/wind.sql` hasta el merge aquí). Fuente GFS 0.25° vía el filtro GRIB de NOMADS; la ingesta es el Worker `nexrad-l3-wind` (`workers/wind/`, cron horario). La referencia Python (`ingest/wind.py`, `l3proc wind`) implementa lo mismo con eccodes y valida al Worker (`scripts/validate_wind_worker.py`).

- Una fila por `(site_id, valid_time)`; valid_times **horarios** en la ventana de 72 h (huecos solo si NOMADS falló > 12 h). La PK cubre el único lookup del viewer (`WHERE site_id = ? AND valid_time >= ? AND valid_time < ?`).
- `r2_key`: `{SITE}/WIND/{YYYY}/{MM}/{DD}/{SITE}_WIND_{YYYYMMDD}_{HHMMSS}_c{YYYYMMDDHH}f{FFF}.json` — **inmutable**, el ciclo va en el nombre. Un ciclo más nuevo (upsert gana solo si `cycle_time` es mayor) sube objeto nuevo y borra el anterior tras el upsert.
- Formato del JSON (contrato con el viewer): `{"header": {nx, ny, lo1, la1, dx, dy, refTime, forecastHour}, "u": […], "v": […]}` — u/v en m/s a 2 decimales, longitud `nx*ny`, row-major desde la esquina NO (filas norte→sur, columnas oeste→este), `lo1` en [-180, 180). Dominio por sitio: `radars.lat/lon ± 6°` expandido a múltiplos de 0.25° (nodos = grilla GFS, subset puro).
- `forecast_hour` 0–12 con ciclos cada 6 h → continuidad horaria y ~2 h de colchón si un ciclo se retrasa.
- Retención: mismo sweep de 72 h que los rasters (Worker `nexrad-l3-ops`, por `valid_time`); la reconciliación R2↔D1 cubre `rasters` **y** `wind_grids`.
- CORS: los JSON se leen con `fetch` directo desde el navegador — misma allowlist que los COGs (van en el mismo bucket, ya cubierto). Pendiente de verificar: si el dominio del bucket no comprime JSON en el edge, subir con `content-encoding: gzip` (hoy se sube plano).
