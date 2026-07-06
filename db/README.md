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
