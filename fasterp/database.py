"""Pooled PostgreSQL access with schema and transaction boundaries."""

from __future__ import annotations

import atexit
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, ClassVar, Self

from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DatabaseSettings


class Database:
    """Own one bounded connection pool per settings tuple in this process."""

    _instances: ClassVar[dict[tuple[object, ...], Database]] = {}
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __new__(cls, settings: DatabaseSettings, *, open_pool: bool = True):
        key = (
            settings.url,
            settings.schema,
            settings.pool_min_size,
            settings.pool_max_size,
            settings.pool_timeout,
            settings.pool_recycle,
            settings.pool_max_idle,
            settings.application_name,
        )
        with cls._lock:
            instance = cls._instances.get(key)
            if instance is None:
                instance = super().__new__(cls)
                instance._cache_key = key
                instance._setup(settings, open_pool=open_pool)
                cls._instances[key] = instance
            elif open_pool and not instance._opened:
                instance._open_unlocked()
        return instance

    def __init__(self, settings: DatabaseSettings, *, open_pool: bool = True) -> None:
        # Initialization is performed exactly once under the class lock in __new__.
        pass

    def _setup(self, settings: DatabaseSettings, *, open_pool: bool) -> None:
        self.settings = settings
        self.pool = ConnectionPool(
            conninfo=settings.url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            timeout=settings.pool_timeout,
            max_lifetime=settings.pool_recycle,
            max_idle=settings.pool_max_idle,
            kwargs={
                "row_factory": dict_row,
                "application_name": settings.application_name,
            },
            check=ConnectionPool.check_connection,
            open=False,
        )
        self._opened = False
        if open_pool:
            self._open_unlocked()

    def _open_unlocked(self) -> None:
        self.pool.open(wait=True)
        self._opened = True

    def open(self) -> None:
        with self._lock:
            if not self._opened:
                self._open_unlocked()

    def close(self) -> None:
        with self._lock:
            if self._opened:
                self.pool.close()
            self._opened = False
            if self._instances.get(self._cache_key) is self:
                self._instances.pop(self._cache_key, None)

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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def close_database_pools() -> None:
    """Dispose all cached pools during process shutdown."""
    with Database._lock:
        instances = list(Database._instances.values())
        Database._instances.clear()
    for database in instances:
        if database._opened:
            database.pool.close()
        database._opened = False


atexit.register(close_database_pools)
