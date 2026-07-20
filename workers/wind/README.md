# nexrad-l3-wind — Worker de ingesta de viento GFS

> **Deprecado (2026-07-20).** La ingesta autoritativa de viento se movió al
> servicio Docker `wind` (`ingest/wind.py`, `l3proc wind`, ver
> `docker-compose.yml` y `db/README.md`). Este Worker se conserva en el repo
> como referencia/rollback, no desplegado. Motivo: el plan Free de Cloudflare
> Workers no permite subir `limits.cpu_ms`; ver el README de
> `workers/lightning/` para el detalle (el mismo motivo aplicó ahí primero).

Worker de Cloudflare con cron horario (`37 * * * *`): viento **GFS 0.25°
10 m** (u/v) desde el filtro GRIB de NOMADS → JSON por (sitio,
valid_time) en R2 + fila en `wind_grids` (D1). Consumidor: la capa de
partículas animadas de LAMULA-WebViewer. Contrato (schema, claves R2,
formato JSON) en `db/README.md` y migración `0003_wind_grids.sql`.

Sin contenedor en el VPS: puro fetch→decode→publicar, bindings nativos
D1/R2, sin secrets propios. La **referencia Python** (`ingest/wind.py`,
`l3proc wind`) implementa la misma lógica con eccodes y queda para
validación cruzada y como fallback.

## Diseño

- **Fuente: filtro GRIB de NOMADS** (`filter_gfs_0p25.pl`). OPeNDAP/DODS
  fue retirado (SCN 25-81, verificado 2026-07-18) — no hay fuente JSON;
  por eso el decoder GRIB2 propio en `src/grib.ts`.
- **El decoder soporta exactamente lo que el filtro emite**: template
  3.0 + 5.0 (`grid_simple`), sin bitmap. El filtro re-empaqueta los
  subsets **sur→norte** (`jScansPositively=1`) aunque los GFS crudos van
  norte→sur — el decoder normaliza ambos a filas norte→sur (contrato).
  Todo lo demás lanza error: mejor reventar visible que decodificar mal.
- **Una descarga por (ciclo, fh)** con el bbox unión de los sitios de
  `radars`; recorte local por sitio (grilla regular alineada a 0.25° →
  subset por índice, sin resampleo).
- **Estado = D1.** Upsert gana solo con `cycle_time` más nuevo; objeto
  R2 reemplazado se borra tras el upsert; corrida idempotente.
- **Presupuesto `MAX_FETCHES` por corrida** (def. 20): cortesía con
  NOMADS (bloquean IPs > ~120 hits/min; pausa `NOMADS_PAUSE_MS` entre
  requests) y margen para el límite de subrequests del plan. Los
  valid_times van del más nuevo al más viejo: si el presupuesto se
  agota, lo fresco ya está publicado y el backfill de 72 h converge en
  ~4 corridas.
- **Retención**: la hace el sweep de `nexrad-l3-ops` (que también cubre
  `wind_grids` en su reconciliación R2↔D1 — desplegar ese Worker ANTES
  que este, o el reconcile viejo borra los JSON `WIND/` como huérfanos).

## Comandos

```bash
npm install            # una vez
npm run check          # tsc del worker
npx tsc --noEmit -p test   # tsc de los tests
npm test               # decoder vs golden de eccodes + lógica pura (sin red)
npm run dev            # local: curl "http://localhost:8787/__scheduled?cron=37+*+*+*+*"
npm run deploy         # requiere CLOUDFLARE_API_TOKEN / wrangler login
```

El fixture de `test/data/` es un subset real del filtro (2026-07-17 12Z
f003, unión AMX+JUA); `golden.json` lo generó eccodes vía
`ingest.wind.decode_grib`. Validación online contra la referencia
Python, con el Worker ya corriendo:

```bash
uv run python scripts/validate_wind_worker.py   # credenciales en env
```
