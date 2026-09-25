"""Physical warehouse stock, FEFO allocation, and inventory controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json

from psycopg import Connection

from .database import Database
from .errors import DomainError, InsufficientStockError


ZERO = Decimal("0")
SIX = Decimal("0.000001")


def quantity(value) -> Decimal:
    return Decimal(str(value)).quantize(SIX)


@dataclass(frozen=True)
class StockAllocation:
    """One physical quantity; lot and serial belong on the same row."""

    quantity: Decimal
    location_id: int | None = None
    batch_id: int | None = None
    batch_code: str | None = None
    manufactured_on: date | None = None
    expires_on: date | None = None
    serial_number_id: int | None = None
    serial_code: str | None = None
    disposition: str = "Available"
    source_split_id: int | None = None

    def normalized(self) -> "StockAllocation":
        return StockAllocation(
            quantity(self.quantity), self.location_id, self.batch_id,
            self.batch_code.strip() if self.batch_code else None,
            self.manufactured_on, self.expires_on, self.serial_number_id,
            self.serial_code.strip() if self.serial_code else None,
            self.disposition, self.source_split_id,
        )


@dataclass(frozen=True)
class OpeningStockRow:
    item_code: str
    warehouse_code: str
    location_code: str
    quantity: Decimal
    unit_cost: Decimal
    batch_code: str | None = None
    manufactured_on: date | None = None
    expires_on: date | None = None
    serial_code: str | None = None
    disposition: str = "Available"


class WarehouseService:
    """Keep physical detail and warehouse-level inventory ledger in step."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def default_location(connection: Connection, company_id: int, warehouse_id: int) -> int:
        row = connection.execute(
            """INSERT INTO warehouse_locations
                   (company_id,warehouse_id,code,name,location_type,pickable)
               VALUES (%s,%s,'DEFAULT','Default location','Bin',true)
               ON CONFLICT (company_id,warehouse_id,code)
               DO UPDATE SET active=true RETURNING id""",
            (company_id, warehouse_id),
        ).fetchone()
        return row["id"]

    @staticmethod
    def _location(connection, company_id, warehouse_id, location_id):
        if location_id is None:
            location_id = WarehouseService.default_location(
                connection, company_id, warehouse_id
            )
        row = connection.execute(
            """SELECT * FROM warehouse_locations WHERE id=%s AND company_id=%s
               AND warehouse_id=%s AND active=true""",
            (location_id, company_id, warehouse_id),
        ).fetchone()
        if not row:
            raise DomainError("Location is inactive or outside the selected warehouse")
        return row

    @staticmethod
    def _batch(connection, company_id, item_id, allocation, *, inbound):
        batch_id = allocation.batch_id
        if not batch_id and allocation.batch_code:
            row = connection.execute(
                """SELECT * FROM batches WHERE company_id=%s AND item_id=%s
                   AND batch_code=%s FOR UPDATE""",
                (company_id, item_id, allocation.batch_code),
            ).fetchone()
            if row is None and inbound:
                batch_id = connection.execute(
                    """INSERT INTO batches
                       (company_id,item_id,batch_code,manufactured_on,expires_on)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (company_id, item_id, allocation.batch_code,
                     allocation.manufactured_on, allocation.expires_on),
                ).fetchone()["id"]
            elif row:
                batch_id = row["id"]
        if batch_id:
            row = connection.execute(
                """SELECT * FROM batches WHERE id=%s AND company_id=%s AND item_id=%s
                   AND active=true FOR UPDATE""",
                (batch_id, company_id, item_id),
            ).fetchone()
            if not row:
                raise DomainError("Lot does not belong to this company and item")
            for field, supplied in (
                ("manufactured_on", allocation.manufactured_on),
                ("expires_on", allocation.expires_on),
            ):
                if supplied is not None and row[field] != supplied:
                    raise DomainError("Lot dates differ from the recorded lot; use an audited correction")
            return row
        return None

    @staticmethod
    def _serial(connection, company_id, item_id, allocation, batch_id, *, inbound, event_type):
        serial_id = allocation.serial_number_id
        if not serial_id and allocation.serial_code:
            row = connection.execute(
                "SELECT * FROM serial_numbers WHERE company_id=%s AND serial_code=%s FOR UPDATE",
                (company_id, allocation.serial_code),
            ).fetchone()
            if row is None and inbound:
                serial_id = connection.execute(
                    """INSERT INTO serial_numbers
                       (company_id,item_id,serial_code,batch_id,status)
                       VALUES (%s,%s,%s,%s,'Available') RETURNING id""",
                    (company_id, item_id, allocation.serial_code, batch_id),
                ).fetchone()["id"]
            elif row:
                serial_id = row["id"]
        if serial_id:
            row = connection.execute(
                "SELECT * FROM serial_numbers WHERE id=%s AND company_id=%s FOR UPDATE",
                (serial_id, company_id),
            ).fetchone()
            if not row or row["item_id"] != item_id or (batch_id and row["batch_id"] not in (None, batch_id)):
                raise DomainError("Serial number does not match the item and lot")
            if inbound and event_type != "Transfer":
                if row["warehouse_id"] is not None:
                    raise DomainError("Serial number is already in stock")
                if row["status"] == "Delivered" and event_type != "Sales Return":
                    raise DomainError("Delivered serial can only re-enter through a sales return")
            return row
        return None

    @staticmethod
    def _minimum_shelf_life(connection, customer_id):
        if customer_id is None:
            return 0
        row = connection.execute(
            "SELECT min_remaining_shelf_life_days FROM customers WHERE id=%s",
            (customer_id,),
        ).fetchone()
        return row["min_remaining_shelf_life_days"] if row else 0

    @staticmethod
    def _eligible_sql() -> str:
        return """slice.disposition='Available' AND location.pickable=true
          AND location.active=true
          AND (NOT item.tracks_expiry OR batch.expires_on IS NOT NULL)
          AND (batch.id IS NULL OR batch.expires_on IS NULL OR batch.expires_on >= %s)
          AND NOT EXISTS (SELECT 1 FROM lot_holds hold
                          WHERE hold.batch_id=slice.batch_id AND hold.released_at IS NULL)
          AND NOT EXISTS (SELECT 1 FROM temperature_excursions excursion
                          WHERE excursion.zone_id=location.temperature_zone_id
                            AND excursion.status='Open')"""

    def _candidate_slices(self, connection, company_id, item_id, warehouse_id,
                          event_date, customer_id=None, *, outbound_delivery=True):
        minimum = self._minimum_shelf_life(connection, customer_id)
        cutoff = date.fromordinal(event_date.toordinal() + minimum)
        where = self._eligible_sql() if outbound_delivery else "true"
        rows = connection.execute(
            f"""SELECT slice.*,batch.expires_on,location.pickable
                  FROM stock_slices slice
                  JOIN items item ON item.id=slice.item_id
                  JOIN warehouse_locations location ON location.id=slice.location_id
                  LEFT JOIN batches batch ON batch.id=slice.batch_id
                 WHERE slice.company_id=%s AND slice.item_id=%s
                   AND slice.warehouse_id=%s AND slice.quantity>0 AND {where}
                 ORDER BY batch.expires_on ASC NULLS LAST,
                          slice.first_received_at,slice.location_id,slice.id
                 FOR UPDATE OF slice""",
            (company_id, item_id, warehouse_id, cutoff)
            if outbound_delivery else (company_id, item_id, warehouse_id),
        ).fetchall()
        return rows

    def post_physical_line(self, connection: Connection, *, company_id: int,
                           event_type: str, ledger_entry_id: int, line,
                           event_date: date, posting_at: datetime) -> None:
        item = connection.execute(
            """SELECT tracks_batches,tracks_serials,tracks_expiry
                 FROM items WHERE id=%s AND company_id=%s""",
            (line.item_id, company_id),
        ).fetchone()
        if line.quantity > ZERO:
            self._receive_splits(connection, company_id, event_type, ledger_entry_id,
                                 line, item, posting_at)
        else:
            self._issue_splits(connection, company_id, event_type, ledger_entry_id,
                               line, item, event_date)
        ledger = connection.execute(
            "SELECT quantity_after FROM inventory_ledger_entries WHERE id=%s",
            (ledger_entry_id,),
        ).fetchone()
        physical = connection.execute(
            """SELECT COALESCE(sum(quantity),0) AS quantity FROM stock_slices
                 WHERE company_id=%s AND item_id=%s AND warehouse_id=%s""",
            (company_id, line.item_id, line.warehouse_id),
        ).fetchone()["quantity"]
        if quantity(physical) != quantity(ledger["quantity_after"]):
            raise DomainError("Physical stock does not reconcile with the inventory ledger")

    def _receive_splits(self, connection, company_id, event_type, ledger_id,
                        line, item, posting_at):
        allocations = line.allocations
        if not allocations:
            if item["tracks_batches"] or item["tracks_serials"]:
                raise DomainError("Tracked receipts require lot and serial allocations")
            allocations = (StockAllocation(line.quantity,
                                           disposition="QC" if event_type == "Sales Return" else "Available"),)
        if sum((allocation.quantity for allocation in allocations), ZERO) != line.quantity:
            raise DomainError("Physical receipt allocations must total the ledger quantity")
        for allocation in allocations:
            if allocation.quantity <= ZERO:
                raise DomainError("Receipt allocation quantity must be positive")
            location = self._location(connection, company_id, line.warehouse_id,
                                      allocation.location_id)
            batch = self._batch(connection, company_id, line.item_id, allocation,
                                inbound=True)
            batch_id = batch["id"] if batch else None
            if item["tracks_batches"] and not batch_id:
                raise DomainError("Item requires a lot number")
            if item["tracks_expiry"] and (not batch or not batch["expires_on"]):
                raise DomainError("Item requires a lot expiry date")
            serial = self._serial(connection, company_id, line.item_id, allocation,
                                  batch_id, inbound=True, event_type=event_type)
            serial_id = serial["id"] if serial else None
            if item["tracks_serials"] and (not serial_id or allocation.quantity != 1):
                raise DomainError("Serialized receipt requires one serial per unit")
            disposition = ("QC" if event_type == "Sales Return" else
                           "Rejected" if line.source_line_type == "Purchase Receipt Rejected Item"
                           else allocation.disposition)
            if disposition not in {"Available", "QC", "Hold", "Rejected", "Damaged"}:
                raise DomainError("Unknown stock disposition")
            if serial_id and connection.execute(
                """SELECT 1 FROM stock_slices WHERE company_id=%s
                     AND serial_number_id=%s AND quantity>0""",
                (company_id, serial_id),
            ).fetchone():
                raise DomainError("Serial number is already present in physical stock")
            connection.execute(
                """INSERT INTO stock_slices
                   (company_id,item_id,warehouse_id,location_id,batch_id,
                    serial_number_id,disposition,quantity,first_received_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (company_id,item_id,warehouse_id,location_id,
                                batch_id,serial_number_id,disposition)
                   DO UPDATE SET quantity=stock_slices.quantity+excluded.quantity,
                                 updated_at=now()""",
                (company_id, line.item_id, line.warehouse_id, location["id"],
                 batch_id, serial_id, disposition, allocation.quantity, posting_at),
            )
            if serial_id:
                connection.execute(
                    """UPDATE serial_numbers SET warehouse_id=%s,batch_id=COALESCE(batch_id,%s),
                       status=%s,updated_at=now() WHERE id=%s""",
                    (line.warehouse_id, batch_id,
                     "Returned" if event_type == "Sales Return" else "Available", serial_id),
                )
            self._record_split(connection, ledger_id, company_id, line,
                               location["id"], batch_id, serial_id, disposition,
                               allocation.quantity, allocation.source_split_id)

    def _issue_splits(self, connection, company_id, event_type, ledger_id,
                      line, item, event_date):
        remaining = abs(line.quantity)
        delivery = event_type == "Delivery"
        candidates = self._candidate_slices(
            connection, company_id, line.item_id, line.warehouse_id,
            event_date, line.customer_id, outbound_delivery=delivery,
        )
        if delivery and line.sales_order_item_id:
            reserved_ids = {row["stock_slice_id"] for row in connection.execute(
                """SELECT stock_slice_id FROM stock_reservations
                    WHERE sales_order_item_id=%s AND status='Active'""",
                (line.sales_order_item_id,),
            ).fetchall()}
            candidates.sort(key=lambda row: 0 if row["id"] in reserved_ids else 1)
        if line.allocations:
            if sum((a.quantity for a in line.allocations), ZERO) != remaining:
                raise DomainError("Physical issue allocations must total the ledger quantity")
            selected = []
            explicitly_used = {}
            for allocation in line.allocations:
                if allocation.quantity <= ZERO:
                    raise DomainError("Issue allocation quantity must be positive")
                match = next((row for row in candidates
                              if (allocation.location_id is None or row["location_id"] == allocation.location_id)
                              and (allocation.batch_id is None or row["batch_id"] == allocation.batch_id)
                              and (allocation.serial_number_id is None or row["serial_number_id"] == allocation.serial_number_id)
                              and allocation.disposition == row["disposition"]
                              and row["id"] not in explicitly_used
                              and row["quantity"] >= allocation.quantity), None)
                if not match:
                    raise DomainError("Specified stock allocation is unavailable")
                explicitly_used[match["id"]] = (
                    explicitly_used.get(match["id"], ZERO) + allocation.quantity
                )
                selected.append((match, allocation.quantity, allocation.source_split_id))
        else:
            selected = []
            for row in candidates:
                free = quantity(row["quantity"] - row["reserved_qty"])
                if delivery and line.sales_order_item_id:
                    reservation = connection.execute(
                        """SELECT id,quantity,consumed_qty FROM stock_reservations
                           WHERE sales_order_item_id=%s AND stock_slice_id=%s
                             AND status='Active' FOR UPDATE""",
                        (line.sales_order_item_id, row["id"]),
                    ).fetchone()
                    if reservation:
                        free += quantity(reservation["quantity"] - reservation["consumed_qty"])
                used = min(free, remaining)
                if used > ZERO:
                    selected.append((row, used, None))
                    remaining -= used
                if remaining == ZERO:
                    break
            if remaining > ZERO:
                raise InsufficientStockError("No eligible lot/location has enough available stock")
        used_by_slice = {}
        for row, used, source_split_id in selected:
            used_by_slice[row["id"]] = used_by_slice.get(row["id"], ZERO) + used
            if used_by_slice[row["id"]] > row["quantity"]:
                raise InsufficientStockError("Physical stock would become negative")
            reserved_here = ZERO
            if delivery and line.sales_order_item_id:
                reservation = connection.execute(
                    """SELECT * FROM stock_reservations
                       WHERE sales_order_item_id=%s AND stock_slice_id=%s
                         AND status='Active' FOR UPDATE""",
                    (line.sales_order_item_id, row["id"]),
                ).fetchone()
                if reservation:
                    reserved_here = min(used, reservation["quantity"] - reservation["consumed_qty"])
                    consumed = reservation["consumed_qty"] + reserved_here
                    connection.execute(
                        """UPDATE stock_reservations
                           SET consumed_qty=%s,status=%s WHERE id=%s""",
                        (consumed, "Consumed" if consumed == reservation["quantity"] else "Active",
                         reservation["id"]),
                    )
            if used - reserved_here > row["quantity"] - row["reserved_qty"]:
                raise InsufficientStockError("Stock is reserved for another order")
            connection.execute(
                """UPDATE stock_slices
                   SET quantity=quantity-%s,reserved_qty=reserved_qty-%s,updated_at=now()
                   WHERE id=%s""",
                (used, reserved_here, row["id"]),
            )
            if row["serial_number_id"]:
                if used != 1:
                    raise DomainError("Serialized issue requires exactly one unit")
                connection.execute(
                    """UPDATE serial_numbers SET warehouse_id=NULL,status=%s,
                       updated_at=now() WHERE id=%s""",
                    ("Delivered" if delivery else "Consumed", row["serial_number_id"]),
                )
            self._record_split(connection, ledger_id, company_id, line,
                               row["location_id"], row["batch_id"],
                               row["serial_number_id"], row["disposition"],
                               -used, source_split_id)

    @staticmethod
    def _record_split(connection, ledger_id, company_id, line, location_id,
                      batch_id, serial_id, disposition, qty_change, source_split_id):
        connection.execute(
            """INSERT INTO inventory_allocation_splits
               (ledger_entry_id,company_id,item_id,warehouse_id,location_id,
                batch_id,serial_number_id,disposition,quantity_change,source_split_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (ledger_id, company_id, line.item_id, line.warehouse_id,
             location_id, batch_id, serial_id, disposition, qty_change, source_split_id),
        )
        if batch_id or serial_id:
            connection.execute(
                """INSERT INTO inventory_tracking_entries
                   (ledger_entry_id,batch_id,serial_number_id,quantity)
                   VALUES (%s,%s,%s,%s)""",
                (ledger_id, batch_id, serial_id, qty_change),
            )

    def create_location(self, *, company_id: int, warehouse_id: int, code: str,
                        name: str, actor: str, parent_id: int | None = None,
                        location_type: str = "Bin", pickable: bool = True,
                        temperature_zone_id: int | None = None) -> int:
        code = code.strip().upper()
        if not code or not name.strip():
            raise DomainError("Location code and name are required")
        with self.database.transaction() as connection:
            warehouse = connection.execute(
                "SELECT 1 FROM warehouses WHERE id=%s AND company_id=%s AND active=true",
                (warehouse_id, company_id),
            ).fetchone()
            if not warehouse:
                raise DomainError("Warehouse does not belong to company")
            if parent_id:
                self._location(connection, company_id, warehouse_id, parent_id)
            if temperature_zone_id:
                zone = connection.execute(
                    """SELECT 1 FROM temperature_zones
                       WHERE id=%s AND company_id=%s AND warehouse_id=%s AND active=true""",
                    (temperature_zone_id, company_id, warehouse_id),
                ).fetchone()
                if not zone:
                    raise DomainError("Temperature zone does not belong to warehouse")
            row = connection.execute(
                """INSERT INTO warehouse_locations
                   (company_id,warehouse_id,parent_id,temperature_zone_id,code,
                    name,location_type,pickable)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (company_id, warehouse_id, parent_id, temperature_zone_id,
                 code, name.strip(), location_type, pickable),
            ).fetchone()
            return row["id"]

    def reserve_order_line(self, order_line_id: int, *, company_id: int,
                           actor: str,
                           requested_quantity: Decimal | None = None,
                           planned_date: date | None = None,
                           preferred_slice_id: int | None = None,
                           override_reason: str | None = None) -> list[dict]:
        """Reserve eligible physical stock in FEFO order, atomically."""
        if preferred_slice_id is not None and not (override_reason or "").strip():
            raise DomainError("A lot selection override requires an audit reason")
        with self.database.transaction() as connection:
            line = connection.execute(
                """SELECT line.*,ord.company_id,ord.customer_id,ord.delivery_date,
                          ord.document_state,ord.status AS order_status
                     FROM sales_order_items line
                     JOIN sales_orders ord ON ord.id=line.order_id
                    WHERE line.id=%s AND ord.company_id=%s FOR UPDATE OF line""",
                (order_line_id, company_id),
            ).fetchone()
            if not line or line["document_state"] != "Posted" or line["order_status"] in (
                "Closed", "Cancelled", "On Hold"
            ):
                raise DomainError("Only an open posted sales order can reserve stock")
            active = connection.execute(
                """SELECT COALESCE(sum(quantity-consumed_qty),0) AS quantity
                     FROM stock_reservations
                    WHERE sales_order_item_id=%s AND status='Active'""",
                (order_line_id,),
            ).fetchone()["quantity"]
            unreserved = quantity(
                line["qty"] - line["delivered_qty"] + line["returned_qty"] - active
            )
            demand = quantity(requested_quantity if requested_quantity is not None else unreserved)
            if demand <= ZERO or demand > unreserved:
                raise DomainError("Reservation exceeds the unfulfilled order quantity")
            candidates = self._candidate_slices(
                connection, line["company_id"], line["item_id"],
                line["warehouse_id"], planned_date or line["delivery_date"] or date.today(),
                line["customer_id"],
            )
            if preferred_slice_id is not None:
                candidates = [row for row in candidates
                              if row["id"] == preferred_slice_id]
                if not candidates:
                    raise DomainError("Selected stock is ineligible for this customer and date")
            remaining = demand
            allocations = []
            for slice_row in candidates:
                free = quantity(slice_row["quantity"] - slice_row["reserved_qty"])
                take = min(free, remaining)
                if take <= ZERO:
                    continue
                connection.execute(
                    """INSERT INTO stock_reservations
                       (company_id,sales_order_item_id,stock_slice_id,quantity,reserved_by)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (sales_order_item_id,stock_slice_id)
                       WHERE status='Active'
                       DO UPDATE SET quantity=stock_reservations.quantity+excluded.quantity""",
                    (line["company_id"], order_line_id, slice_row["id"], take, actor),
                )
                connection.execute(
                    """UPDATE stock_slices SET reserved_qty=reserved_qty+%s,
                       updated_at=now() WHERE id=%s""",
                    (take, slice_row["id"]),
                )
                allocations.append({"stock_slice_id": slice_row["id"],
                                    "batch_id": slice_row["batch_id"],
                                    "location_id": slice_row["location_id"],
                                    "quantity": take})
                if preferred_slice_id is not None:
                    connection.execute(
                        """INSERT INTO warehouse_allocation_overrides
                           (company_id,sales_order_item_id,stock_slice_id,
                            quantity,actor,reason)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (company_id, order_line_id, slice_row["id"], take,
                         actor, override_reason.strip()),
                    )
                remaining -= take
                if remaining == ZERO:
                    break
            if remaining > ZERO:
                raise InsufficientStockError("Insufficient eligible stock for FEFO reservation")
            return allocations

    @staticmethod
    def _release_slice_reservations(connection, slice_ids, actor, reason,
                                    order_line_id=None):
        if not slice_ids:
            return
        connection.execute(
            "SELECT id FROM stock_slices WHERE id=ANY(%s) ORDER BY id FOR UPDATE",
            (sorted(set(slice_ids)),),
        ).fetchall()
        reservations = connection.execute(
            """SELECT * FROM stock_reservations WHERE stock_slice_id=ANY(%s)
               AND status='Active' AND (%s::bigint IS NULL OR sales_order_item_id=%s)
               ORDER BY stock_slice_id,id FOR UPDATE""",
            (slice_ids, order_line_id, order_line_id),
        ).fetchall()
        for reservation in reservations:
            remaining = reservation["quantity"] - reservation["consumed_qty"]
            connection.execute(
                """UPDATE stock_slices SET reserved_qty=reserved_qty-%s,
                   updated_at=now() WHERE id=%s""",
                (remaining, reservation["stock_slice_id"]),
            )
            connection.execute(
                """UPDATE stock_reservations SET status='Released',released_at=now(),
                   release_reason=%s WHERE id=%s""",
                (f"{reason} ({actor})", reservation["id"]),
            )

    def release_order_reservations(self, order_line_id: int, *, company_id: int,
                                   actor: str,
                                   reason: str) -> None:
        with self.database.transaction() as connection:
            rows = connection.execute(
                """SELECT stock_slice_id FROM stock_reservations
                   WHERE sales_order_item_id=%s AND company_id=%s AND status='Active'""",
                (order_line_id, company_id),
            ).fetchall()
            self._release_slice_reservations(
                connection, [row["stock_slice_id"] for row in rows], actor, reason,
                order_line_id,
            )

    def hold_lot(self, batch_id: int, *, company_id: int,
                 actor: str, reason: str) -> int:
        if not reason.strip():
            raise DomainError("Lot hold requires a reason")
        with self.database.transaction() as connection:
            batch = connection.execute(
                "SELECT company_id FROM batches WHERE id=%s AND company_id=%s FOR UPDATE",
                (batch_id, company_id),
            ).fetchone()
            if not batch:
                raise DomainError("Lot not found")
            hold_id = connection.execute(
                """INSERT INTO lot_holds(company_id,batch_id,reason,created_by)
                   VALUES (%s,%s,%s,%s) RETURNING id""",
                (batch["company_id"], batch_id, reason.strip(), actor),
            ).fetchone()["id"]
            slices = connection.execute(
                "SELECT id FROM stock_slices WHERE batch_id=%s FOR UPDATE", (batch_id,)
            ).fetchall()
            self._release_slice_reservations(
                connection, [row["id"] for row in slices], actor, "Lot held"
            )
            return hold_id

    def release_lot_hold(self, hold_id: int, *, company_id: int,
                         actor: str, reason: str) -> None:
        if not reason.strip():
            raise DomainError("Releasing a lot requires a reason")
        with self.database.transaction() as connection:
            updated = connection.execute(
                """UPDATE lot_holds SET released_at=now(),released_by=%s,
                   release_reason=%s WHERE id=%s AND company_id=%s
                   AND released_at IS NULL RETURNING id""",
                (actor, reason.strip(), hold_id, company_id),
            ).fetchone()
            if not updated:
                raise DomainError("Active lot hold not found")

    def amend_lot_dates(self, batch_id: int, *, company_id: int,
                        manufactured_on: date | None, expires_on: date | None,
                        actor: str, reason: str) -> int:
        """Correct a supplier lot's dates with an immutable before/after record."""
        if not reason.strip():
            raise DomainError("Lot correction requires an audit reason")
        if manufactured_on and expires_on and expires_on < manufactured_on:
            raise DomainError("Expiry cannot be before manufacture date")
        with self.database.transaction() as connection:
            batch = connection.execute(
                """SELECT batch.*,item.tracks_expiry
                     FROM batches batch
                     JOIN items item ON item.id=batch.item_id
                    WHERE batch.id=%s AND batch.company_id=%s FOR UPDATE OF batch""",
                (batch_id, company_id),
            ).fetchone()
            if not batch:
                raise DomainError("Lot not found")
            if batch["tracks_expiry"] and expires_on is None:
                raise DomainError("Expiry controlled item requires an expiry date")
            if (batch["manufactured_on"] == manufactured_on and
                    batch["expires_on"] == expires_on):
                raise DomainError("Lot dates have not changed")
            amendment_id = connection.execute(
                """INSERT INTO warehouse_lot_amendments
                   (company_id,batch_id,old_manufactured_on,new_manufactured_on,
                    old_expires_on,new_expires_on,amended_by,reason)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (company_id, batch_id, batch["manufactured_on"],
                 manufactured_on, batch["expires_on"], expires_on,
                 actor, reason.strip()),
            ).fetchone()["id"]
            connection.execute(
                """UPDATE batches SET manufactured_on=%s,expires_on=%s
                   WHERE id=%s""",
                (manufactured_on, expires_on, batch_id),
            )
            slices = connection.execute(
                """SELECT id FROM stock_slices WHERE company_id=%s AND batch_id=%s
                   ORDER BY id FOR UPDATE""",
                (company_id, batch_id),
            ).fetchall()
            self._release_slice_reservations(
                connection, [row["id"] for row in slices], actor,
                "Lot dates amended; reallocate using current eligibility",
            )
            return amendment_id

    def create_temperature_zone(self, *, company_id: int, warehouse_id: int,
                                code: str, name: str, minimum_c: Decimal,
                                maximum_c: Decimal,
                                reading_interval_hours: int = 24) -> int:
        with self.database.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM warehouses WHERE id=%s AND company_id=%s",
                (warehouse_id, company_id),
            ).fetchone():
                raise DomainError("Warehouse does not belong to company")
            return connection.execute(
                """INSERT INTO temperature_zones
                   (company_id,warehouse_id,code,name,minimum_c,maximum_c,
                    reading_interval_hours)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (company_id, warehouse_id, code.strip().upper(), name.strip(),
                 minimum_c, maximum_c, reading_interval_hours),
            ).fetchone()["id"]

    def record_temperature(self, zone_id: int, *, company_id: int,
                           measured_at: datetime,
                           temperature_c: Decimal, actor: str,
                           note: str | None = None) -> int:
        with self.database.transaction() as connection:
            zone = connection.execute(
                """SELECT * FROM temperature_zones
                   WHERE id=%s AND company_id=%s AND active=true FOR UPDATE""",
                (zone_id, company_id),
            ).fetchone()
            if not zone:
                raise DomainError("Temperature zone not found")
            reading_id = connection.execute(
                """INSERT INTO temperature_readings
                   (company_id,zone_id,measured_at,temperature_c,recorded_by,note)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (zone["company_id"], zone_id, measured_at, temperature_c, actor, note),
            ).fetchone()["id"]
            if not zone["minimum_c"] <= temperature_c <= zone["maximum_c"]:
                connection.execute(
                    """INSERT INTO temperature_excursions
                       (company_id,zone_id,reading_id) VALUES (%s,%s,%s)""",
                    (zone["company_id"], zone_id, reading_id),
                )
                slices = connection.execute(
                    """SELECT slice.id FROM stock_slices slice
                       JOIN warehouse_locations location ON location.id=slice.location_id
                      WHERE location.temperature_zone_id=%s AND slice.quantity>0
                      FOR UPDATE OF slice""",
                    (zone_id,),
                ).fetchall()
                self._release_slice_reservations(
                    connection, [row["id"] for row in slices], actor,
                    "Temperature excursion",
                )
            return reading_id

    def release_temperature_excursion(self, excursion_id: int, *, company_id: int,
                                      actor: str,
                                      reason: str) -> None:
        if not reason.strip():
            raise DomainError("Temperature release requires a review reason")
        with self.database.transaction() as connection:
            updated = connection.execute(
                """UPDATE temperature_excursions
                   SET status='Released',reviewed_by=%s,reviewed_at=now(),
                       review_reason=%s WHERE id=%s AND company_id=%s
                       AND status='Open' RETURNING id""",
                (actor, reason.strip(), excursion_id, company_id),
            ).fetchone()
            if not updated:
                raise DomainError("Open temperature excursion not found")

    def set_customer_shelf_life(self, customer_id: int, *, company_id: int,
                                days: int) -> None:
        if days < 0:
            raise DomainError("Remaining shelf life cannot be negative")
        with self.database.transaction() as connection:
            if not connection.execute(
                """UPDATE customers SET min_remaining_shelf_life_days=%s,
                   updated_at=now() WHERE id=%s AND company_id=%s RETURNING id""",
                (days, customer_id, company_id),
            ).fetchone():
                raise DomainError("Customer not found")

    def transfer_location(self, stock_slice_id: int, *, company_id: int,
                          destination_location_id: int, amount: Decimal,
                          actor: str, reason: str,
                          disposition: str | None = None) -> int:
        """Move unreserved stock within one warehouse without changing valuation."""
        amount = quantity(amount)
        if amount <= ZERO or not reason.strip():
            raise DomainError("Transfer requires a positive quantity and reason")
        with self.database.transaction() as connection:
            source = connection.execute(
                """SELECT * FROM stock_slices WHERE id=%s AND company_id=%s
                   FOR UPDATE""",
                (stock_slice_id, company_id),
            ).fetchone()
            if not source:
                raise DomainError("Source stock not found")
            destination = self._location(
                connection, company_id, source["warehouse_id"],
                destination_location_id,
            )
            target_disposition = disposition or source["disposition"]
            if target_disposition not in {"Available", "QC", "Hold", "Rejected", "Damaged"}:
                raise DomainError("Unknown target disposition")
            if (source["location_id"] == destination["id"] and
                    source["disposition"] == target_disposition):
                raise DomainError("Source and destination are the same")
            if source["quantity"] - source["reserved_qty"] < amount:
                raise InsufficientStockError("Transfer exceeds unreserved stock")
            if source["serial_number_id"] and amount != 1:
                raise DomainError("Transfer a serialized unit as quantity one")
            if target_disposition == "Available":
                if source["batch_id"] and connection.execute(
                    """SELECT 1 FROM lot_holds WHERE batch_id=%s AND released_at IS NULL""",
                    (source["batch_id"],),
                ).fetchone():
                    raise DomainError("Held lot cannot be released to available stock")
                if destination["temperature_zone_id"] and connection.execute(
                    """SELECT 1 FROM temperature_excursions
                       WHERE zone_id=%s AND status='Open'""",
                    (destination["temperature_zone_id"],),
                ).fetchone():
                    raise DomainError("Temperature excursion blocks this location")
                if source["batch_id"] and connection.execute(
                    """SELECT 1 FROM batches
                       WHERE id=%s AND expires_on<current_date""",
                    (source["batch_id"],),
                ).fetchone():
                    raise DomainError("Expired lot cannot be released to available stock")
            connection.execute(
                """UPDATE stock_slices SET quantity=quantity-%s,updated_at=now()
                   WHERE id=%s""",
                (amount, stock_slice_id),
            )
            connection.execute(
                """INSERT INTO stock_slices
                   (company_id,item_id,warehouse_id,location_id,batch_id,
                    serial_number_id,disposition,quantity,first_received_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (company_id,item_id,warehouse_id,location_id,
                                batch_id,serial_number_id,disposition)
                   DO UPDATE SET quantity=stock_slices.quantity+excluded.quantity,
                                 updated_at=now()""",
                (company_id, source["item_id"], source["warehouse_id"],
                 destination["id"], source["batch_id"],
                 source["serial_number_id"], target_disposition, amount,
                 source["first_received_at"]),
            )
            return connection.execute(
                """INSERT INTO warehouse_transfers
                   (company_id,item_id,warehouse_id,from_location_id,to_location_id,
                    batch_id,serial_number_id,from_disposition,to_disposition,
                    quantity,actor,reason)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (company_id, source["item_id"], source["warehouse_id"],
                 source["location_id"], destination["id"], source["batch_id"],
                 source["serial_number_id"], source["disposition"],
                 target_disposition, amount, actor, reason.strip()),
            ).fetchone()["id"]

    def count_stock(self, stock_slice_id: int, *, company_id: int,
                    counted_quantity: Decimal, adjustment_account_id: int | None,
                    actor: str, reason: str, count_date: date | None = None) -> int:
        """Post a counted slice and its valuation difference in one transaction."""
        from .accounting import AccountingService, PostingLine, amount
        from .inventory import InventoryLine, InventoryService

        counted = quantity(counted_quantity)
        if counted < ZERO or not reason.strip():
            raise DomainError("Count requires a nonnegative quantity and reason")
        count_date = count_date or date.today()
        with self.database.transaction() as connection:
            slice_row = connection.execute(
                """SELECT slice.*,item.tracks_serials
                     FROM stock_slices slice
                     JOIN items item ON item.id=slice.item_id
                    WHERE slice.id=%s AND slice.company_id=%s FOR UPDATE OF slice""",
                (stock_slice_id, company_id),
            ).fetchone()
            if not slice_row:
                raise DomainError("Stock slice not found")
            expected = quantity(slice_row["quantity"])
            variance = counted - expected
            if slice_row["tracks_serials"] and counted not in (ZERO, Decimal("1")):
                raise DomainError("Serialized stock count must be zero or one")
            settings = connection.execute(
                """SELECT inventory_account_id FROM company_accounting_settings
                    WHERE company_id=%s""",
                (company_id,),
            ).fetchone()
            if variance and (not settings or not settings["inventory_account_id"]):
                raise DomainError("Inventory account is not configured")
            if variance and not adjustment_account_id:
                raise DomainError("Select an adjustment expense account")
            if adjustment_account_id and not connection.execute(
                """SELECT 1 FROM accounts WHERE id=%s AND company_id=%s
                    AND account_type='Expense' AND active=true""",
                (adjustment_account_id, company_id),
            ).fetchone():
                raise DomainError("Adjustment account must be an active expense account")
            count_id = connection.execute(
                "SELECT nextval(pg_get_serial_sequence('warehouse_stock_counts','id')) AS id"
            ).fetchone()["id"]
            event_id = batch_id = None
            if variance:
                allocation = StockAllocation(
                    abs(variance), location_id=slice_row["location_id"],
                    batch_id=slice_row["batch_id"],
                    serial_number_id=slice_row["serial_number_id"],
                    disposition=slice_row["disposition"],
                )
                balance = connection.execute(
                    """SELECT valuation_rate FROM inventory_balances
                       WHERE company_id=%s AND item_id=%s AND warehouse_id=%s""",
                    (company_id, slice_row["item_id"], slice_row["warehouse_id"]),
                ).fetchone()
                rate = balance["valuation_rate"] if balance else ZERO
                code = f"COUNT-{count_id}"
                event_id = InventoryService(self.database).post_event(
                    company_id=company_id, event_type="Stock Count",
                    voucher_type="Warehouse Stock Count", voucher_id=count_id,
                    voucher_code=code, event_date=count_date,
                    lines=[InventoryLine(
                        slice_row["item_id"], slice_row["warehouse_id"], variance,
                        unit_cost=rate if variance > ZERO else None,
                        source_line_type="Warehouse Stock Count",
                        source_line_id=count_id, allocations=(allocation,),
                    )], actor=actor, connection=connection,
                )
                value_change = connection.execute(
                    """SELECT sum(value_change) AS value FROM inventory_ledger_entries
                        WHERE event_id=%s""",
                    (event_id,),
                ).fetchone()["value"]
                posted_amount = abs(amount(value_change))
                if posted_amount:
                    inventory = settings["inventory_account_id"]
                    if value_change > ZERO:
                        postings = [
                            PostingLine(inventory, debit=posted_amount),
                            PostingLine(adjustment_account_id, credit=posted_amount),
                        ]
                    else:
                        postings = [
                            PostingLine(adjustment_account_id, debit=posted_amount),
                            PostingLine(inventory, credit=posted_amount),
                        ]
                    batch_id = AccountingService(self.database).post_voucher(
                        company_id=company_id, voucher_type="Warehouse Stock Count",
                        voucher_id=count_id, voucher_code=code,
                        posting_date=count_date, lines=postings,
                        actor=actor, connection=connection,
                    )
            connection.execute(
                """INSERT INTO warehouse_stock_counts
                   (id,company_id,stock_slice_id,item_id,warehouse_id,location_id,
                    batch_id,serial_number_id,disposition,expected_qty,counted_qty,
                    variance_qty,adjustment_account_id,inventory_event_id,
                    posting_batch_id,counted_by,reason)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (count_id, company_id, stock_slice_id, slice_row["item_id"],
                 slice_row["warehouse_id"], slice_row["location_id"],
                 slice_row["batch_id"], slice_row["serial_number_id"],
                 slice_row["disposition"], expected, counted, variance,
                 adjustment_account_id, event_id, batch_id, actor, reason.strip()),
            )
            return count_id

    def transfer_warehouse(self, stock_slice_id: int, *, company_id: int,
                           destination_location_id: int, amount: Decimal,
                           actor: str, reason: str,
                           transfer_date: date | None = None) -> int:
        """Move stock and its exact ledger value between warehouses atomically."""
        from .inventory import InventoryLine, InventoryService, number

        amount = quantity(amount)
        if amount <= ZERO or not reason.strip():
            raise DomainError("Transfer requires a positive quantity and reason")
        transfer_date = transfer_date or date.today()
        with self.database.transaction() as connection:
            source = connection.execute(
                """SELECT * FROM stock_slices WHERE id=%s AND company_id=%s
                   FOR UPDATE""",
                (stock_slice_id, company_id),
            ).fetchone()
            if not source:
                raise DomainError("Source stock not found")
            destination = connection.execute(
                """SELECT * FROM warehouse_locations WHERE id=%s AND company_id=%s
                   AND active=true FOR UPDATE""",
                (destination_location_id, company_id),
            ).fetchone()
            if not destination or destination["warehouse_id"] == source["warehouse_id"]:
                raise DomainError("Select a location in another warehouse")
            if source["quantity"] - source["reserved_qty"] < amount:
                raise InsufficientStockError("Transfer exceeds unreserved stock")
            if source["serial_number_id"] and amount != Decimal("1"):
                raise DomainError("Transfer serialized stock one unit at a time")
            transfer_id = connection.execute(
                """SELECT nextval(pg_get_serial_sequence(
                   'warehouse_interwarehouse_transfers','id')) AS id"""
            ).fetchone()["id"]
            inventory = InventoryService(self.database)
            allocation = StockAllocation(
                amount, location_id=source["location_id"],
                batch_id=source["batch_id"],
                serial_number_id=source["serial_number_id"],
                disposition=source["disposition"],
            )
            outbound_event_id = inventory.post_event(
                company_id=company_id, event_type="Transfer",
                voucher_type="Warehouse Transfer Out", voucher_id=transfer_id,
                voucher_code=f"WT-{transfer_id}-OUT", event_date=transfer_date,
                lines=[InventoryLine(
                    source["item_id"], source["warehouse_id"], -amount,
                    source_line_type="Warehouse Transfer",
                    source_line_id=transfer_id, allocations=(allocation,),
                )], actor=actor, connection=connection,
            )
            value_out = -connection.execute(
                """SELECT sum(value_change) AS amount FROM inventory_ledger_entries
                    WHERE event_id=%s""",
                (outbound_event_id,),
            ).fetchone()["amount"]
            rate = number(value_out / amount)
            inbound_event_id = inventory.post_event(
                company_id=company_id, event_type="Transfer",
                voucher_type="Warehouse Transfer In", voucher_id=transfer_id,
                voucher_code=f"WT-{transfer_id}-IN", event_date=transfer_date,
                lines=[InventoryLine(
                    source["item_id"], destination["warehouse_id"], amount,
                    unit_cost=rate, additional_cost=number(value_out - rate * amount),
                    source_line_type="Warehouse Transfer",
                    source_line_id=transfer_id,
                    allocations=(StockAllocation(
                        amount, location_id=destination_location_id,
                        batch_id=source["batch_id"],
                        serial_number_id=source["serial_number_id"],
                        disposition=source["disposition"],
                    ),),
                )], actor=actor, connection=connection,
            )
            value_in = connection.execute(
                """SELECT sum(value_change) AS amount FROM inventory_ledger_entries
                    WHERE event_id=%s""",
                (inbound_event_id,),
            ).fetchone()["amount"]
            if number(value_in) != number(value_out):
                raise DomainError("Warehouse transfer did not preserve inventory value")
            connection.execute(
                """INSERT INTO warehouse_interwarehouse_transfers
                   (id,company_id,item_id,from_warehouse_id,to_warehouse_id,
                    from_location_id,to_location_id,batch_id,serial_number_id,
                    disposition,quantity,outbound_event_id,inbound_event_id,
                    actor,reason)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (transfer_id, company_id, source["item_id"],
                 source["warehouse_id"], destination["warehouse_id"],
                 source["location_id"], destination_location_id,
                 source["batch_id"], source["serial_number_id"],
                 source["disposition"], amount, outbound_event_id,
                 inbound_event_id, actor, reason.strip()),
            )
            return transfer_id

    def import_opening_stock(self, rows: list[OpeningStockRow], *,
                             company_id: int, offset_account_id: int,
                             actor: str, reason: str,
                             opening_date: date | None = None) -> int:
        """Import verified opening lots once, with an offsetting equity posting."""
        from .accounting import AccountingService, PostingLine, amount
        from .inventory import InventoryLine, InventoryService

        if not rows or len(rows) > 1000 or not reason.strip():
            raise DomainError("Opening import needs 1–1000 rows and a reason")
        opening_date = opening_date or date.today()
        canonical = json.dumps([
            {key: str(value) if value is not None else None
             for key, value in row.__dict__.items()} for row in rows
        ], sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(canonical.encode()).hexdigest()
        with self.database.transaction() as connection:
            if connection.execute(
                """SELECT 1 FROM warehouse_opening_imports
                    WHERE company_id=%s AND payload_hash=%s""",
                (company_id, payload_hash),
            ).fetchone():
                raise DomainError("This opening stock file was already imported")
            settings = connection.execute(
                """SELECT inventory_account_id FROM company_accounting_settings
                    WHERE company_id=%s""",
                (company_id,),
            ).fetchone()
            if not settings or not settings["inventory_account_id"]:
                raise DomainError("Inventory account is not configured")
            if not connection.execute(
                """SELECT 1 FROM accounts WHERE id=%s AND company_id=%s
                   AND account_type='Equity' AND active=true""",
                (offset_account_id, company_id),
            ).fetchone():
                raise DomainError("Opening offset must be an active equity account")
            items = {row["code"]: row for row in connection.execute(
                "SELECT id,code FROM items WHERE company_id=%s AND active=true",
                (company_id,),
            ).fetchall()}
            warehouses = {row["code"]: row for row in connection.execute(
                "SELECT id,code FROM warehouses WHERE company_id=%s AND active=true",
                (company_id,),
            ).fetchall()}
            locations = {(row["warehouse_id"], row["code"]): row for row in
                         connection.execute(
                """SELECT id,warehouse_id,code FROM warehouse_locations
                    WHERE company_id=%s AND active=true""", (company_id,)
            ).fetchall()}
            lines = []
            pairs = set()
            for row in rows:
                item = items.get(row.item_code.strip())
                warehouse = warehouses.get(row.warehouse_code.strip())
                location = locations.get((warehouse["id"], row.location_code.strip())) if warehouse else None
                qty = quantity(row.quantity)
                cost = quantity(row.unit_cost)
                if not item or not warehouse or not location or qty <= ZERO or cost < ZERO:
                    raise DomainError("Opening row has unknown codes or invalid quantity/cost")
                pair = (item["id"], warehouse["id"])
                pairs.add(pair)
                lines.append(InventoryLine(
                    item["id"], warehouse["id"], qty, cost,
                    source_line_type="Warehouse Opening Import",
                    allocations=(StockAllocation(
                        qty, location_id=location["id"],
                        batch_code=row.batch_code, manufactured_on=row.manufactured_on,
                        expires_on=row.expires_on, serial_code=row.serial_code,
                        disposition=row.disposition,
                    ),),
                ))
            for item_id, warehouse_id in sorted(pairs):
                if connection.execute(
                    """SELECT 1 FROM inventory_ledger_entries
                        WHERE company_id=%s AND item_id=%s AND warehouse_id=%s
                        LIMIT 1""",
                    (company_id, item_id, warehouse_id),
                ).fetchone():
                    raise DomainError("Opening stock is only allowed before first inventory posting")
            import_id = connection.execute(
                "SELECT nextval(pg_get_serial_sequence('warehouse_opening_imports','id')) AS id"
            ).fetchone()["id"]
            event_id = InventoryService(self.database).post_event(
                company_id=company_id, event_type="Opening",
                voucher_type="Warehouse Opening Import", voucher_id=import_id,
                voucher_code=f"OPEN-{import_id}", event_date=opening_date,
                lines=lines, actor=actor, connection=connection,
            )
            total_value = connection.execute(
                """SELECT sum(value_change) AS value FROM inventory_ledger_entries
                    WHERE event_id=%s""", (event_id,)
            ).fetchone()["value"]
            posted_value = amount(total_value)
            batch_id = None
            if posted_value:
                batch_id = AccountingService(self.database).post_voucher(
                    company_id=company_id, voucher_type="Warehouse Opening Import",
                    voucher_id=import_id, voucher_code=f"OPEN-{import_id}",
                    posting_date=opening_date, actor=actor, connection=connection,
                    lines=[
                        PostingLine(settings["inventory_account_id"], debit=posted_value),
                        PostingLine(offset_account_id, credit=posted_value),
                    ],
                )
            connection.execute(
                """INSERT INTO warehouse_opening_imports
                   (id,company_id,offset_account_id,inventory_event_id,
                    posting_batch_id,payload_hash,row_count,imported_by,reason)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (import_id, company_id, offset_account_id, event_id,
                 batch_id, payload_hash, len(rows), actor, reason.strip()),
            )
            return import_id

    def identify_legacy_stock(self, stock_slice_id: int, *, company_id: int,
                              allocation: StockAllocation, actor: str,
                              reason: str) -> int:
        """Assign verified identity to held legacy stock without changing valuation."""
        allocation = allocation.normalized()
        if allocation.quantity <= ZERO or not reason.strip():
            raise DomainError("Identification requires a positive quantity and reason")
        with self.database.transaction() as connection:
            source = connection.execute(
                """SELECT slice.*,item.tracks_batches,item.tracks_serials,
                          item.tracks_expiry
                     FROM stock_slices slice
                     JOIN items item ON item.id=slice.item_id
                    WHERE slice.id=%s AND slice.company_id=%s FOR UPDATE OF slice""",
                (stock_slice_id, company_id),
            ).fetchone()
            if (not source or source["disposition"] != "Hold"
                    or source["batch_id"] is not None
                    or source["serial_number_id"] is not None
                    or not (source["tracks_batches"] or source["tracks_serials"])):
                raise DomainError("Choose an unidentified held tracked stock slice")
            if source["quantity"] < allocation.quantity:
                raise InsufficientStockError("Identification exceeds held stock")
            location = self._location(
                connection, company_id, source["warehouse_id"],
                allocation.location_id or source["location_id"],
            )
            batch = self._batch(connection, company_id, source["item_id"],
                                allocation, inbound=True)
            batch_id = batch["id"] if batch else None
            if source["tracks_batches"] and not batch_id:
                raise DomainError("Lot code is required")
            if source["tracks_expiry"] and (not batch or not batch["expires_on"]):
                raise DomainError("Expiry date is required")
            serial_id = None
            if source["tracks_serials"]:
                if allocation.quantity != Decimal("1") or not allocation.serial_code:
                    raise DomainError("Identify each serial as one unit")
                serial = connection.execute(
                    """SELECT * FROM serial_numbers WHERE company_id=%s
                       AND serial_code=%s FOR UPDATE""",
                    (company_id, allocation.serial_code),
                ).fetchone()
                if serial:
                    if serial["item_id"] != source["item_id"] or (
                            batch_id and serial["batch_id"] not in (None, batch_id)):
                        raise DomainError("Serial does not match this item and lot")
                    serial_id = serial["id"]
                else:
                    serial_id = connection.execute(
                        """INSERT INTO serial_numbers
                           (company_id,item_id,serial_code,batch_id,status)
                           VALUES (%s,%s,%s,%s,'Available') RETURNING id""",
                        (company_id, source["item_id"], allocation.serial_code,
                         batch_id),
                    ).fetchone()["id"]
                if connection.execute(
                    """SELECT 1 FROM stock_slices
                        WHERE company_id=%s AND serial_number_id=%s AND quantity>0""",
                    (company_id, serial_id),
                ).fetchone():
                    raise DomainError("Serial number is already identified in stock")
            connection.execute(
                """UPDATE stock_slices SET quantity=quantity-%s,updated_at=now()
                    WHERE id=%s""",
                (allocation.quantity, source["id"]),
            )
            connection.execute(
                """INSERT INTO stock_slices
                   (company_id,item_id,warehouse_id,location_id,batch_id,
                    serial_number_id,disposition,quantity,first_received_at)
                   VALUES (%s,%s,%s,%s,%s,%s,'Hold',%s,%s)
                   ON CONFLICT (company_id,item_id,warehouse_id,location_id,
                                batch_id,serial_number_id,disposition)
                   DO UPDATE SET quantity=stock_slices.quantity+excluded.quantity,
                                 updated_at=now()""",
                (company_id, source["item_id"], source["warehouse_id"],
                 location["id"], batch_id, serial_id, allocation.quantity,
                 source["first_received_at"]),
            )
            if serial_id:
                connection.execute(
                    """UPDATE serial_numbers SET warehouse_id=%s,
                       batch_id=COALESCE(batch_id,%s),status='Available',
                       updated_at=now() WHERE id=%s""",
                    (source["warehouse_id"], batch_id, serial_id),
                )
            return connection.execute(
                """INSERT INTO warehouse_stock_identifications
                   (company_id,item_id,warehouse_id,source_stock_slice_id,
                    destination_location_id,batch_id,serial_number_id,
                    quantity,identified_by,reason)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (company_id, source["item_id"], source["warehouse_id"],
                 source["id"], location["id"], batch_id, serial_id,
                 allocation.quantity, actor, reason.strip()),
            ).fetchone()["id"]
