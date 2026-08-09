"""Validated FastERP runtime configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


SCHEMA_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class DatabaseSettings:
    """PostgreSQL connection settings sourced without exposing credentials."""

    url: str
    schema: str = "fast_erp"
    pool_min_size: int = 1
    pool_max_size: int = 10

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("DB_URL is required for the PostgreSQL runtime")
        if not SCHEMA_PATTERN.fullmatch(self.schema):
            raise ValueError("DB_SCHEMA contains an unsafe PostgreSQL identifier")
        if self.pool_min_size < 0 or self.pool_max_size < 1:
            raise ValueError("Database pool sizes must be positive")
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("DB_POOL_MIN_SIZE cannot exceed DB_POOL_MAX_SIZE")

    @classmethod
    def from_env(cls) -> "DatabaseSettings":
        return cls(
            url=os.getenv("DB_URL", ""),
            schema=os.getenv("DB_SCHEMA", "fast_erp"),
            pool_min_size=int(os.getenv("DB_POOL_MIN_SIZE", "1")),
            pool_max_size=int(os.getenv("DB_POOL_MAX_SIZE", "10")),
        )
