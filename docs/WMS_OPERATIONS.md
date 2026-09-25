# Warehouse inventory operations

The warehouse module uses PostgreSQL and the `fast_erp` schema. Apply migrations
with `.venv/bin/python scripts/migrate_postgres.py --schema fast_erp` after setting
`DB_URL` in the ignored `.env` file. The SQLite demo does not provide lot, serial,
FEFO, temperature or warehouse query features. Do not seed the production schema.

## Set up stock

Configure a company, warehouses, items and the inventory account first. Create
locations from **Locations** and assign temperature zones where needed. Use
**Opening stock import** on Inventory Control only before an item's first
inventory posting in a warehouse. Paste CSV with `item_code,warehouse_code,
location_code,quantity,unit_cost`; add `batch_code`, `manufactured_on`,
`expires_on`, `serial_code` and `disposition` as needed. Each import is audited,
posts its value against an equity account, and rejects a duplicate file.

For new purchases, use the purchase order's **Receive stock** form. Enter the
accepted and rejected quantities, location, lot, expiry and serial details.
Each serial occupies one physical unit. Rejected receipts remain unavailable.

## Control and trace stock

**Inventory Control** shows on hand, available, reserved, held and expiring
stock, recent movement charts, reorder alerts and temperature attention. The
warehouse and date filters scope the movement charts. Open **Stock by location
and lot** to move stock, change its status or post a cycle count. Counts post
the inventory variance and an offsetting expense entry in one transaction.
**Allocation blockers** lists open order lines whose unreserved demand exceeds
eligible stock; it distinguishes a shelf-life cutoff from a general shortage.

The **FEFO reservations** page allocates the earliest eligible expiry for each
sales order. Set each customer's minimum remaining shelf life in **Customers**.
Use **Choose lot** only when a specific eligible lot is required; the selected
slice, quantity, operator and reason are retained in an immutable override log.
Held, expired, QC, rejected and temperature affected stock is excluded from
delivery. Physical lot selection is separate from inventory cost valuation.
Returned sales stock enters QC. Use a lot's trace page to review receipts,
movements, reservations, deliveries and returns, or to place or release a lot
hold. A held legacy slice can be identified against a verified label; it
remains held until a separate release action.
Correct supplier lot dates on the lot trace page with a reason and source
evidence. Corrections retain the old and new dates and release any affected
reservations so they can be checked against current shelf life rules.

Enter zone temperatures on **Temperature control**. Out of range readings hold
affected stock and release its reservations. Review the affected stock list,
then record a reason to release an excursion. No scanner is required.

## Inventory queries

**Inventory Query Lab** accepts a question through the configured BYOK model
or a SQL `SELECT`. Queries can read only the four tenant scoped `wms_*_report`
views, run in a read only transaction, time out after five seconds and return
at most 500 rows. Charts use returned numeric columns. Use the lot trace and
stock reconciliation indicator for operational checks before changing data.
