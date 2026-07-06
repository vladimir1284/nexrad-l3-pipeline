"""Convención de claves R2: {site}/{mnemo}/{YYYY}/{MM}/{DD}/{site}_{mnemo}_{ts}.tif."""

from datetime import datetime


def raster_key(site_id: str, mnemonic: str, vol_time: datetime) -> str:
    stamp = vol_time.strftime("%Y%m%d_%H%M%S")
    return f"{site_id}/{mnemonic}/{vol_time:%Y/%m/%d}/{site_id}_{mnemonic}_{stamp}.tif"
