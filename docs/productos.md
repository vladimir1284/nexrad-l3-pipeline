# Productos

Verificados contra el feed real el **2026-07-04** parseando muestras de `KAMX`/`TJUA` con MetPy 1.7.1 (`Level3File` decodifica todos sin error).

!!! danger "Códigos legacy retirados"
    Los códigos legacy 19, 20, 27, 41, 57-raster, 78, 79, 80, 94 y 99-bajas **ya no fluyen** por el IDD. Cualquier documentación o software que los referencie está obsoleto. El 99 solo fluye en cortes altos (`N2U`, `N3U`, `NBU`…), fuera de alcance por la decisión de elevación única.

## Alcance del demo

| Categoría | Producto (código / mnemónico) | Geometría nativa | Grilla AEQD resultante |
|---|---|---|---|
| Base | Reflectividad super-res (153 / `N0B`) | 720 × 1840 radial, 0.25 km × 0.5°, 460 km | 3680 × 3680 @ 0.25 km |
| Base | Velocidad super-res (154 / `N0G`) | 720 × 1200 radial, 0.25 km × 0.5°, 300 km | 2400 × 2400 @ 0.25 km |
| Derivados | Echo tops mejorado (135 / `EET`) | 360 × 346 radial, 1 km × 1°, 346 km | 692 × 692 @ 1 km |
| Derivados | VIL digital (134 / `DVL`) | 360 × 460 radial, 1 km × 1°, 460 km | 920 × 920 @ 1 km |
| Hidrometeorología | Precip 1h (170 / `DAA`) | 360 × 920 radial, 0.25 km × 1°, 230 km | 1840 × 1840 @ 0.25 km |
| Hidrometeorología | Precip 3h (173 / `DU3`) | 360 × 920 radial, 0.25 km × 1°, 230 km | 1840 × 1840 @ 0.25 km |
| Hidrometeorología | Precip storm-total (172 / `DTA`) | 360 × 920 radial, 0.25 km × 1°, 230 km | 1840 × 1840 @ 0.25 km |
| Cinemática | VAD/VWP (48 / `NVW`) | vectores, no-raster | — (tabla `vwp` en D1) |
| Fenómenos | Mesociclones (141 / `NMD`) | no-raster | — (tabla `phenomena`, kind `meso`) |
| Fenómenos | Tracking de celdas (58 / `NST`) | no-raster | — (tabla `phenomena`, kind `storm_cell`) |

!!! warning "NHI y TVS fuera del alcance (revisión 2026-07-10)"
    Los productos 59/`NHI` (granizo) y 61/`NTV` (TVS) **no fluyen en el bucket**: barrido de junio–julio 2026 en sitios con tormenta activa = 0 claves. La señal de tornado viaja igualmente en la **columna TVS del NMD** (atributo `tvs` de cada mesociclón en D1); el granizo queda sin cobertura en el demo.

Los COG resultantes en uint8 comprimido pesan pocos cientos de KB. Todos bajo el cap de textura WebGL de 4096 px.

## Calibración de los COG

Contrato único para el viewer: `físico = nivel · value_scale + value_offset` (niveles ≥ 2; 0 = below threshold/nodata, 1 = range folded). Donde la codificación nativa no es lineal, el pipeline re-encodea:

| Producto | Codificación nativa | En el COG |
|---|---|---|
| `N0B`/`N0G` | lineal (thresholds ×10) | niveles nativos tal cual (dBZ / kt) |
| `EET` | bits 0–6 = topes kft + 2; bit 7 = flag *topped* | flag enmascarado; lineal en kft |
| `DVL` | float16 NEXRAD (bias 16, no IEEE); lineal hasta nivel 20, logarítmico encima | re-encodeado lineal @ 0.35 kg/m² |
| `DAA`/`DU3`/`DTA` | scale/offset float32 en halfwords, centésimas de pulgada | niveles nativos; scale/offset convertidos a mm |

## Muestras para desarrollo

Bucket S3 público **`unidata-nexrad-level3`** — fuente del poller en producción y muestras reales para desarrollo, tests y replay:

- Claves: `SITE_MNEMO_YYYY_MM_DD_HH_MM_SS` (sitio sin prefijo K/T: `AMX`, `BYX`, `JUA`).
- Acceso anónimo: botocore `UNSIGNED`.

```python
import boto3
from botocore import UNSIGNED
from botocore.config import Config

s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED), region_name="us-east-1")
r = s3.list_objects_v2(Bucket="unidata-nexrad-level3", Prefix="AMX_N0B_2026_07_04", MaxKeys=5)
```
