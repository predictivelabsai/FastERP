"""End-to-end one-time ERP migration orchestration and cutover gates."""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from fasterp.database import Database
from fasterp.errors import DomainError

from .apply import Applier
from .connectors import CsvBundleConnector, ErpNextRestConnector, SapBusinessOneConnector
from .mapping import normalizers_for
from .masters import ensure_accounting_settings
from .reconcile import Reconciler
from .staging import MigrationRunService
from .transactions import full_handlers, transaction_handlers
from .validation import Validator
from .connectors.base import canonical_hash


SAP_TRACKED_LINES = "DocumentLines($expand=SerialNumbers,BatchNumbers)"
SAP_EXPANDS = {
    "Orders": ("DocumentLines",), "DeliveryNotes": (SAP_TRACKED_LINES,),
    "Invoices": ("DocumentLines",), "PurchaseOrders": ("DocumentLines",),
    "PurchaseDeliveryNotes": (SAP_TRACKED_LINES,),
    "PurchaseInvoices": ("DocumentLines",),
    "IncomingPayments": ("PaymentInvoices",),
    "OutgoingPayments": ("PaymentInvoices",),
    "JournalEntries": ("JournalEntryLines",),
    "Quotations": ("DocumentLines",), "Returns": (SAP_TRACKED_LINES,),
    "CreditNotes": ("DocumentLines",), "PurchaseReturns": (SAP_TRACKED_LINES,),
    "PurchaseCreditNotes": ("DocumentLines",),
    "PurchaseRequests": ("DocumentLines",),
    "PurchaseQuotations": ("DocumentLines",),
}


class MigrationOrchestrator:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.runs = MigrationRunService(database)

    def connector_for(self, source: dict):
        configuration = source.get("configuration") or {}
        prefix = (source.get("credential_env_prefix") or "").rstrip("_")
        connector_type = source["connector_type"]
        if connector_type == "sap_business_one_odata_v4":
            return SapBusinessOneConnector.from_environment(
                base_url=source["base_url"], company_db=source["source_company_db"],
                credential_env_prefix=prefix,
                verify_tls=bool(configuration.get("verify_tls", True)),
                timeout=float(configuration.get("timeout", 30)),
            )
        if connector_type == "erpnext_rest":
            return ErpNextRestConnector(
                base_url=source["base_url"],
                api_key=os.getenv(f"{prefix}_API_KEY", ""),
                api_secret=os.getenv(f"{prefix}_API_SECRET", ""),
                verify_tls=bool(configuration.get("verify_tls", True)),
                timeout=float(configuration.get("timeout", 30)),
            )
        if connector_type == "csv_bundle":
            return CsvBundleConnector(
                configuration["directory"], key_fields=configuration.get("key_fields")
            )
        raise DomainError(f"No executable connector factory for {connector_type}")

    def record_failback(
        self, run_id: int, *, actor: str, restore_reference: str,
        traffic_stopped: bool,
    ) -> None:
        """Record controlled failback after traffic is stopped and a restore is selected."""

        if not traffic_stopped:
            raise DomainError("Failback requires confirmation that FastERP traffic is stopped")
        if not restore_reference.strip():
            raise DomainError("Failback requires an external backup/restore reference")
        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT status FROM migration_runs WHERE id=%s FOR UPDATE", (run_id,)
            ).fetchone()
            if not run or run["status"] != "Completed":
                raise DomainError("Only a completed cutover run can be failed back")
            connection.execute(
                "UPDATE migration_runs SET rollback_reference=%s,updated_at=now() WHERE id=%s",
                (restore_reference.strip(), run_id),
            )
            self.runs.transition(
                run_id, "Rolled Back", actor=actor,
                message="Traffic stopped and deployment/database restore point selected",
                connection=connection,
            )

    def execute(
        self,
        *,
        source_name: str,
        launch_date: date,
        actor: str,
        mode: str = "dry_run",
        approver: str | None = None,
        source_control_totals: dict[str, Any] | None = None,
        source_frozen: bool = False,
        include_unsupported: bool = False,
        page_size: int = 100,
        backup_reference: str | None = None,
    ) -> dict[str, Any]:
        source = self.database.one(
            "SELECT * FROM migration_sources WHERE name=%s AND active=true", (source_name,)
        )
        if not source:
            raise DomainError(f"Active migration source not found: {source_name}")
        if mode == "cutover" and not source_frozen:
            raise DomainError("Cutover requires explicit confirmation that source posting is frozen")
        if mode == "cutover" and (not approver or approver.strip() == actor.strip()):
            raise DomainError("Cutover requires a named approver different from the operator")
        if mode == "cutover" and source_control_totals is None:
            raise DomainError("Cutover requires source financial control totals")
        if mode == "cutover":
            required_controls = {
                "opening_trial_balance", "opening_inventory", "trial_balance", "ar_aging",
                "ap_aging", "inventory", "tax",
            }
            missing_controls = sorted(required_controls - set(source_control_totals or {}))
            if missing_controls:
                raise DomainError(
                    "Cutover control totals are missing: " + ", ".join(missing_controls)
                )
        if mode == "cutover" and not (backup_reference or "").strip():
            raise DomainError("Cutover requires a verified pre-cutover backup reference")
        prior_rehearsal = None
        if mode == "cutover":
            prior_rehearsal = self.database.one(
                """SELECT id,manifest_hash FROM migration_runs
                    WHERE source_id=%s AND mode='dry_run' AND status='Validated'
                      AND error_count=0 AND manifest_hash IS NOT NULL
                    ORDER BY id DESC LIMIT 1""",
                (source["id"],),
            )
            if not prior_rehearsal:
                raise DomainError("Cutover requires a successful validated dry run")
        history_from = date(launch_date.year - 1, 1, 1)
        history_to = launch_date + timedelta(days=7)
        run_id = self.runs.create_run(
            source_id=source["id"], history_from=history_from, history_to=history_to,
            mode=mode, requested_by=actor,
            scope={
                "history": "current_and_previous_year_plus_older_open",
                "launch_date": launch_date.isoformat(), "cutover": "T+7",
            },
        )
        connector = None
        try:
            connector = self.connector_for(source)
            connector.test_connection()
            capabilities = self.runs.discover(run_id, connector, actor=actor)
            capability_manifest = canonical_hash([
                {
                    "source_object": row.source_object,
                    "available": row.available,
                    "supports_filter": row.supports_filter,
                    "supports_expand": row.supports_expand,
                    "key_fields": row.key_fields,
                }
                for row in capabilities
            ])
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE migration_runs SET manifest_hash=%s WHERE id=%s",
                    (capability_manifest, run_id),
                )
            if (
                prior_rehearsal
                and prior_rehearsal["manifest_hash"] != capability_manifest
            ):
                raise DomainError(
                    "Source capabilities changed after the approved dry run; run another dry run"
                )
            available = [row.source_object for row in capabilities if row.available]
            normalizers = normalizers_for(source["connector_type"])
            handlers, order = full_handlers(source["connector_type"])
            supported = set(normalizers) | set(handlers)
            objects = available if include_unsupported else [name for name in available if name in supported]
            filters = self._filters(source["connector_type"], objects, history_from)
            expands = SAP_EXPANDS if source["connector_type"] == "sap_business_one_odata_v4" else {}
            self.runs.extract(
                run_id, connector, objects, actor=actor, page_size=page_size,
                filters=filters, expands=expands,
            )
            skipped = self.runs.apply_history_scope(run_id, actor=actor)
            warnings, errors = Validator(
                self.database, normalizers
            ).validate(run_id, actor=actor)
            result = {
                "run_id": run_id, "mode": mode, "history_from": history_from.isoformat(),
                "history_to": history_to.isoformat(), "cutover": "T+7",
                "objects": objects, "skipped": skipped,
                "warnings": warnings, "errors": errors, "status": "Validated",
            }
            if mode != "cutover":
                return result
            if errors:
                raise DomainError(f"Cutover run has {errors} validation errors")
            self.runs.approve(run_id, approver=approver or actor)
            with self.database.transaction() as connection:
                connection.execute(
                    """UPDATE migration_runs SET cutover_started_at=now(),rollback_reference=%s
                        WHERE id=%s""",
                    (backup_reference, run_id),
                )
                connection.execute(
                    """INSERT INTO migration_reconciliation_artifacts
                           (run_id,artifact_type,content_type,storage_path,content_hash,summary)
                       VALUES (%s,'Backup','text/plain',%s,%s,%s)""",
                    (
                        run_id, backup_reference,
                        canonical_hash({"backup_reference": backup_reference}),
                        Jsonb({"verified_by": approver or actor}),
                    ),
                )
            reconciler = Reconciler(self.database)
            opening: dict[str, int | None] = {}

            def apply_opening() -> None:
                ensure_accounting_settings(
                    self.database, source["company_id"], source.get("configuration") or {}
                )
                opening["inventory_event"] = reconciler.apply_opening_inventory(
                    run_id, source_control_totals["opening_inventory"], actor=actor
                )
                opening["accounting_batch"] = reconciler.apply_opening_trial_balance(
                    run_id, source_control_totals["opening_trial_balance"], actor=actor
                )

            transaction_map, _transaction_order = transaction_handlers(
                source["connector_type"]
            )
            applied, archived = Applier(self.database, handlers, order).apply(
                run_id, actor=actor, before_transactions=apply_opening,
                transaction_objects=set(transaction_map),
            )
            count_passed = Reconciler(self.database).reconcile_counts(
                run_id, actor=actor, complete=False
            )
            financial_passed = Reconciler(self.database).reconcile_financials(
                run_id, source_control_totals or {}, actor=actor
            )
            if financial_passed:
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE migration_runs SET cutover_completed_at=now() WHERE id=%s",
                        (run_id,),
                    )
            return {
                **result, "status": "Completed" if financial_passed else "Failed",
                "applied": applied, "archived": archived,
                "opening_inventory_event": opening.get("inventory_event"),
                "opening_accounting_batch": opening.get("accounting_batch"),
                "count_reconciliation": count_passed,
                "financial_reconciliation": financial_passed,
            }
        except Exception as exc:
            status = self.database.scalar(
                "SELECT status FROM migration_runs WHERE id=%s", (run_id,)
            )
            if status not in {"Failed", "Completed", "Rolled Back"}:
                try:
                    self.runs.transition(
                        run_id, "Failed", actor=actor,
                        message=f"Migration orchestration failed ({type(exc).__name__})",
                    )
                except Exception as state_error:
                    # Preserve the original migration failure if state recording fails.
                    exc.add_note(
                        f"Could not record failed migration state: {type(state_error).__name__}"
                    )
            raise
        finally:
            if connector is not None:
                connector.close()

    @staticmethod
    def _filters(connector_type: str, objects: list[str], history_from: date) -> dict[str, str]:
        if connector_type != "sap_business_one_odata_v4":
            return {}
        transactional = set(SAP_EXPANDS)
        full_scan = {"IncomingPayments", "OutgoingPayments"}
        value = history_from.isoformat()
        return {
            name: f"DocDate ge '{value}' or DocumentStatus eq 'bost_Open'"
            for name in objects
            if name in transactional and name not in full_scan | {"JournalEntries"}
        } | ({"JournalEntries": f"ReferenceDate ge '{value}'"} if "JournalEntries" in objects else {})
