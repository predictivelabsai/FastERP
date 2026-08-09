"""Static and unit checks for the PostgreSQL migration runner."""

from pathlib import Path

import pytest

from scripts.migrate_postgres import migrations, render, validate_schema


def test_migration_series_is_contiguous_and_records_its_version():
    found = migrations()
    assert [migration.version[:4] for migration in found] == [
        f"{number:04d}" for number in range(1, 14)
    ]
    for migration in found:
        assert migration.version in migration.source
        assert len(migration.checksum) == 64


def test_schema_render_does_not_change_migration_version_or_payload():
    source = """CREATE SCHEMA IF NOT EXISTS fast_erp;
CREATE TABLE fast_erp.example (value TEXT DEFAULT 'fast_erp');
INSERT INTO fast_erp.example VALUES ('0001_fast_erp_baseline');
"""
    rendered = render(source, "fast_erp_trial_123")
    assert "CREATE SCHEMA IF NOT EXISTS fast_erp_trial_123;" in rendered
    assert "fast_erp_trial_123.example" in rendered
    assert "DEFAULT 'fast_erp'" in rendered
    assert "'0001_fast_erp_baseline'" in rendered


@pytest.mark.parametrize(
    "schema", ["FastERP", "fast-erp", "fast erp", "public;drop schema public", "1fast"]
)
def test_schema_validation_rejects_unsafe_names(schema):
    with pytest.raises(ValueError):
        validate_schema(schema)


def test_all_migration_files_are_under_postgres_directory():
    directory = Path(__file__).parents[1] / "migrations" / "postgres"
    assert all(migration.path.parent == directory for migration in migrations(directory))
