import os
from datetime import UTC, datetime

import pytest

from ingest.replay import inject, latest_keys

NOW = datetime(2026, 7, 6, 15, 50, 0, tzinfo=UTC)


class FakeS3:
    """list_objects_v2 + download_fileobj sobre un dict clave→bytes."""

    def __init__(self, objects: dict[str, bytes]):
        self._objects = objects

    def list_objects_v2(self, Bucket, Prefix, MaxKeys, ContinuationToken=None):
        keys = sorted(k for k in self._objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def download_fileobj(self, bucket, key, fh):
        fh.write(self._objects[key])


def _key(day: str, hhmmss: str) -> str:
    return f"AMX_N0B_{day}_{hhmmss}"


def test_latest_keys_solo_hoy():
    s3 = FakeS3({_key("2026_07_06", t): b"" for t in ("10_00_00", "11_00_00", "12_00_00")})
    keys = latest_keys("AMX", "N0B", 2, s3=s3, now=NOW)
    assert keys == [_key("2026_07_06", "11_00_00"), _key("2026_07_06", "12_00_00")]


def test_latest_keys_completa_con_dia_anterior():
    s3 = FakeS3(
        {
            _key("2026_07_05", "23_00_00"): b"",
            _key("2026_07_05", "23_30_00"): b"",
            _key("2026_07_06", "00_10_00"): b"",
        }
    )
    keys = latest_keys("AMX", "N0B", 3, s3=s3, now=NOW)
    assert keys == [
        _key("2026_07_05", "23_00_00"),
        _key("2026_07_05", "23_30_00"),
        _key("2026_07_06", "00_10_00"),
    ]


def test_inject_escritura_atomica(tmp_path):
    payload = b"producto-crudo"
    s3 = FakeS3({_key("2026_07_06", "15_45_17"): payload})
    dest = tmp_path / "incoming"

    injected = inject(dest, ["AMX"], ["N0B"], 1, s3=s3)

    assert injected == [_key("2026_07_06", "15_45_17")]
    assert (dest / injected[0]).read_bytes() == payload
    # sin temporales huérfanos
    assert [p.name for p in dest.iterdir()] == injected


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("REPLAY_NETWORK_TEST"), reason="test de red opcional (REPLAY_NETWORK_TEST=1)"
)
def test_bucket_real_lista_y_baja(tmp_path):
    keys = latest_keys("AMX", "N0B", 1)
    if not keys:
        pytest.skip("bucket sin productos AMX N0B hoy")
    injected = inject(tmp_path, ["AMX"], ["N0B"], 1)
    assert len(injected) == 1
    raw = tmp_path / injected[0]
    assert raw.stat().st_size > 10_000

    from ingest.decoder.level3 import decode_file

    assert decode_file(raw).site_id == "AMX"
