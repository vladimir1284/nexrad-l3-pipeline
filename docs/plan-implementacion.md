# Plan de implementación

Restricciones: despliegue en **Docker Swarm** (nodo único) y **validación funcional en cada capa**. Estrategia: rebanada vertical primero — un solo producto (`N0B`) de punta a punta antes de ensanchar.

## Implicaciones Swarm

1. **Colocación LDM ↔ procesador.** Se comunican por directorio compartido (FILE + watcher). Con nodo único basta un volumen local nombrado; si el swarm crece, ambos servicios llevan `placement.constraints` al mismo nodo (label `node.labels.nexrad==true`).
2. **Stack file, no compose plano.** `docker stack deploy` ignora `restart:`; se usa `deploy:` (replicas, restart_policy, resources). LDM = 1 réplica siempre (cola de productos con estado); procesador = 1 réplica (inotify sobre directorio local no se paraleliza).
3. **Registry obligatorio.** Swarm hace pull, no build. CI construye y empuja a `ghcr.io`.
4. **Credenciales R2/D1 = Docker secrets**, no variables de entorno en el stack file.
5. **HEALTHCHECK nativo en cada imagen** → Swarm reinicia containers unhealthy automáticamente.

## Mecanismos de validación

| Capa | Mecanismo | Qué prueba |
|---|---|---|
| Unit/integración | pytest + muestras reales cacheadas del bucket `unidata-nexrad-level3` (golden tests: decode → grid → COG con valores esperados) | lógica de dominio, sin red ni LDM |
| Integración storage | MinIO en CI para R2 (S3-compatible); D1 de test real (tier gratuito) | subida, schema, batching |
| E2E sin LDM | **Injector de replay**: baja productos recientes del bucket S3 y los deja caer en el directorio de entrada — misma ruta que producción | procesador completo → R2 + D1 |
| Healthcheck LDM | proceso `ldmd` vivo + edad del último producto recibido < umbral | conectividad IDD real |
| Healthcheck procesador | heartbeat (mtime de fichero) + backlog del directorio de entrada < umbral | servicio vivo y al día |
| **Monitor de frescura E2E** | servicio del stack: por cada sitio configurado, D1 tiene raster < 30 min **y** `HEAD` del objeto R2 correspondiente responde | cadena completa IDD → LDM → procesador → R2/D1 |
| Reconciliación | el sweep de retención reporta huérfanos R2↔D1 (métrica, no solo limpieza) | consistencia entre almacenes |
| CI | GitHub Actions: ruff + pytest + build/push de imágenes a ghcr | cada commit |

El monitor de frescura es viable porque el feed es continuo (volumen cada 4–10 min por sitio según VCP); el umbral de 30 min separa "roto" de "cielo despejado". **Alerta por Telegram** (bot existente) cuando un sitio pasa a rojo y cuando se recupera; el estado unhealthy en Swarm queda como señal local.

## Fases

Cada fase tiene una **puerta de validación**: no se avanza sin pasarla. La parte manual de cada puerta (QGIS, dashboards, alertas) está detallada en [Validaciones manuales](validaciones.md).

### F0 — Andamiaje

`pyproject.toml` (uv), ruff, pytest, estructura `ingest/`, CI esqueleto (lint + tests + build).

> **Puerta:** CI verde.

### F1 — Núcleo offline (solo N0B)

Decoder (MetPy) + grillado AEQD + escritura COG, expuesto como CLI: `l3proc process <fichero> → .tif`.

> **Puerta:** golden tests con muestras de KAMX/TJUA; `gdalinfo` confirma CRS/geotransform/overviews; el COG abre correctamente en QGIS.

### F2 — Storage

Schema D1 + migraciones en `db/`, cliente R2 (S3 API), cliente D1 (HTTP API con batching). El CLI ahora publica.

> **Puerta:** test de integración contra MinIO + D1 de test; fila D1 y objeto R2 coinciden (clave, tamaño, metadata).

### F3 — Servicio + replay

Watcher (watchdog) como servicio persistente + injector de replay desde el bucket S3.

> **Puerta:** e2e local — el injector mete 20 productos; script verifica: 20 COGs en R2, 20 filas en D1, backlog vacío, fallidos preservados en directorio de errores.

### F4 — LDM + stack Swarm

Contenedor LDM (`ldmd.conf` con request real, `pqact.conf` con patrón único), stack file con volúmenes/secrets/healthchecks, deploy al swarm.

> **Puerta:** monitor de frescura verde 24 h seguidas con los 3 sitios.

### F5 — Retención + monitor como servicio

Sweep de 72 h + reconciliación + monitor de frescura como servicio del stack con alertas Telegram.

> **Puerta:** inyectar datos con timestamps viejos → el sweep los borra; borrar una fila D1 a mano → la reconciliación lo reporta; apagar el procesador → llega alerta Telegram y llega la recuperación al reencenderlo.

### F6 — Resto de productos + fenómenos

N0G/EET/DVL/DAA/DU3/DTA vía config (mismo camino raster) + parsing propio de NMD/NST/NHI/NTV + NVW → D1.

> **Puerta:** golden tests por producto; fenómenos visibles en D1 con un caso real de tormenta sacado del bucket.

El demo es visible para el viewer desde **F4** con un solo producto — por eso N0B primero y el resto al final.

## Documentación

Esta documentación (MkDocs Material) se despliega automáticamente a **Cloudflare Pages** cuando cambian `docs/**` o `mkdocs.yml` en `main` (workflow `docs.yml`: build con `uvx mkdocs build --strict` + `wrangler pages deploy`).

Desarrollo local:

```bash
uvx --with mkdocs-material mkdocs serve   # preview en http://localhost:8000
uvx --with mkdocs-material mkdocs build --strict
```

Setup una sola vez (ya no repetir):

1. Crear el proyecto Pages: `wrangler pages project create nexrad-l3-docs --production-branch main`.
2. Secrets en GitHub: `CLOUDFLARE_API_TOKEN` (permiso *Cloudflare Pages — Edit*) y `CLOUDFLARE_ACCOUNT_ID`.
