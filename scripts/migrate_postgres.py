"""Apply FastERP PostgreSQL migrations safely and deterministically."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

try:
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover - presents a better CLI error
    psycopg = None
    sql = None


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "postgres"
SCHEMA_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")
QUALIFIED_SCHEMA_PATTERN = re.compile(r"\bfast_erp(?=\.)")
CREATE_SCHEMA_PATTERN = re.compile(
    r"(?m)^(CREATE SCHEMA IF NOT EXISTS )fast_erp(;)"
)


@dataclass(frozen=True)
class Migration:
    """One ordered SQL migration and its content checksum."""

    version: str
    path: Path
    checksum: str
    source: str


def migrations(directory: Path = MIGRATIONS) -> list[Migration]:
    """Return validated migrations in filename order."""

    found: list[Migration] = []
    for path in sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        source = path.read_text(encoding="utf-8")
        found.append(
            Migration(
                version=path.stem,
                path=path,
                checksum=hashlib.sha256(source.encode()).hexdigest(),
                source=source,
            )
        )
    versions = [migration.version for migration in found]
    if not found:
        raise RuntimeError(f"No migrations found in {directory}")
    if len(versions) != len(set(versions)):
        raise RuntimeError("Migration versions must be unique")
    expected = list(range(1, len(found) + 1))
    actual = [int(migration.version[:4]) for migration in found]
    if actual != expected:
        raise RuntimeError(
            f"Migration sequence must be contiguous: expected {expected}, got {actual}"
        )
    return found


def validate_schema(schema: str) -> str:
    """Reject unsafe or unsupported PostgreSQL identifiers."""

    if not SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError(
            "DB_SCHEMA must start with a lowercase letter/underscore and contain "
            "only lowercase letters, digits, and underscores"
        )
    return schema


def render(source: str, schema: str) -> str:
    """Render only schema identifiers, preserving data and migration versions."""

    schema = validate_schema(schema)
    rendered = QUALIFIED_SCHEMA_PATTERN.sub(schema, source)
    return CREATE_SCHEMA_PATTERN.sub(rf"\g<1>{schema}\g<2>", rendered)


def _relation_exists(connection, schema: str, relation: str) -> bool:
    return bool(
        connection.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (f"{schema}.{relation}",)
        ).fetchone()[0]
    )


def _has_checksum_column(connection, schema: str) -> bool:
    return bool(
        connection.execute(
            """SELECT EXISTS (
                   SELECT 1 FROM information_schema.columns
                    WHERE table_schema=%s AND table_name='schema_migrations'
                      AND column_name='checksum'
               )""",
            (schema,),
        ).fetchone()[0]
    )


def _applied(connection, schema: str) -> dict[str, str | None]:
    if not _relation_exists(connection, schema, "schema_migrations"):
        return {}
    if _has_checksum_column(connection, schema):
        query = sql.SQL("SELECT version, checksum FROM {}.schema_migrations").format(
            sql.Identifier(schema)
        )
        return dict(connection.execute(query).fetchall())
    query = sql.SQL("SELECT version FROM {}.schema_migrations").format(
        sql.Identifier(schema)
    )
    return {row[0]: None for row in connection.execute(query).fetchall()}


def apply_migrations(
    database_url: str,
    schema: str = "fast_erp",
    *,
    target: str | None = None,
    check_only: bool = False,
) -> list[str]:
    """Apply pending migrations and return the versions changed."""

    if psycopg is None:
        raise RuntimeError("Install requirements.txt before running migrations")
    if not database_url:
        raise ValueError("DB_URL is required")
    schema = validate_schema(schema)
    available = migrations()
    if target and target not in {migration.version for migration in available}:
        raise ValueError(f"Unknown migration target: {target}")

    changed: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as connection:
        lock_name = f"FastERP migrations:{schema}"
        connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (lock_name,))
        try:
            applied = _applied(connection, schema)
            for migration in available:
                if migration.version in applied:
                    existing_checksum = applied[migration.version]
                    if existing_checksum and existing_checksum != migration.checksum:
                        raise RuntimeError(
                            f"Applied migration changed on disk: {migration.version}"
                        )
                elif check_only:
                    changed.append(migration.version)
                else:
                    with connection.transaction():
                        connection.execute(render(migration.source, schema))
                    changed.append(migration.version)
                    print(f"applied {migration.version}")
                if target == migration.version:
                    break

            if not check_only and _has_checksum_column(connection, schema):
                applied_now = _applied(connection, schema)
                by_version = {migration.version: migration for migration in available}
                with connection.transaction():
                    for version, checksum in applied_now.items():
                        migration = by_version.get(version)
                        if not migration:
                            continue
                        if checksum and checksum != migration.checksum:
                            raise RuntimeError(
                                f"Applied migration changed on disk: {version}"
                            )
                        query = sql.SQL(
                            "UPDATE {}.schema_migrations SET checksum=%s "
                            "WHERE version=%s AND checksum IS NULL"
                        ).format(sql.Identifier(schema))
                        connection.execute(
                            query, (migration.checksum, migration.version)
                        )
        finally:
            connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_name,))
    return changed


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--database-url", help="Override DB_URL")
    result.add_argument("--schema", help="Override DB_SCHEMA")
    result.add_argument("--target", help="Stop after this migration version")
    result.add_argument(
        "--check", action="store_true", help="List pending migrations without applying"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    args = parser().parse_args(argv)
    database_url = args.database_url or os.getenv("DB_URL", "")
    schema = args.schema or os.getenv("DB_SCHEMA", "fast_erp")
    try:
        pending = apply_migrations(
            database_url,
            schema,
            target=args.target,
            check_only=args.check,
        )
    except (ValueError, RuntimeError, psycopg.Error if psycopg else RuntimeError) as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1
    if args.check:
        print("\n".join(pending) if pending else "database is current")
        return 2 if pending else 0
    current = args.target or migrations()[-1].version
    print(f"database current at {current}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
