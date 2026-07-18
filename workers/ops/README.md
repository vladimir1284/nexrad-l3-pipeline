# nexrad-l3-ops — Worker de operación

Worker de Cloudflare que reemplaza a los servicios `monitor` y `sweep`
del stack Swarm. Motivo del movimiento: el monitor vivía en el mismo
VPS que vigila — VPS caído = monitor caído = ninguna alerta. En Workers
el monitor sobrevive a la muerte del VPS, que es exactamente cuando
hace falta. El sweep viene de acompañante: puro D1+R2, bindings nativos
lo simplifican (sin boto3, sin HTTP API firmada).

Dos crons en un solo Worker (`src/index.ts` despacha por expresión):

| Cron | Función | Equivalente Python (borrado en la migración) |
|---|---|---|
| `*/5 * * * *` | monitor de frescura E2E + Telegram | `ingest/monitor.py` |
| `17 * * * *` | retención 72 h + reconciliación R2↔D1 | `ingest/retention/sweep.py` |

Estado del monitor (último verde/rojo por sitio, para alertar solo en
transiciones) en la tabla D1 `ops_monitor_state`
(migración `db/migrations/0002_ops_monitor_state.sql` — **ya aplicada**
a la base remota `nexrad-l3` el 2026-07-10).

## Diferencias deliberadas con la versión Python

- **Primer chequeo de un sitio manda resumen 🩺 por Telegram.** El
  original solo hablaba en transiciones: arranque en verde = mudo para
  siempre, indistinguible de monitor muerto. (Esto explica el "monitor
  no detecta frescura" investigado el 2026-07-10: **no había bug** —
  `check_site` contra D1/R2 reales dio los 3 sitios frescos, edades
  1.6–2.5 min, `vol_time` UTC correcto. Era silencio por diseño.
  Zona horaria descartada.)
- **La reconciliación ignora objetos R2 con < 1 h en el bucket** y
  verifica filas colgantes con HEAD antes de borrarlas: `publish` sube
  a R2 antes de insertar en D1 y un sweep en esa ventana veía huérfanos
  falsos. La versión Python tiene esa carrera.
- En JS, `Date.parse` de un ISO sin zona es hora *local*: todos los
  `vol_time` (UTC naive en D1) se parsean con `"Z"` explícita
  (`parseUtc()`). No quitar.
- **Sweep y reconciliación cubren `rasters` y `wind_grids`**
  (`KEYED_TABLES`): todo objeto del bucket debe estar referenciado por
  una de esas tablas o la reconciliación lo borra como huérfano. Si otra
  tabla con `r2_key` aparece algún día, añadirla ahí **antes** de que su
  ingesta suba el primer objeto.

## Estado de la migración (2026-07-10)

Hecho: deploy (`npx wrangler deploy` con token con permiso Workers
Scripts Edit), secrets de Telegram puestos (`wrangler secret put
TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`), primer chequeo del monitor
verificado en el tail (23:20 UTC — 3 sitios 🟢, resumen 🩺 enviado a
Telegram), servicios `monitor` y `sweep` retirados de
`docker-compose.yml` y docs actualizadas.

Pendiente:

1. **Verificar el sweep** en el tail al minuto 17 de la hora
   (`npx wrangler tail nexrad-l3-ops`).
2. **Redeploy del stack en Portainer** (*Pull and redeploy*) para que
   tome el `docker-compose.yml` sin `monitor`/`sweep`; borrar los
   secrets de Swarm `nexrad_telegram_*` que solo ellos usaban.
   Mientras convivan, no pasa nada grave: sweeps duplicados son
   idempotentes y las alertas llegan por duplicado.

El código Python equivalente (`ingest/monitor.py`, `ingest/retention/`,
sus tests y los subcomandos `l3proc sweep`/`l3proc monitor`) se borró
al validar el Worker — decisión del 2026-07-10 para evitar drift de
implementaciones duplicadas. Está en el historial de git si hiciera
falta recuperarlo.

## Typecheck

```bash
npm install
npx tsc --noEmit
```
