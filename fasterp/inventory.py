"""Transactional inventory quantity and valuation posting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal

from psycopg import Connection

from .accounting import ZERO
from .database import Database
from .errors import DomainError, InsufficientStockError, PeriodLockedError


SIX = Decimal("0.000001")


def number(value: Decimal | int | float | str | None) -> Decimal:
    if value is None:
        return ZERO
    return Decimal(str(value)).quantize(SIX)


@dataclass(frozen=True)
class InventoryLine:
    item_id: int
    warehouse_id: int
    quantity: Decimal
    unit_cost: Decimal | None = None
    additional_cost: Decimal = ZERO
    offset_warehouse_id: int | None = None
    source_line_type: str | None = None
    source_line_id: int | None = None
    forced_unit_cost: Decimal | None = None

    def normalized(self) -> "InventoryLine":
        return InventoryLine(
            item_id=self.item_id,
            warehouse_id=self.warehouse_id,
            quantity=number(self.quantity),
            unit_cost=None if self.unit_cost is None else number(self.unit_cost),
            additional_cost=number(self.additional_cost),
            offset_warehouse_id=self.offset_warehouse_id,
            source_line_type=self.source_line_type,
            source_line_id=self.source_line_id,
            forced_unit_cost=(
                None if self.forced_unit_cost is None else number(self.forced_unit_cost)
            ),
        )


class InventoryService:
    """Post immutable inventory events and maintain rebuildable projections."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def assert_period_open(connection: Connection, company_id: int, posting_date: date) -> None:
        period = connection.execute(
            """SELECT code, status FROM fiscal_periods
                WHERE company_id=%s AND %s BETWEEN starts_on AND ends_on
                ORDER BY starts_on DESC LIMIT 1""",
            (company_id, posting_date),
        ).fetchone()
        if period and period["status"] == "Locked":
            raise PeriodLockedError(
                f"Fiscal period {period['code']} is locked for {posting_date}"
            )

    def post_event(
        self,
        *,
        company_id: int,
        event_type: str,
        voucher_type: str,
        voucher_id: int,
        voucher_code: str,
        event_date: date,
        lines: list[InventoryLine],
        actor: str,
        posting_time: time = time(12, 0),
        connection: Connection | None = None,
        reverses_event_id: int | None = None,
    ) -> int:
        normalized = [line.normalized() for line in lines]
        if not normalized or any(line.quantity == ZERO for line in normalized):
            raise DomainError("Inventory events require non-zero lines")
        if connection is not None:
            return self._post(
                connection, company_id, event_type, voucher_type, voucher_id,
                voucher_code, event_date, posting_time, normalized, actor,
                reverses_event_id,
            )
        with self.database.transaction() as transaction:
            return self._post(
                transaction, company_id, event_type, voucher_type, voucher_id,
                voucher_code, event_date, posting_time, normalized, actor,
                reverses_event_id,
            )

    def _post(
        self,
        connection: Connection,
        company_id: int,
        event_type: str,
        voucher_type: str,
        voucher_id: int,
        voucher_code: str,
        event_date: date,
        posting_time: time,
        lines: list[InventoryLine],
        actor: str,
        reverses_event_id: int | None,
    ) -> int:
        self.assert_period_open(connection, company_id, event_date)
        existing = connection.execute(
            """SELECT id, document_state FROM inventory_events
                WHERE company_id=%s AND voucher_type=%s AND voucher_id=%s
                  AND event_type=%s""",
            (company_id, voucher_type, voucher_id, event_type),
        ).fetchone()
        if existing:
            if existing["document_state"] == "Posted":
                return existing["id"]
            raise DomainError("Inventory voucher already exists in a non-posted state")
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"inventory:{company_id}",)
        )
        event_sequence = connection.execute(
            "SELECT COALESCE(max(sequence_no),0)+1 AS value FROM inventory_events WHERE company_id=%s",
            (company_id,),
        ).fetchone()["value"]
        event_id = connection.execute(
            """INSERT INTO inventory_events (
                   company_id,event_type,event_date,posting_time,sequence_no,
                   voucher_type,voucher_id,voucher_code,document_state,
                   reverses_event_id,posted_at,posted_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'Posted',%s,now(),%s)
               RETURNING id""",
            (
                company_id, event_type, event_date, posting_time, event_sequence,
                voucher_type, voucher_id, voucher_code, reverses_event_id, actor,
            ),
        ).fetchone()["id"]
        posting_at = connection.execute(
            """SELECT (%s::date + %s::time) AT TIME ZONE timezone AS value
                  FROM companies WHERE id=%s""",
            (event_date, posting_time, company_id),
        ).fetchone()["value"]
        for line_number, line in enumerate(lines, 1):
            self._post_line(
                connection, company_id, event_id, line_number, line, posting_at,
                event_date, voucher_code,
            )
        return event_id

    def _post_line(
        self,
        connection: Connection,
        company_id: int,
        event_id: int,
        line_number: int,
        line: InventoryLine,
        posting_at,
        event_date: date,
        voucher_code: str,
    ) -> None:
        item = connection.execute(
            """SELECT valuation_method, standard_cost, allow_negative_stock
                 FROM items WHERE id=%s AND company_id=%s AND active=true""",
            (line.item_id, company_id),
        ).fetchone()
        if not item:
            raise DomainError(f"Active item {line.item_id} does not belong to company")
        warehouse = connection.execute(
            "SELECT id FROM warehouses WHERE id=%s AND company_id=%s AND active=true",
            (line.warehouse_id, company_id),
        ).fetchone()
        if not warehouse:
            raise DomainError(f"Active warehouse {line.warehouse_id} does not belong to company")

        connection.execute(
            """INSERT INTO inventory_balances
                   (company_id,item_id,warehouse_id,quantity,inventory_value,valuation_rate)
               VALUES (%s,%s,%s,0,0,0) ON CONFLICT DO NOTHING""",
            (company_id, line.item_id, line.warehouse_id),
        )
        balance = connection.execute(
            """SELECT * FROM inventory_balances
                WHERE company_id=%s AND item_id=%s AND warehouse_id=%s FOR UPDATE""",
            (company_id, line.item_id, line.warehouse_id),
        ).fetchone()
        company_negative = connection.execute(
            "SELECT negative_stock_allowed FROM companies WHERE id=%s", (company_id,)
        ).fetchone()["negative_stock_allowed"]
        old_qty = number(balance["quantity"])
        old_value = number(balance["inventory_value"])
        old_rate = number(balance["valuation_rate"])
        new_qty = number(old_qty + line.quantity)
        if new_qty < ZERO and not (item["allow_negative_stock"] or company_negative):
            raise InsufficientStockError(
                f"Item {line.item_id} would have quantity {new_qty} in warehouse {line.warehouse_id}"
            )

        method = item["valuation_method"]
        incoming_rate: Decimal | None = None
        outgoing_rate: Decimal | None = None
        cost_queue = None
        if line.quantity > ZERO:
            incoming_rate = self._incoming_rate(item, line)
            value_change = number(line.quantity * incoming_rate + line.additional_cost)
            new_value = number(old_value + value_change)
            if method == "Standard Cost":
                valuation_rate = number(item["standard_cost"])
                new_value = number(new_qty * valuation_rate)
                value_change = number(new_value - old_value)
            elif new_qty > ZERO:
                valuation_rate = number(new_value / new_qty)
            else:
                valuation_rate = incoming_rate or old_rate
        else:
            issue_qty = abs(line.quantity)
            outgoing_value: Decimal | None = None
            if line.forced_unit_cost is not None:
                outgoing_rate = line.forced_unit_cost
            elif method == "FIFO":
                outgoing_rate, cost_queue, outgoing_value = self._consume_fifo(
                    connection, company_id, line.item_id, line.warehouse_id,
                    issue_qty, old_rate, allow_shortfall=new_qty < ZERO,
                )
            elif method == "Standard Cost":
                if item["standard_cost"] is None:
                    raise DomainError(f"Item {line.item_id} has no standard cost")
                outgoing_rate = number(item["standard_cost"])
            else:
                outgoing_rate = old_rate
            value_change = number(
                -(outgoing_value if outgoing_value is not None else issue_qty * outgoing_rate)
            )
            new_value = number(old_value + value_change)
            valuation_rate = (
                number(new_value / new_qty)
                if new_qty > ZERO
                else (old_rate if new_qty < ZERO else ZERO)
            )

        event_line_id = connection.execute(
            """INSERT INTO inventory_event_lines (
                   event_id,line_number,item_id,warehouse_id,offset_warehouse_id,
                   quantity,unit_cost,additional_cost,source_line_type,source_line_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (
                event_id, line_number, line.item_id, line.warehouse_id,
                line.offset_warehouse_id, line.quantity,
                incoming_rate if line.quantity > ZERO else outgoing_rate,
                line.additional_cost, line.source_line_type, line.source_line_id,
            ),
        ).fetchone()["id"]
        ledger_sequence = connection.execute(
            "SELECT COALESCE(max(sequence_no),0)+1 AS value FROM inventory_ledger_entries WHERE company_id=%s",
            (company_id,),
        ).fetchone()["value"]
        ledger_id = connection.execute(
            """INSERT INTO inventory_ledger_entries (
                   company_id,event_id,event_line_id,item_id,warehouse_id,posting_at,
                   sequence_no,quantity_change,quantity_after,incoming_rate,outgoing_rate,
                   valuation_rate,value_change,value_after,valuation_method,cost_queue)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
               RETURNING id""",
            (
                company_id, event_id, event_line_id, line.item_id, line.warehouse_id,
                posting_at, ledger_sequence, line.quantity, new_qty, incoming_rate,
                outgoing_rate, valuation_rate, value_change, new_value, method,
                json.dumps(cost_queue) if cost_queue is not None else None,
            ),
        ).fetchone()["id"]
        if method == "FIFO" and line.quantity > ZERO:
            connection.execute(
                """INSERT INTO inventory_cost_layers (
                       company_id,item_id,warehouse_id,source_ledger_entry_id,
                       received_at,original_qty,remaining_qty,unit_cost)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    company_id, line.item_id, line.warehouse_id, ledger_id,
                    posting_at, line.quantity, line.quantity, incoming_rate,
                ),
            )
        connection.execute(
            """UPDATE inventory_balances
                  SET quantity=%s,inventory_value=%s,valuation_rate=%s,
                      last_ledger_entry_id=%s,updated_at=now()
                WHERE company_id=%s AND item_id=%s AND warehouse_id=%s""",
            (
                new_qty, new_value, valuation_rate, ledger_id,
                company_id, line.item_id, line.warehouse_id,
            ),
        )
        connection.execute(
            """INSERT INTO item_warehouse_stock (item_id,warehouse_id,quantity,average_cost)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (item_id,warehouse_id) DO UPDATE
               SET quantity=excluded.quantity,average_cost=excluded.average_cost,updated_at=now()""",
            (line.item_id, line.warehouse_id, new_qty, valuation_rate),
        )
        connection.execute(
            "UPDATE items SET stock_qty=(SELECT COALESCE(sum(quantity),0) FROM inventory_balances WHERE item_id=%s),updated_at=now() WHERE id=%s",
            (line.item_id, line.item_id),
        )
        connection.execute(
            """INSERT INTO stock_moves
                   (company_id,item_id,warehouse_id,move_date,direction,qty,unit_cost,ref)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                company_id, line.item_id, line.warehouse_id, event_date,
                "In" if line.quantity > ZERO else "Out", abs(line.quantity),
                incoming_rate if line.quantity > ZERO else outgoing_rate, voucher_code,
            ),
        )

    @staticmethod
    def _incoming_rate(item, line: InventoryLine) -> Decimal:
        if item["valuation_method"] == "Standard Cost":
            if item["standard_cost"] is None:
                raise DomainError(f"Item {line.item_id} has no standard cost")
            return number(item["standard_cost"])
        if line.unit_cost is None or line.unit_cost < ZERO:
            raise DomainError(f"Incoming item {line.item_id} requires a non-negative unit cost")
        return line.unit_cost

    @staticmethod
    def _consume_fifo(
        connection: Connection,
        company_id: int,
        item_id: int,
        warehouse_id: int,
        issue_qty: Decimal,
        fallback_rate: Decimal,
        *,
        allow_shortfall: bool,
    ) -> tuple[Decimal, list[dict[str, str]], Decimal]:
        remaining = issue_qty
        value = ZERO
        consumed: list[dict[str, str]] = []
        layers = connection.execute(
            """SELECT id, remaining_qty, unit_cost FROM inventory_cost_layers
                WHERE company_id=%s AND item_id=%s AND warehouse_id=%s
                  AND remaining_qty > 0 ORDER BY received_at,id FOR UPDATE""",
            (company_id, item_id, warehouse_id),
        ).fetchall()
        for layer in layers:
            if remaining <= ZERO:
                break
            used = min(number(layer["remaining_qty"]), remaining)
            rate = number(layer["unit_cost"])
            connection.execute(
                "UPDATE inventory_cost_layers SET remaining_qty=remaining_qty-%s WHERE id=%s",
                (used, layer["id"]),
            )
            value += used * rate
            remaining -= used
            consumed.append(
                {"layer_id": str(layer["id"]), "quantity": str(used), "rate": str(rate)}
            )
        if remaining > ZERO:
            if not allow_shortfall:
                raise InsufficientStockError(
                    f"FIFO layers are short by {remaining} for item {item_id}"
                )
            value += remaining * fallback_rate
            consumed.append(
                {"layer_id": "negative", "quantity": str(remaining), "rate": str(fallback_rate)}
            )
        exact_value = number(value)
        return number(exact_value / issue_qty), consumed, exact_value

    def reverse_event(
        self,
        event_id: int,
        *,
        voucher_id: int,
        voucher_code: str,
        event_date: date,
        actor: str,
    ) -> int:
        with self.database.transaction() as connection:
            event = connection.execute(
                "SELECT * FROM inventory_events WHERE id=%s FOR UPDATE", (event_id,)
            ).fetchone()
            if not event or event["document_state"] != "Posted":
                raise DomainError("Only a posted inventory event can be reversed")
            rows = connection.execute(
                """SELECT line.*, ledger.valuation_rate
                    FROM inventory_event_lines line
                    JOIN inventory_ledger_entries ledger ON ledger.event_line_id=line.id
                    WHERE line.event_id=%s ORDER BY line.line_number""",
                (event_id,),
            ).fetchall()
            lines = [
                InventoryLine(
                    item_id=row["item_id"], warehouse_id=row["warehouse_id"],
                    quantity=-row["quantity"], unit_cost=row["valuation_rate"],
                    forced_unit_cost=row["valuation_rate"],
                    source_line_type=row["source_line_type"],
                    source_line_id=row["source_line_id"],
                )
                for row in rows
            ]
            reversal = self.post_event(
                company_id=event["company_id"], event_type="Adjustment",
                voucher_type=f"Reversal:{event['voucher_type']}", voucher_id=voucher_id,
                voucher_code=voucher_code, event_date=event_date, lines=lines,
                actor=actor, connection=connection, reverses_event_id=event_id,
            )
            connection.execute(
                "UPDATE inventory_events SET document_state='Cancelled' WHERE id=%s",
                (event_id,),
            )
            return reversal
