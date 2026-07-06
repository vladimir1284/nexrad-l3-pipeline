import json

import httpx
import pytest

from ingest.storage.d1 import D1Client, D1Error
from ingest.storage.keys import raster_key


def _client(handler):
    return D1Client("acct", "dbid", "tok", transport=httpx.MockTransport(handler))


def test_execute_manda_sql_y_params_y_devuelve_filas():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [{"success": True, "results": [{"n": 1}]}],
            },
        )

    with _client(handler) as d1:
        rows = d1.execute("SELECT ? AS n", [1])

    assert rows == [{"n": 1}]
    assert seen["url"].endswith("/accounts/acct/d1/database/dbid/query")
    assert seen["auth"] == "Bearer tok"
    assert seen["body"] == {"sql": "SELECT ? AS n", "params": [1]}


def test_error_de_api_lanza_d1error():
    def handler(request):
        return httpx.Response(
            401, json={"success": False, "errors": [{"code": 10000, "message": "auth"}]}
        )

    with _client(handler) as d1, pytest.raises(D1Error, match="10000"):
        d1.execute("SELECT 1")


def test_error_de_red_lanza_d1error():
    def handler(request):
        raise httpx.ConnectError("boom")

    with _client(handler) as d1, pytest.raises(D1Error, match="red"):
        d1.execute("SELECT 1")


def test_raster_key():
    from datetime import datetime

    key = raster_key("AMX", "N0B", datetime(2026, 7, 6, 15, 45, 17))
    assert key == "AMX/N0B/2026/07/06/AMX_N0B_20260706_154517.tif"
