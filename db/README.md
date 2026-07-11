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

Estado actual del parser (`ingest/phenomena/parse.py`) + extensión acordada con el viewer (⏳ pendiente de implementar aquí; prerequisito de la fase F4 del viewer):

| `kind` | Clave | Estado | Contenido |
|---|---|---|---|
| `storm_cell` | `azran_nm` | ✅ | `[az_deg, range_nm]` posición radar-céntrica del tabular |
| `storm_cell` | `movement_deg`, `movement_kt` | ✅ | vector de movimiento |
| `storm_cell` | `new` | ✅ | celda nueva en este volumen |
| `storm_cell` | `past`, `forecast` | ⏳ | arrays `[[x_km, y_km], …]` de los packets 23/24 del symbology (SCIT) |
| `storm_cell` | `vil_kg_m2`, `dbz_max`, `top_kft` | ⏳ | del bloque tabular "STORM CELL ATTRIBUTES" de NST |
| `storm_cell` | `poh_pct`, `posh_pct`, `hail_size_in` | ⏳ | probabilidad (POH/POSH) y tamaño de granizo, mismo tabular (NHI/NTV no fluyen en el feed; esta es la señal de granizo) |
| `meso` | `radius_km` + atributos del tabular NMD | ✅ | atributos del mesociclón; la columna TVS del NMD es la señal TVS |

Notas para la implementación ⏳:

- Solo parser + tests golden — cero migración D1, cero cambio en el viewer hasta que aterrice.
- Los charts de tendencia del viewer arman series temporales por `cell_id` cross-volumen: el `cell_id` del RPG debe seguir guardándose tal cual (estable entre volúmenes).
- Los goldens de fenómenos existentes cambian de sha al crecer `attrs` — regenerarlos es parte de la tarea, con verificación manual de 2–3 celdas contra el tabular crudo.
