"""Process-wide PostgreSQL pool ownership."""

from fasterp.config import DatabaseSettings
from fasterp.database import Database, close_database_pools


def test_database_reuses_one_pool_per_settings_without_connecting():
    settings = DatabaseSettings(
        "postgresql://unused/fast_erp",
        pool_min_size=0,
        pool_max_size=3,
    )
    first = Database(settings, open_pool=False)
    second = Database(settings, open_pool=False)
    other = Database(
        DatabaseSettings(
            "postgresql://unused/other",
            pool_min_size=0,
            pool_max_size=3,
        ),
        open_pool=False,
    )

    assert first is second
    assert first.pool is second.pool
    assert other is not first
    close_database_pools()
