# FastERP warehouse inventory plan

## Scope and baseline

Build inventory control on PostgreSQL in the `fast_erp` schema. Support any
number of warehouses, locations, SKUs, and users through configuration; no
scanner or pick/pack/ship workflow is required for the first release. Every
stocked item may require lot, serial, expiry, temperature, and customer
remaining-shelf-life controls. Temperature readings are entered manually;
customer shelf-life minima are set once per customer. The fresh production
schema has migrations 0001–0013 and no company or stock data. Do not add
synthetic seed data there.

An illustrative SME workload is 500 SKUs, 100 orders and 250 order lines per
day, based on a published small-warehouse example. Another SME profile places
dedicated warehouses at 100–500 SKUs and 30–150 orders per day. These are test
fixtures, not limits. Include larger performance fixtures and concurrent users.

Sources: [Shelfbot example configurations](https://shelfbot.com/docs/example-configurations),
[InventoryFlow SME profiles](https://inventoryflow.co/ecommerce-order-management-system/warehouse-management-system/).

## Current gaps

The PostgreSQL ledger and warehouse balances in `fasterp/inventory.py` are a
sound accounting foundation, but live receipts and deliveries do not carry
location, lot or serial allocations. `inventory_tracking_entries` are used by
the import path only; imported batch and serial rows can describe the same
physical unit twice. The SQLite fallback has only item-wide stock totals.
FIFO cost layers must remain distinct from physical FEFO selection.

## Target data model

| Entity | Purpose and key constraints |
| --- | --- |
| `warehouse_locations` | Company and warehouse scoped hierarchy with stable code, type, pickable flag, active flag, and optional temperature zone. Reject cycles and cross-warehouse parent links. |
| `stock_slices` | Rebuildable on-hand projection keyed by company, item, warehouse, location, lot, serial and disposition. Quantity is nonnegative; serialized slices hold zero or one unit. |
| `inventory_allocation_splits` | Immutable physical detail for each inventory ledger line: signed quantity, location, lot, optional serial, disposition, and source document line. Split sums equal the ledger quantity. One row carries both lot and serial when both apply. |
| `stock_reservations` | Order line, stock slice, reserved quantity, state, expiry and actor. Active reservations cannot exceed eligible stock. |
| `temperature_zones`, `temperature_readings`, `temperature_excursions` | Configured range, time-stamped manual readings, operator and notes; link out-of-range readings to affected locations and held stock. |
| `customers.min_remaining_shelf_life_days` | One nonnegative customer-level minimum, checked against each expiry-controlled item at planned dispatch. |
| `lot_holds` | Reason, actor, start/end and audit history; a lot hold excludes every matching slice from availability and FEFO. |

Keep `inventory_ledger_entries` and `inventory_balances` as the financial and
warehouse-level record. Physical movements update the new split ledger and
slice projection in the same transaction as the existing posting and GL.
Location/status transfers create paired signed physical movements with no net
company stock or inventory value change. Supplier lot identity and expiry
corrections require audited amendments; existing codes must not silently
change meaning. Enforce same-company and same-item references in service code
and database constraints.

## Allocation policy

Calculate available quantity as on-hand minus active reservations. Exclude QC,
hold, rejected, damaged and expired stock. For expiry-controlled items, select
eligible lots by earliest expiry, then receipt time, location and stable ID;
split across lots when required. Check the customer's minimum remaining shelf
life against planned dispatch or delivery date. Use receipt order for items
without expiry. Lock candidate slices and reservations in a consistent order
and retry serialization conflicts. A deliberate override must record reason,
actor and selected lot. FEFO chooses physical stock; FIFO or moving-average
costing remains a separate valuation rule.

## Inventory dashboard

Add an Inventory Control dashboard to the existing FastERP shell. Filter by
company, warehouse and date. Show on-hand, available, reserved and held units;
lots expiring in 7/30/90 days; stock below reorder level; and orders blocked by
FEFO or customer shelf life. Add manual temperature readings due and out of
range when temperature control is delivered in phase 4. Each card opens the
underlying item, lot, location and source documents.
Include a lot trace view showing receipt, transfers, reservations, deliveries,
returns and current balance. Read figures from the stock projections and show
a reconciliation indicator against the inventory ledger. No scanner controls
are needed.

## Delivery sequence

1. Add locations, physical splits, stock slices and invariants in migration
   0014. Extend `InventoryLine` and normal posting to require physical detail
   for tracked items. Add reconciliation against warehouse balances.
2. Add partial receipt, putaway, transfer, stock adjustment and cycle-count
   forms. Capture manufacture/expiry dates, serials, lot and disposition.
   Returns enter QC or hold until released.
3. Add reservation and FEFO service, the Inventory Control dashboard,
   near-expiry alerts, customer shelf-life validation and lot quarantine/recall
   reports. Integrate reservations with sales delivery without adding scanning.
4. Add temperature configuration, manual readings, out-of-range alerts,
   affected-stock review and release workflow. An out-of-range reading
   automatically holds stock in the affected zone until an authorized review
   releases it; record the reading, operator, decision and reason.
5. Add controlled opening-stock import and reconciliation for any existing
   deployments. Unidentified tracked stock enters hold. Keep the new empty
   production schema free of synthetic demo records.

## Acceptance checks

Receive two lots with different expiry dates into different locations; reserve
the earliest eligible lot; exclude a held or expired lot; enforce a customer's
remaining-shelf-life threshold; prevent concurrent over-reservation; return
the exact shipped lot to QC; and trace a recalled lot from receipt through
customer delivery. After every operation, physical slice totals must match
the warehouse ledger, and inventory value must match posted accounting.

Reference patterns: [ERPNext serial and batch transaction bundles](https://docs.frappe.io/erpnext/serial-and-batch-bundle),
[InvenTree stock items and locations](https://docs.inventree.org/en/latest/stock/),
[OpenBoxes FEFO picking](https://docs.openboxes.com/en/latest/api-guide/outbound/stockMovementStatus/),
and [OCA warehouse modules](https://github.com/OCA/wms).
