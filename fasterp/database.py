"""Pooled PostgreSQL access with schema and transaction boundaries."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DatabaseSettings


class Database:
    """Own a connection pool and provide schema-scoped transactions."""

    def __init__(self, settings: DatabaseSettings, *, open_pool: bool = True) -> None:
        self.settings = settings
        self.pool = ConnectionPool(
            conninfo=settings.url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        if open_pool:
            self.open()

    def open(self) -> None:
        self.pool.open(wait=True)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        with self.pool.connection() as connection:
            connection.execute(
                sql.SQL("SET search_path TO {}, public").format(
                    sql.Identifier(self.settings.schema)
                )
            )
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.connection() as connection:
            with connection.transaction():
                yield connection

    def rows(self, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return list(connection.execute(query, params).fetchall())

    def one(self, query: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self.connection() as connection:
            return connection.execute(query, params).fetchone()

    def scalar(self, query: str, params: Sequence[Any] = ()) -> Any:
        row = self.one(query, params)
        return next(iter(row.values())) if row else None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
