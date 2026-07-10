# nexrad-l3-ops — Worker de operación

Worker de Cloudflare que reemplaza a los servicios `monitor` y `sweep`
del stack Swarm. Motivo del movimiento: el monitor vivía en el mismo
VPS que vigila — VPS caído = monitor caído = ninguna alerta. En Workers
el monitor sobrevive a la muerte del VPS, que es exactamente cuando
hace falta. El sweep viene de acompañante: puro D1+R2, bindings nativos
lo simplifican (sin boto3, sin HTTP API firmada).

Dos crons en un solo Worker (`src/index.ts` despacha por expresión):

| Cron | Función | Equivalente Python |
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

## Pasos pendientes para completar la migración

1. **Permiso del token.** El token de `CLOUDFLARE_API_TOKEN` (en `.env`)
   solo tiene D1 — el deploy falló con `Authentication error [code: 10000]`.
   En dash.cloudflare.com → My Profile → API Tokens, añadir al token
   (o crear uno nuevo con) el permiso **Account → Workers Scripts → Edit**.
2. **Deploy** (desde este directorio):
   ```bash
   npm install
   set -a; source ../../.env; set +a
   npx wrangler deploy
   ```
3. **Secrets de Telegram** (los mismos valores que los secrets de Swarm
   `nexrad_telegram_*`; sin ellos el Worker queda en modo solo-log):
   ```bash
   npx wrangler secret put TELEGRAM_BOT_TOKEN
   npx wrangler secret put TELEGRAM_CHAT_ID
   ```
4. **Verificar**: `npx wrangler tail nexrad-l3-ops` y esperar al
   siguiente múltiplo de 5 min — debe llegar el resumen 🩺 del primer
   chequeo (a Telegram si hay secrets, al log si no). El sweep corre
   al minuto 17 de cada hora.
5. **Retirar del stack Swarm**: borrar los servicios `monitor` y `sweep`
   de `docker-compose.yml` (y los secrets `nexrad_telegram_*` que solo
   ellos usan), redeploy del stack en Portainer (*Pull and redeploy*).
   Mientras convivan, no pasa nada grave: sweeps duplicados son
   idempotentes y las alertas llegan por duplicado.
6. **Actualizar docs**: `docs/operacion.md` (tabla de servicios, diagrama,
   prueba de alertas con `docker service scale`) y `CLAUDE.md` (sección
   Despliegue) para reflejar 2 servicios en Swarm + este Worker.
7. **Decidir el destino del código Python** (`ingest/monitor.py`,
   `ingest/retention/`): borrarlos con sus tests una vez validado el
   Worker en producción, o conservarlos como herramienta manual
   (`l3proc sweep/monitor`). Duplicado permanente = drift — no dejarlos
   "por si acaso" sin decisión.

## Typecheck

```bash
npm install
npx tsc --noEmit
```
