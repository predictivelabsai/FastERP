"""Idempotent dependency-ordered application and unsupported-object archival."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from psycopg import Connection
from psycopg.types.json import Jsonb

from fasterp.database import Database
from fasterp.errors import DocumentStateError, DomainError

from .staging import MigrationRunService


@dataclass(frozen=True)
class ApplyContext:
    run_id: int
    source_id: int
    company_id: int | None


@dataclass(frozen=True)
class ApplyResult:
    entity_type: str
    entity_id: int
    action: str = "Insert"


Handler = Callable[[Connection, ApplyContext, dict], ApplyResult]


class Applier:
    def __init__(
        self,
        database: Database,
        handlers: dict[str, Handler] | None = None,
        object_order: list[str] | None = None,
    ) -> None:
        self.database = database
        self.handlers = handlers or {}
        self.object_order = object_order or list(self.handlers)
        self.runs = MigrationRunService(database)

    def apply(
        self,
        run_id: int,
        *,
        actor: str,
        before_transactions: Callable[[], None] | None = None,
        transaction_objects: set[str] | None = None,
    ) -> tuple[int, int]:
        self.runs.transition(run_id, "Applying", actor=actor)
        applied = 0
        archived = 0
        with self.database.connection() as read_connection:
            run = read_connection.execute(
                """SELECT run.id,run.source_id,source.company_id
                    FROM migration_runs run JOIN migration_sources source ON source.id=run.source_id
                    WHERE run.id=%s""",
                (run_id,),
            ).fetchone()
        context = ApplyContext(run_id, run["source_id"], run["company_id"])
        order = {name: index for index, name in enumerate(self.object_order)}
        records = self.database.rows(
            """SELECT * FROM migration_staging_records
                WHERE run_id=%s AND status IN ('Valid','Warning','Applied','Archived')""",
            (run_id,),
        )
        records.sort(key=lambda row: (order.get(row["source_object"], 10_000), row["id"]))
        opening_applied = False
        for sequence, record in enumerate(records):
            if (
                before_transactions and not opening_applied
                and record["source_object"] in (transaction_objects or set())
            ):
                before_transactions()
                opening_applied = True
            if record["status"] in {"Applied", "Archived"}:
                applied += record["status"] == "Applied"
                archived += record["status"] == "Archived"
                continue
            handler = self.handlers.get(record["source_object"])
            with self.database.transaction() as connection:
                crosswalk = connection.execute(
                    """SELECT * FROM migration_crosswalks
                        WHERE source_id=%s AND source_object=%s AND source_key=%s""",
                    (context.source_id, record["source_object"], record["source_key"]),
                ).fetchone()
                if crosswalk:
                    if crosswalk["payload_hash"] != record["payload_hash"]:
                        raise DomainError(
                            f"Applied source changed: {record['source_object']}/{record['source_key']}"
                        )
                    result = ApplyResult(
                        crosswalk["target_table"], crosswalk["target_id"],
                        "Archive" if crosswalk["target_table"] == "migration_archived_objects" else "Link",
                    )
                elif handler:
                    result = handler(connection, context, record["normalized_payload"])
                    self._record_crosswalk(connection, context, record, result)
                else:
                    connection.execute(
                        """INSERT INTO migration_archived_objects (
                               run_id,source_id,source_object,source_key,source_document_no,
                               payload,payload_hash,archive_reason)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'No operational handler')
                           ON CONFLICT DO NOTHING""",
                        (
                            run_id, context.source_id, record["source_object"],
                            record["source_key"], record["source_document_no"],
                            Jsonb(record["raw_payload"]), record["payload_hash"],
                        ),
                    )
                    connection.execute(
                        """UPDATE migration_staging_records
                              SET status='Archived',archived_reason='No operational handler',updated_at=now()
                            WHERE id=%s""",
                        (record["id"],),
                    )
                    self._manifest(connection, run_id, sequence, record, None, "Archive", "Applied")
                    archived += 1
                    continue
                record_status = "Archived" if result.action == "Archive" else "Applied"
                connection.execute(
                    """UPDATE migration_staging_records
                          SET status='Applied',target_table=%s,target_id=%s,
                              archived_reason=CASE WHEN %s='Archived' THEN 'Non-operational source document' ELSE NULL END,
                              apply_attempts=apply_attempts+1,updated_at=now()
                        WHERE id=%s""",
                    (result.entity_type, result.entity_id, record_status, record["id"]),
                )
                if record_status == "Archived":
                    connection.execute(
                        "UPDATE migration_staging_records SET status='Archived' WHERE id=%s",
                        (record["id"],),
                    )
                self._manifest(
                    connection, run_id, sequence, record, result,
                    result.action, "Applied",
                )
                applied += record_status == "Applied"
                archived += record_status == "Archived"
        if before_transactions and not opening_applied:
            before_transactions()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE migration_runs SET applied_count=%s,updated_at=now() WHERE id=%s",
                (applied, run_id),
            )
            self.runs.transition(run_id, "Reconciling", actor=actor, connection=connection)
        return applied, archived

    @staticmethod
    def _record_crosswalk(connection, context, record, result: ApplyResult) -> None:
        connection.execute(
            """INSERT INTO migration_crosswalks (
                   source_id,source_object,source_key,source_document_no,target_table,
                   target_id,payload_hash,first_run_id,last_run_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                context.source_id, record["source_object"], record["source_key"],
                record["source_document_no"], result.entity_type, result.entity_id,
                record["payload_hash"], context.run_id, context.run_id,
            ),
        )
        if context.company_id is not None:
            for field_name, field_value in record["raw_payload"].items():
                if not (field_name.startswith("U_") or field_name.startswith("custom_")):
                    continue
                connection.execute(
                    """INSERT INTO custom_fields
                           (company_id,entity_type,entity_id,field_name,field_value)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (company_id,entity_type,entity_id,field_name)
                       DO UPDATE SET field_value=excluded.field_value,updated_at=now()""",
                    (
                        context.company_id, result.entity_type, result.entity_id,
                        field_name, Jsonb(field_value),
                    ),
                )
        connection.execute(
            """INSERT INTO external_references (
                   source_id,entity_type,entity_id,source_object,source_key,source_document_no)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (
                context.source_id, result.entity_type, result.entity_id,
                record["source_object"], record["source_key"],
                record["source_document_no"],
            ),
        )

    @staticmethod
    def _manifest(connection, run_id, sequence, record, result, action, status) -> None:
        connection.execute(
            """INSERT INTO migration_apply_manifests (
                   run_id,sequence_number,source_object,source_key,staging_record_id,
                   target_entity_type,target_entity_id,action,status,payload_hash,applied_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
               ON CONFLICT (run_id,source_object,source_key) DO UPDATE SET
                   status=excluded.status,applied_at=excluded.applied_at""",
            (
                run_id, sequence, record["source_object"], record["source_key"],
                record["id"], result.entity_type if result else None,
                result.entity_id if result else None, action, status,
                record["payload_hash"],
            ),
        )
