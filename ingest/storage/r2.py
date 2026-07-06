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
            ),
        )

    def upload_file(self, path: str | Path, key: str, content_type: str = "image/tiff") -> None:
        # put_object (no multipart): los COG del demo son de pocos MB y así
        # la subida es atómica — o el objeto está entero o no está.
        with open(path, "rb") as fh:
            self._s3.put_object(Bucket=self.bucket, Key=key, Body=fh, ContentType=content_type)

    def head(self, key: str) -> dict | None:
        """Metadata del objeto (ContentLength incluido) o None si no existe."""
        try:
            return self._s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                return None
            raise
