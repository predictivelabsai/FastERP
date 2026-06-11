"""Generate a synthetic FastERP database (deterministic, no PII)."""
from __future__ import annotations

import random
from datetime import timedelta

import db

RNG = random.Random(20260611)
TODAY = db.TODAY

CUST_PREFIX = ["Northwind", "Apex", "Lumen", "Helios", "Vertex", "Cobalt", "Bluewave", "Sterling",
               "Quanta", "Evergreen", "Meridian", "Ironclad", "Granite", "Harbor", "Juniper",
               "Keystone", "Monarch", "Orbit", "Polaris", "Summit"]
CUST_SUFFIX = ["Retail", "Industries", "Logistics", "Manufacturing", "Trading", "Group", "Supply Co"]
TERRITORIES = ["UK", "DACH", "Nordics", "Iberia", "Benelux", "North America", "APAC"]
ITEM_NAMES = {
    "Raw Material": ["Steel Sheet", "Aluminium Bar", "Copper Wire", "Resin Pellets", "Glass Panel"],
    "Components": ["Control Board", "Power Module", "Sensor Array", "Hydraulic Valve", "Bearing Set"],
    "Finished Goods": ["Desk Lamp", "Office Chair", "Smart Thermostat", "Water Pump", "LED Panel", "Air Purifier"],
    "Consumables": ["Lubricant 5L", "Cleaning Kit", "Filter Cartridge", "Adhesive Tube"],
    "Packaging": ["Carton Box L", "Bubble Wrap Roll", "Pallet Wrap", "Shipping Label Pack"],
}
UOMS = ["Nos", "Box", "Kg", "Roll", "Set", "Litre"]


def _d(days):
    return (TODAY + timedelta(days=days)).isoformat()


def build():
    db.init_schema()
    with db.cursor() as conn:
        for t in ("chat_messages", "stock_moves", "invoices", "sales_order_items", "sales_orders", "items", "customers"):
            conn.execute(f"DELETE FROM {t}")

    # customers
    custs, seen = [], set()
    while len(custs) < 24:
        nm = f"{RNG.choice(CUST_PREFIX)} {RNG.choice(CUST_SUFFIX)}"
        if nm in seen:
            continue
        seen.add(nm)
        custs.append((nm, RNG.choice(TERRITORIES), RNG.choice([10000, 25000, 50000, 100000, 250000]), _d(-RNG.randint(60, 900))))
    with db.cursor() as conn:
        conn.executemany("INSERT INTO customers(name,territory,credit_limit,created) VALUES (?,?,?,?)", custs)
        cust_ids = [r[0] for r in conn.execute("SELECT id FROM customers").fetchall()]

    # items
    items = []
    code_n = 1000
    for grp, names in ITEM_NAMES.items():
        for nm in names:
            code_n += 1
            rate = round(RNG.uniform(4, 900), 2)
            stock = RNG.randint(0, 600)
            reorder = RNG.choice([20, 40, 60, 80])
            items.append((f"ITM-{code_n}", nm, grp, RNG.choice(UOMS), rate, stock, reorder))
    with db.cursor() as conn:
        conn.executemany("INSERT INTO items(code,name,item_group,uom,rate,stock_qty,reorder_level) VALUES (?,?,?,?,?,?,?)", items)
        item_rows = conn.execute("SELECT id,rate FROM items").fetchall()

    # sales orders + line items
    status_weights = [("Draft", 8), ("Confirmed", 16), ("Delivered", 14), ("Invoiced", 26), ("Closed", 30), ("Cancelled", 6)]
    statuses = [s for s, w in status_weights for _ in range(w)]
    so_n = 5000
    orders, all_lines = [], []
    for _ in range(70):
        so_n += 1
        cust = RNG.choice(cust_ids)
        odate = -RNG.randint(2, 180)
        status = RNG.choice(statuses)
        n_lines = RNG.randint(1, 5)
        lines = []
        total = 0
        for _ in range(n_lines):
            it = RNG.choice(item_rows)
            qty = RNG.randint(1, 40)
            rate = round(it["rate"] * RNG.uniform(0.95, 1.1), 2)
            amount = round(qty * rate, 2)
            total += amount
            lines.append((it["id"], qty, rate, amount))
        orders.append((f"SO-{so_n}", cust, _d(odate), _d(odate + RNG.randint(3, 21)), status, round(total, 2)))
        all_lines.append(lines)
    with db.cursor() as conn:
        conn.executemany(
            "INSERT INTO sales_orders(code,customer_id,order_date,delivery_date,status,total) VALUES (?,?,?,?,?,?)", orders)
        order_rows = conn.execute("SELECT id,customer_id,order_date,status,total FROM sales_orders ORDER BY id").fetchall()
        for o, lines in zip(order_rows, all_lines):
            for (item_id, qty, rate, amount) in lines:
                conn.execute("INSERT INTO sales_order_items(order_id,item_id,qty,rate,amount) VALUES (?,?,?,?,?)",
                             (o["id"], item_id, qty, rate, amount))

    # invoices (for Invoiced/Closed orders)
    inv_n = 7000
    invoices = []
    moves = []
    for o in order_rows:
        if o["status"] in ("Invoiced", "Closed", "Delivered"):
            inv_n += 1
            idate = o["order_date"]
            due = (db.date.fromisoformat(idate) + timedelta(days=30)).isoformat()
            total = o["total"]
            # payment status
            if o["status"] == "Closed":
                paid, status = total, "Paid"
            else:
                roll = RNG.random()
                overdue = due < TODAY.isoformat()
                if roll < 0.4:
                    paid, status = total, "Paid"
                elif roll < 0.6:
                    paid, status = round(total * RNG.uniform(0.3, 0.6), 2), "Partly Paid"
                else:
                    paid, status = 0, ("Overdue" if overdue else "Unpaid")
                if status in ("Partly Paid", "Unpaid") and overdue and RNG.random() < 0.6:
                    status = "Overdue"
            invoices.append((f"INV-{inv_n}", o["id"], o["customer_id"], idate, due, total, paid, status))
        # stock out-moves for delivered/invoiced/closed
        if o["status"] in ("Delivered", "Invoiced", "Closed"):
            for it in db.rows("SELECT item_id, qty FROM sales_order_items WHERE order_id=?", (o["id"],)):
                moves.append((it["item_id"], o["order_date"], "Out", it["qty"], o["status"]))
    # some inbound stock moves (replenishment)
    for it in item_rows:
        for _ in range(RNG.randint(1, 3)):
            moves.append((it["id"], _d(-RNG.randint(1, 120)), "In", RNG.randint(50, 300), "Purchase Receipt"))
    with db.cursor() as conn:
        conn.executemany(
            "INSERT INTO invoices(code,order_id,customer_id,invoice_date,due_date,total,paid,status) VALUES (?,?,?,?,?,?,?,?)", invoices)
        conn.executemany("INSERT INTO stock_moves(item_id,move_date,direction,qty,ref) VALUES (?,?,?,?,?)", moves)

    print(f"FastERP seeded → {db.DB_PATH}")
    print(f"  {len(custs)} customers · {len(items)} items · {len(orders)} sales orders · "
          f"{len(invoices)} invoices · {len(moves)} stock moves")


if __name__ == "__main__":
    build()
