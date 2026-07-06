"""Cliente D1 vía HTTP API de Cloudflare.

Sin transacciones entre requests (limitación de la API HTTP): cada
`execute` es atómico por sí solo. `execute_many` reusa la conexión
(keep-alive) y corta en el primer error — el publisher ordena los
statements para que un corte deje estado consistente (dimensiones
primero, hechos al final).
"""

from typing import Any

import httpx

API_BASE = "https://api.cloudflare.com/client/v4"


class D1Error(Exception):
    pass


class D1Client:
    def __init__(
        self,
        account_id: str,
        database_id: str,
        api_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = f"{API_BASE}/accounts/{account_id}/d1/database/{database_id}/query"
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30.0,
            transport=transport,
        )

    def execute(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict]:
        """Ejecuta un statement; devuelve las filas del resultado."""
        try:
            resp = self._http.post(self._url, json={"sql": sql, "params": list(params)})
        except httpx.HTTPError as exc:
            raise D1Error(f"error de red contra D1: {exc}") from exc
        body = resp.json()
        if not body.get("success"):
            raise D1Error(f"D1 respondió error (HTTP {resp.status_code}): {body.get('errors')}")
        results = body.get("result") or []
        return results[0].get("results", []) if results else []

    def execute_many(self, statements: list[tuple[str, list[Any]]]) -> None:
        for sql, params in statements:
            self.execute(sql, params)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "D1Client":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
