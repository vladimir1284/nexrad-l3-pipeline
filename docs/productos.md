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
| Cinemática | VAD/VWP (48 / `NVW`) | vectores, no-raster | — (D1) |
| Fenómenos | Mesociclones (141 / `NMD`) | no-raster | — (D1) |
| Fenómenos | Tracking de celdas (58 / `NST`) | no-raster | — (D1) |
| Fenómenos | Granizo (59 / `NHI`) | no-raster, **episódico** | — (D1) |
| Fenómenos | TVS (61 / `NTV`) | no-raster, **episódico** | — (D1) |

`NHI`/`NTV` solo se generan cuando hay celdas activas — su ausencia no es fallo del pipeline.

Los COG resultantes en uint8/uint16 comprimido pesan pocos MB en el peor caso. Todos bajo el cap de textura WebGL de 4096 px.

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
