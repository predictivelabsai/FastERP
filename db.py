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
CREATE INDEX IF NOT EXISTS idx_so_status ON sales_orders(status);
CREATE INDEX IF NOT EXISTS idx_inv_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_soi_order ON sales_order_items(order_id);
"""


def init_schema():
    with cursor() as conn:
        conn.executescript(SCHEMA)


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
