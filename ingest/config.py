"""Configuración por variables de entorno.

Cada variable admite la variante `<NOMBRE>_FILE` apuntando a un fichero
con el valor (convención Docker secrets); si ambas existen, gana `_FILE`.
"""

import os
from dataclasses import dataclass, field
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


def env_optional(name: str, default: str | None = None) -> str | None:
    """Como `_env`, pero devuelve `None` en vez de lanzar si falta (config opcional)."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text().strip()
        except OSError as exc:
            raise ConfigError(f"no se pudo leer {name}_FILE={file_path}: {exc}") from exc
    return os.environ.get(name, default)


@dataclass(frozen=True)
class StorageConfig:
    r2_endpoint: str
    r2_bucket: str
    # repr=False: nunca deben aparecer en logs/prints accidentales del objeto.
    r2_access_key_id: str = field(repr=False)
    r2_secret_access_key: str = field(repr=False)
    pg_host: str
    pg_port: str
    pg_db: str
    pg_user: str
    pg_password: str = field(repr=False)

    @classmethod
    def from_env(cls) -> "StorageConfig":
        return cls(
            r2_endpoint=_env("R2_ENDPOINT"),
            r2_bucket=_env("R2_BUCKET"),
            r2_access_key_id=_env("R2_ACCESS_KEY_ID"),
            r2_secret_access_key=_env("R2_SECRET_ACCESS_KEY"),
            pg_host=_env("PG_HOST"),
            pg_port=_env("PG_PORT", "5432"),
            pg_db=_env("PG_DB"),
            pg_user=_env("PG_USER"),
            pg_password=_env("PG_PASSWORD"),
        )

    @property
    def pg_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_db} "
            f"user={self.pg_user} password={self.pg_password}"
        )
