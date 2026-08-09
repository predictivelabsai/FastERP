"""Persist migration count reconciliation and complete passing runs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from psycopg.types.json import Jsonb

from fasterp.database import Database
from fasterp.accounting import AccountingService, PostingLine, amount
from fasterp.errors import DomainError
from fasterp.inventory import InventoryLine, InventoryService, number

from .staging import MigrationRunService
from .connectors.base import canonical_hash
from .tracking import apply_tracking


class Reconciler:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.runs = MigrationRunService(database)

    def reconcile_counts(self, run_id: int, *, actor: str, complete: bool = True) -> bool:
        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT status,source_id FROM migration_runs WHERE id=%s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if not run or run["status"] != "Reconciling":
                raise ValueError("Run must be reconciling")
            objects = connection.execute(
                """SELECT source_object,count(*) AS staged,
                          count(*) FILTER (WHERE status='Applied') AS applied,
                          count(*) FILTER (WHERE status='Archived') AS archived,
                          count(*) FILTER (WHERE status='Skipped') AS skipped,
                          count(*) FILTER (WHERE status IN ('Invalid','Failed')) AS failed
                     FROM migration_staging_records WHERE run_id=%s
                    GROUP BY source_object ORDER BY source_object""",
                (run_id,),
            ).fetchall()
            all_passed = True
            for row in objects:
                handled = row["applied"] + row["archived"] + row["skipped"]
                passed = row["staged"] == handled and row["failed"] == 0
                all_passed = all_passed and passed
                connection.execute(
                    """INSERT INTO migration_reconciliations (
                           run_id,check_code,object_type,dimension,source_value,
                           target_value,difference,passed,details)
                       VALUES (%s,'record_count',%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (run_id,check_code,object_type,dimension) DO UPDATE SET
                           source_value=excluded.source_value,target_value=excluded.target_value,
                           difference=excluded.difference,passed=excluded.passed,
                           details=excluded.details,checked_at=now()""",
                    (
                        run_id, row["source_object"], Jsonb({}), row["staged"],
                        handled, row["staged"] - handled, passed,
                        Jsonb({"applied": row["applied"], "archived": row["archived"],
                               "skipped": row["skipped"], "failed": row["failed"]}),
                    ),
                )
            if complete:
                self.runs.transition(
                    run_id, "Completed" if all_passed else "Failed", actor=actor,
                    connection=connection,
                )
            if all_passed and complete:
                connection.execute(
                    "UPDATE migration_runs SET completed_at=now(),updated_at=now() WHERE id=%s",
                    (run_id,),
                )
            return all_passed

    def apply_opening_trial_balance(
        self,
        run_id: int,
        balances: dict[str, str | int | float],
        *,
        actor: str,
    ) -> int | None:
        """Post one idempotent opening voucher from signed account balances.

        Positive amounts are debits and negative amounts are credits. The
        supplied balance must net to zero and use FastERP account codes.
        """

        clean = {
            str(code): amount(value)
            for code, value in balances.items()
            if amount(value) != 0
        }
        if not clean:
            return None
        if sum(clean.values(), Decimal("0")) != 0:
            raise DomainError("Opening trial balance must net to zero")
        with self.database.transaction() as connection:
            run = connection.execute(
                """SELECT source.company_id,run.history_from
                     FROM migration_runs run
                     JOIN migration_sources source ON source.id=run.source_id
                    WHERE run.id=%s FOR UPDATE OF run""",
                (run_id,),
            ).fetchone()
            if not run or not run["company_id"]:
                raise DomainError("Opening balance run must belong to a target company")
            company_id = run["company_id"]
            accounts = {
                row["code"]: row["id"]
                for row in connection.execute(
                    "SELECT id,code FROM accounts WHERE company_id=%s AND active=true",
                    (company_id,),
                ).fetchall()
            }
            missing = sorted(set(clean) - set(accounts))
            if missing:
                raise DomainError(
                    "Opening balance account mapping is missing: " + ", ".join(missing)
                )
            code = f"MIG-OPEN-{run_id}"
            journal = connection.execute(
                """SELECT id FROM journal_entries
                    WHERE company_id=%s AND code=%s""",
                (company_id, code),
            ).fetchone()
            if journal:
                journal_id = journal["id"]
                posted = {
                    row["code"]: amount(row["value"])
                    for row in connection.execute(
                        """SELECT account.code,sum(entry.debit-entry.credit) AS value
                             FROM gl_entries entry
                             JOIN accounts account ON account.id=entry.account_id
                            WHERE entry.company_id=%s
                              AND entry.voucher_type='Migration Opening'
                              AND entry.voucher_id=%s
                            GROUP BY account.code""",
                        (company_id, journal_id),
                    ).fetchall()
                }
                if posted and posted != clean:
                    raise DomainError("Opening trial balance changed after it was posted")
            else:
                local_currency = connection.execute(
                    "SELECT local_currency FROM companies WHERE id=%s", (company_id,)
                ).fetchone()["local_currency"]
                journal_id = connection.execute(
                    """INSERT INTO journal_entries
                           (company_id,code,entry_date,memo,status,document_state,
                            posted_at,posted_by,transaction_currency,
                            transaction_exchange_rate)
                       VALUES (%s,%s,%s,'Migration opening balance','Posted','Posted',
                               now(),%s,%s,1) RETURNING id""",
                    (company_id, code, run["history_from"], actor, local_currency),
                ).fetchone()["id"]
                for line_number, (account_code, value) in enumerate(clean.items(), 1):
                    connection.execute(
                        """INSERT INTO journal_lines
                               (journal_id,line_number,account_id,debit,credit,memo)
                           VALUES (%s,%s,%s,%s,%s,'Migration opening balance')""",
                        (
                            journal_id, line_number, accounts[account_code],
                            value if value > 0 else 0,
                            -value if value < 0 else 0,
                        ),
                    )
            lines = [
                PostingLine(
                    accounts[account_code],
                    debit=value if value > 0 else Decimal("0"),
                    credit=-value if value < 0 else Decimal("0"),
                    memo="Migration opening balance",
                )
                for account_code, value in clean.items()
            ]
            return AccountingService(self.database).post_voucher(
                company_id=company_id, voucher_type="Migration Opening",
                voucher_id=journal_id, voucher_code=code,
                posting_date=run["history_from"], lines=lines, actor=actor,
                connection=connection,
            )

    def apply_opening_inventory(
        self,
        run_id: int,
        balances: dict[str, dict[str, str | int | float]],
        *,
        actor: str,
    ) -> int | None:
        """Post the idempotent item/warehouse opening quantity and value."""

        if not balances:
            return None
        control_hash = canonical_hash(balances)
        prior_control = self.database.one(
            """SELECT content_hash FROM migration_reconciliation_artifacts
                WHERE run_id=%s AND artifact_type='Inventory'
                  AND summary->>'kind'='opening' ORDER BY id DESC LIMIT 1""",
            (run_id,),
        )
        if prior_control and prior_control["content_hash"] != control_hash:
            raise DomainError("Opening inventory controls changed after they were recorded")
        with self.database.connection() as connection:
            run = connection.execute(
                """SELECT source.company_id,run.history_from
                     FROM migration_runs run
                     JOIN migration_sources source ON source.id=run.source_id
                    WHERE run.id=%s""",
                (run_id,),
            ).fetchone()
            if not run or not run["company_id"]:
                raise DomainError("Opening inventory run must belong to a target company")
            company_id = run["company_id"]
            item_ids = {
                row["code"]: row["id"]
                for row in connection.execute(
                    "SELECT id,code FROM items WHERE company_id=%s AND active=true",
                    (company_id,),
                ).fetchall()
            }
            warehouse_ids = {
                row["code"]: row["id"]
                for row in connection.execute(
                    "SELECT id,code FROM warehouses WHERE company_id=%s AND active=true",
                    (company_id,),
                ).fetchall()
            }
            existing = connection.execute(
                """SELECT id FROM inventory_events
                    WHERE company_id=%s AND voucher_type='Migration Opening'
                      AND voucher_id=%s AND event_type='Opening'""",
                (company_id, run_id),
            ).fetchone()
        lines = []
        expected = {}
        tracking_lines = []
        for key, values in balances.items():
            try:
                item_code, warehouse_code = key.split("|", 1)
            except ValueError as exc:
                raise DomainError(
                    f"Opening inventory key must be ITEM|WAREHOUSE: {key}"
                ) from exc
            if item_code not in item_ids or warehouse_code not in warehouse_ids:
                raise DomainError(f"Opening inventory mapping is missing: {key}")
            quantity = number(values.get("quantity"))
            inventory_value = number(values.get("value"))
            if quantity < 0 or inventory_value < 0:
                raise DomainError(f"Opening inventory cannot be negative: {key}")
            if quantity == 0:
                if inventory_value != 0:
                    raise DomainError(f"Zero opening quantity must have zero value: {key}")
                continue
            expected[key] = {"quantity": quantity, "value": inventory_value}
            tracking_lines.append({
                "item_code": item_code,
                "serials": values.get("serials") or [],
                "batches": values.get("batches") or [],
            })
            lines.append(InventoryLine(
                item_ids[item_code], warehouse_ids[warehouse_code], quantity,
                unit_cost=number(inventory_value / quantity),
                source_line_type="Migration Opening Inventory",
            ))
        if not lines:
            return None
        if existing:
            actual = {
                f"{row['item_code']}|{row['warehouse_code']}": {
                    "quantity": number(row["quantity"]),
                    "value": number(row["value"]),
                }
                for row in self.database.rows(
                    """SELECT item.code AS item_code,warehouse.code AS warehouse_code,
                              ledger.quantity_change AS quantity,
                              ledger.value_change AS value
                         FROM inventory_ledger_entries ledger
                         JOIN items item ON item.id=ledger.item_id
                         JOIN warehouses warehouse ON warehouse.id=ledger.warehouse_id
                        WHERE ledger.event_id=%s""",
                    (existing["id"],),
                )
            }
            if actual != expected:
                raise DomainError("Opening inventory changed after it was posted")
            if not prior_control:
                with self.database.transaction() as connection:
                    connection.execute(
                        """INSERT INTO migration_reconciliation_artifacts
                               (run_id,artifact_type,content_type,content_hash,summary)
                           VALUES (%s,'Inventory','application/json',%s,%s)""",
                        (run_id, control_hash, Jsonb({"kind": "opening"})),
                    )
            return existing["id"]
        with self.database.transaction() as connection:
            event_id = InventoryService(self.database).post_event(
                company_id=company_id, event_type="Opening",
                voucher_type="Migration Opening", voucher_id=run_id,
                voucher_code=f"MIG-OPEN-STOCK-{run_id}",
                event_date=run["history_from"], lines=lines, actor=actor,
                connection=connection,
            )
            apply_tracking(connection, event_id, tracking_lines)
            connection.execute(
                """INSERT INTO migration_reconciliation_artifacts
                       (run_id,artifact_type,content_type,content_hash,summary)
                   VALUES (%s,'Inventory','application/json',%s,%s)
                   ON CONFLICT DO NOTHING""",
                (run_id, control_hash, Jsonb({"kind": "opening"})),
            )
            return event_id

    def target_snapshot(self, company_id: int, *, as_of: date) -> dict:
        """Build the financial control totals used for migration sign-off."""

        with self.database.connection() as connection:
            trial_balance = {
                row["code"]: str(row["value"])
                for row in connection.execute(
                    """SELECT account.code,sum(entry.debit-entry.credit) AS value
                         FROM gl_entries entry JOIN accounts account ON account.id=entry.account_id
                        WHERE entry.company_id=%s AND entry.entry_date<=%s
                        GROUP BY account.code ORDER BY account.code""",
                    (company_id, as_of),
                ).fetchall()
            }
            inventory = {
                f"{row['item_code']}|{row['warehouse_code']}": {
                    "quantity": str(row["quantity"]), "value": str(row["value"]),
                }
                for row in connection.execute(
                    """SELECT item.code AS item_code,warehouse.code AS warehouse_code,
                              balance.quantity,balance.inventory_value AS value
                         FROM inventory_balances balance
                         JOIN items item ON item.id=balance.item_id
                         JOIN warehouses warehouse ON warehouse.id=balance.warehouse_id
                        WHERE balance.company_id=%s ORDER BY item.code,warehouse.code""",
                    (company_id,),
                ).fetchall()
            }
            ar = self._aging(
                connection, "invoices", "customers", "customer_id", company_id, as_of
            )
            ap = self._aging(
                connection, "purchase_invoices", "suppliers", "supplier_id", company_id, as_of
            )
            tax = {
                f"sales|{row['period']}|{row['code']}": str(row["value"])
                for row in connection.execute(
                    """SELECT to_char(invoice.invoice_date,'YYYY-MM') AS period,
                              tax.code,sum(line.tax_amount) AS value
                         FROM invoice_items line JOIN invoices invoice ON invoice.id=line.invoice_id
                         JOIN tax_codes tax ON tax.id=line.tax_code_id
                        WHERE invoice.company_id=%s AND invoice.invoice_date<=%s
                        GROUP BY period,tax.code""",
                    (company_id, as_of),
                ).fetchall()
            }
            tax.update({
                f"purchase|{row['period']}|{row['code']}": str(row["value"])
                for row in connection.execute(
                    """SELECT to_char(invoice.invoice_date,'YYYY-MM') AS period,
                              tax.code,sum(line.tax_amount) AS value
                         FROM purchase_invoice_items line
                         JOIN purchase_invoices invoice ON invoice.id=line.purchase_invoice_id
                         JOIN tax_codes tax ON tax.id=line.tax_code_id
                        WHERE invoice.company_id=%s AND invoice.invoice_date<=%s
                        GROUP BY period,tax.code""",
                    (company_id, as_of),
                ).fetchall()
            })
        return {
            "as_of": as_of.isoformat(), "trial_balance": trial_balance,
            "ar_aging": ar, "ap_aging": ap, "inventory": inventory, "tax": tax,
        }

    @staticmethod
    def _aging(connection, table, party_table, party_column, company_id, as_of):
        rows = connection.execute(
            f"""SELECT party.code AS party_code,invoice.currency,
                       CASE WHEN invoice.due_date>%(as_of)s THEN 'Current'
                            WHEN %(as_of)s-invoice.due_date<=30 THEN '1-30'
                            WHEN %(as_of)s-invoice.due_date<=60 THEN '31-60'
                            WHEN %(as_of)s-invoice.due_date<=90 THEN '61-90'
                            ELSE '90+' END AS bucket,
                       sum(invoice.outstanding_amount) AS value
                  FROM {table} invoice
                  JOIN {party_table} party ON party.id=invoice.{party_column}
                 WHERE invoice.company_id=%(company)s AND invoice.invoice_date<=%(as_of)s
                   AND invoice.document_state='Posted' AND invoice.outstanding_amount>0
                 GROUP BY party.code,invoice.currency,bucket
                 ORDER BY party.code,invoice.currency,bucket""",
            {"company": company_id, "as_of": as_of},
        ).fetchall()
        return {
            f"{row['party_code']}|{row['currency']}|{row['bucket']}": str(row["value"])
            for row in rows
        }

    def reconcile_financials(
        self,
        run_id: int,
        expected: dict,
        *,
        actor: str,
        tolerance: Decimal = Decimal("0.01"),
    ) -> bool:
        """Compare source control totals to FastERP and persist sign-off evidence."""

        with self.database.transaction() as connection:
            run = connection.execute(
                """SELECT run.status,source.company_id,run.history_to
                     FROM migration_runs run JOIN migration_sources source ON source.id=run.source_id
                    WHERE run.id=%s FOR UPDATE OF run""",
                (run_id,),
            ).fetchone()
            if not run or run["status"] != "Reconciling" or not run["company_id"]:
                raise ValueError("Run must be reconciling and assigned to a company")
        target = self.target_snapshot(run["company_id"], as_of=run["history_to"])
        passed_all = True
        with self.database.transaction() as connection:
            for section in ("trial_balance", "ar_aging", "ap_aging", "inventory", "tax"):
                source_values = self._flatten(expected.get(section, {}))
                target_values = self._flatten(target.get(section, {}))
                for key in sorted(set(source_values) | set(target_values)):
                    source_value = Decimal(str(source_values.get(key, "0")))
                    target_value = Decimal(str(target_values.get(key, "0")))
                    difference = target_value - source_value
                    passed = abs(difference) <= tolerance
                    passed_all = passed_all and passed
                    connection.execute(
                        """INSERT INTO migration_reconciliations
                               (run_id,check_code,object_type,dimension,source_value,
                                target_value,difference,passed,details)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (run_id,check_code,object_type,dimension) DO UPDATE SET
                               source_value=excluded.source_value,target_value=excluded.target_value,
                               difference=excluded.difference,passed=excluded.passed,
                               details=excluded.details,checked_at=now()""",
                        (
                            run_id, f"financial_{section}", section,
                            Jsonb({"key": key}), source_value, target_value,
                            difference, passed, Jsonb({"tolerance": str(tolerance)}),
                        ),
                    )
            artifact_hash = canonical_hash({"expected": expected, "target": target})
            connection.execute(
                """INSERT INTO migration_reconciliation_artifacts
                       (run_id,artifact_type,content_type,content_hash,summary)
                   VALUES (%s,'Manifest','application/json',%s,%s)
                   ON CONFLICT DO NOTHING""",
                (run_id, artifact_hash, Jsonb({"expected": expected, "target": target})),
            )
            prior_failed = connection.execute(
                "SELECT count(*) AS value FROM migration_reconciliations WHERE run_id=%s AND passed=false",
                (run_id,),
            ).fetchone()["value"]
            passed_all = passed_all and prior_failed == 0
            self.runs.transition(
                run_id, "Completed" if passed_all else "Failed", actor=actor,
                connection=connection,
            )
            if passed_all:
                connection.execute(
                    "UPDATE migration_runs SET completed_at=now(),updated_at=now() WHERE id=%s",
                    (run_id,),
                )
        return passed_all

    @classmethod
    def _flatten(cls, value, prefix="") -> dict[str, str]:
        result = {}
        if isinstance(value, dict):
            for key, child in value.items():
                child_prefix = f"{prefix}.{key}" if prefix else str(key)
                result.update(cls._flatten(child, child_prefix))
        else:
            result[prefix] = str(value)
        return result
