from typing import Literal

from pydantic import BaseModel

KeyedTable = Literal["rasters", "wind_grids", "lightning_buckets"]


class RasterCheck(BaseModel):
    vol_time: str
    r2_key: str


class WindCheck(BaseModel):
    valid_time: str
    r2_key: str


class LightningCheck(BaseModel):
    bucket_start: str
    r2_key: str | None


class MonitorStateRow(BaseModel):
    site_id: str
    fresh: int


class MonitorStateUpsert(BaseModel):
    site_id: str
    fresh: int
    reason: str
    updated_at: str


class ExpiredR2Keys(BaseModel):
    rasters: list[str]
    wind_grids: list[str]
    lightning_buckets: list[str]


class PurgeRequest(BaseModel):
    cutoff: str


class PurgeResult(BaseModel):
    rasters: int
    wind_grids: int
    lightning_buckets: int
    phenomena: int
    vwp: int


class ReconcileKeys(BaseModel):
    rasters: list[str]
    wind_grids: list[str]
    lightning_buckets: list[str]


class DeleteDanglingRequest(BaseModel):
    table: KeyedTable
    keys: list[str]


class DeleteDanglingResult(BaseModel):
    deleted: int
