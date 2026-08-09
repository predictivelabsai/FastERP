"""FastERP — an open-source ERP slice built with FastHTML.

A server-side, HTMX-driven port of ERPNext's Order-to-Cash + Inventory: items &
stock, customers, sales orders, invoices with AR aging, and an AI assistant
grounded in the live (synthetic) data.

Run:
    python web_app.py            # http://localhost:5011

Login: admin@fasterp.example / FastERP2026$  (override via .env)
"""
from __future__ import annotations

import os
import json
import secrets
import uuid
import logging

from dotenv import load_dotenv
load_dotenv()

from fasthtml.common import (
    fast_app, serve, Div, H1, P, A, Form, Input, Button, NotStr,
    RedirectResponse, Script, Style, Link, Title,
)
from starlette.responses import StreamingResponse, Response, FileResponse, JSONResponse

import db
from web.layout import page, LAYOUT_CSS
from web import views, ai
from web.landing import landing_page
from web.seo import register_seo_routes
from web.developer import developer_page
from web import account_auth, google_auth, accounting
from web.api import api

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger("fasterp")

VALID_EMAIL = os.getenv("FASTERP_ADMIN_EMAIL", "admin@fasterp.example")
VALID_PASSWORD = os.getenv("FASTERP_ADMIN_PASSWORD", "FastERP2026$")
ENV_LABEL = os.getenv("FASTERP_ENV_LABEL", "FastERP")
SECRET = os.getenv("FASTERP_SECRET", secrets.token_hex(32))
PORT = int(os.getenv("FASTERP_PORT", "5011"))

app, rt = fast_app(live=False, pico=False, secret_key=SECRET, hdrs=[Style(LAYOUT_CSS)])
app.mount("/api", api)


account_auth.register_fasthtml_routes(rt, app_name="FastERP", session_key="user", success_path="/")


@rt("/swagger.json")
def get():
    return JSONResponse(api.openapi())


@rt("/developers", methods=["GET"])
def developers():
    return developer_page()


def _user(session):
    return session.get("user")


def _thread(session):
    if "thread" not in session:
        session["thread"] = uuid.uuid4().hex
    return session["thread"]


def _guard(session, active, builder):
    if not _user(session):
        return RedirectResponse("/login", status_code=303)
    content = builder() if callable(builder) else builder
    if not isinstance(content, tuple):
        content = (content,)
    return page(active, ENV_LABEL, _user(session), _thread(session), *content)


def _login_card(error="", email=""):
    return Title("FastERP — Sign in"), Style(LAYOUT_CSS), Div(
        Form(H1("FastERP"), P("Sign in to your operations workspace"),
             Input(name="email", type="email", placeholder="Email", value=email, required=True),
             Input(name="password", type="password", placeholder="Password", required=True),
             P(error, cls="error") if error else None,
             Button("Sign in", cls="btn primary", type="submit"),
             P(NotStr("Demo: <code>admin@fasterp.example</code> / <code>FastERP2026$</code>"), cls="hint"),
             method="post", action="/login", cls="login-card"), cls="login-wrap")


@rt("/login")
def get(session):
    if _user(session):
        return RedirectResponse("/", status_code=303)
    return _login_card()


@rt("/login")
def post(session, email: str = "", password: str = ""):
    if email.strip().lower() == VALID_EMAIL.lower() and password == VALID_PASSWORD:
        session["user"] = email.strip().lower()
        return RedirectResponse("/", status_code=303)
    return _login_card("Invalid email or password.", email)



@rt("/auth/google")
def google_start(session, request):
    if not google_auth.enabled():
        return RedirectResponse("/login?error=Google+sign-in+is+not+configured", status_code=303)
    state = google_auth.new_state()
    session["google_oauth_state"] = state
    return RedirectResponse(google_auth.authorize_url(request, state), status_code=303)


@rt("/auth/google/callback")
def google_callback(session, request, code: str = "", state: str = "", error: str = ""):
    if error or not code or state != session.pop("google_oauth_state", None):
        return RedirectResponse("/login?error=Google+sign-in+failed", status_code=303)
    identity = google_auth.exchange(request, code)
    if not identity:
        return RedirectResponse("/login?error=Google+account+is+not+authorised", status_code=303)
    account_auth.accounts.link_google(identity["email"], identity["name"])
    session["user"] = identity["email"]
    return RedirectResponse("/", status_code=303)


@rt("/logout")
def get(session):
    session.pop("user", None)
    return RedirectResponse("/login", status_code=303)


@rt("/")
def get(session):
    if not _user(session):
        return landing_page()
    return _guard(session, "dashboard", views.dashboard)


@rt("/orders")
def get(session, status: str = "All", q: str = ""):
    return _guard(session, "orders", lambda: views.orders_list(status, q))


@rt("/orders/{oid}")
def get(session, oid: int):
    return _guard(session, "orders", lambda: views.order_detail(oid))


def _ofrag(session, oid):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    return views.order_main(oid)


@rt("/orders/{oid}/confirm")
def post(session, oid: int):
    db.confirm_order(oid)
    return _ofrag(session, oid)


@rt("/orders/{oid}/deliver")
def post(session, oid: int):
    db.deliver_order(oid)
    return _ofrag(session, oid)


@rt("/orders/{oid}/invoice")
def post(session, oid: int):
    db.invoice_order(oid)
    return _ofrag(session, oid)


@rt("/invoices/{inv_id}/pay")
def post(session, inv_id: int, amount: float = 0):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    inv = db.one("SELECT order_id FROM invoices WHERE id=?", (inv_id,))
    db.record_payment(inv_id, amount)
    return views.order_main(inv["order_id"]) if inv and inv["order_id"] else Response("ok")


@rt("/invoices")
def get(session, status: str = "All"):
    return _guard(session, "invoices", lambda: views.invoices_list(status))


@rt("/items")
def get(session, group: str = "All", q: str = ""):
    return _guard(session, "items", lambda: views.items_list(group, q))


@rt("/customers")
def get(session):
    return _guard(session, "customers", views.customers_list)


# --- buying -----------------------------------------------------------------

@rt("/suppliers")
def get(session):
    return _guard(session, "suppliers", views.suppliers_list)


@rt("/suppliers/new")
def post(session, name: str = "", territory: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    if name.strip():
        db.create_supplier(name, territory)
    # re-render the suppliers main block (form + table)
    return views.suppliers_list()[1]


@rt("/purchase")
def get(session, status: str = "All"):
    return _guard(session, "purchase", lambda: views.purchase_orders_list(status))


@rt("/purchase/new")
def get(session):
    return _guard(session, "purchase", views.po_new_form)


@rt("/purchase/new")
async def post(session, request):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    form = await request.form()
    supplier_id = int(form.get("supplier_id") or 0)
    lines = []
    for n in range(5):
        item = form.get(f"item_{n}")
        if not item:
            continue
        qty = float(form.get(f"qty_{n}") or 0)
        rate = float(form.get(f"rate_{n}") or 0)
        if qty > 0:
            lines.append((int(item), qty, rate))
    pid = db.create_po(supplier_id, lines) if supplier_id and lines else None
    if pid:
        return RedirectResponse(f"/purchase/{pid}", status_code=303)
    return RedirectResponse("/purchase/new", status_code=303)


@rt("/purchase/{pid}")
def get(session, pid: int):
    return _guard(session, "purchase", lambda: views.po_detail(pid))


@rt("/purchase/{pid}/receive")
def post(session, pid: int):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    db.receive_po(pid)
    return views.po_main(pid)


# --- finance / general ledger ----------------------------------------------

@rt("/accounting")
def get(session):
    return _guard(session, "accounting", accounting.overview)


@rt("/accounting/accounts")
def get(session):
    return _guard(session, "accounts", accounting.chart_of_accounts)


@rt("/accounting/expenses")
def get(session):
    return _guard(session, "expenses", accounting.expenses)


@rt("/accounting/expenses/new")
def get(session):
    return _guard(session, "expenses", accounting.expense_form)


@rt("/accounting/expenses/new")
async def post(session, request):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    form = await request.form()
    try:
        db.create_expense(
            int(form.get("supplier_id") or 0), str(form.get("category") or ""),
            str(form.get("description") or ""), float(form.get("net_amount") or 0),
            int(form.get("tax_code_id") or 0), str(form.get("currency") or "GBP"),
            int(form.get("business_unit_id") or 0), int(form.get("project_id") or 0),
            str(form.get("note") or ""))
    except (TypeError, ValueError):
        return _guard(session, "expenses", accounting.expense_form)
    return RedirectResponse("/accounting/expenses", status_code=303)


@rt("/accounting/journals")
def get(session):
    return _guard(session, "journals", accounting.journals)


@rt("/accounting/journals/new")
def get(session):
    return _guard(session, "journals", accounting.journal_form)


@rt("/accounting/journals/new")
async def post(session, request):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    form = await request.form()
    lines = [(form.get(f"account_{n}"), form.get(f"debit_{n}") or 0,
              form.get(f"credit_{n}") or 0, form.get(f"unit_{n}"),
              form.get(f"project_{n}")) for n in range(4)]
    jid = db.create_journal(str(form.get("entry_date") or ""), str(form.get("memo") or ""), lines)
    if not jid:
        return _guard(session, "journals",
                      lambda: accounting.journal_form("Journal is not balanced. Debits must equal credits."))
    return RedirectResponse("/accounting/journals", status_code=303)


@rt("/accounting/projects")
def get(session):
    return _guard(session, "projects", accounting.projects)


@rt("/accounting/reports")
def get(session, report: str = "pnl"):
    return _guard(session, "reports", lambda: accounting.reports(report))


@rt("/accounting/setup")
def get(session):
    return _guard(session, "accounting_setup", accounting.settings)


@rt("/docs/assets/receipts/{filename}")
def get(session, filename: str):
    if not _user(session) or "/" in filename or ".." in filename:
        return Response("Not found", status_code=404)
    path = os.path.join(os.path.dirname(__file__), "docs", "assets", "receipts", filename)
    return FileResponse(path) if os.path.isfile(path) else Response("Not found", status_code=404)

@rt("/ledger")
def get(session, account: str = "All"):
    return _guard(session, "ledger", lambda: views.gl_view(account))


@rt("/ai")
def get(session):
    body = (views._title("AI Assistant", "Chat lives in the right rail. Ask in plain English or use slash-commands."),
            Div(NotStr(
                "<div class='card'><h3>What you can ask</h3><ul style='line-height:1.8;'>"
                "<li>“How much is outstanding from customers, and how much is overdue?”</li>"
                "<li>“Which items need reordering?”</li>"
                "<li>“How are sales orders tracking by status?”</li></ul>"
                "<p style='color:var(--text-mute)'>Slash-commands (no API key): "
                "<code>/sales</code> <code>/ar</code> <code>/stock</code> <code>/top</code></p></div>")))
    return _guard(session, "ai", body)


@rt("/guide")
def get(session):
    body = (views._title("User Guide", "How to drive FastERP"), Div(NotStr("""
<div class='card'><h3>Dashboard</h3><p>Revenue, receivables (with overdue), inventory value and low-stock count;
sales orders by status, AR aging, monthly invoiced sales, and low-stock items.</p></div>
<div class='card'><h3>Sales Orders</h3><p>Filter by status; open an order for its line items, totals, and linked invoice.</p></div>
<div class='card'><h3>Invoices (AR)</h3><p>Accounts-receivable list with outstanding amounts and status (Unpaid / Partly Paid / Paid / Overdue).</p></div>
<div class='card'><h3>Items & Stock</h3><p>Inventory by group with stock levels, value, and a reorder flag.</p></div>
<div class='card'><h3>AI Assistant</h3><p>The right rail chats over a live ERP snapshot. Set <code>MODEL_PROVIDER</code> + a key in
<code>.env</code> for free-form chat; slash-commands always work.</p></div>""")))
    return _guard(session, "guide", body)


@rt("/chat/new")
def get(session):
    session["thread"] = uuid.uuid4().hex
    return P("Ask about sales, receivables or stock — or use /sales /ar /stock /help.", cls="chat-empty-hint")


@rt("/chat/stream")
async def post(session, message: str = "", thread_id: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    message = (message or "").strip()
    if not message:
        return Response("No message", status_code=400)
    tid = thread_id or _thread(session)

    async def gen():
        db.add_chat_message(tid, "user", message)
        full = []
        async for chunk in ai.stream_chat(message):
            if chunk.startswith("data: "):
                try:
                    tok = json.loads(chunk[6:]).get("token")
                    if tok:
                        full.append(tok)
                except Exception:
                    pass
            yield chunk
        db.add_chat_message(tid, "assistant", "".join(full))

    return StreamingResponse(gen(), media_type="text/event-stream")


def _ensure_db():
    existed = db.db_exists()
    db.init_schema()
    if db.using_postgres():
        if not db.scalar("SELECT count(*) FROM companies"):
            logger.warning(
                "PostgreSQL schema is ready but has no companies; "
                "run scripts/seed_postgres.py before using ERP screens"
            )
    elif not existed:
        logger.info("No database found — seeding synthetic ERP data…")
        import seed
        seed.build()


_ensure_db()


register_seo_routes(app)

if __name__ == "__main__":
    logger.info("FastERP on http://localhost:%s  (login %s)", PORT, VALID_EMAIL)
    serve(port=PORT, reload=os.getenv("FASTERP_RELOAD", "0") == "1")
