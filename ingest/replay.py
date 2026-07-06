"""Injector de replay: baja productos recientes del bucket público de
Unidata y los deja caer en el directorio de entrada — misma ruta que
producción, sin levantar LDM.

Bucket `unidata-nexrad-level3`, acceso anónimo. Claves
`SITE_MNEMO_YYYY_MM_DD_HH_MM_SS` con el sitio sin prefijo K/T.
"""

import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore import UNSIGNED
from botocore.config import Config

BUCKET = "unidata-nexrad-level3"

log = logging.getLogger("l3proc")


def _anonymous_s3():
    return boto3.client("s3", config=Config(signature_version=UNSIGNED), region_name="us-east-1")


def latest_keys(
    site: str, mnemonic: str, count: int, *, s3=None, now: datetime | None = None
) -> list[str]:
    """Las `count` claves más recientes para un sitio+producto.

    Lista por prefijo diario (las claves ordenan cronológicamente dentro
    del prefijo); si el día actual no llena la cuota, completa con el
    día anterior (medianoche UTC).
    """
    s3 = s3 or _anonymous_s3()
    now = now or datetime.now(UTC)
    keys: list[str] = []
    day = now
    for _ in range(2):
        prefix = f"{site}_{mnemonic}_{day:%Y_%m_%d}"
        page = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix, MaxKeys=1000)
        found = sorted(c["Key"] for c in page.get("Contents", []))
        while page.get("IsTruncated"):
            page = s3.list_objects_v2(
                Bucket=BUCKET,
                Prefix=prefix,
                MaxKeys=1000,
                ContinuationToken=page["NextContinuationToken"],
            )
            found += sorted(c["Key"] for c in page.get("Contents", []))
        keys = found + keys  # el día anterior se antepone
        if len(keys) >= count:
            break
        day = datetime.fromtimestamp(day.timestamp() - 86400, tz=UTC)
    return keys[-count:]


def inject(
    input_dir: Path,
    sites: list[str],
    mnemonics: list[str],
    count_per_pair: int,
    *,
    s3=None,
) -> list[str]:
    """Baja y deposita productos con escritura atómica (tmp + rename,
    mismo filesystem) para que el watcher nunca vea ficheros a medias.
    Devuelve las claves inyectadas."""
    s3 = s3 or _anonymous_s3()
    input_dir.mkdir(parents=True, exist_ok=True)
    injected: list[str] = []
    for site in sites:
        for mnemo in mnemonics:
            for key in latest_keys(site, mnemo, count_per_pair, s3=s3):
                with tempfile.NamedTemporaryFile(
                    dir=input_dir, prefix=f".{key}.", delete=False
                ) as tmp:
                    s3.download_fileobj(BUCKET, key, tmp)
                Path(tmp.name).rename(input_dir / key)
                injected.append(key)
                log.info("inyectado %s", key)
    return injected
