"""FastERP data layer — SQLite, an Order-to-Cash + Inventory slice of ERPNext.

ERPNext is ~527 doctypes; FastERP models the Selling + Stock vertical: items,
customers, sales orders (+ line items), sales invoices (with AR aging), and
stock movements. All synthetic.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

DB_PATH = os.getenv("FASTERP_DB") or str(Path(__file__).parent / "fasterp.sqlite")
TODAY = date(2026, 6, 11)

ORDER_STATUSES = ["Draft", "Confirmed", "Delivered", "Invoiced", "Closed", "Cancelled"]
OPEN_ORDER = ["Confirmed", "Delivered"]
INVOICE_STATUSES = ["Unpaid", "Partly Paid", "Paid", "Overdue"]
ITEM_GROUPS = ["Raw Material", "Components", "Finished Goods", "Consumables", "Packaging"]
# estimated cost ratio for COGS posting (no per-item cost field in this slice)
COGS_RATIO = 0.6


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def cursor():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def db_exists() -> bool:
    p = Path(DB_PATH)
    return p.exists() and p.stat().st_size > 0


def rows(sql, params=()):
    with cursor() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql, params=()):
    with cursor() as conn:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None


def scalar(sql, params=()):
    with cursor() as conn:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    territory     TEXT,
    credit_limit  REAL,
    created       TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    code          TEXT,
    name          TEXT NOT NULL,
    item_group    TEXT,
    uom           TEXT,
    rate          REAL,
    stock_qty     REAL NOT NULL DEFAULT 0,
    reorder_level REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sales_orders (
    id            INTEGER PRIMARY KEY,
    code          TEXT,
    customer_id   INTEGER REFERENCES customers(id),
    order_date    TEXT,
    delivery_date TEXT,
    status        TEXT NOT NULL DEFAULT 'Draft',
    total         REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sales_order_items (
    id            INTEGER PRIMARY KEY,
    order_id      INTEGER REFERENCES sales_orders(id),
    item_id       INTEGER REFERENCES items(id),
    qty           REAL,
    rate          REAL,
    amount        REAL
);
CREATE TABLE IF NOT EXISTS invoices (
    id            INTEGER PRIMARY KEY,
    code          TEXT,
    order_id      INTEGER REFERENCES sales_orders(id),
    customer_id   INTEGER REFERENCES customers(id),
    invoice_date  TEXT,
    due_date      TEXT,
    total         REAL,
    paid          REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'Unpaid'
);
CREATE TABLE IF NOT EXISTS stock_moves (
    id            INTEGER PRIMARY KEY,
    item_id       INTEGER REFERENCES items(id),
    move_date     TEXT,
    direction     TEXT,           -- 'In' | 'Out'
    qty           REAL,
    ref           TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id            INTEGER PRIMARY KEY,
    thread_id     TEXT NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL,
    created       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppliers (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    territory     TEXT,
    created       TEXT
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id            INTEGER PRIMARY KEY,
    code          TEXT,
    supplier_id   INTEGER REFERENCES suppliers(id),
    order_date    TEXT,
    status        TEXT NOT NULL DEFAULT 'Draft',   -- Draft | Ordered | Received | Cancelled
    total         REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS purchase_order_items (
    id            INTEGER PRIMARY KEY,
    po_id         INTEGER REFERENCES purchase_orders(id),
    item_id       INTEGER REFERENCES items(id),
    qty           REAL,
    rate          REAL,
    amount        REAL
);
CREATE TABLE IF NOT EXISTS gl_entries (
    id            INTEGER PRIMARY KEY,
    entry_date    TEXT,
    account       TEXT NOT NULL,    -- Accounts Receivable | Sales Revenue | Cash | Inventory | Accounts Payable
    debit         REAL NOT NULL DEFAULT 0,
    credit        REAL NOT NULL DEFAULT 0,
    ref           TEXT              -- e.g. INV-7001, PO-6001
);
CREATE TABLE IF NOT EXISTS accounts (
    code          TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    account_type  TEXT NOT NULL,
    normal_side   TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS currencies (
    code          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    rate_to_gbp   REAL NOT NULL,
    updated       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS business_units (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    region        TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    customer_id   INTEGER REFERENCES customers(id),
    business_unit_id INTEGER REFERENCES business_units(id),
    status        TEXT NOT NULL,
    budget        REAL NOT NULL DEFAULT 0,
    start_date    TEXT,
    end_date      TEXT
);
CREATE TABLE IF NOT EXISTS tax_codes (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    rate          REAL NOT NULL,
    recoverable   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS expenses (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    expense_date  TEXT NOT NULL,
    supplier_id   INTEGER REFERENCES suppliers(id),
    category      TEXT NOT NULL,
    description   TEXT,
    net_amount    REAL NOT NULL,
    tax_amount    REAL NOT NULL DEFAULT 0,
    total         REAL NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'GBP',
    exchange_rate REAL NOT NULL DEFAULT 1,
    business_unit_id INTEGER REFERENCES business_units(id),
    project_id    INTEGER REFERENCES projects(id),
    status        TEXT NOT NULL DEFAULT 'Posted',
    note          TEXT
);
CREATE TABLE IF NOT EXISTS journal_entries (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    entry_date    TEXT NOT NULL,
    memo          TEXT,
    status        TEXT NOT NULL DEFAULT 'Posted'
);
CREATE TABLE IF NOT EXISTS journal_lines (
    id            INTEGER PRIMARY KEY,
    journal_id    INTEGER NOT NULL REFERENCES journal_entries(id),
    account       TEXT NOT NULL,
    debit         REAL NOT NULL DEFAULT 0,
    credit        REAL NOT NULL DEFAULT 0,
    business_unit_id INTEGER REFERENCES business_units(id),
    project_id    INTEGER REFERENCES projects(id)
);
CREATE TABLE IF NOT EXISTS transaction_links (
    id            INTEGER PRIMARY KEY,
    source_ref    TEXT NOT NULL,
    target_ref    TEXT NOT NULL,
    link_type     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY,
    entity_type   TEXT NOT NULL,
    entity_id     INTEGER NOT NULL,
    filename      TEXT NOT NULL,
    path          TEXT NOT NULL,
    note          TEXT,
    created       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_fields (
    id            INTEGER PRIMARY KEY,
    entity_type   TEXT NOT NULL,
    entity_id     INTEGER NOT NULL,
    field_name    TEXT NOT NULL,
    field_value   TEXT
);
CREATE INDEX IF NOT EXISTS idx_so_status ON sales_orders(status);
CREATE INDEX IF NOT EXISTS idx_inv_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_soi_order ON sales_order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_poi_po ON purchase_order_items(po_id);
CREATE INDEX IF NOT EXISTS idx_gl_account ON gl_entries(account);
"""

# Chart of accounts (sign: +1 debit-normal, -1 credit-normal)
ACCOUNTS = {
    "Bank": 1, "Cash": 1, "Accounts Receivable": 1, "Inventory": 1,
    "Prepaid Expenses": 1, "Equipment": 1, "Accounts Payable": -1,
    "Sales Tax Payable": -1, "Accrued Expenses": -1, "Owner's Equity": -1,
    "Retained Earnings": -1, "Sales Revenue": -1, "Service Revenue": -1,
    "Other Income": -1, "Cost of Goods Sold": 1, "Payroll Expense": 1,
    "Rent Expense": 1, "Software Expense": 1, "Travel Expense": 1,
    "Marketing Expense": 1, "Professional Fees": 1, "Utilities Expense": 1,
}
OPERATING_EXPENSES = {
    "Payroll Expense", "Rent Expense", "Software Expense", "Travel Expense",
    "Marketing Expense", "Professional Fees", "Utilities Expense",
}


def init_schema():
    with cursor() as conn:
        conn.executescript(SCHEMA)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(gl_entries)")}
        for name, ddl in (
            ("business_unit_id", "INTEGER REFERENCES business_units(id)"),
            ("project_id", "INTEGER REFERENCES projects(id)"),
            ("currency", "TEXT NOT NULL DEFAULT 'GBP'"),
            ("exchange_rate", "REAL NOT NULL DEFAULT 1"),
            ("memo", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE gl_entries ADD COLUMN {name} {ddl}")


def kpis() -> dict:
    paid = scalar("SELECT COALESCE(SUM(paid),0) FROM invoices") or 0
    receivable = scalar("SELECT COALESCE(SUM(total-paid),0) FROM invoices WHERE status!='Paid'") or 0
    overdue = scalar("SELECT COALESCE(SUM(total-paid),0) FROM invoices WHERE status='Overdue'") or 0
    inv_value = scalar("SELECT COALESCE(SUM(stock_qty*rate),0) FROM items") or 0
    open_q = ",".join("?" * len(OPEN_ORDER))
    return {
        "revenue": paid,
        "receivable": receivable,
        "overdue": overdue,
        "inventory_value": inv_value,
        "open_orders": scalar(f"SELECT COUNT(*) FROM sales_orders WHERE status IN ({open_q})", tuple(OPEN_ORDER)) or 0,
        "low_stock": scalar("SELECT COUNT(*) FROM items WHERE stock_qty <= reorder_level") or 0,
        "customers": scalar("SELECT COUNT(*) FROM customers") or 0,
    }


def orders_by_status():
    out = []
    for s in ORDER_STATUSES:
        r = one("SELECT COUNT(*) n, COALESCE(SUM(total),0) v FROM sales_orders WHERE status=?", (s,))
        out.append({"status": s, "count": r["n"], "value": r["v"]})
    return out


def sales_order(oid):
    return one("""SELECT so.*, c.name customer, c.territory FROM sales_orders so
                  LEFT JOIN customers c ON c.id=so.customer_id WHERE so.id=?""", (oid,))


def order_items(oid):
    return rows("""SELECT soi.*, i.name item, i.code FROM sales_order_items soi
                   LEFT JOIN items i ON i.id=soi.item_id WHERE soi.order_id=?""", (oid,))


def invoice_for_order(oid):
    return one("SELECT * FROM invoices WHERE order_id=?", (oid,))


# --- order-to-cash transactions ---------------------------------------------

def next_action(order):
    """The next step in the O2C flow for an order, or None."""
    return {"Draft": "confirm", "Confirmed": "deliver",
            "Delivered": "invoice"}.get(order["status"])


def confirm_order(oid) -> bool:
    o = sales_order(oid)
    if not o or o["status"] != "Draft":
        return False
    with cursor() as conn:
        conn.execute("UPDATE sales_orders SET status='Confirmed' WHERE id=?", (oid,))
    return True


def deliver_order(oid) -> bool:
    """Confirmed → Delivered: decrement stock and write stock-out moves."""
    o = sales_order(oid)
    if not o or o["status"] != "Confirmed":
        return False
    with cursor() as conn:
        for li in conn.execute("SELECT item_id, qty FROM sales_order_items WHERE order_id=?", (oid,)).fetchall():
            conn.execute("UPDATE items SET stock_qty = MAX(0, stock_qty - ?) WHERE id=?", (li["qty"], li["item_id"]))
            conn.execute("INSERT INTO stock_moves(item_id,move_date,direction,qty,ref) VALUES (?,date('now'),'Out',?,?)",
                         (li["item_id"], li["qty"], o["code"]))
        conn.execute("UPDATE sales_orders SET status='Delivered' WHERE id=?", (oid,))
    return True


def invoice_order(oid) -> int | None:
    """Delivered → Invoiced: raise a Sales Invoice (Unpaid, due in 30 days)."""
    o = sales_order(oid)
    if not o or o["status"] != "Delivered" or invoice_for_order(oid):
        return None
    with cursor() as conn:
        n = (conn.execute("SELECT COALESCE(MAX(id),7000) FROM invoices").fetchone()[0]) + 1
        conn.execute(
            """INSERT INTO invoices(code,order_id,customer_id,invoice_date,due_date,total,paid,status)
               VALUES (?,?,?,date('now'),date('now','+30 day'),?,0,'Unpaid')""",
            (f"INV-{n}", oid, o["customer_id"], o["total"]))
        inv_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        code = conn.execute("SELECT code FROM invoices WHERE id=?", (inv_id,)).fetchone()[0]
        conn.execute("UPDATE sales_orders SET status='Invoiced' WHERE id=?", (oid,))
    # GL (outside the write txn): book revenue and relieve inventory at est. cost
    cogs = round(o["total"] * COGS_RATIO, 2)
    post_gl([("Accounts Receivable", o["total"], 0), ("Sales Revenue", 0, o["total"])], ref=code)
    post_gl([("Cost of Goods Sold", cogs, 0), ("Inventory", 0, cogs)], ref=code)
    return inv_id


def record_payment(invoice_id, amount: float) -> bool:
    inv = one("SELECT * FROM invoices WHERE id=?", (invoice_id,))
    if not inv or amount <= 0:
        return False
    pay = min(inv["total"] - inv["paid"], amount)
    paid = inv["paid"] + pay
    # tolerate sub-£1 rounding so a "pay in full" (rounded) settles the invoice
    status = "Paid" if paid >= inv["total"] - 1.0 else "Partly Paid"
    with cursor() as conn:
        conn.execute("UPDATE invoices SET paid=?, status=? WHERE id=?", (paid, status, invoice_id))
        # Fully paid invoice closes its order
        if status == "Paid" and inv["order_id"]:
            conn.execute("UPDATE sales_orders SET status='Closed' WHERE id=?", (inv["order_id"],))
    # GL: debit Cash / credit Accounts Receivable (outside the write txn above)
    post_gl([("Cash", pay, 0), ("Accounts Receivable", 0, pay)], ref=inv["code"])
    return True


# --- general ledger (double-entry) ------------------------------------------

def post_gl(lines, ref: str, entry_date: str | None = None) -> bool:
    """Post a balanced journal. `lines` is a list of (account, debit, credit).

    Raises nothing on imbalance — callers build balanced tuples — but skips a
    no-op (all-zero) entry. Uses its own transaction; never call inside another
    open `with cursor()` block (would self-deadlock on the SQLite write lock).
    """
    lines = [(a, round(d, 2), round(c, 2)) for a, d, c in lines if (d or c)]
    if not lines:
        return False
    with cursor() as conn:
        for account, debit, credit in lines:
            conn.execute(
                "INSERT INTO gl_entries(entry_date,account,debit,credit,ref) "
                "VALUES (COALESCE(?,date('now')),?,?,?,?)",
                (entry_date, account, debit, credit, ref))
    return True


def trial_balance():
    """One row per account: total debits, credits and the signed balance."""
    out = []
    for account, normal in ACCOUNTS.items():
        r = one("SELECT COALESCE(SUM(debit),0) d, COALESCE(SUM(credit),0) c "
                "FROM gl_entries WHERE account=?", (account,))
        d, c = r["d"], r["c"]
        # present the balance on the account's normal side as a positive number
        bal = (d - c) * normal
        out.append({"account": account, "debit": d, "credit": c,
                    "normal": "Dr" if normal > 0 else "Cr", "balance": bal})
    return out


def gl_entries(account: str | None = None, limit: int = 200):
    if account:
        return rows("SELECT * FROM gl_entries WHERE account=? ORDER BY id DESC LIMIT ?",
                    (account, limit))
    return rows("SELECT * FROM gl_entries ORDER BY id DESC LIMIT ?", (limit,))


def gl_totals():
    r = one("SELECT COALESCE(SUM(debit),0) d, COALESCE(SUM(credit),0) c FROM gl_entries")
    return {"debit": r["d"], "credit": r["c"], "balanced": abs(r["d"] - r["c"]) < 0.01}


# --- buying (procure-to-stock) ----------------------------------------------

def suppliers():
    return rows("""SELECT s.*,
                     (SELECT COUNT(*) FROM purchase_orders po WHERE po.supplier_id=s.id) po_count,
                     (SELECT COALESCE(SUM(total),0) FROM purchase_orders po WHERE po.supplier_id=s.id) spend
                   FROM suppliers s ORDER BY s.name""")


def supplier(sid):
    return one("SELECT * FROM suppliers WHERE id=?", (sid,))


def create_supplier(name: str, territory: str = "") -> int:
    with cursor() as conn:
        conn.execute("INSERT INTO suppliers(name,territory,created) VALUES (?,?,date('now'))",
                     (name.strip(), territory.strip()))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


PO_STATUSES = ["Draft", "Ordered", "Received", "Cancelled"]


def purchase_orders(status: str | None = None):
    sql = """SELECT po.*, s.name supplier FROM purchase_orders po
             LEFT JOIN suppliers s ON s.id=po.supplier_id"""
    if status:
        return rows(sql + " WHERE po.status=? ORDER BY po.id DESC", (status,))
    return rows(sql + " ORDER BY po.id DESC")


def purchase_order(pid):
    return one("""SELECT po.*, s.name supplier, s.territory FROM purchase_orders po
                  LEFT JOIN suppliers s ON s.id=po.supplier_id WHERE po.id=?""", (pid,))


def po_items(pid):
    return rows("""SELECT poi.*, i.name item, i.code FROM purchase_order_items poi
                   LEFT JOIN items i ON i.id=poi.item_id WHERE poi.po_id=?""", (pid,))


def create_po(supplier_id, lines, status: str = "Ordered") -> int | None:
    """lines: list of (item_id, qty, rate). Computes amounts + total."""
    lines = [(int(i), float(q), float(r)) for i, q, r in lines if float(q) > 0]
    if not supplier_id or not lines:
        return None
    total = sum(q * r for _, q, r in lines)
    with cursor() as conn:
        n = (conn.execute("SELECT COALESCE(MAX(id),6000) FROM purchase_orders").fetchone()[0]) + 1
        conn.execute(
            "INSERT INTO purchase_orders(code,supplier_id,order_date,status,total) "
            "VALUES (?,?,date('now'),?,?)", (f"PO-{n}", supplier_id, status, total))
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for item_id, qty, rate in lines:
            conn.execute(
                "INSERT INTO purchase_order_items(po_id,item_id,qty,rate,amount) "
                "VALUES (?,?,?,?,?)", (pid, item_id, qty, rate, qty * rate))
    return pid


def receive_po(pid) -> bool:
    """Ordered → Received: increment stock, write 'In' moves, post GL
    (debit Inventory / credit Accounts Payable)."""
    po = purchase_order(pid)
    if not po or po["status"] != "Ordered":
        return False
    with cursor() as conn:
        for li in conn.execute("SELECT item_id, qty FROM purchase_order_items WHERE po_id=?",
                               (pid,)).fetchall():
            conn.execute("UPDATE items SET stock_qty = stock_qty + ? WHERE id=?",
                         (li["qty"], li["item_id"]))
            conn.execute("INSERT INTO stock_moves(item_id,move_date,direction,qty,ref) "
                         "VALUES (?,date('now'),'In',?,?)", (li["item_id"], li["qty"], po["code"]))
        conn.execute("UPDATE purchase_orders SET status='Received' WHERE id=?", (pid,))
    # GL posting outside the write txn (avoids self-deadlock)
    post_gl([("Inventory", po["total"], 0), ("Accounts Payable", 0, po["total"])], ref=po["code"])
    return True


# --- accounting workspace --------------------------------------------------

def accounting_kpis():
    balances = {r["account"]: r["balance"] for r in trial_balance()}
    revenue = sum(balances.get(a, 0) for a in ("Sales Revenue", "Service Revenue", "Other Income"))
    expenses = sum(balances.get(a, 0) for a in OPERATING_EXPENSES)
    return {
        "cash": balances.get("Cash", 0) + balances.get("Bank", 0),
        "receivable": balances.get("Accounts Receivable", 0),
        "payable": balances.get("Accounts Payable", 0),
        "revenue": revenue,
        "expenses": expenses,
        "net_income": revenue - expenses - balances.get("Cost of Goods Sold", 0),
        "tax_payable": balances.get("Sales Tax Payable", 0),
    }


def account_rows():
    return rows("""SELECT a.*, COALESCE(SUM(g.debit),0) debit,
                          COALESCE(SUM(g.credit),0) credit
                   FROM accounts a LEFT JOIN gl_entries g ON g.account=a.name
                   GROUP BY a.code ORDER BY a.code""")


def expense_rows(limit=200):
    return rows("""SELECT e.*, s.name supplier, b.name business_unit, p.name project
                   FROM expenses e
                   LEFT JOIN suppliers s ON s.id=e.supplier_id
                   LEFT JOIN business_units b ON b.id=e.business_unit_id
                   LEFT JOIN projects p ON p.id=e.project_id
                   ORDER BY e.expense_date DESC, e.id DESC LIMIT ?""", (limit,))


def create_expense(supplier_id, category, description, net_amount, tax_code_id,
                   currency="GBP", business_unit_id=None, project_id=None, note=""):
    net = round(float(net_amount), 2)
    tax = one("SELECT * FROM tax_codes WHERE id=?", (tax_code_id,)) if tax_code_id else None
    tax_amount = round(net * (tax["rate"] if tax else 0) / 100, 2)
    fx = one("SELECT rate_to_gbp FROM currencies WHERE code=?", (currency,)) or {"rate_to_gbp": 1}
    total = net + tax_amount
    with cursor() as conn:
        n = (conn.execute("SELECT COALESCE(MAX(id),0) FROM expenses").fetchone()[0]) + 8001
        code = f"EXP-{n}"
        conn.execute(
            """INSERT INTO expenses(code,expense_date,supplier_id,category,description,
               net_amount,tax_amount,total,currency,exchange_rate,business_unit_id,
               project_id,note) VALUES (?,date('now'),?,?,?,?,?,?,?,?,?,?,?)""",
            (code, supplier_id or None, category, description, net, tax_amount, total,
             currency, fx["rate_to_gbp"], business_unit_id or None, project_id or None, note))
        expense_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    base_net, base_tax = round(net * fx["rate_to_gbp"], 2), round(tax_amount * fx["rate_to_gbp"], 2)
    lines = [(category, base_net, 0), ("Accounts Payable", 0, base_net + base_tax)]
    if base_tax:
        lines.append(("Sales Tax Payable", base_tax, 0))
    with cursor() as conn:
        for account, debit, credit in lines:
            conn.execute(
                """INSERT INTO gl_entries
                   (entry_date,account,debit,credit,ref,business_unit_id,project_id,
                    currency,exchange_rate,memo)
                   VALUES (date('now'),?,?,?,?,?,?,?,?,?)""",
                (account, debit, credit, code, business_unit_id or None, project_id or None,
                 currency, fx["rate_to_gbp"], description))
    return expense_id


def create_journal(entry_date, memo, lines):
    """Create a balanced manual journal from (account, debit, credit, unit, project)."""
    clean = [(a, round(float(d or 0), 2), round(float(c or 0), 2), u or None, p or None)
             for a, d, c, u, p in lines if a and (float(d or 0) or float(c or 0))]
    if len(clean) < 2 or abs(sum(x[1] for x in clean) - sum(x[2] for x in clean)) >= 0.01:
        return None
    with cursor() as conn:
        n = (conn.execute("SELECT COALESCE(MAX(id),0) FROM journal_entries").fetchone()[0]) + 9001
        code = f"JE-{n}"
        conn.execute("INSERT INTO journal_entries(code,entry_date,memo) VALUES (?,?,?)",
                     (code, entry_date or TODAY.isoformat(), memo))
        jid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for account, debit, credit, unit, project in clean:
            conn.execute("""INSERT INTO journal_lines
                            (journal_id,account,debit,credit,business_unit_id,project_id)
                            VALUES (?,?,?,?,?,?)""", (jid, account, debit, credit, unit, project))
            conn.execute("""INSERT INTO gl_entries
                            (entry_date,account,debit,credit,ref,business_unit_id,project_id,memo)
                            VALUES (?,?,?,?,?,?,?,?)""",
                         (entry_date or TODAY.isoformat(), account, debit, credit, code,
                          unit, project, memo))
    return jid


def journal_rows(limit=100):
    return rows("""SELECT j.*, COUNT(l.id) lines, SUM(l.debit) total
                   FROM journal_entries j LEFT JOIN journal_lines l ON l.journal_id=j.id
                   GROUP BY j.id ORDER BY j.entry_date DESC, j.id DESC LIMIT ?""", (limit,))


def project_rows():
    return rows("""SELECT p.*, c.name customer, b.name business_unit,
                     COALESCE(SUM(CASE WHEN g.account IN
                       ('Sales Revenue','Service Revenue','Other Income') THEN g.credit-g.debit ELSE 0 END),0) revenue,
                     COALESCE(SUM(CASE WHEN g.account LIKE '%Expense'
                       OR g.account IN ('Professional Fees','Cost of Goods Sold')
                       THEN g.debit-g.credit ELSE 0 END),0) costs
                   FROM projects p
                   LEFT JOIN customers c ON c.id=p.customer_id
                   LEFT JOIN business_units b ON b.id=p.business_unit_id
                   LEFT JOIN gl_entries g ON g.project_id=p.id
                   GROUP BY p.id ORDER BY p.status, p.name""")


def profit_and_loss(unit_id=None, project_id=None):
    where, params = [], []
    if unit_id:
        where.append("business_unit_id=?")
        params.append(unit_id)
    if project_id:
        where.append("project_id=?")
        params.append(project_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    data = rows("""SELECT account, SUM(debit) debit, SUM(credit) credit
                   FROM gl_entries""" + clause + " GROUP BY account", tuple(params))
    result = []
    for r in data:
        if r["account"] in ("Sales Revenue", "Service Revenue", "Other Income"):
            result.append({"section": "Income", "account": r["account"],
                           "amount": r["credit"] - r["debit"]})
        elif r["account"] == "Cost of Goods Sold" or r["account"] in OPERATING_EXPENSES:
            result.append({"section": "Expenses", "account": r["account"],
                           "amount": r["debit"] - r["credit"]})
    return sorted(result, key=lambda x: (x["section"], x["account"]))


def balance_sheet():
    tb = trial_balance()
    assets = {"Bank", "Cash", "Accounts Receivable", "Inventory", "Prepaid Expenses", "Equipment"}
    liabilities = {"Accounts Payable", "Sales Tax Payable", "Accrued Expenses"}
    equity = {"Owner's Equity", "Retained Earnings"}
    result = {
        "Assets": [r for r in tb if r["account"] in assets],
        "Liabilities": [r for r in tb if r["account"] in liabilities],
        "Equity": [r for r in tb if r["account"] in equity],
    }
    pnl = profit_and_loss()
    earnings = (sum(r["amount"] for r in pnl if r["section"] == "Income")
                - sum(r["amount"] for r in pnl if r["section"] == "Expenses"))
    result["Equity"].append({"account": "Current Earnings", "balance": earnings,
                             "debit": 0, "credit": earnings, "normal": "Cr"})
    return result


def tax_summary():
    return rows("""SELECT substr(expense_date,1,7) period, SUM(net_amount) taxable,
                          SUM(tax_amount) input_tax
                   FROM expenses GROUP BY period ORDER BY period DESC""")
