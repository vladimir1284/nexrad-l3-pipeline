"""Configuración por variables de entorno.

Cada variable admite la variante `<NOMBRE>_FILE` apuntando a un fichero
con el valor (convención Docker secrets); si ambas existen, gana `_FILE`.
"""

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


def _env(name: str, default: str | None = None) -> str:
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text().strip()
        except OSError as exc:
            raise ConfigError(f"no se pudo leer {name}_FILE={file_path}: {exc}") from exc
    value = os.environ.get(name, default)
    if value is None:
        raise ConfigError(f"falta la variable {name} (o {name}_FILE)")
    return value


@dataclass(frozen=True)
class StorageConfig:
    r2_endpoint: str
    r2_bucket: str
    r2_access_key_id: str
    r2_secret_access_key: str
    cf_account_id: str
    d1_database_id: str
    cf_api_token: str

    @classmethod
    def from_env(cls) -> "StorageConfig":
        return cls(
            r2_endpoint=_env("R2_ENDPOINT"),
            r2_bucket=_env("R2_BUCKET"),
            r2_access_key_id=_env("R2_ACCESS_KEY_ID"),
            r2_secret_access_key=_env("R2_SECRET_ACCESS_KEY"),
            cf_account_id=_env("CLOUDFLARE_ACCOUNT_ID"),
            d1_database_id=_env("D1_DATABASE_ID"),
            cf_api_token=_env("CLOUDFLARE_API_TOKEN"),
        )
