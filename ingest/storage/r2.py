"""Cliente R2 vía S3 API (boto3). También habla con MinIO en tests."""

from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class R2Client:
    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
    ) -> None:
        self.bucket = bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(
                retries={"max_attempts": 3, "mode": "standard"},
                s3={"addressing_style": "path"},
                # Timeouts cortos: un endpoint inaccesible debe ser una
                # excepción visible en segundos, no un cuelgue silencioso
                # de minutos (60 s × reintentos por defecto).
                connect_timeout=10,
                read_timeout=60,
            ),
        )

    def upload_file(self, path: str | Path, key: str, content_type: str = "image/tiff") -> None:
        # put_object (no multipart): los COG del demo son de pocos MB y así
        # la subida es atómica — o el objeto está entero o no está.
        # cache-control inmutable: la clave incluye vol_time, nunca se
        # reescribe (consumidor: LAMULA-WebViewer, fetch único a blob).
        with open(path, "rb") as fh:
            self._s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=fh,
                ContentType=content_type,
                CacheControl="public, max-age=31536000, immutable",
            )

    def head(self, key: str) -> dict | None:
        """Metadata del objeto (ContentLength incluido) o None si no existe."""
        try:
            return self._s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                return None
            raise

    def list_keys(self, prefix: str = "") -> list[str]:
        """Todas las claves del bucket (paginado). Escala de demo: miles."""
        keys: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys

    def delete_keys(self, keys: list[str]) -> None:
        """Borrado en lotes de 1000 (límite de delete_objects)."""
        for i in range(0, len(keys), 1000):
            chunk = keys[i : i + 1000]
            self._s3.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
            )
