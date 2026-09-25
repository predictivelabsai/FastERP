"""Guarded PostgreSQL analytics over tenant-scoped WMS reporting views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg import sql as pg_sql
from sqlglot import exp, parse
from sqlglot.errors import ParseError

from .database import Database


REPORT_VIEWS = (
    "wms_stock_report", "wms_movement_report", "wms_lot_report",
    "wms_temperature_report",
)
ALLOWED_FUNCTIONS = {
    "Abs", "Avg", "Cast", "Ceil", "Coalesce", "Concat", "Count",
    "CurrentDate", "CurrentTimestamp", "DateAdd", "DateDiff", "DateTrunc",
    "Extract", "Floor", "Greatest", "Least",
    "Length", "Lower", "Max", "Min", "Nullif", "Round", "Sum",
    "TimestampTrunc", "TimeToStr", "TryCast", "Upper",
}


class WarehouseQueryError(ValueError):
    """A query was rejected or could not run safely."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    sql: str


def validate_query(statement: str) -> str:
    """Accept one SELECT over the four governed reporting views."""
    source = (statement or "").strip()
    if not source or len(source) > 12000:
        raise WarehouseQueryError("Enter a SELECT query under 12,000 characters")
    if "--" in source or "/*" in source or "*/" in source:
        raise WarehouseQueryError("SQL comments are not allowed")
    try:
        expressions = [part for part in parse(source, read="postgres") if part]
    except (ParseError, ValueError) as exc:
        raise WarehouseQueryError("SQL could not be parsed") from exc
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Select):
        raise WarehouseQueryError("Only one SELECT statement is allowed")
    tree = expressions[0]
    if tree.args.get("with_") or tree.find(exp.Lock) or tree.find(exp.Into):
        raise WarehouseQueryError("CTEs, locking and SELECT INTO are not allowed")
    tables = list(tree.find_all(exp.Table))
    if not tables:
        raise WarehouseQueryError("Choose at least one warehouse report view")
    for table in tables:
        if (table.name.lower() not in REPORT_VIEWS or table.catalog or table.db):
            raise WarehouseQueryError("Query only warehouse reporting views")
    for function in tree.find_all(exp.Func):
        if type(function).__name__ not in ALLOWED_FUNCTIONS:
            raise WarehouseQueryError("Query contains an unsupported function")
    for node in tree.walk():
        if isinstance(node, (exp.Command, exp.Insert, exp.Update, exp.Delete,
                             exp.Create, exp.Drop, exp.Alter, exp.Merge)):
            raise WarehouseQueryError("Only read-only queries are allowed")
    return tree.sql(dialect="postgres")


class WarehouseQueryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def schema_prompt(self) -> str:
        with self.database.pool.connection() as connection:
            columns = connection.execute(
                """SELECT table_name,column_name,data_type
                     FROM information_schema.columns
                    WHERE table_schema=%s AND table_name=ANY(%s)
                    ORDER BY table_name,ordinal_position""",
                (self.database.settings.schema, list(REPORT_VIEWS)),
            ).fetchall()
        grouped = {name: [] for name in REPORT_VIEWS}
        for row in columns:
            grouped[row["table_name"]].append(
                f"{row['column_name']} {row['data_type']}"
            )
        return "\n".join(
            f"{name}({', '.join(grouped[name])})" for name in REPORT_VIEWS
        )

    def run(self, statement: str, *, company_id: int,
            limit: int = 200) -> QueryResult:
        safe_sql = validate_query(statement)
        limit = min(max(int(limit), 1), 500)
        try:
            with self.database.pool.connection() as connection:
                with connection.transaction():
                    connection.execute("SET TRANSACTION READ ONLY")
                    connection.execute("SET LOCAL statement_timeout='5000ms'")
                    connection.execute(
                        pg_sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                            pg_sql.Identifier(self.database.settings.schema)
                        )
                    )
                    connection.execute(
                        "SELECT set_config('fasterp.company_id',%s,true)",
                        (str(company_id),),
                    )
                    cursor = connection.execute(
                        f"SELECT * FROM ({safe_sql}) AS warehouse_result LIMIT %s",
                        (limit,),
                    )
                    names = [item.name for item in cursor.description]
                    records = cursor.fetchall()
                    return QueryResult(names,
                                       [[row[name] for name in names] for row in records],
                                       safe_sql)
        except WarehouseQueryError:
            raise
        except Exception as exc:
            raise WarehouseQueryError(
                f"Warehouse query failed ({type(exc).__name__})"
            ) from exc
