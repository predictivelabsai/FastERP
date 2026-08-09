"""PostgreSQL-first FastERP domain and migration services."""

from .config import DatabaseSettings
from .database import Database

__all__ = ["Database", "DatabaseSettings"]
