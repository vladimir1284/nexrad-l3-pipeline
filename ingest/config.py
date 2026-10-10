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


@dataclass(frozen=True)
class MosaicConfig:
    """Parámetros del mosaico multi-radar.

    `h_ref`/`r_ref` son la calibración del campo de calidad y los ajusta el
    experto mirando solapes reales: por eso van por entorno y no horneados en
    la imagen. `method` permite cambiar de promedio pesado a "gana el haz más
    bajo" sin redeploy de código.

    `MOSAIC_DOMAINS` declara la pertenencia, no la geometría: la malla se
    calcula de las filas vivas de `radars`, igual que todo lo demás aquí.
    Formato: ``GULF=AMX,BYX,HDC,EOX;OTRO=XXX,YYY``.
    """

    domains: tuple[tuple[str, tuple[str, ...]], ...]
    products: tuple[int, ...]
    cell_m: float
    method: str
    h_ref_m: float
    r_ref_m: float
    slot_s: int
    slot_tolerance_s: int
    min_sites: int
    lookback_s: int
    max_slots: int

    @classmethod
    def from_env(cls) -> "MosaicConfig":
        method = _env("MOSAIC_METHOD", "weighted")
        if method not in ("weighted", "lowest_beam"):
            raise ConfigError(f"MOSAIC_METHOD inválido: {method!r}")
        return cls(
            domains=_parse_domains(_env("MOSAIC_DOMAINS", "")),
            products=tuple(int(c) for c in _env("MOSAIC_PRODUCTS", "153").split(",") if c.strip()),
            cell_m=float(_env("MOSAIC_CELL_M", "1000")),
            method=method,
            h_ref_m=float(_env("MOSAIC_H_REF_M", "4000")),
            r_ref_m=float(_env("MOSAIC_R_REF_M", "200000")),
            slot_s=int(_env("MOSAIC_SLOT_S", "300")),
            slot_tolerance_s=int(_env("MOSAIC_SLOT_TOLERANCE_S", "150")),
            min_sites=int(_env("MOSAIC_MIN_SITES", "1")),
            lookback_s=int(_env("MOSAIC_LOOKBACK_S", "3600")),
            max_slots=int(_env("MOSAIC_MAX_SLOTS", "12")),
        )


def _parse_domains(raw: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``GULF=AMX,BYX;OTRO=XXX`` -> (("GULF", ("AMX","BYX")), ("OTRO", ("XXX",)))."""
    salida = []
    for bloque in raw.split(";"):
        bloque = bloque.strip()
        if not bloque:
            continue
        if "=" not in bloque:
            raise ConfigError(f"MOSAIC_DOMAINS mal formado en {bloque!r} (falta '=')")
        domain_id, sitios = bloque.split("=", 1)
        miembros = tuple(s.strip() for s in sitios.split(",") if s.strip())
        if not domain_id.strip() or not miembros:
            raise ConfigError(f"MOSAIC_DOMAINS mal formado en {bloque!r}")
        salida.append((domain_id.strip(), miembros))
    return tuple(salida)
