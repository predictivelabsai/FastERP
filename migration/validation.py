"""Source-neutral normalization and validation of staged records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from psycopg.types.json import Jsonb

from fasterp.database import Database
from fasterp.errors import DocumentStateError

from .staging import MigrationRunService


@dataclass(frozen=True)
class Issue:
    severity: str
    rule_code: str
    message: str
    field_name: str | None = None
    details: dict[str, Any] | None = None


Normalizer = Callable[[dict[str, Any]], tuple[dict[str, Any], list[Issue]]]


class Validator:
    def __init__(self, database: Database, normalizers: dict[str, Normalizer] | None = None) -> None:
        self.database = database
        self.normalizers = normalizers or {}
        self.runs = MigrationRunService(database)

    def validate(self, run_id: int, *, actor: str) -> tuple[int, int]:
        self.runs.transition(run_id, "Validating", actor=actor)
        warnings = 0
        errors = 0
        with self.database.transaction() as connection:
            source_id = connection.execute(
                "SELECT source_id FROM migration_runs WHERE id=%s", (run_id,)
            ).fetchone()["source_id"]
            records = connection.execute(
                """SELECT id,source_object,raw_payload,dependencies FROM migration_staging_records
                    WHERE run_id=%s AND status<>'Skipped' ORDER BY id FOR UPDATE""",
                (run_id,),
            ).fetchall()
            for record in records:
                normalizer = self.normalizers.get(record["source_object"], default_normalizer)
                try:
                    normalized, issues = normalizer(record["raw_payload"])
                except Exception as exc:
                    normalized = {}
                    issues = [
                        Issue("Error", "normalization_failed", "Record normalization failed", details={"error_type": type(exc).__name__})
                    ]
                for dependency in record["dependencies"] or []:
                    available = connection.execute(
                        """SELECT 1 FROM migration_staging_records
                            WHERE run_id=%s AND source_object=%s AND source_key=%s
                              AND status<>'Skipped'
                           UNION ALL
                           SELECT 1 FROM migration_crosswalks
                            WHERE source_id=%s AND source_object=%s AND source_key=%s
                           LIMIT 1""",
                        (
                            run_id, dependency.get("object"), dependency.get("key"),
                            source_id, dependency.get("object"), dependency.get("key"),
                        ),
                    ).fetchone()
                    if not available:
                        issues.append(Issue(
                            "Error", "missing_dependency",
                            f"Missing source dependency {dependency.get('object')}/{dependency.get('key')}",
                            details=dependency,
                        ))
                status = "Valid"
                if any(issue.severity == "Error" for issue in issues):
                    status = "Invalid"
                elif any(issue.severity == "Warning" for issue in issues):
                    status = "Warning"
                connection.execute(
                    """UPDATE migration_staging_records
                          SET normalized_payload=%s,status=%s,updated_at=now()
                        WHERE id=%s""",
                    (Jsonb(normalized), status, record["id"]),
                )
                for issue in issues:
                    connection.execute(
                        """INSERT INTO migration_validation_issues (
                               run_id,staging_record_id,severity,rule_code,field_name,
                               message,details)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            run_id, record["id"], issue.severity, issue.rule_code,
                            issue.field_name, issue.message, Jsonb(issue.details or {}),
                        ),
                    )
                    warnings += issue.severity == "Warning"
                    errors += issue.severity == "Error"
            connection.execute(
                """UPDATE migration_runs
                      SET warning_count=%s,error_count=%s,updated_at=now()
                    WHERE id=%s""",
                (warnings, errors, run_id),
            )
            self.runs.transition(run_id, "Validated", actor=actor, connection=connection)
        return warnings, errors


def default_normalizer(payload: dict[str, Any]) -> tuple[dict[str, Any], list[Issue]]:
    if not isinstance(payload, dict):
        return {}, [Issue("Error", "payload_not_object", "Payload must be a JSON object")]
    return dict(payload), []


def require_fields(*fields: str) -> Normalizer:
    def normalize(payload: dict[str, Any]) -> tuple[dict[str, Any], list[Issue]]:
        normalized, issues = default_normalizer(payload)
        for field in fields:
            if normalized.get(field) in (None, ""):
                issues.append(
                    Issue("Error", "required_field", f"{field} is required", field_name=field)
                )
        return normalized, issues
    return normalize
