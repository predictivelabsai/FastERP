"""Immutable, multicurrency general-ledger posting services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from psycopg import Connection

from .database import Database
from .errors import ImbalanceError, PeriodLockedError


ZERO = Decimal("0")
MONEY = Decimal("0.0001")


def amount(value: Decimal | int | float | str | None) -> Decimal:
    """Convert external numeric input without binary-float ledger drift."""

    if value is None:
        return ZERO
    return Decimal(str(value)).quantize(MONEY)


@dataclass(frozen=True)
class PostingLine:
    account_id: int
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    account_currency: str | None = None
    account_debit: Decimal = ZERO
    account_credit: Decimal = ZERO
    transaction_currency: str | None = None
    transaction_debit: Decimal = ZERO
    transaction_credit: Decimal = ZERO
    reporting_currency: str | None = None
    reporting_debit: Decimal = ZERO
    reporting_credit: Decimal = ZERO
    party_id: int | None = None
    due_date: date | None = None
    business_unit_id: int | None = None
    project_id: int | None = None
    memo: str | None = None
    source_line_type: str | None = None
    source_line_id: int | None = None
    reverses_entry_id: int | None = None

    def normalized(self) -> "PostingLine":
        values = {
            field: amount(getattr(self, field))
            for field in (
                "debit", "credit", "account_debit", "account_credit",
                "transaction_debit", "transaction_credit",
                "reporting_debit", "reporting_credit",
            )
        }
        return PostingLine(
            **{
                **self.__dict__,
                **values,
            }
        )


class AccountingService:
    """Post and reverse complete vouchers in one database transaction."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def assert_period_open(connection: Connection, company_id: int, posting_date: date) -> None:
        row = connection.execute(
            """SELECT code, status FROM fiscal_periods
                WHERE company_id=%s AND %s BETWEEN starts_on AND ends_on
                ORDER BY starts_on DESC LIMIT 1""",
            (company_id, posting_date),
        ).fetchone()
        if row and row["status"] == "Locked":
            raise PeriodLockedError(
                f"Fiscal period {row['code']} is locked for {posting_date}"
            )

    @staticmethod
    def validate_lines(lines: list[PostingLine]) -> list[PostingLine]:
        normalized = [line.normalized() for line in lines]
        if len(normalized) < 2:
            raise ImbalanceError("A posting requires at least two lines")
        for line in normalized:
            if (line.debit > ZERO) == (line.credit > ZERO):
                raise ImbalanceError("Each posting line must be debit or credit, not both")
        debits = sum((line.debit for line in normalized), ZERO)
        credits = sum((line.credit for line in normalized), ZERO)
        if debits == ZERO or debits != credits:
            raise ImbalanceError(f"Voucher is not balanced: debit {debits}, credit {credits}")
        return normalized

    def post_voucher(
        self,
        *,
        company_id: int,
        voucher_type: str,
        voucher_id: int,
        voucher_code: str,
        posting_date: date,
        lines: list[PostingLine],
        actor: str,
        connection: Connection | None = None,
        reverses_batch_id: int | None = None,
    ) -> int:
        normalized = self.validate_lines(lines)
        if connection is not None:
            return self._post(
                connection, company_id, voucher_type, voucher_id, voucher_code,
                posting_date, normalized, actor, reverses_batch_id,
            )
        with self.database.transaction() as transaction:
            return self._post(
                transaction, company_id, voucher_type, voucher_id, voucher_code,
                posting_date, normalized, actor, reverses_batch_id,
            )

    def _post(
        self,
        connection: Connection,
        company_id: int,
        voucher_type: str,
        voucher_id: int,
        voucher_code: str,
        posting_date: date,
        lines: list[PostingLine],
        actor: str,
        reverses_batch_id: int | None,
    ) -> int:
        self.assert_period_open(connection, company_id, posting_date)
        existing = connection.execute(
            """SELECT id, status FROM posting_batches
                WHERE company_id=%s AND voucher_type=%s AND voucher_id=%s""",
            (company_id, voucher_type, voucher_id),
        ).fetchone()
        if existing:
            if existing["status"] == "Posted":
                return existing["id"]
            raise ImbalanceError("Voucher already has a non-posted accounting batch")
        batch = connection.execute(
            """INSERT INTO posting_batches
                   (company_id,voucher_type,voucher_id,voucher_code,posting_date,
                    status,reverses_batch_id)
               VALUES (%s,%s,%s,%s,%s,'Draft',%s) RETURNING id""",
            (
                company_id, voucher_type, voucher_id, voucher_code, posting_date,
                reverses_batch_id,
            ),
        ).fetchone()["id"]
        for number, line in enumerate(lines, 1):
            connection.execute(
                """INSERT INTO gl_entries (
                       company_id,entry_date,account_id,debit,credit,ref,
                       business_unit_id,project_id,currency,memo,posting_batch_id,
                       line_number,voucher_type,voucher_id,voucher_code,party_id,due_date,
                       account_currency,account_debit,account_credit,
                       transaction_currency,transaction_debit,transaction_credit,
                       reporting_currency,reporting_debit,reporting_credit,
                       reverses_entry_id,source_line_type,source_line_id)
                   VALUES (
                       %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                       %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    company_id, posting_date, line.account_id, line.debit, line.credit,
                    voucher_code, line.business_unit_id, line.project_id,
                    line.account_currency, line.memo, batch, number, voucher_type,
                    voucher_id, voucher_code, line.party_id, line.due_date,
                    line.account_currency, line.account_debit, line.account_credit,
                    line.transaction_currency, line.transaction_debit,
                    line.transaction_credit, line.reporting_currency,
                    line.reporting_debit, line.reporting_credit,
                    line.reverses_entry_id, line.source_line_type, line.source_line_id,
                ),
            )
        connection.execute(
            """UPDATE posting_batches
                  SET status='Posted', posted_at=now(), posted_by=%s
                WHERE id=%s""",
            (actor, batch),
        )
        return batch

    def reverse_batch(
        self,
        batch_id: int,
        *,
        reversal_voucher_type: str,
        reversal_voucher_id: int,
        reversal_voucher_code: str,
        posting_date: date,
        actor: str,
    ) -> int:
        with self.database.transaction() as connection:
            batch = connection.execute(
                "SELECT * FROM posting_batches WHERE id=%s FOR UPDATE", (batch_id,)
            ).fetchone()
            if not batch or batch["status"] != "Posted":
                raise ImbalanceError("Only a posted batch can be reversed")
            original = connection.execute(
                "SELECT * FROM gl_entries WHERE posting_batch_id=%s ORDER BY line_number",
                (batch_id,),
            ).fetchall()
            lines = [
                PostingLine(
                    account_id=row["account_id"], debit=row["credit"],
                    credit=row["debit"], account_currency=row["account_currency"],
                    account_debit=row["account_credit"],
                    account_credit=row["account_debit"],
                    transaction_currency=row["transaction_currency"],
                    transaction_debit=row["transaction_credit"],
                    transaction_credit=row["transaction_debit"],
                    reporting_currency=row["reporting_currency"],
                    reporting_debit=row["reporting_credit"],
                    reporting_credit=row["reporting_debit"], party_id=row["party_id"],
                    due_date=row["due_date"], business_unit_id=row["business_unit_id"],
                    project_id=row["project_id"], memo=f"Reversal: {row['memo'] or ''}".strip(),
                    reverses_entry_id=row["id"], source_line_type=row["source_line_type"],
                    source_line_id=row["source_line_id"],
                )
                for row in original
            ]
            reversal = self.post_voucher(
                company_id=batch["company_id"], voucher_type=reversal_voucher_type,
                voucher_id=reversal_voucher_id, voucher_code=reversal_voucher_code,
                posting_date=posting_date, lines=lines, actor=actor,
                connection=connection, reverses_batch_id=batch_id,
            )
            connection.execute(
                "UPDATE posting_batches SET status='Reversed' WHERE id=%s", (batch_id,)
            )
            return reversal

    @staticmethod
    def account_id(connection: Connection, company_id: int, code_or_name: str) -> int:
        row = connection.execute(
            """SELECT id FROM accounts
                WHERE company_id=%s AND (code=%s OR name=%s) AND active=true""",
            (company_id, code_or_name, code_or_name),
        ).fetchone()
        if not row:
            raise ImbalanceError(f"Active account not found: {code_or_name}")
        return row["id"]

    @staticmethod
    def batch_entries(connection: Connection, batch_id: int) -> list[dict[str, Any]]:
        return list(
            connection.execute(
                "SELECT * FROM gl_entries WHERE posting_batch_id=%s ORDER BY line_number",
                (batch_id,),
            ).fetchall()
        )
