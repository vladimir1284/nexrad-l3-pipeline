# nexrad-l3-pipeline

Pipeline **headless** de ingesta de productos NEXRAD Level III desde el espejo S3 público del feed de NSF Unidata (`unidata-nexrad-level3`, acceso anónimo), con generación de artefactos geoespaciales (Cloud-Optimized GeoTIFF) en Cloudflare R2 y metadatos/fenómenos en Cloudflare D1.

**Propósito:** montar un demo de nuestro visualizador web de productos de radar. El visualizador (LAMULA-WebViewer, proyecto aparte basado en OpenLayers) consume directamente los COG desde R2 y consulta D1; este proyecto no renderiza ni visualiza nada.

Es el hermano "cloud/demo" de **LAMULA-Ingest**: misma lógica de dominio NEXRAD (decodificación, extracción de fenómenos), pero cambia la fuente (bucket S3 público de Unidata en vez de nbtcp desde un ORPG) y el destino (R2 + D1 en vez de FTP + PostgreSQL). Los aspectos comunes viven en un paquete Python compartido.

## Alcance del demo

- **Sitios:** 2–4 radares configurables, propuesta inicial Florida/Caribe: `KAMX` (Miami), `KBYX` (Key West), `TJUA` (Puerto Rico). Lista en config, sin hardcodear.
- **Productos:** ver [Productos](productos.md) — códigos vivos verificados contra el feed real.
- **Elevación única:** 0.5° para productos radiales por elevación.
- **Retención:** 3 días (72 h), configurable.

## Documentos

- [Arquitectura](arquitectura.md) — flujo de datos y componentes.
- [Decisiones de diseño](decisiones.md) — decisiones cerradas y sus motivos.
- [Productos](productos.md) — tabla de productos con geometrías nativas reales.
- [Plan de implementación](plan-implementacion.md) — fases, puertas de validación y despliegue en Swarm.
- [Operación del stack](operacion.md) — responsabilidades de cada servicio, flujo de un producto y diagnóstico.
