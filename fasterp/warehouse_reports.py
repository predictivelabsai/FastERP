"""Company-scoped warehouse dashboards and lot trace queries."""

from __future__ import annotations

from datetime import date, timedelta

from .database import Database
from .errors import DomainError


class WarehouseReports:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _scope(connection, company_id: int) -> None:
        connection.execute("SELECT set_config('fasterp.company_id',%s,true)",
                           (str(company_id),))

    def dashboard(self, company_id: int, warehouse_code: str | None = None,
                  *, start_date: date | None = None,
                  end_date: date | None = None) -> dict:
        end_date = end_date or date.today()
        start_date = start_date or end_date - timedelta(days=30)
        if start_date > end_date:
            raise DomainError("Start date must be on or before end date")
        with self.database.transaction() as connection:
            self._scope(connection, company_id)
            params = (warehouse_code,) if warehouse_code else ()
            where = "WHERE warehouse_code=%s" if warehouse_code else ""
            totals = connection.execute(
                f"""SELECT COALESCE(sum(on_hand),0) AS on_hand,
                          COALESCE(sum(available),0) AS available,
                          COALESCE(sum(reserved),0) AS reserved,
                          COALESCE(sum(held),0) AS held
                     FROM wms_stock_report {where}""", params,
            ).fetchone()
            expiry = connection.execute(
                f"""SELECT COALESCE(sum(on_hand) FILTER
                           (WHERE expiry_date BETWEEN current_date AND current_date+7),0) AS days_7,
                          COALESCE(sum(on_hand) FILTER
                           (WHERE expiry_date BETWEEN current_date AND current_date+30),0) AS days_30,
                          COALESCE(sum(on_hand) FILTER
                           (WHERE expiry_date BETWEEN current_date AND current_date+90),0) AS days_90
                     FROM wms_stock_report {where}""", params,
            ).fetchone()
            daily = connection.execute(
                f"""SELECT event_date,
                          sum(quantity_change) FILTER (WHERE quantity_change>0) AS incoming,
                          -sum(quantity_change) FILTER (WHERE quantity_change<0) AS outgoing
                     FROM wms_movement_report
                     {where} {'AND' if where else 'WHERE'} event_date BETWEEN %s AND %s
                       AND event_type<>'Transfer'
                    GROUP BY event_date ORDER BY event_date""", (*params, start_date, end_date),
            ).fetchall()
            by_warehouse = connection.execute(
                f"""SELECT warehouse_code,sum(on_hand) AS on_hand,
                          sum(available) AS available,sum(held) AS held
                     FROM wms_stock_report {where} GROUP BY warehouse_code
                    ORDER BY warehouse_code""", params,
            ).fetchall()
            low_stock = connection.execute(
                """SELECT item.code,item.name,
                          COALESCE(sum(balance.quantity),0) AS stock_qty,
                          item.reorder_level
                     FROM items item
                     LEFT JOIN inventory_balances balance
                       ON balance.item_id=item.id AND balance.company_id=item.company_id
                      AND (%s::text IS NULL OR balance.warehouse_id IN (
                           SELECT id FROM warehouses WHERE company_id=%s AND code=%s))
                    WHERE item.company_id=%s AND item.inventory_item=true
                    GROUP BY item.id,item.code,item.name,item.reorder_level
                   HAVING COALESCE(sum(balance.quantity),0)<=item.reorder_level
                    ORDER BY COALESCE(sum(balance.quantity),0)-item.reorder_level,
                             item.code LIMIT 30""",
                (warehouse_code, company_id, warehouse_code, company_id),
            ).fetchall()
            blocked = connection.execute(
                """SELECT count(*) AS count FROM sales_order_items line
                     JOIN sales_orders ord ON ord.id=line.order_id
                     JOIN items item ON item.id=line.item_id
                    WHERE ord.company_id=%s AND ord.document_state='Posted'
                      AND ord.status NOT IN ('Closed','Cancelled')
                      AND item.inventory_item=true
                      AND (%s::text IS NULL OR line.warehouse_id IN (
                           SELECT id FROM warehouses WHERE company_id=%s AND code=%s))
                      AND line.qty-line.delivered_qty+line.returned_qty >
                          COALESCE((SELECT sum(res.quantity-res.consumed_qty)
                                      FROM stock_reservations res
                                     WHERE res.sales_order_item_id=line.id
                                       AND res.status='Active'),0)""",
                (company_id, warehouse_code, company_id, warehouse_code),
            ).fetchone()["count"]
            allocation_blockers = connection.execute(
                """WITH demand AS (
                       SELECT line.id AS order_line_id,ord.id AS order_id,
                              ord.code AS order_code,item.code AS item_code,
                              warehouse.code AS warehouse_code,
                              line.qty-line.delivered_qty+line.returned_qty
                                -COALESCE(reserved.quantity,0) AS needed,
                              GREATEST(COALESCE(ord.delivery_date,current_date),
                                       current_date)
                                +COALESCE(customer.min_remaining_shelf_life_days,0)
                                  AS minimum_expiry
                         FROM sales_order_items line
                         JOIN sales_orders ord ON ord.id=line.order_id
                         JOIN items item ON item.id=line.item_id
                         JOIN warehouses warehouse ON warehouse.id=line.warehouse_id
                         LEFT JOIN customers customer ON customer.id=ord.customer_id
                         LEFT JOIN LATERAL (
                              SELECT sum(quantity-consumed_qty) AS quantity
                                FROM stock_reservations reservation
                               WHERE reservation.sales_order_item_id=line.id
                                 AND reservation.status='Active'
                         ) reserved ON true
                        WHERE ord.company_id=%s AND ord.document_state='Posted'
                          AND ord.status NOT IN ('Closed','Cancelled','On Hold')
                          AND item.inventory_item=true
                          AND (%s::text IS NULL OR warehouse.code=%s)
                     ), availability AS (
                       SELECT demand.*,
                              COALESCE((SELECT sum(stock.available)
                                          FROM wms_stock_report stock
                                         WHERE stock.item_code=demand.item_code
                                           AND stock.warehouse_code=demand.warehouse_code),0)
                                  AS available_now,
                              COALESCE((SELECT sum(stock.available)
                                          FROM wms_stock_report stock
                                         WHERE stock.item_code=demand.item_code
                                           AND stock.warehouse_code=demand.warehouse_code
                                           AND (stock.expiry_date IS NULL OR
                                                stock.expiry_date>=demand.minimum_expiry)),0)
                                  AS eligible
                         FROM demand WHERE demand.needed>0
                     )
                     SELECT *,count(*) OVER () AS total_blockers
                       FROM availability WHERE needed>eligible
                      ORDER BY order_id,order_line_id LIMIT 30""",
                (company_id, warehouse_code, warehouse_code),
            ).fetchall()
            zone_rows = connection.execute(
                """SELECT zone.id,zone.code,zone.name,zone.reading_interval_hours,
                          max(reading.measured_at) AS last_reading,
                          bool_or(excursion.status='Open') AS has_excursion
                     FROM temperature_zones zone
                     LEFT JOIN temperature_readings reading ON reading.zone_id=zone.id
                     LEFT JOIN temperature_excursions excursion ON excursion.zone_id=zone.id
                    WHERE zone.company_id=%s AND zone.active=true
                      AND (%s::text IS NULL OR zone.warehouse_id IN (
                           SELECT id FROM warehouses WHERE company_id=%s AND code=%s))
                    GROUP BY zone.id,zone.code,zone.name,zone.reading_interval_hours
                    ORDER BY zone.code""",
                (company_id, warehouse_code, company_id, warehouse_code),
            ).fetchall()
            warehouses = connection.execute(
                """SELECT code,name FROM warehouses WHERE company_id=%s AND active=true
                   ORDER BY code""", (company_id,)
            ).fetchall()
            return {
                "totals": totals, "expiry": expiry, "daily": daily,
                "by_warehouse": by_warehouse, "low_stock": low_stock,
                "blocked_orders": blocked,
                "allocation_blockers": allocation_blockers,
                "zones": zone_rows,
                "warehouses": warehouses,
            }

    def stock_rows(self, company_id: int, *, warehouse_code: str | None = None,
                   filter_name: str = "all", days: int = 30,
                   limit: int = 200) -> list[dict]:
        if days not in (7, 30, 90):
            raise DomainError("Expiry horizon must be 7, 30 or 90 days")
        filters = {
            "all": "true", "available": "available>0", "held": "held>0",
            "expiring": "expiry_date BETWEEN current_date AND current_date+%s",
            "low": "available<=0 AND on_hand>0",
        }
        if filter_name not in filters:
            raise DomainError("Unknown stock filter")
        with self.database.transaction() as connection:
            self._scope(connection, company_id)
            return connection.execute(
                f"""SELECT * FROM wms_stock_report
                    WHERE {filters[filter_name]}
                      AND (%s::text IS NULL OR warehouse_code=%s)
                    ORDER BY expiry_date NULLS LAST,item_code,location_code
                    LIMIT %s""",
                ((days,) if filter_name == "expiring" else ())
                + (warehouse_code, warehouse_code, min(limit, 500)),
            ).fetchall()

    def movements(self, company_id: int, *, warehouse_code: str | None = None,
                  limit: int = 200) -> list[dict]:
        with self.database.transaction() as connection:
            self._scope(connection, company_id)
            return connection.execute(
                """SELECT * FROM wms_movement_report
                    WHERE (%s::text IS NULL OR warehouse_code=%s)
                    ORDER BY event_date DESC,movement_id DESC LIMIT %s""",
                (warehouse_code, warehouse_code, min(limit, 500)),
            ).fetchall()

    def lot_trace(self, company_id: int, batch_id: int) -> dict:
        with self.database.transaction() as connection:
            self._scope(connection, company_id)
            lot = connection.execute(
                "SELECT * FROM wms_lot_report WHERE batch_id=%s", (batch_id,)
            ).fetchone()
            if not lot:
                raise DomainError("Lot not found")
            movements = connection.execute(
                """SELECT event.event_date,event.event_type,event.voucher_code,
                          warehouse.code AS warehouse_code,
                          location.code AS location_code,split.disposition,
                          split.quantity_change,customer.name AS customer_name,
                          supplier.name AS supplier_name,split.id AS sort_id
                     FROM inventory_allocation_splits split
                     JOIN inventory_ledger_entries ledger ON ledger.id=split.ledger_entry_id
                     JOIN inventory_events event ON event.id=ledger.event_id
                     JOIN warehouse_locations location ON location.id=split.location_id
                     JOIN warehouses warehouse ON warehouse.id=split.warehouse_id
                     LEFT JOIN sales_deliveries delivery ON event.voucher_type IN
                          ('Sales Delivery','Sales Return') AND event.voucher_id=delivery.id
                     LEFT JOIN customers customer ON customer.id=delivery.customer_id
                     LEFT JOIN purchase_receipts receipt ON event.voucher_type IN
                          ('Purchase Receipt','Purchase Return') AND event.voucher_id=receipt.id
                     LEFT JOIN suppliers supplier ON supplier.id=receipt.supplier_id
                    WHERE split.company_id=%s AND split.batch_id=%s
                    UNION ALL
                   SELECT transfer.occurred_at::date,'Transfer',
                          'WT-' || transfer.id::text,warehouse.code,
                          source.code,transfer.from_disposition,-transfer.quantity,
                          NULL::text,NULL::text,-(transfer.id*2)
                     FROM warehouse_transfers transfer
                     JOIN warehouses warehouse ON warehouse.id=transfer.warehouse_id
                     JOIN warehouse_locations source ON source.id=transfer.from_location_id
                    WHERE transfer.company_id=%s AND transfer.batch_id=%s
                    UNION ALL
                   SELECT transfer.occurred_at::date,'Transfer',
                          'WT-' || transfer.id::text,warehouse.code,
                          destination.code,transfer.to_disposition,transfer.quantity,
                          NULL::text,NULL::text,-(transfer.id*2+1)
                     FROM warehouse_transfers transfer
                     JOIN warehouses warehouse ON warehouse.id=transfer.warehouse_id
                     JOIN warehouse_locations destination ON destination.id=transfer.to_location_id
                    WHERE transfer.company_id=%s AND transfer.batch_id=%s
                    UNION ALL
                   SELECT identification.identified_at::date,'Identification',
                          'IDENT-' || identification.id::text,warehouse.code,
                          location.code,'Hold',identification.quantity,
                          NULL::text,NULL::text,-(identification.id*2+1000000000)
                     FROM warehouse_stock_identifications identification
                     JOIN warehouses warehouse ON warehouse.id=identification.warehouse_id
                     JOIN warehouse_locations location
                       ON location.id=identification.destination_location_id
                    WHERE identification.company_id=%s AND identification.batch_id=%s
                    UNION ALL
                   SELECT reservation.reserved_at::date,
                          'Reservation ' || reservation.quantity::text ||
                          ' / ' || reservation.status,
                          ord.code,warehouse.code,location.code,
                          slice.disposition,0::numeric,
                          customer.name,NULL::text,-(reservation.id*2+2000000000)
                     FROM stock_reservations reservation
                     JOIN stock_slices slice ON slice.id=reservation.stock_slice_id
                     JOIN warehouses warehouse ON warehouse.id=slice.warehouse_id
                     JOIN warehouse_locations location ON location.id=slice.location_id
                     JOIN sales_order_items order_line
                       ON order_line.id=reservation.sales_order_item_id
                     JOIN sales_orders ord ON ord.id=order_line.order_id
                     JOIN customers customer ON customer.id=ord.customer_id
                    WHERE reservation.company_id=%s AND slice.batch_id=%s
                    ORDER BY event_date,sort_id""",
                (company_id, batch_id, company_id, batch_id,
                 company_id, batch_id, company_id, batch_id,
                 company_id, batch_id),
            ).fetchall()
            locations = connection.execute(
                """SELECT warehouse.code AS warehouse_code,
                          location.code AS location_code,slice.disposition,
                          sum(slice.quantity) AS quantity
                     FROM stock_slices slice
                     JOIN warehouses warehouse ON warehouse.id=slice.warehouse_id
                     JOIN warehouse_locations location ON location.id=slice.location_id
                    WHERE slice.company_id=%s AND slice.batch_id=%s AND slice.quantity>0
                    GROUP BY warehouse.code,location.code,slice.disposition
                    ORDER BY warehouse.code,location.code""",
                (company_id, batch_id),
            ).fetchall()
            holds = connection.execute(
                """SELECT id,reason,created_by,created_at FROM lot_holds
                    WHERE company_id=%s AND batch_id=%s AND released_at IS NULL
                    ORDER BY created_at DESC""",
                (company_id, batch_id),
            ).fetchall()
            amendments = connection.execute(
                """SELECT old_manufactured_on,new_manufactured_on,
                          old_expires_on,new_expires_on,amended_by,reason,amended_at
                     FROM warehouse_lot_amendments
                    WHERE company_id=%s AND batch_id=%s
                    ORDER BY amended_at DESC LIMIT 50""",
                (company_id, batch_id),
            ).fetchall()
            return {"lot": lot, "movements": movements,
                    "locations": locations, "holds": holds,
                    "amendments": amendments}

    def reconciliation(self, company_id: int) -> list[dict]:
        with self.database.transaction() as connection:
            return connection.execute(
                """SELECT item.code AS item_code,warehouse.code AS warehouse_code,
                          balance.quantity AS ledger_quantity,
                          COALESCE(sum(slice.quantity),0) AS physical_quantity
                     FROM inventory_balances balance
                     JOIN items item ON item.id=balance.item_id
                     JOIN warehouses warehouse ON warehouse.id=balance.warehouse_id
                     LEFT JOIN stock_slices slice ON slice.company_id=balance.company_id
                       AND slice.item_id=balance.item_id AND slice.warehouse_id=balance.warehouse_id
                    WHERE balance.company_id=%s
                    GROUP BY item.code,warehouse.code,balance.quantity
                   HAVING balance.quantity<>COALESCE(sum(slice.quantity),0)
                    ORDER BY item.code,warehouse.code""",
                (company_id,),
            ).fetchall()

    def reservation_lines(self, company_id: int) -> list[dict]:
        with self.database.transaction() as connection:
            return connection.execute(
                """SELECT line.id AS order_line_id,ord.id AS order_id,
                          ord.code AS order_code,
                          item.code AS item_code,item.name AS item_name,
                          warehouse.code AS warehouse_code,
                          customer.name AS customer_name,
                          line.qty-line.delivered_qty+line.returned_qty AS remaining,
                          COALESCE((SELECT sum(res.quantity-res.consumed_qty)
                                      FROM stock_reservations res
                                     WHERE res.sales_order_item_id=line.id
                                       AND res.status='Active'),0) AS reserved
                     FROM sales_order_items line
                     JOIN sales_orders ord ON ord.id=line.order_id
                     JOIN items item ON item.id=line.item_id
                     JOIN warehouses warehouse ON warehouse.id=line.warehouse_id
                     LEFT JOIN customers customer ON customer.id=ord.customer_id
                    WHERE ord.company_id=%s AND ord.document_state='Posted'
                      AND ord.status NOT IN ('Closed','Cancelled','On Hold')
                      AND line.qty-line.delivered_qty+line.returned_qty>0
                    ORDER BY ord.delivery_date NULLS LAST,ord.id,line.line_number
                    LIMIT 200""",
                (company_id,),
            ).fetchall()
