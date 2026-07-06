"""Integración real: S3 (MinIO en CI / R2) y D1 de test.

Se saltan solos si el entorno no trae credenciales — así el suite corre
completo en local sin red y en forks sin secrets.
"""

import contextlib
import os
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from ingest.storage.keys import raster_key

pytestmark = pytest.mark.integration

S3_VARS = ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
D1_VARS = ("CLOUDFLARE_ACCOUNT_ID", "D1_DATABASE_ID", "CLOUDFLARE_API_TOKEN")

requires_s3 = pytest.mark.skipif(
    not all(os.environ.get(v) for v in S3_VARS), reason="sin endpoint S3/MinIO en el entorno"
)
requires_d1 = pytest.mark.skipif(
    not all(os.environ.get(v) for v in D1_VARS), reason="sin credenciales D1 en el entorno"
)


@requires_s3
def test_r2_upload_head_roundtrip(tmp_path):
    import boto3

    from ingest.storage.r2 import R2Client

    endpoint = os.environ["R2_ENDPOINT"]
    bucket = os.environ["R2_BUCKET"]
    ak, sk = os.environ["R2_ACCESS_KEY_ID"], os.environ["R2_SECRET_ACCESS_KEY"]

    # MinIO arranca vacío: crear el bucket si falta (en R2 real ya existe).
    s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=ak, aws_secret_access_key=sk)
    with contextlib.suppress(s3.exceptions.ClientError):
        s3.create_bucket(Bucket=bucket)

    payload = tmp_path / "cog.tif"
    payload.write_bytes(b"II*\x00" + os.urandom(1024))
    key = raster_key("TST", "N0B", datetime(2026, 7, 6, 12, 0, 0))

    client = R2Client(endpoint, bucket, ak, sk)
    assert client.head(key) is None
    client.upload_file(payload, key)

    meta = client.head(key)
    assert meta is not None
    assert meta["ContentLength"] == payload.stat().st_size
    assert meta["ContentType"] == "image/tiff"

    s3.delete_object(Bucket=bucket, Key=key)


@requires_d1
def test_d1_insert_select_delete():
    from ingest.storage.d1 import D1Client

    marker = f"itest-{uuid.uuid4().hex[:8]}"
    with D1Client(
        os.environ["CLOUDFLARE_ACCOUNT_ID"],
        os.environ["D1_DATABASE_ID"],
        os.environ["CLOUDFLARE_API_TOKEN"],
    ) as d1:
        d1.execute(
            "INSERT INTO radars (site_id, lat, lon, height_m, proj4, first_seen_at, last_seen_at)"
            " VALUES (?, 0, 0, 0, ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            [marker, "+proj=aeqd"],
        )
        rows = d1.execute("SELECT site_id FROM radars WHERE site_id = ?", [marker])
        assert rows == [{"site_id": marker}]
        d1.execute("DELETE FROM radars WHERE site_id = ?", [marker])
        assert d1.execute("SELECT 1 AS x FROM radars WHERE site_id = ?", [marker]) == []


@requires_s3
@requires_d1
def test_publish_e2e_r2_d1_coinciden(tmp_path):
    """Puerta F2: fila D1 y objeto R2 coinciden en clave y tamaño."""
    from ingest.config import StorageConfig
    from ingest.decoder.level3 import decode_file
    from ingest.gridding.aeqd import grid_radial
    from ingest.gridding.cog import write_cog
    from ingest.storage.d1 import D1Client
    from ingest.storage.publish import publish_cog
    from ingest.storage.r2 import R2Client

    cfg = StorageConfig.from_env()
    prod = decode_file(Path(__file__).parent / "data" / "AMX_N0B_2026_07_06_15_45_17")
    grid = grid_radial(prod)
    cog = write_cog(grid, prod, tmp_path / "amx.tif")

    r2 = R2Client(cfg.r2_endpoint, cfg.r2_bucket, cfg.r2_access_key_id, cfg.r2_secret_access_key)
    with D1Client(cfg.cf_account_id, cfg.d1_database_id, cfg.cf_api_token) as d1:
        result = publish_cog(cog, prod, grid, r2, d1)

        meta = r2.head(result.r2_key)
        assert meta is not None and meta["ContentLength"] == result.size_bytes

        row = d1.execute(
            "SELECT r2_key, size_bytes FROM rasters WHERE r2_key = ?", [result.r2_key]
        )[0]
        assert row["r2_key"] == result.r2_key
        assert row["size_bytes"] == meta["ContentLength"]

        # limpieza
        d1.execute("DELETE FROM rasters WHERE r2_key = ?", [result.r2_key])
    r2._s3.delete_object(Bucket=r2.bucket, Key=result.r2_key)
