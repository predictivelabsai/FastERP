"""Immutable extraction, capability discovery, and run state management."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from fasterp.database import Database
from fasterp.errors import DocumentStateError, DomainError

from .connectors.base import SourceConnector, canonical_hash


RUN_TRANSITIONS = {
    "Created": {"Discovering", "Extracting", "Failed", "Cancelled"},
    "Discovering": {"Discovered", "Failed"},
    "Discovered": {"Extracting", "Failed", "Cancelled"},
    "Extracting": {"Staged", "Failed"},
    "Staged": {"Extracting", "Validating", "Failed", "Cancelled"},
    "Validating": {"Validated", "Failed"},
    "Validated": {"Approved", "Validating", "Failed", "Cancelled"},
    "Approved": {"Applying", "Failed", "Cancelled"},
    "Applying": {"Reconciling", "Failed"},
    "Reconciling": {"Completed", "Failed"},
    "Completed": {"Rolled Back"},
    "Failed": {"Extracting", "Validating", "Applying", "Cancelled"},
    "Cancelled": set(),
    "Rolled Back": set(),
}


class MigrationRunService:
    """Persist connector output with resumable and auditable checkpoints."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_source(
        self,
        *,
        name: str,
        connector_type: str,
        company_id: int | None = None,
        base_url: str | None = None,
        source_company_db: str | None = None,
        credential_env_prefix: str | None = None,
        configuration: dict[str, Any] | None = None,
    ) -> int:
        with self.database.transaction() as connection:
            return connection.execute(
                """INSERT INTO migration_sources
                       (company_id,name,connector_type,base_url,source_company_db,
                        credential_env_prefix,configuration)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (name) DO UPDATE SET
                       company_id=excluded.company_id,
                       connector_type=excluded.connector_type,
                       base_url=excluded.base_url,
                       source_company_db=excluded.source_company_db,
                       credential_env_prefix=excluded.credential_env_prefix,
                       configuration=excluded.configuration,
                       updated_at=now()
                   RETURNING id""",
                (
                    company_id, name, connector_type, base_url, source_company_db,
                    credential_env_prefix, Jsonb(configuration or {}),
                ),
            ).fetchone()["id"]

    def create_run(
        self,
        *,
        source_id: int,
        history_from: date,
        history_to: date,
        mode: str = "dry_run",
        requested_by: str,
        scope: dict[str, Any] | None = None,
        mapping_set_id: int | None = None,
    ) -> int:
        with self.database.transaction() as connection:
            return connection.execute(
                """INSERT INTO migration_runs
                       (source_id,mapping_set_id,mode,status,history_from,history_to,
                        scope,requested_by)
                   VALUES (%s,%s,%s,'Created',%s,%s,%s,%s) RETURNING id""",
                (
                    source_id, mapping_set_id, mode, history_from, history_to,
                    Jsonb(scope or {}), requested_by,
                ),
            ).fetchone()["id"]

    def transition(
        self,
        run_id: int,
        next_status: str,
        *,
        actor: str | None = None,
        message: str | None = None,
        connection=None,
    ) -> None:
        if connection is None:
            with self.database.transaction() as transaction:
                self.transition(
                    run_id, next_status, actor=actor, message=message,
                    connection=transaction,
                )
                return
        run = connection.execute(
            "SELECT status FROM migration_runs WHERE id=%s FOR UPDATE", (run_id,)
        ).fetchone()
        if not run:
            raise DomainError(f"Migration run {run_id} does not exist")
        if next_status == run["status"]:
            return
        if next_status not in RUN_TRANSITIONS.get(run["status"], set()):
            raise DocumentStateError(
                f"Migration run cannot transition {run['status']} → {next_status}"
            )
        connection.execute(
            "UPDATE migration_runs SET status=%s,updated_at=now() WHERE id=%s",
            (next_status, run_id),
        )
        connection.execute(
            """INSERT INTO migration_audit_events
                   (run_id,actor,event_type,message,details)
               VALUES (%s,%s,%s,%s,%s)""",
            (
                run_id, actor, f"status:{next_status}",
                message or f"Migration run moved to {next_status}", Jsonb({}),
            ),
        )

    def discover(
        self, run_id: int, connector: SourceConnector, *, actor: str
    ) -> list:
        self.transition(run_id, "Discovering", actor=actor)
        try:
            capabilities = connector.discover()
            with self.database.transaction() as connection:
                source_id = connection.execute(
                    "SELECT source_id FROM migration_runs WHERE id=%s", (run_id,)
                ).fetchone()["source_id"]
                for capability in capabilities:
                    connection.execute(
                        """INSERT INTO migration_source_capabilities (
                               source_id,source_object,available,supports_filter,
                               supports_expand,key_fields,fields,estimated_count,
                               metadata_hash,discovered_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                           ON CONFLICT (source_id,source_object) DO UPDATE SET
                               available=excluded.available,
                               supports_filter=excluded.supports_filter,
                               supports_expand=excluded.supports_expand,
                               key_fields=excluded.key_fields,
                               fields=excluded.fields,
                               estimated_count=excluded.estimated_count,
                               metadata_hash=excluded.metadata_hash,
                               discovered_at=now()""",
                        (
                            source_id, capability.source_object, capability.available,
                            capability.supports_filter, capability.supports_expand,
                            Jsonb(list(capability.key_fields)), Jsonb(list(capability.fields)),
                            capability.estimated_count, capability.metadata_hash,
                        ),
                    )
                connection.execute(
                    "UPDATE migration_runs SET source_snapshot_at=now(),updated_at=now() WHERE id=%s",
                    (run_id,),
                )
                self.transition(run_id, "Discovered", actor=actor, connection=connection)
            return capabilities
        except Exception:
            self.transition(run_id, "Failed", actor=actor, message="Capability discovery failed")
            raise

    def extract(
        self,
        run_id: int,
        connector: SourceConnector,
        source_objects: list[str],
        *,
        actor: str,
        page_size: int = 100,
        filters: dict[str, str] | None = None,
        expands: dict[str, tuple[str, ...]] | None = None,
        restart_completed: bool = False,
    ) -> int:
        self.transition(run_id, "Extracting", actor=actor)
        filters = filters or {}
        expands = expands or {}
        try:
            for source_object in source_objects:
                self._extract_object(
                    run_id, connector, source_object, page_size=page_size,
                    filter_expression=filters.get(source_object),
                    expand=expands.get(source_object, ()),
                    restart_completed=restart_completed,
                )
            with self.database.transaction() as connection:
                count = connection.execute(
                    "SELECT count(*) AS value FROM migration_staging_records WHERE run_id=%s",
                    (run_id,),
                ).fetchone()["value"]
                connection.execute(
                    """UPDATE migration_runs
                          SET source_count=%s,staged_count=%s,updated_at=now()
                        WHERE id=%s""",
                    (count, count, run_id),
                )
                self.transition(run_id, "Staged", actor=actor, connection=connection)
            return count
        except Exception as exc:
            with self.database.transaction() as connection:
                connection.execute(
                    """UPDATE migration_runs
                          SET status='Failed',error_count=error_count+1,updated_at=now()
                        WHERE id=%s""",
                    (run_id,),
                )
                connection.execute(
                    """INSERT INTO migration_audit_events
                           (run_id,actor,event_type,message,details)
                       VALUES (%s,%s,'extraction_failed','Extraction failed',%s)""",
                    (run_id, actor, Jsonb({"error_type": type(exc).__name__})),
                )
            raise

    def _extract_object(
        self,
        run_id: int,
        connector: SourceConnector,
        source_object: str,
        *,
        page_size: int,
        filter_expression: str | None,
        expand: tuple[str, ...] = (),
        restart_completed: bool,
    ) -> None:
        checkpoint = self.database.one(
            "SELECT * FROM migration_checkpoints WHERE run_id=%s AND source_object=%s",
            (run_id, source_object),
        )
        if checkpoint and checkpoint["completed"] and not restart_completed:
            return
        cursor = None if restart_completed else (checkpoint or {}).get("cursor_value")
        while True:
            page = connector.extract(
                source_object, cursor=cursor, page_size=page_size,
                filter_expression=filter_expression,
                expand=expand,
            )
            page_hash = canonical_hash([record.payload_hash for record in page.records])
            with self.database.transaction() as connection:
                sequence = connection.execute(
                    """SELECT COALESCE(max(sequence_number),-1)+1 AS value
                          FROM migration_batches WHERE run_id=%s AND source_object=%s""",
                    (run_id, source_object),
                ).fetchone()["value"]
                batch_id = connection.execute(
                    """INSERT INTO migration_batches (
                           run_id,source_object,sequence_number,status,request_cursor,
                           response_cursor,source_count,payload_hash,started_at,completed_at)
                       VALUES (%s,%s,%s,'Extracted',%s,%s,%s,%s,now(),now())
                       RETURNING id""",
                    (
                        run_id, source_object, sequence, cursor, page.next_cursor,
                        len(page.records), page_hash,
                    ),
                ).fetchone()["id"]
                staged = 0
                for record in page.records:
                    existing = connection.execute(
                        """SELECT id,payload_hash FROM migration_staging_records
                            WHERE run_id=%s AND source_object=%s AND source_key=%s""",
                        (run_id, source_object, record.source_key),
                    ).fetchone()
                    if existing:
                        if existing["payload_hash"] != record.payload_hash:
                            raise DomainError(
                                f"Source payload changed within run for {source_object}/{record.source_key}"
                            )
                        continue
                    connection.execute(
                        """INSERT INTO migration_staging_records (
                               run_id,batch_id,source_object,source_key,source_document_no,
                               source_updated_at,raw_payload,payload_hash,status,dependencies)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'Extracted',%s)""",
                        (
                            run_id, batch_id, source_object, record.source_key,
                            record.document_number, record.updated_at, Jsonb(record.payload),
                            record.payload_hash,
                            Jsonb([{"object": obj, "key": key} for obj, key in record.dependencies]),
                        ),
                    )
                    staged += 1
                connection.execute(
                    "UPDATE migration_batches SET staged_count=%s WHERE id=%s",
                    (staged, batch_id),
                )
                connection.execute(
                    """INSERT INTO migration_checkpoints
                           (run_id,source_object,cursor_value,last_source_key,completed,updated_at)
                       VALUES (%s,%s,%s,%s,%s,now())
                       ON CONFLICT (run_id,source_object) DO UPDATE SET
                           cursor_value=excluded.cursor_value,
                           last_source_key=excluded.last_source_key,
                           completed=excluded.completed,
                           updated_at=now()""",
                    (
                        run_id, source_object, page.next_cursor,
                        page.records[-1].source_key if page.records else None,
                        page.next_cursor is None,
                    ),
                )
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

    def approve(self, run_id: int, *, approver: str) -> None:
        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT status,error_count FROM migration_runs WHERE id=%s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if not run or run["status"] != "Validated":
                raise DocumentStateError("Only a validated run can be approved")
            unresolved = connection.execute(
                """SELECT count(*) AS value FROM migration_validation_issues
                    WHERE run_id=%s AND severity='Error' AND resolved=false""",
                (run_id,),
            ).fetchone()["value"]
            if unresolved:
                raise DomainError(f"Migration run has {unresolved} unresolved errors")
            connection.execute(
                """UPDATE migration_runs SET approved_by=%s,approved_at=now(),updated_at=now()
                    WHERE id=%s""",
                (approver, run_id),
            )
            self.transition(run_id, "Approved", actor=approver, connection=connection)

    def apply_history_scope(self, run_id: int, *, actor: str) -> int:
        """Skip closed transaction history outside the configured two-year window."""

        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT status,history_from,history_to FROM migration_runs WHERE id=%s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if not run or run["status"] != "Staged":
                raise DocumentStateError("History scope can only be applied to a staged run")
            records = connection.execute(
                """SELECT id,source_object,raw_payload FROM migration_staging_records
                    WHERE run_id=%s AND status='Extracted' FOR UPDATE""",
                (run_id,),
            ).fetchall()
            skipped = 0
            for record in records:
                decision = _history_decision(
                    record["source_object"], record["raw_payload"],
                    run["history_from"], run["history_to"],
                )
                if decision == "skip":
                    connection.execute(
                        """UPDATE migration_staging_records
                              SET status='Skipped',archived_reason='Closed outside migration history window',
                                  updated_at=now() WHERE id=%s""",
                        (record["id"],),
                    )
                    skipped += 1
            connection.execute(
                """INSERT INTO migration_audit_events(run_id,actor,event_type,message,details)
                   VALUES (%s,%s,'history_scope','Applied current/previous year plus older-open scope',%s)""",
                (
                    run_id, actor,
                    Jsonb({"history_from": str(run["history_from"]),
                           "history_to": str(run["history_to"]), "skipped": skipped}),
                ),
            )
            return skipped


_TRANSACTION_OBJECTS = {
    "Orders", "DeliveryNotes", "Invoices", "IncomingPayments", "PurchaseOrders",
    "PurchaseDeliveryNotes", "PurchaseInvoices", "OutgoingPayments", "JournalEntries",
    "Sales Order", "Delivery Note", "Sales Invoice", "Payment Entry", "Purchase Order",
    "Purchase Receipt", "Purchase Invoice", "Journal Entry", "Stock Entry",
    "Stock Reconciliation",
    "Quotations", "Returns", "CreditNotes", "PurchaseReturns", "PurchaseCreditNotes",
    "Quotation",
    "PurchaseRequests", "PurchaseQuotations", "Material Request",
    "Request for Quotation", "Supplier Quotation",
}


def _history_decision(source_object, payload, history_from, history_to):
    if source_object not in _TRANSACTION_OBJECTS:
        return "include"
    date_value = (
        payload.get("DocDate") or payload.get("ReferenceDate")
        or payload.get("posting_date") or payload.get("transaction_date")
    )
    try:
        posting_date = date.fromisoformat(str(date_value)[:10])
    except (TypeError, ValueError):
        return "include"
    if history_from <= posting_date <= history_to:
        return "include"
    status = str(payload.get("DocumentStatus") or payload.get("status") or "").lower()
    outstanding = payload.get("OpenAmount") or payload.get("outstanding_amount") or 0
    if source_object in {"IncomingPayments", "OutgoingPayments"}:
        total = Decimal(str(payload.get("DocTotal") or 0))
        applied = sum(
            Decimal(str(row.get("SumApplied") or row.get("AppliedFC") or 0))
            for row in payload.get("PaymentInvoices") or []
        )
        outstanding = max(Decimal("0"), total - applied)
    elif source_object == "Payment Entry":
        outstanding = (
            payload.get("unallocated_amount")
            or payload.get("difference_amount")
            or outstanding
        )
    is_open = (
        status in {"bost_open", "open", "overdue", "unpaid", "partly paid", "to bill"}
        or (source_object in {
                "Invoices", "PurchaseInvoices", "Sales Invoice", "Purchase Invoice",
                "IncomingPayments", "OutgoingPayments", "Payment Entry",
            }
            and Decimal(str(outstanding or 0)) > 0)
    )
    return "include" if is_open else "skip"
