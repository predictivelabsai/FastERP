"""Serial and batch traceability attached to immutable inventory ledger rows."""

from __future__ import annotations

from collections import defaultdict, deque
from decimal import Decimal

from fasterp.errors import DomainError


def apply_tracking(connection, event_id: int, source_lines: list[dict]) -> None:
    """Validate and persist source serial/batch allocations for one event."""

    queues = defaultdict(deque)
    for line in source_lines:
        queues[str(line.get("item_code"))].append(line)
    ledger_rows = connection.execute(
        """SELECT ledger.id,ledger.quantity_change,item.id AS item_id,item.code,
                  item.tracks_serials,item.tracks_batches,event_line.warehouse_id
             FROM inventory_ledger_entries ledger
             JOIN inventory_event_lines event_line ON event_line.id=ledger.event_line_id
             JOIN items item ON item.id=ledger.item_id
            WHERE ledger.event_id=%s ORDER BY event_line.line_number""",
        (event_id,),
    ).fetchall()
    for ledger in ledger_rows:
        candidates = queues.get(str(ledger["code"]))
        source = candidates.popleft() if candidates else {}
        serials = source.get("serials") or []
        batches = source.get("batches") or []
        quantity = Decimal(str(ledger["quantity_change"]))
        absolute = abs(quantity)
        direction = Decimal("1") if quantity > 0 else Decimal("-1")
        if ledger["tracks_serials"]:
            if absolute != absolute.to_integral_value() or len(serials) != int(absolute):
                raise DomainError(
                    f"Serial allocation count does not match quantity for {ledger['code']}"
                )
        if ledger["tracks_batches"]:
            batch_total = sum(
                (Decimal(str(row.get("quantity") or 0)) for row in batches),
                Decimal("0"),
            )
            if batch_total != absolute:
                raise DomainError(
                    f"Batch allocation quantity does not match quantity for {ledger['code']}"
                )
        batch_ids = {}
        for row in batches:
            batch = connection.execute(
                """INSERT INTO batches
                       (company_id,item_id,batch_code,manufactured_on,expires_on)
                   SELECT event.company_id,%s,%s,%s,%s
                     FROM inventory_events event WHERE event.id=%s
                   ON CONFLICT (company_id,item_id,batch_code) DO UPDATE SET
                       manufactured_on=COALESCE(excluded.manufactured_on,batches.manufactured_on),
                       expires_on=COALESCE(excluded.expires_on,batches.expires_on),active=true
                   RETURNING id""",
                (
                    ledger["item_id"], row["code"], row.get("manufactured_on"),
                    row.get("expires_on"), event_id,
                ),
            ).fetchone()["id"]
            batch_ids[row["code"]] = batch
            connection.execute(
                """INSERT INTO inventory_tracking_entries
                       (ledger_entry_id,batch_id,quantity) VALUES (%s,%s,%s)""",
                (ledger["id"], batch, direction * Decimal(str(row["quantity"]))),
            )
        for row in serials:
            batch_id = batch_ids.get(row.get("batch_code"))
            status = "Available" if direction > 0 else "Delivered"
            serial = connection.execute(
                """INSERT INTO serial_numbers
                       (company_id,item_id,serial_code,batch_id,warehouse_id,status,
                        warranty_expires_on)
                   SELECT event.company_id,%s,%s,%s,%s,%s,%s
                     FROM inventory_events event WHERE event.id=%s
                   ON CONFLICT (company_id,serial_code) DO UPDATE SET
                       item_id=excluded.item_id,batch_id=COALESCE(excluded.batch_id,serial_numbers.batch_id),
                       warehouse_id=excluded.warehouse_id,status=excluded.status,
                       warranty_expires_on=COALESCE(excluded.warranty_expires_on,
                                                   serial_numbers.warranty_expires_on),
                       updated_at=now()
                   RETURNING id""",
                (
                    ledger["item_id"], row["code"], batch_id,
                    ledger["warehouse_id"] if direction > 0 else None,
                    status, row.get("warranty_expires_on"), event_id,
                ),
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO inventory_tracking_entries
                       (ledger_entry_id,serial_number_id,quantity)
                   VALUES (%s,%s,%s)""",
                (ledger["id"], serial, direction),
            )
