"""Center-pane renderers for FastERP."""
from __future__ import annotations

from fasthtml.common import (
    Div, H1, H3, P, Span, A, Table, Thead, Tbody, Tr, Th, Td, Form, Input, Button, NotStr, Strong,
)

import db
from web.layout import kpi_card, money, NAV_ITEMS


def _pill(text, kind=""):
    return Span(text, cls="pill " + (kind or str(text)).lower().replace(" ", "").replace("/", ""))


def _title(title, sub="", *actions):
    return Div(Div(H1(title), P(sub, cls="sub") if sub else None),
               Div(*actions) if actions else None, cls="page-title")


# ---------- dashboard -------------------------------------------------------

def dashboard():
    k = db.kpis()
    obs = db.orders_by_status()
    mx = max((o["value"] for o in obs), default=1) or 1
    funnel = [Div(Div(o["status"], style="color:var(--text-dim);"),
                  Div(Div(cls="funnel-bar", style=f"width:{max(2,100*o['value']/mx):.0f}%;")),
                  Div(f"{money(o['value'])} · {o['count']}", cls="v"), cls="funnel-row") for o in obs]

    # monthly sales (invoiced)
    company_id = db.current_company_id()
    if company_id is not None:
        monthly = db.rows(
            """SELECT substr(invoice_date,1,7) m, ROUND(SUM(total)) v FROM invoices
                WHERE company_id=? GROUP BY m ORDER BY m DESC LIMIT 6""",
            (company_id,),
        )
    else:
        monthly = db.rows("""SELECT substr(invoice_date,1,7) m, ROUND(SUM(total)) v FROM invoices
                             GROUP BY m ORDER BY m DESC LIMIT 6""")
    mtbl = Table(Thead(Tr(Th("Month"), Th("Invoiced", cls="num"))),
                 Tbody(*[Tr(Td(r["m"]), Td(money(r["v"]), cls="num")) for r in reversed(monthly)]), cls="tbl")

    # AR aging
    aging_where = "company_id=? AND status!='Paid'" if company_id is not None else "status!='Paid'"
    aging = db.rows(
        f"""SELECT status, ROUND(SUM(total-paid)) due, COUNT(*) n FROM invoices
             WHERE {aging_where} GROUP BY status""",
        (company_id,) if company_id is not None else (),
    )
    atbl = Table(Thead(Tr(Th("Status"), Th("Invoices", cls="num"), Th("Outstanding", cls="num"))),
                 Tbody(*[Tr(Td(_pill(r["status"])), Td(str(r["n"]), cls="num"), Td(money(r["due"]), cls="num"))
                         for r in aging] or [Tr(Td("All settled 🎉", colspan="3"))]), cls="tbl")

    low_where = "company_id=? AND stock_qty <= reorder_level" if company_id is not None else "stock_qty <= reorder_level"
    low = db.rows(
        f"""SELECT name, code, stock_qty, reorder_level, uom FROM items
             WHERE {low_where} ORDER BY (stock_qty-reorder_level) LIMIT 8""",
        (company_id,) if company_id is not None else (),
    )
    ltbl = Table(Thead(Tr(Th("Item"), Th("In stock", cls="num"), Th("Reorder at", cls="num"))),
                 Tbody(*[Tr(Td(f"{r['name']}"), Td(f"{r['stock_qty']:.0f} {r['uom']}", cls="num"),
                            Td(f"{r['reorder_level']:.0f}", cls="num")) for r in low]
                       or [Tr(Td("Stock healthy 🎉", colspan="3"))]), cls="tbl")

    return (
        _title("Operations Dashboard", "Order-to-cash & inventory — fully synthetic demo data."),
        Div(kpi_card("Revenue (paid)", money(k["revenue"]), f"{k['open_orders']} open orders", tone="ok"),
            kpi_card("Receivables", money(k["receivable"]), f"{money(k['overdue'])} overdue", tone="danger" if k["overdue"] else ""),
            kpi_card("Inventory value", money(k["inventory_value"]), f"{k['low_stock']} below reorder", tone="danger" if k["low_stock"] else ""),
            kpi_card("Customers", k["customers"]), cls="kpi-grid"),
        Div(Div(Div(H3("Sales orders by status"), cls="card-header"), *funnel, cls="card"),
            Div(Div(H3("Receivables (AR) aging"), cls="card-header"), atbl, cls="card"), cls="grid-2"),
        Div(Div(Div(H3("Monthly invoiced sales"), cls="card-header"), mtbl, cls="card"),
            Div(Div(H3("Low-stock items"), cls="card-header"), ltbl, cls="card"), cls="grid-2"),
    )


# ---------- sales orders ----------------------------------------------------

def orders_list(status="All", q=""):
    seg = Div(*[A(s, href=f"/orders?status={s}", cls="" + ("active" if status == s else ""))
                for s in ["All"] + db.ORDER_STATUSES], cls="seg")
    company_id = db.current_company_id()
    where, params = (["so.company_id=?"], [company_id]) if company_id is not None else ([], [])
    if status != "All":
        where.append("so.status=?")
        params.append(status)
    if q:
        where.append("(so.code LIKE ? OR c.name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    orders = db.rows(f"""SELECT so.*, c.name customer FROM sales_orders so
                         LEFT JOIN customers c ON c.id=so.customer_id {clause}
                         ORDER BY so.order_date DESC LIMIT 200""", tuple(params))
    tbl = Table(Thead(Tr(Th("Order"), Th("Customer"), Th("Date"), Th("Delivery"), Th("Status"), Th("Total", cls="num"))),
                Tbody(*[Tr(Td(A(o["code"], href=f"/orders/{o['id']}")), Td(o["customer"] or "—"),
                           Td(o["order_date"]), Td(o["delivery_date"]), Td(_pill(o["status"])),
                           Td(money(o["total"]), cls="num")) for o in orders]
                      or [Tr(Td("No orders.", colspan="6"))]), cls="tbl")
    search = Form(Input(type="search", name="q", value=q, placeholder="Search orders…"),
                  Input(type="hidden", name="status", value=status), cls="toolbar", method="get", action="/orders")
    return _title("Sales Orders", f"{len(orders)} shown"), seg, search, Div(tbl, cls="card")


_ACTION_LABEL = {"confirm": "✓ Confirm order", "deliver": "🚚 Deliver (decrement stock)",
                 "invoice": "🧾 Raise invoice"}


def _o2c_steps(status):
    """Render the order-to-cash flow as a progress strip."""
    flow = ["Draft", "Confirmed", "Delivered", "Invoiced", "Closed"]
    if status == "Cancelled":
        return Div(_pill("Cancelled"), cls="o2c")
    idx = flow.index(status) if status in flow else 0
    chips = []
    for i, s in enumerate(flow):
        cls = "o2c-step done" if i <= idx else "o2c-step"
        chips.append(Span(s, cls=cls))
        if i < len(flow) - 1:
            chips.append(Span("→", cls="o2c-arrow"))
    return Div(*chips, cls="o2c")


def order_main(oid):
    o = db.sales_order(oid)
    if not o:
        return Div(P("No such order."))
    items = db.order_items(oid)
    inv = db.invoice_for_order(oid)
    lines = Table(Thead(Tr(Th("Item"), Th("Code"), Th("Qty", cls="num"), Th("Rate", cls="num"), Th("Amount", cls="num"))),
                  Tbody(*[Tr(Td(li["item"]), Td(li["code"]), Td(f"{li['qty']:.0f}", cls="num"),
                             Td(money(li["rate"]), cls="num"), Td(money(li["amount"]), cls="num")) for li in items],
                        Tr(Td(""), Td(""), Td(""), Td(Strong("Total"), cls="num"), Td(Strong(money(o["total"])), cls="num"))),
                  cls="tbl")

    # action area — drives the order-to-cash flow
    action = db.next_action(o)
    action_bits = [_o2c_steps(o["status"])]
    if action:
        action_bits.append(Button(_ACTION_LABEL[action], cls="btn primary",
                                   **{"hx-post": f"/orders/{oid}/{action}", "hx-target": "#order-main",
                                      "hx-swap": "innerHTML"}))
    if inv:
        outstanding = inv["total"] - inv["paid"]
        if outstanding > 0.01:
            action_bits.append(Form(
                Span(f"Invoice {inv['code']} · {money(outstanding)} outstanding", style="margin-right:8px;color:var(--text-dim);"),
                Input(type="number", name="amount", value=int(outstanding), step="100", style="width:120px;"),
                Button("💷 Record payment", cls="btn primary", type="submit"),
                **{"hx-post": f"/invoices/{inv['id']}/pay", "hx-target": "#order-main", "hx-swap": "innerHTML"},
                cls="inline-form", style="margin-top:10px;"))
        else:
            action_bits.append(Div(f"✓ Invoice {inv['code']} paid in full.", cls="paid-note"))
    actions = Div(Div(H3("Order-to-cash"), cls="card-header"), *action_bits, cls="card")

    info = Div(Div(H3("Order"), cls="card-header"),
               Div(Span("Customer", cls="k"), Span(o["customer"]),
                   Span("Territory", cls="k"), Span(o["territory"] or "—"),
                   Span("Status", cls="k"), _pill(o["status"]),
                   Span("Order date", cls="k"), Span(o["order_date"]),
                   Span("Invoice", cls="k"),
                   Span(A(f"{inv['code']} · {inv['status']}", href="/invoices") if inv else "Not invoiced"),
                   cls="kv"), cls="card")
    return Div(Div(actions, Div(Div(H3("Line items"), cls="card-header"), lines, cls="card")),
               info, cls="detail-grid")


def order_detail(oid):
    o = db.sales_order(oid)
    if not o:
        return _title("Order not found"), P("No such order.")
    return (_title(o["code"], f"{o['customer']} · {money(o['total'])}", A("← All orders", href="/orders", cls="btn")),
            Div(order_main(oid), id="order-main"))


# ---------- invoices --------------------------------------------------------

def invoices_list(status="All"):
    seg = Div(*[A(s, href=f"/invoices?status={s}", cls="" + ("active" if status == s else ""))
                for s in ["All"] + db.INVOICE_STATUSES], cls="seg")
    company_id = db.current_company_id()
    where, params = [], []
    if company_id is not None:
        where.append("inv.company_id=?")
        params.append(company_id)
    if status != "All":
        where.append("inv.status=?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    invs = db.rows(f"""SELECT inv.*, c.name customer, so.code order_code FROM invoices inv
                       LEFT JOIN customers c ON c.id=inv.customer_id
                       LEFT JOIN sales_orders so ON so.id=inv.order_id {clause}
                       ORDER BY inv.due_date LIMIT 200""", tuple(params))
    total_due = sum(i["total"] - i["paid"] for i in invs if i["status"] != "Paid")
    tbl = Table(Thead(Tr(Th("Invoice"), Th("Customer"), Th("Invoiced"), Th("Due"), Th("Total", cls="num"),
                         Th("Outstanding", cls="num"), Th("Status"))),
                Tbody(*[Tr(Td(i["code"]), Td(i["customer"] or "—"), Td(i["invoice_date"]), Td(i["due_date"]),
                           Td(money(i["total"]), cls="num"),
                           Td(money(i["total"] - i["paid"]), cls="num"), Td(_pill(i["status"])))
                        for i in invs] or [Tr(Td("No invoices.", colspan="7"))]), cls="tbl")
    return (_title("Invoices (Accounts Receivable)", f"{len(invs)} shown · {money(total_due)} outstanding"),
            seg, Div(tbl, cls="card"))


# ---------- items / stock ---------------------------------------------------

def items_list(group="All", q=""):
    seg = Div(*[A(s, href=f"/items?group={s}", cls="" + ("active" if group == s else ""))
                for s in ["All"] + db.ITEM_GROUPS], cls="seg")
    company_id = db.current_company_id()
    where, params = (["company_id=?"], [company_id]) if company_id is not None else ([], [])
    if group != "All":
        where.append("item_group=?")
        params.append(group)
    if q:
        where.append("(name LIKE ? OR code LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    items = db.rows(f"SELECT * FROM items {clause} ORDER BY (stock_qty<=reorder_level) DESC, name LIMIT 300", tuple(params))
    rows_ = []
    for it in items:
        low = it["stock_qty"] <= it["reorder_level"]
        rows_.append(Tr(
            Td(Strong(it["name"])), Td(it["code"]), Td(it["item_group"]),
            Td(money(it["rate"]), cls="num"),
            Td(f"{it['stock_qty']:.0f} {it['uom']}", cls="num"),
            Td(money(it["stock_qty"] * it["rate"]), cls="num"),
            Td(_pill("Reorder", "low") if low else _pill("OK", "ok2"))))
    tbl = Table(Thead(Tr(Th("Item"), Th("Code"), Th("Group"), Th("Rate", cls="num"),
                         Th("In stock", cls="num"), Th("Value", cls="num"), Th("Status"))),
                Tbody(*rows_), cls="tbl")
    search = Form(Input(type="search", name="q", value=q, placeholder="Search items…"),
                  Input(type="hidden", name="group", value=group), cls="toolbar", method="get", action="/items")
    return _title("Items & Stock", f"{len(items)} items"), seg, search, Div(tbl, cls="card")


def customers_list():
    company_id = db.current_company_id()
    company_clause = "WHERE c.company_id=?" if company_id is not None else ""
    custs = db.rows(
        f"""SELECT c.*,
                    (SELECT COUNT(*) FROM sales_orders so WHERE so.customer_id=c.id) orders,
                    (SELECT COALESCE(SUM(total-paid),0) FROM invoices
                       WHERE customer_id=c.id AND status!='Paid') outstanding
               FROM customers c {company_clause}
              ORDER BY outstanding DESC, c.name""",
        (company_id,) if company_id is not None else (),
    )
    tbl = Table(Thead(Tr(Th("Customer"), Th("Territory"), Th("Credit limit", cls="num"),
                         Th("Orders", cls="num"), Th("Outstanding", cls="num"))),
                Tbody(*[Tr(Td(Strong(c["name"])), Td(c["territory"] or "—"),
                           Td(money(c["credit_limit"]), cls="num"), Td(str(c["orders"]), cls="num"),
                           Td(money(c["outstanding"]), cls="num")) for c in custs]), cls="tbl")
    return _title("Customers", f"{len(custs)} customers"), Div(tbl, cls="card")


# ---------- suppliers -------------------------------------------------------

def suppliers_list():
    sups = db.suppliers()
    tbl = Table(Thead(Tr(Th("Supplier"), Th("Territory"), Th("Purchase orders", cls="num"),
                         Th("Total spend", cls="num"))),
                Tbody(*[Tr(Td(Strong(s["name"])), Td(s["territory"] or "—"),
                           Td(str(s["po_count"]), cls="num"), Td(money(s["spend"]), cls="num"))
                        for s in sups] or [Tr(Td("No suppliers.", colspan="4"))]), cls="tbl")
    add = Form(Input(name="name", placeholder="New supplier name…", required=True),
               Input(name="territory", placeholder="Territory"),
               Button("+ Add supplier", cls="btn primary", type="submit"),
               cls="inline-form", **{"hx-post": "/suppliers/new", "hx-target": "#sup-main", "hx-swap": "innerHTML"})
    return (_title("Suppliers", f"{len(sups)} suppliers"),
            Div(Div(Div(H3("Add supplier"), cls="card-header"), add, cls="card"),
                Div(tbl, cls="card"), id="sup-main"))


# ---------- purchase orders -------------------------------------------------

def _po_pill(status):
    return Span(status, cls="pill " + status.lower())


def purchase_orders_list(status="All"):
    seg = Div(*[A(s, href=f"/purchase?status={s}", cls="" + ("active" if status == s else ""))
                for s in ["All"] + db.PO_STATUSES], cls="seg")
    pos = db.purchase_orders(None if status == "All" else status)
    tbl = Table(Thead(Tr(Th("PO"), Th("Supplier"), Th("Date"), Th("Status"), Th("Total", cls="num"))),
                Tbody(*[Tr(Td(A(p["code"], href=f"/purchase/{p['id']}")), Td(p["supplier"] or "—"),
                           Td(p["order_date"]), Td(_po_pill(p["status"])),
                           Td(money(p["total"]), cls="num")) for p in pos]
                      or [Tr(Td("No purchase orders.", colspan="5"))]), cls="tbl")
    return (_title("Purchase Orders", f"{len(pos)} shown",
                   A("+ New PO", href="/purchase/new", cls="btn primary")),
            seg, Div(tbl, cls="card"))


def po_main(pid):
    po = db.purchase_order(pid)
    if not po:
        return Div(P("No such purchase order."))
    items = db.po_items(pid)
    lines = Table(Thead(Tr(Th("Item"), Th("Code"), Th("Qty", cls="num"), Th("Rate", cls="num"), Th("Amount", cls="num"))),
                  Tbody(*[Tr(Td(li["item"]), Td(li["code"]), Td(f"{li['qty']:.0f}", cls="num"),
                             Td(money(li["rate"]), cls="num"), Td(money(li["amount"]), cls="num")) for li in items],
                        Tr(Td(""), Td(""), Td(""), Td(Strong("Total"), cls="num"), Td(Strong(money(po["total"])), cls="num"))),
                  cls="tbl")
    action_bits = []
    if po["status"] == "Ordered":
        action_bits.append(P("Receiving this PO increases stock and posts to the ledger "
                             "(debit Inventory / credit Accounts Payable).", cls="sub"))
        action_bits.append(Button("📥 Receive (stock in + post GL)", cls="btn primary",
                                   **{"hx-post": f"/purchase/{pid}/receive", "hx-target": "#po-main",
                                      "hx-swap": "innerHTML"}))
    elif po["status"] == "Received":
        action_bits.append(Div("✓ Received — stock updated and posted to the general ledger.", cls="paid-note"))
    else:
        action_bits.append(P(f"Status: {po['status']}.", cls="sub"))
    actions = Div(Div(H3("Receiving"), cls="card-header"), *action_bits, cls="card")
    info = Div(Div(H3("Purchase order"), cls="card-header"),
               Div(Span("Supplier", cls="k"), Span(po["supplier"]),
                   Span("Territory", cls="k"), Span(po["territory"] or "—"),
                   Span("Status", cls="k"), _po_pill(po["status"]),
                   Span("Order date", cls="k"), Span(po["order_date"]),
                   Span("Total", cls="k"), Span(money(po["total"])), cls="kv"), cls="card")
    return Div(Div(actions, Div(Div(H3("Line items"), cls="card-header"), lines, cls="card")),
               info, cls="detail-grid")


def po_detail(pid):
    po = db.purchase_order(pid)
    if not po:
        return _title("PO not found"), P("No such purchase order.")
    return (_title(po["code"], f"{po['supplier']} · {money(po['total'])}",
                   A("← All purchase orders", href="/purchase", cls="btn")),
            Div(po_main(pid), id="po-main"))


def po_new_form():
    sups = db.suppliers()
    company_id = db.current_company_id()
    if company_id is not None:
        items = db.rows(
            "SELECT id, code, name, rate FROM items WHERE company_id=? ORDER BY name",
            (company_id,),
        )
    else:
        items = db.rows("SELECT id, code, name, rate FROM items ORDER BY name")
    # five blank line rows; each is item-select + qty + rate
    item_opts = "".join(f'<option value="{i["id"]}" data-rate="{i["rate"]:.2f}">{i["code"]} · {i["name"]}</option>'
                        for i in items)
    rows_ = []
    for n in range(5):
        rows_.append(Tr(
            Td(NotStr(f'<select name="item_{n}" class="po-item" style="width:100%"><option value="">—</option>{item_opts}</select>')),
            Td(NotStr(f'<input type="number" name="qty_{n}" min="0" step="1" value="0" style="width:90px" class="num">'), cls="num"),
            Td(NotStr(f'<input type="number" name="rate_{n}" min="0" step="0.01" value="0" style="width:110px" class="num">'), cls="num")))
    tbl = Table(Thead(Tr(Th("Item"), Th("Qty", cls="num"), Th("Rate", cls="num"))), Tbody(*rows_), cls="tbl")
    sup_select = NotStr('<select name="supplier_id" required style="min-width:240px">'
                        + "".join(f'<option value="{s["id"]}">{s["name"]}</option>' for s in sups)
                        + '</select>')
    form = Form(
        Div(Span("Supplier  ", cls="k"), sup_select,
            style="margin-bottom:12px;display:flex;gap:8px;align-items:center;"),
        tbl,
        Div(Button("Create purchase order", cls="btn primary", type="submit"),
            A("Cancel", href="/purchase", cls="btn"), style="margin-top:12px;display:flex;gap:8px;"),
        method="post", action="/purchase/new",
        # auto-fill rate from the selected item's default rate
        oninput="if(event.target.classList.contains('po-item')){var o=event.target.selectedOptions[0];"
                "var r=event.target.closest('tr').querySelector('input[name^=rate_]');"
                "if(o&&r&&(!r.value||r.value=='0'))r.value=o.dataset.rate||'';}")
    return (_title("New Purchase Order", "Pick a supplier and add item lines",
                   A("← All purchase orders", href="/purchase", cls="btn")),
            Div(form, cls="card"))


# ---------- general ledger --------------------------------------------------

def gl_view(account="All"):
    tb = db.trial_balance()
    totals = db.gl_totals()
    tb_tbl = Table(Thead(Tr(Th("Account"), Th("Normal"), Th("Debits", cls="num"),
                            Th("Credits", cls="num"), Th("Balance", cls="num"))),
                   Tbody(*[Tr(Td(A(r["account"], href=f"/ledger?account={r['account']}")),
                              Td(r["normal"]), Td(money(r["debit"]), cls="num"),
                              Td(money(r["credit"]), cls="num"),
                              Td(Strong(f"{money(r['balance'])} {r['normal']}"), cls="num")) for r in tb],
                         Tr(Td(Strong("Totals")), Td(""), Td(Strong(money(totals["debit"])), cls="num"),
                            Td(Strong(money(totals["credit"])), cls="num"),
                            Td(_pill("Balanced", "ok2") if totals["balanced"] else _pill("Out of balance", "low"),
                               cls="num"))), cls="tbl")

    seg = Div(A("All entries", href="/ledger", cls="" + ("active" if account == "All" else "")),
              *[A(a, href=f"/ledger?account={a}", cls="" + ("active" if account == a else ""))
                for a in db.ACCOUNTS], cls="seg")
    entries = db.gl_entries(None if account == "All" else account)
    ledger_tbl = Table(Thead(Tr(Th("Date"), Th("Account"), Th("Ref"), Th("Debit", cls="num"), Th("Credit", cls="num"))),
                       Tbody(*[Tr(Td(e["entry_date"]), Td(e["account"]), Td(e["ref"] or "—"),
                                  Td(money(e["debit"]) if e["debit"] else "", cls="num"),
                                  Td(money(e["credit"]) if e["credit"] else "", cls="num")) for e in entries]
                             or [Tr(Td("No entries.", colspan="5"))]), cls="tbl")
    return (_title("General Ledger", "Double-entry postings from invoices, payments and receipts"),
            Div(Div(H3("Trial balance"), cls="card-header"), tb_tbl, cls="card"),
            seg, Div(Div(H3("Ledger entries"), cls="card-header"), ledger_tbl, cls="card"))
