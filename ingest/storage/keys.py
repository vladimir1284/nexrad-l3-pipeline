"""Convención de claves R2: {site}/{mnemo}/{YYYY}/{MM}/{DD}/{site}_{mnemo}_{ts}.tif."""

from datetime import datetime


def raster_key(site_id: str, mnemonic: str, vol_time: datetime) -> str:
    stamp = vol_time.strftime("%Y%m%d_%H%M%S")
    return f"{site_id}/{mnemonic}/{vol_time:%Y/%m/%d}/{site_id}_{mnemonic}_{stamp}.tif"


def mosaic_key(domain_id: str, mnemonic: str, slot_time: datetime) -> str:
    """Clave del COG compuesto: mosaic/{dominio}/{mnemo}/{YYYY}/{MM}/{DD}/....tif

    Prefijo `mosaic/` propio para que el sweep y la reconciliación puedan
    distinguirlo de los COG por radar sin parsear el resto de la clave.
    Determinista en (dominio, producto, slot): recomponer un slot reescribe
    el mismo objeto, así que el upsert es idempotente de punta a punta.
    """
    stamp = slot_time.strftime("%Y%m%d_%H%M%S")
    return f"mosaic/{domain_id}/{mnemonic}/{slot_time:%Y/%m/%d}/{domain_id}_{mnemonic}_{stamp}.tif"
