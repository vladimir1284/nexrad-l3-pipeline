# nexrad-l3-lightning — Worker de ingesta de rayos GLM

> **Deprecado (2026-07-20).** La cuenta está en plan Free de Cloudflare
> Workers: no permite subir `limits.cpu_ms` (falla con
> `CPU limits are not supported for the Free plan [code: 100328]`), y el
> parse HDF5 de GLM (~60 ms/frame) excede el default en la mayoría de
> invocaciones — confirmado con GraphQL Analytics (~69% `exceededResources`
> en las 72 h previas al corte). La ingesta autoritativa se movió al
> servicio Docker `lightning` (`ingest/lightning.py`, `l3proc lightning`, ver
> `docker-compose.yml` y `db/README.md`), puerto fiel de la lógica de este
> Worker con `h5py` en vez de h5wasm. Este directorio se conserva como
> referencia/rollback — los gotchas de formato GLM documentados abajo (attrs
> `_Unsigned`, frame extra por cubo, etc.) siguen siendo válidos para el
> port en Python.

Worker de Cloudflare que puebla la capa de rayos del viewer: descargas
del **GOES-19 GLM** (producto `GLM-L2-LCFA`, bucket público
`noaa-goes19`, un netCDF-4 cada 20 s) → JSON por (sitio, cubo de 300 s)
en R2 + fila **siempre** (aun con 0 rayos) en `lightning_buckets`.
Contrato completo en `db/README.md`; spec acordada con el viewer
2026-07-19.

Dos crons (`src/index.ts` despacha por expresión):

| Cron | Función |
|---|---|
| `* * * * *` | cubos cerrados hace ≥ 90 s en el lookback de 30 min que falten para algún sitio (camino caliente + recuperación de caídas breves) |
| `39 * * * *` | mismo algoritmo con lookback de 72 h — rellena huecos largos (los ficheros GLM siguen en S3) |

Idempotente (PK + `INSERT OR IGNORE`, objetos inmutables); estado = D1.
La retención NO vive aquí: sweep + reconciliación en `nexrad-l3-ops`.

## Deploy

```bash
npm install            # regenera src/vendor/ (hook prepare)
npx wrangler deploy
```

**ORDEN**: `nexrad-l3-ops` va desplegado ANTES (su reconciliación debe
conocer `lightning_buckets`, o borra los primeros JSON como huérfanos
pasada la gracia de 1 h) y la migración `0004_lightning_buckets.sql`
aplicada antes que ops.

Prueba local del cron (con D1 local migrada y `radars` sembrada):

```bash
npx wrangler dev --test-scheduled
curl "http://localhost:8787/__scheduled?cron=*+*+*+*+*"
```

## h5wasm vendorizado (resultado del spike 2026-07-19)

Los LCFA son HDF5 con compresión interna; se parsean con
[h5wasm](https://github.com/usnistgov/h5wasm), pero el paquete npm trae
un build SINGLE_FILE de Emscripten: wasm embebido como string +
`WebAssembly.instantiate(bytes)` en runtime — **prohibido en workerd**
(sin code-gen dinámico). `scripts/vendor-h5wasm.mjs` (corre solo en
`npm install`) lo adapta:

1. extrae el binario wasm (3.4 MB) interceptando `WebAssembly.instantiate`,
2. parchea `hdf5_hl.js` para importar el `.wasm` como módulo (wrangler
   lo precompila) e inyectarlo vía el hook `instantiateWasm` de
   Emscripten,
3. genera un `.d.ts` mínimo para `src/glm.ts`.

`src/vendor/` está gitignored — regenerable y determinista para la
versión **pinada** de h5wasm (0.10.3; al subirla, el script valida sus
anchors y falla ruidoso si el dist cambió). Bundle resultante: ~9 MB
raw / **1.9 MB gzip** (límite 10 MB del plan de pago).

Medidas del spike (fichero real de 380 KB, 617 flashes): parse ~60 ms
en workerd. Caveats:

- **No leer datasets escalares** (`product_time`): disparan
  `"name not defined"` en h5wasm bajo workerd. La base de tiempo sale
  del atributo `units` de `flash_time_offset_of_first_event`
  (equivalente, verificado).
- Los offsets de tiempo son **uint16 empaquetado** (atributo
  `_Unsigned` sobre `<i2` crudo): reinterpretar con `& 0xffff` antes de
  aplicar scale/offset.
- Flashes con primer evento ANTES de la ventana del fichero existen
  (offsets negativos, verificado con datos reales) — por eso cada cubo
  lista un frame extra (`s = bucket_end` inclusive) y filtra por el
  tiempo real del flash (`framesForBucket` + `strikesForSite`).

## Decisiones

- **Cubos con < 16 frames listados se difieren hasta 1 h** (blip del
  listado S3 ≠ hueco real del downlink GOES); pasada la hora se ingiere
  lo que haya y queda registrado en el log.
- Presupuesto por corrida: `MAX_BUCKETS` (minutero, def. 4) /
  `MAX_BUCKETS_BACKFILL` (horario, def. 30) — peor caso ~30×16 GETs +
  parse, de ahí `limits.cpu_ms = 120000`. Del más nuevo al más viejo:
  lo fresco primero si el presupuesto se agota.
- Fuente por var (`GLM_BASE`): un sitio fuera del disco GOES-East
  necesitaría `noaa-goes18` — cambio de adaptador, mismo contrato.
- Filtro de calidad `flash_quality_flag == 0` y radio 460 km: valores
  del contrato (el primero provisional, pendiente de criterio experto).

## Tests / typecheck

```bash
npm test                  # lógica pura (core.ts) bajo node --test
npx tsc --noEmit          # worker
npx tsc --noEmit -p test  # tests
```
