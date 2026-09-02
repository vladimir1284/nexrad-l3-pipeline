# nexrad-l3-api

API HTTP mínima en frente de Postgres, exclusiva para `workers/ops`
(Cloudflare Worker). Existe porque un Worker no puede abrir una conexión
SQL directa a un Postgres self-hosted en el Swarm sin meter Hyperdrive +
Tunnel + Access de por medio (ver plan de migración) — `fetch()` a esta
API es más simple y acota la superficie expuesta en internet a un puñado
de endpoints fijos, en vez del protocolo de wire completo de Postgres.

`lamula-webviewer` y `ingest/` **no pasan por aquí**: el viewer (movido
al Swarm) habla Postgres directo por red interna, e ingest también.
Esta API solo cubre lo que `workers/ops` necesita: chequeos de
frescura, estado del monitor y el sweep de retención + reconciliación
R2↔Postgres.

## Correr en local

```bash
cd api
uv sync
PG_HOST=localhost PG_DB=nexrad_l3 PG_USER=nexrad PG_PASSWORD=nexrad \
NEXRAD_API_TOKEN_OPS=dev-token \
uv run uvicorn app.main:app --reload --port 8080
```

## Auth

Un solo bearer token (`NEXRAD_API_TOKEN_OPS`), scope único — ver
`app/auth.py`. `GET /healthz` es la única ruta sin auth (liveness para
Traefik/Docker healthcheck).

## Endpoints (todos bajo `/v1`, bearer requerido)

| Método | Ruta | Espeja de `workers/ops/src/index.ts` |
|---|---|---|
| GET | `/v1/checks/raster?site=` | `checkRaster` |
| GET | `/v1/checks/wind?site=` | `checkWind` |
| GET | `/v1/checks/lightning?site=` | `checkLightning` |
| GET | `/v1/layers/{layer}/active` (`wind`\|`lightning`) | `layerActive` |
| GET | `/v1/monitor-state` | lectura de `ops_monitor_state` |
| POST | `/v1/monitor-state` | upsert batch de `ops_monitor_state` |
| GET | `/v1/sweep/expired-r2-keys?cutoff=` | mitad-lectura de `sweepWindow` |
| POST | `/v1/sweep/purge` | mitad-escritura de `sweepWindow` (después de que el Worker ya borró R2) |
| GET | `/v1/reconcile/keys` | mitad-lectura de `reconcile` |
| POST | `/v1/reconcile/delete-dangling` | mitad-escritura de `reconcile` |

Orden crash-safe preservado en el Worker: `GET expired-r2-keys` (o
`reconcile/keys`) → el Worker borra objetos R2 vía su binding nativo →
recién entonces `POST purge` / `POST delete-dangling` borra filas. Esta
API nunca toca R2.
