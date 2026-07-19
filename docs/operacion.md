# Operación del stack

Dos piezas: el stack `nexrad` en Docker Swarm (nodo único, gestionado con
Portainer) hace la ingesta, y el Worker de Cloudflare **`nexrad-l3-ops`**
(`workers/ops/`) hace la operación — monitor de frescura y sweep de
retención. El monitor vive fuera del VPS a propósito: VPS caído = monitor
caído = ninguna alerta, que es exactamente el caso que debe detectar.

El stack usa **una sola imagen** (`ghcr.io/vladimir1284/nexrad-l3-pipeline`)
para los dos servicios — cambia solo el comando del entrypoint `l3proc`.
CI reconstruye y publica la imagen en cada push a `main`.

```
 VPS (Swarm)            ┌─────────────────────────────────────────────┐
                        │              volumen `incoming`             │
 bucket S3 público      │  productos crudos + .poll_state.json        │
 unidata-nexrad-level3  │  + .heartbeat + failed/                     │
        │               └─────────────────────────────────────────────┘
        │ list+get cada 60 s        ▲                    │ inotify
        ▼                           │ FILE (tmp+rename)  ▼
   ┌─────────┐                 ┌─────────┐          ┌───────────┐     COG    ┌────────┐
   │ poller  │────────────────▶│ crudos  │─────────▶│ processor │───────────▶│   R2   │
   └─────────┘                 └─────────┘          └───────────┘  metadata  ├────────┤
                                                                   upserts   │   D1   │
 Cloudflare (Worker nexrad-l3-ops)                                           └────────┘
   ┌───────────────┐  cron 17 * * * *: borra > 72 h + reconcilia huérfanos        ▲
   │  sweep        │──────────────────────────────────────────────────────────────┤
   ├───────────────┤  cron */5: ¿raster < 30 min y objeto R2? → 🔴/🟢 Telegram    │
   │  monitor      │──────────────────────────────────────────────────────────────┘
   └───────────────┘  estado por sitio en D1 `ops_monitor_state`
```

## Responsabilidades por servicio

Servicios del stack Swarm:

| Servicio | Comando | Qué hace | Estado que mantiene | Healthcheck |
|---|---|---|---|---|
| `poller` | `poll /data/incoming --interval 60` | Cada 60 s lista claves nuevas por sitio×producto en el bucket público y las deposita en el volumen con escritura atómica (tmp+rename). Catch-up tras caídas capeado a 6 claves por par. | Watermark por par en `.poll_state.json` (en el volumen — sobrevive reinicios sin re-descargar historia) | heartbeat < 300 s |
| `processor` | `watch /data/incoming` | Watcher inotify. Por producto: decodifica (MetPy) → grilla AEQD → COG → sube a R2 → metadata a D1 (upserts idempotentes). Éxito borra el crudo; fallo lo mueve a `failed/` con traza en el log. Al arrancar consume el backlog pendiente en orden de llegada. | Ninguno propio (el backlog vive en el volumen) | heartbeat < 300 s — **solo vivo, nunca por backlog**: reiniciar por atraso no vacía nada |

Crons de los Workers de Cloudflare (`nexrad-l3-ops`, detalle en `workers/ops/README.md`; `nexrad-l3-wind`, en `workers/wind/README.md`; `nexrad-l3-lightning`, en `workers/lightning/README.md`):

| Cron | Qué hace | Estado que mantiene |
|---|---|---|
| `37 * * * *` (wind, Worker `nexrad-l3-wind`) | Viento GFS 10 m desde el filtro de NOMADS para los sitios de `radars` → JSON por (sitio, valid_time) a R2 + fila en `wind_grids`. Idempotente (upsert solo gana con ciclo más nuevo, objeto reemplazado se borra); máx. `MAX_FETCHES` descargas por corrida, lo más fresco primero. | Ninguno — el estado es D1 |
| `* * * * *` + `39 * * * *` (lightning, Worker `nexrad-l3-lightning`) | Rayos GLM (GOES-19) → JSON por (sitio, cubo de 300 s) a R2 + fila **siempre** en `lightning_buckets` (aun con 0 rayos — así el viewer distingue calma de hueco). El minutero ingiere cubos cerrados hace ≥ 90 s del lookback de 30 min; el horario repite con lookback de 72 h (backfill). Idempotente (`INSERT OR IGNORE`, objetos inmutables); máx. `MAX_BUCKETS`/`MAX_BUCKETS_BACKFILL` cubos por corrida, lo fresco primero. | Ninguno — el estado es D1 |
| `*/5 * * * *` (monitor) | Tres capas por sitio: rasters (¿D1 con < 30 min **y** su objeto R2 responde a HEAD? — valida la cadena bucket→poller→processor→R2/D1), viento (`SITE:wind`: ¿cobertura futura ≥ 2 h?) y rayos (`SITE:ltg`: ¿último cubo < 30 min?). Viento/rayos se activan solos cuando su tabla tiene filas. Telegram: resumen 🩺 en el primer chequeo de una clave, después **solo transiciones** (🔴 al caer, 🟢 al recuperar). Sin secrets de Telegram queda en modo solo-log. | Último estado por clave en la tabla D1 `ops_monitor_state` |
| `17 * * * *` (sweep) | Borra rasters con `vol_time`, grillas de viento con `valid_time` y cubos de rayos con `bucket_start` fuera de la ventana de 72 h (objetos R2 primero, filas D1 después) y barre `phenomena`/`vwp`. Después reconcilia R2↔D1: limpia huérfanos (objeto sin fila en `rasters`, `wind_grids` ni `lightning_buckets`, ignorando objetos con < 1 h) y filas colgantes (verificadas con HEAD antes de borrar). | Ninguno |

El umbral de 30 min funciona porque el feed es continuo (volumen cada 4–10 min según VCP): más de 30 min sin producto = cadena rota, no cielo despejado.

## Flujo de un producto

1. El radar genera un volumen; Unidata lo publica en el bucket (~1–5 min de latencia).
2. El poller lo ve en su siguiente ciclo (≤ 60 s), lo baja a `.tmp` y lo renombra — el rename dispara el inotify del processor.
3. El processor lo decodifica, grilla a AEQD, escribe el COG (~2–3 s todo) y publica: objeto a `{site}/{mnemo}/{YYYY}/{MM}/{DD}/....tif` en R2, fila en `rasters` + upserts de `radars`/`products` en D1.
4. El crudo se borra. Latencia total radar→R2 típica: **2–7 min**.
5. Tres días después, el sweep lo borra de R2 y D1.

Fallos por el camino: crudo corrupto → `failed/` (reprocesable: moverlo de vuelta al directorio de entrada); corte a mitad de publicación → los upserts son idempotentes y la clave natural (sitio+producto+volumen) evita duplicados; objeto subido sin fila (o viceversa) → lo detecta y limpia la reconciliación.

## Operar el stack

```bash
# estado general
docker stack services nexrad                  # los 2 en 1/1
docker service logs -f --tail 20 nexrad_processor   # sigue reinicios incluidos

# frescura sin esperar al monitor
docker exec $(docker ps -qf name=nexrad_poller | head -1) \
  sh -c 'ls /data/incoming | grep -v "^\." | wc -l'    # backlog (sano: ~0)

# forzar actualización a la última imagen (re-resuelve :latest)
docker service update --force --image \
  ghcr.io/vladimir1284/nexrad-l3-pipeline:latest nexrad_processor

# simular caída (prueba de alertas) y recuperar
docker service scale nexrad_processor=0   # → 🔴 Telegram en ~35 min
docker service scale nexrad_processor=1   # → 🟢 al recuperar

# logs del Worker de operación (desde workers/ops/, con CLOUDFLARE_API_TOKEN en env)
npx wrangler tail nexrad-l3-ops
```

**Redeploy tras cambios en `docker-compose.yml`**: Portainer → stack `nexrad` → *Pull and redeploy* (re-clona el repo). Solo cambios de imagen no necesitan tocar el stack: `docker service update --force --image ...` por servicio.

**Configuración**: variables no-secretas (`R2_ENDPOINT`, `R2_BUCKET`, `CLOUDFLARE_ACCOUNT_ID`, `D1_DATABASE_ID`, `NEXRAD_SITES`) en el formulario del stack de Portainer; credenciales como secrets de Swarm (`nexrad_r2_access_key_id`, `nexrad_r2_secret_access_key`, `nexrad_cf_api_token`) montados como fichero vía la convención `*_FILE` de `ingest/config.py`. El Worker lleva su propia config: bindings y vars en `workers/ops/wrangler.jsonc`, Telegram como secrets de Wrangler (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

## Diagnóstico rápido

| Síntoma | Causa probable | Dónde mirar |
|---|---|---|
| Servicio `0/1` reiniciando | Crash al arrancar o healthcheck fallando | `docker service logs -f` (sobrevive a los reinicios; `docker service ps` solo guarda 1 tarea de historial en este nodo) |
| Logs vacíos + reinicios | Muere antes de los imports (~8 s) — lib de sistema ausente, OOM | `docker events --filter com.docker.swarm.service.name=...` (exitCode 137 = OOM) |
| 🔴 de un solo sitio | Radar en mantenimiento o feed sin ese sitio | El propio bucket: ¿hay claves nuevas? `aws s3 ls --no-sign-request s3://unidata-nexrad-level3/ --recursive` filtrado por prefijo |
| 🔴 de todos los sitios | Poller o processor caídos, o credenciales rotas | Logs de ambos; `failed/` llenándose = credenciales/red hacia Cloudflare |
| Backlog creciendo con processor sano | Throughput (~25 productos/min) < entrada — no debería con 3 sitios | Añadieron sitios/productos? Revisar tiempos por producto en el log |
