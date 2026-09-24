"""Cliente Postgres directo (psycopg 3), reemplaza D1Client.

Mismo shape público que D1Client (`execute`/`execute_many`), para que
publish.py/wind.py/lightning.py no cambien su forma de llamarlo — solo
las cadenas SQL (placeholders `%s`, ver ingest/storage/publish.py).

A diferencia de D1Client, `execute_many` es ahora una transacción real
(`conn.transaction()`): D1Client no podía ofrecer esto porque cada
`execute` es un request HTTP independiente (ver docstring de d1.py) y el
publisher compensaba ordenando los statements (dimensiones antes que
hechos) para que un corte a mitad dejara estado consistente. Con
Postgres directo esa compensación deja de ser necesaria — se conserva
el mismo orden solo porque no hace daño, no porque siga haciendo falta.
"""

import contextlib
from typing import Any

import psycopg
from psycopg.rows import dict_row


class PgError(Exception):
    pass


class PgClient:
    """Reconecta una vez ante conexión caída (server restart/idle timeout/blip de red):
    servicios de larga vida (`l3proc wind`/`watch`) guardan una instancia por fuera de su
    loop, así que sin esto un solo corte mata las corridas hasta reiniciar el contenedor."""

    def __init__(self, dsn: str, *, conn: psycopg.Connection | None = None) -> None:
        self._dsn = dsn
        self._conn = conn or self._connect()

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=True)
        except psycopg.Error as exc:
            raise PgError(f"no se pudo conectar a Postgres: {exc}") from exc

    def _reconnect(self) -> None:
        with contextlib.suppress(psycopg.Error):
            self._conn.close()
        self._conn = self._connect()

    def execute(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict]:
        try:
            cur = self._conn.execute(sql, params)
        except psycopg.OperationalError:
            self._reconnect()
            try:
                cur = self._conn.execute(sql, params)
            except psycopg.Error as exc:
                raise PgError(f"error de Postgres tras reconectar: {exc}") from exc
        except psycopg.Error as exc:
            raise PgError(f"error de Postgres: {exc}") from exc
        return cur.fetchall() if cur.description else []

    def execute_many(self, statements: list[tuple[str, list[Any]]]) -> None:
        try:
            self._execute_many_tx(statements)
        except psycopg.OperationalError:
            self._reconnect()
            try:
                self._execute_many_tx(statements)
            except psycopg.Error as exc:
                raise PgError(f"error de Postgres tras reconectar: {exc}") from exc
        except psycopg.Error as exc:
            raise PgError(f"error de Postgres: {exc}") from exc

    def _execute_many_tx(self, statements: list[tuple[str, list[Any]]]) -> None:
        with self._conn.transaction():
            for sql, params in statements:
                self._conn.execute(sql, params)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PgClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
