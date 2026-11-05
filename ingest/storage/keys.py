"""Convención de claves R2: {site}/{mnemo}/{YYYY}/{MM}/{DD}/{site}_{mnemo}_{ts}.tif.

Todas las claves admiten un prefijo opcional (`R2_KEY_PREFIX`) para
aislar instancias que comparten bucket: LAMULA Jobs levanta una
instancia del pipeline por job, cada una con su schema de Postgres pero
todas contra el mismo bucket, y sin prefijo dos instancias que procesen
el mismo radar a la misma hora escriben el MISMO objeto. Vacío por
defecto = claves idénticas a las de siempre, producción no cambia.

El prefijo va en la clave, no solo en la subida: lo que se guarda en
`r2_key` es lo que el viewer pide al bucket y lo que la reconciliación
compara, así que tiene que incluirlo.
"""

from datetime import datetime


def normalize_prefix(raw: str | None) -> str:
    """`""` | `"jobs/42"` | `"/jobs/42/"` → `""` | `"jobs/42/"`.

    Idempotente: aplicar dos veces da lo mismo, así que da igual si el
    valor ya venía normalizado de `StorageConfig`.
    """
    cleaned = (raw or "").strip().strip("/")
    return f"{cleaned}/" if cleaned else ""


def raster_key(site_id: str, mnemonic: str, vol_time: datetime, prefix: str = "") -> str:
    stamp = vol_time.strftime("%Y%m%d_%H%M%S")
    return (
        f"{normalize_prefix(prefix)}{site_id}/{mnemonic}/{vol_time:%Y/%m/%d}/"
        f"{site_id}_{mnemonic}_{stamp}.tif"
    )


def mosaic_key(domain_id: str, mnemonic: str, slot_time: datetime, prefix: str = "") -> str:
    """Clave del COG compuesto: mosaic/{dominio}/{mnemo}/{YYYY}/{MM}/{DD}/....tif

    Prefijo `mosaic/` propio para que el sweep y la reconciliación puedan
    distinguirlo de los COG por radar sin parsear el resto de la clave.
    Con `R2_KEY_PREFIX` el discriminante pasa a ser `{prefijo}mosaic/`, y
    el Worker `ops` lo compone igual (misma variable) — ver su cabecera.
    Determinista en (dominio, producto, slot): recomponer un slot reescribe
    el mismo objeto, así que el upsert es idempotente de punta a punta.
    """
    stamp = slot_time.strftime("%Y%m%d_%H%M%S")
    return (
        f"{normalize_prefix(prefix)}mosaic/{domain_id}/{mnemonic}/{slot_time:%Y/%m/%d}/"
        f"{domain_id}_{mnemonic}_{stamp}.tif"
    )
