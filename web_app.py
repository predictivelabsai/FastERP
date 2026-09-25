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
import csv
import io
import json
import secrets
import uuid
import logging
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
load_dotenv()

from fasthtml.common import (
    fast_app, serve, Div, H1, P, A, Form, Input, Button, NotStr,
    RedirectResponse, Script, Style, Link, Title,
)
from starlette.responses import StreamingResponse, Response, FileResponse, JSONResponse

import db
from web.layout import page, LAYOUT_CSS
from web import views, ai, wms_views, wms_ai
from web.landing import landing_page
from web.features import features_page
from web.seo import register_seo_routes
from web.developer import developer_page
from web import account_auth, google_auth, accounting
from web.api import api
from fasterp.warehouse import OpeningStockRow, WarehouseService
from fasterp.warehouse_query import WarehouseQueryError, WarehouseQueryService
from fasterp.errors import DomainError, InsufficientStockError
import byok

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


# --- BYOK: per-org free-query gate + bring-your-own-key settings -------------
byok.register(rt, app, app_name="FastERP")


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


@rt("/features")
def get(session):
    return features_page(signed_in=bool(_user(session)))


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
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    db.confirm_order(oid)
    return _ofrag(session, oid)


@rt("/orders/{oid}/deliver")
def post(session, oid: int):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    db.deliver_order(oid)
    return _ofrag(session, oid)


@rt("/orders/{oid}/invoice")
def post(session, oid: int):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
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


@rt("/customers/{customer_id}/shelf-life")
def post(session, customer_id: int, days: int = 0):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        WarehouseService(db.postgres_database()).set_customer_shelf_life(
            customer_id, company_id=company_id, days=days,
        )
    except DomainError as exc:
        return Response(str(exc), status_code=400)
    return RedirectResponse("/customers", status_code=303)


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
def get(session, pid: int):
    return _guard(session, "purchase", lambda: wms_views.receipt_form(pid))


@rt("/purchase/{pid}/receive")
async def post(session, pid: int, request):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    if not db.using_postgres():
        db.receive_po(pid)
        return views.po_main(pid)
    from fasterp.purchasing import PurchasingService, ReceiptLine
    from fasterp.warehouse import StockAllocation

    company_id = db.current_company_id()
    order = db.purchase_order(pid)
    if not order or order["company_id"] != company_id:
        return Response("Purchase order not found", status_code=404)
    form = await request.form()
    lines = []
    try:
        for source in db.po_items(pid):
            prefix = f"line_{source['id']}_"
            if prefix+"order_line_id" not in form:
                continue
            accepted = Decimal(form.get(prefix+"accepted") or "0")
            rejected = Decimal(form.get(prefix+"rejected") or "0")
            if accepted < 0 or rejected < 0:
                raise ValueError("Receipt quantities cannot be negative")
            if accepted + rejected == 0:
                continue
            item = db.one(
                """SELECT tracks_batches,tracks_serials,tracks_expiry
                     FROM items WHERE id=? AND company_id=?""",
                (source["item_id"], company_id),
            )
            location_id = int(form.get(prefix+"location_id") or 0) or None
            lot_code = str(form.get(prefix+"lot_code") or "").strip() or None
            manufactured = str(form.get(prefix+"manufactured_on") or "").strip()
            expiry = str(form.get(prefix+"expires_on") or "").strip()
            manufactured_on = date.fromisoformat(manufactured) if manufactured else None
            expires_on = date.fromisoformat(expiry) if expiry else None
            serials = [value.strip() for value in re.split(
                r"[,\n]+", str(form.get(prefix+"serials") or "")) if value.strip()]
            if item["tracks_batches"] and not lot_code:
                raise ValueError(f"{source['code']} requires a lot number")
            if item["tracks_expiry"] and not expires_on:
                raise ValueError(f"{source['code']} requires an expiry date")
            if item["tracks_serials"] and (
                accepted + rejected != (accepted + rejected).to_integral_value()
                or len(serials) != int(accepted + rejected)
            ):
                raise ValueError(f"{source['code']} requires one serial per unit")

            def make_allocations(amount, selected_serials, disposition):
                if amount <= 0:
                    return ()
                common = dict(location_id=location_id, batch_code=lot_code,
                              manufactured_on=manufactured_on,
                              expires_on=expires_on, disposition=disposition)
                if selected_serials:
                    return tuple(StockAllocation(Decimal("1"), serial_code=serial,
                                                 **common) for serial in selected_serials)
                return (StockAllocation(amount, **common),)

            accepted_serials = serials[:int(accepted)] if item["tracks_serials"] else []
            rejected_serials = serials[int(accepted):] if item["tracks_serials"] else []
            lines.append(ReceiptLine(
                source["id"], accepted, rejected,
                source["warehouse_id"] if rejected else None,
                make_allocations(accepted, accepted_serials, "Available"),
                make_allocations(rejected, rejected_serials, "Rejected"),
            ))
        if not lines:
            raise ValueError("Enter at least one quantity to receive")
        PurchasingService(db.postgres_database()).receive(
            pid, receipt_date=date.today(), lines=lines, actor=_user(session)
        )
    except (ValueError, InvalidOperation, DomainError, InsufficientStockError) as exc:
        return _guard(session, "purchase", lambda: wms_views.receipt_form(pid, str(exc)))
    return RedirectResponse(f"/purchase/{pid}", status_code=303)


# --- warehouse inventory and FastBI-style analytical queries ----------------

def _warehouse_company():
    if not db.using_postgres():
        return None
    try:
        return db.current_company_id()
    except RuntimeError:
        return None


@rt("/warehouse")
def get(session, warehouse: str = "", start: str = "", end: str = ""):
    return _guard(session, "warehouse",
                  lambda: wms_views.dashboard(warehouse, start, end))


@rt("/warehouse/opening")
def get(session):
    return _guard(session, "warehouse", wms_views.opening_import_page)


@rt("/warehouse/opening")
def post(session, offset_account_id: int = 0, csv_text: str = "", reason: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        reader = csv.DictReader(io.StringIO(csv_text.strip()))
        required = {"item_code", "warehouse_code", "location_code", "quantity", "unit_cost"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("CSV needs item_code, warehouse_code, location_code, quantity and unit_cost")
        rows = []
        for entry in reader:
            if not any(value and str(value).strip() for value in entry.values()):
                continue
            rows.append(OpeningStockRow(
                item_code=str(entry["item_code"] or "").strip(),
                warehouse_code=str(entry["warehouse_code"] or "").strip(),
                location_code=str(entry["location_code"] or "").strip(),
                quantity=Decimal(entry["quantity"]),
                unit_cost=Decimal(entry["unit_cost"]),
                batch_code=(entry.get("batch_code") or "").strip() or None,
                manufactured_on=date.fromisoformat(entry["manufactured_on"])
                    if entry.get("manufactured_on") else None,
                expires_on=date.fromisoformat(entry["expires_on"])
                    if entry.get("expires_on") else None,
                serial_code=(entry.get("serial_code") or "").strip() or None,
                disposition=(entry.get("disposition") or "Available").strip(),
            ))
        WarehouseService(db.postgres_database()).import_opening_stock(
            rows, company_id=company_id, offset_account_id=offset_account_id,
            actor=_user(session), reason=reason,
        )
    except (DomainError, ValueError, TypeError, InvalidOperation) as exc:
        return _guard(session, "warehouse",
                      lambda: wms_views.opening_import_page(str(exc)))
    return RedirectResponse("/warehouse/stock", status_code=303)


@rt("/warehouse/stock")
def get(session, filter: str = "all", warehouse: str = "", days: int = 30):
    return _guard(session, "warehouse",
                  lambda: wms_views.stock_list(filter, warehouse, days))


@rt("/warehouse/stock/{stock_slice_id}/transfer")
def get(session, stock_slice_id: int):
    return _guard(session, "warehouse_locations",
                  lambda: wms_views.transfer_page(stock_slice_id))


@rt("/warehouse/stock/transfer")
def post(session, stock_slice_id: int = 0, destination_location_id: int = 0,
         quantity: str = "", disposition: str = "", reason: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        database = db.postgres_database()
        endpoints = database.one(
            """SELECT source.warehouse_id AS source_warehouse_id,
                      destination.warehouse_id AS destination_warehouse_id,
                      source.disposition
                 FROM stock_slices source
                 JOIN warehouse_locations destination
                   ON destination.id=%s AND destination.company_id=source.company_id
                WHERE source.id=%s AND source.company_id=%s""",
            (destination_location_id, stock_slice_id, company_id),
        )
        if not endpoints:
            raise DomainError("Stock or destination location not found")
        service = WarehouseService(database)
        if endpoints["source_warehouse_id"] == endpoints["destination_warehouse_id"]:
            service.transfer_location(
                stock_slice_id, company_id=company_id,
                destination_location_id=destination_location_id,
                amount=Decimal(quantity), actor=_user(session), reason=reason,
                disposition=disposition,
            )
        else:
            if disposition != endpoints["disposition"]:
                raise DomainError("Move between warehouses before changing stock status")
            service.transfer_warehouse(
                stock_slice_id, company_id=company_id,
                destination_location_id=destination_location_id,
                amount=Decimal(quantity), actor=_user(session), reason=reason,
            )
    except (DomainError, InsufficientStockError, ValueError, InvalidOperation) as exc:
        return _guard(session, "warehouse_locations",
                      lambda: wms_views.transfer_page(stock_slice_id, str(exc)))
    return RedirectResponse("/warehouse/stock", status_code=303)


@rt("/warehouse/stock/{stock_slice_id}/count")
def get(session, stock_slice_id: int):
    return _guard(session, "warehouse",
                  lambda: wms_views.count_page(stock_slice_id))


@rt("/warehouse/stock/count")
def post(session, stock_slice_id: int = 0, counted_quantity: str = "",
         adjustment_account_id: int = 0, reason: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        WarehouseService(db.postgres_database()).count_stock(
            stock_slice_id, company_id=company_id,
            counted_quantity=Decimal(counted_quantity),
            adjustment_account_id=adjustment_account_id or None,
            actor=_user(session), reason=reason,
        )
    except (DomainError, InsufficientStockError, ValueError, InvalidOperation) as exc:
        return _guard(session, "warehouse",
                      lambda: wms_views.count_page(stock_slice_id, str(exc)))
    return RedirectResponse("/warehouse/stock", status_code=303)


@rt("/warehouse/stock/{stock_slice_id}/identify")
def get(session, stock_slice_id: int):
    return _guard(session, "warehouse",
                  lambda: wms_views.identify_page(stock_slice_id))


@rt("/warehouse/stock/identify")
def post(session, stock_slice_id: int = 0, quantity: str = "",
         location_id: int = 0, batch_code: str = "", manufactured_on: str = "",
         expires_on: str = "", serial_code: str = "", reason: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    from fasterp.warehouse import StockAllocation
    try:
        WarehouseService(db.postgres_database()).identify_legacy_stock(
            stock_slice_id, company_id=company_id,
            allocation=StockAllocation(
                Decimal(quantity), location_id=location_id or None,
                batch_code=batch_code.strip() or None,
                manufactured_on=date.fromisoformat(manufactured_on)
                    if manufactured_on else None,
                expires_on=date.fromisoformat(expires_on) if expires_on else None,
                serial_code=serial_code.strip() or None,
                disposition="Hold",
            ), actor=_user(session), reason=reason,
        )
    except (DomainError, InsufficientStockError, ValueError, InvalidOperation) as exc:
        return _guard(session, "warehouse",
                      lambda: wms_views.identify_page(stock_slice_id, str(exc)))
    return RedirectResponse("/warehouse/stock?filter=held", status_code=303)


@rt("/warehouse/movements")
def get(session, warehouse: str = ""):
    return _guard(session, "warehouse", lambda: wms_views.movement_list(warehouse))


@rt("/warehouse/lots/{batch_id}")
def get(session, batch_id: int):
    return _guard(session, "warehouse", lambda: wms_views.lot_trace(batch_id))


@rt("/warehouse/lots/{batch_id}/amend")
def post(session, batch_id: int, manufactured_on: str = "",
         expires_on: str = "", reason: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        WarehouseService(db.postgres_database()).amend_lot_dates(
            batch_id, company_id=company_id,
            manufactured_on=date.fromisoformat(manufactured_on)
                if manufactured_on else None,
            expires_on=date.fromisoformat(expires_on) if expires_on else None,
            actor=_user(session), reason=reason,
        )
    except (DomainError, ValueError) as exc:
        return _guard(session, "warehouse",
                      lambda: wms_views.lot_trace(batch_id, str(exc)))
    return RedirectResponse(f"/warehouse/lots/{batch_id}", status_code=303)


@rt("/warehouse/holds")
def post(session, batch_id: int = 0, reason: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        WarehouseService(db.postgres_database()).hold_lot(
            batch_id, company_id=company_id, actor=_user(session), reason=reason
        )
    except DomainError as exc:
        return Response(str(exc), status_code=400)
    return RedirectResponse(f"/warehouse/lots/{batch_id}", status_code=303)


@rt("/warehouse/holds/{hold_id}/release")
def post(session, hold_id: int, reason: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        WarehouseService(db.postgres_database()).release_lot_hold(
            hold_id, company_id=company_id, actor=_user(session), reason=reason
        )
    except DomainError as exc:
        return Response(str(exc), status_code=400)
    return RedirectResponse("/warehouse", status_code=303)


@rt("/warehouse/reservations")
def get(session):
    return _guard(session, "warehouse", wms_views.reservations_page)


@rt("/warehouse/reservations")
def post(session, order_line_id: int = 0):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        WarehouseService(db.postgres_database()).reserve_order_line(
            order_line_id, company_id=company_id, actor=_user(session)
        )
    except (DomainError, InsufficientStockError) as exc:
        return _guard(session, "warehouse", lambda: wms_views.reservations_page(str(exc)))
    return RedirectResponse("/warehouse/reservations", status_code=303)


@rt("/warehouse/reservations/{order_line_id}/override")
def get(session, order_line_id: int):
    return _guard(session, "warehouse",
                  lambda: wms_views.reservation_override_page(order_line_id))


@rt("/warehouse/reservations/override")
def post(session, order_line_id: int = 0, stock_slice_id: int = 0,
         quantity: str = "", reason: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        WarehouseService(db.postgres_database()).reserve_order_line(
            order_line_id, company_id=company_id, actor=_user(session),
            requested_quantity=Decimal(quantity), preferred_slice_id=stock_slice_id,
            override_reason=reason,
        )
    except (DomainError, InsufficientStockError, ValueError, InvalidOperation) as exc:
        return _guard(session, "warehouse",
                      lambda: wms_views.reservation_override_page(order_line_id, str(exc)))
    return RedirectResponse("/warehouse/reservations", status_code=303)


@rt("/warehouse/locations")
def get(session):
    return _guard(session, "warehouse_locations", wms_views.locations_page)


@rt("/warehouse/locations")
def post(session, warehouse_id: int = 0, code: str = "", name: str = "",
         temperature_zone_id: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        WarehouseService(db.postgres_database()).create_location(
            company_id=company_id, warehouse_id=warehouse_id, code=code,
            name=name, actor=_user(session),
            temperature_zone_id=int(temperature_zone_id) if temperature_zone_id else None,
        )
    except (DomainError, ValueError) as exc:
        return _guard(session, "warehouse_locations",
                      lambda: wms_views.locations_page(str(exc)))
    return RedirectResponse("/warehouse/locations", status_code=303)


@rt("/warehouse/temperature")
def get(session):
    return _guard(session, "warehouse", wms_views.temperature_page)


@rt("/warehouse/temperature/zones")
def post(session, warehouse_id: int = 0, code: str = "", name: str = "",
         minimum_c: str = "", maximum_c: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        WarehouseService(db.postgres_database()).create_temperature_zone(
            company_id=company_id, warehouse_id=warehouse_id,
            code=code, name=name, minimum_c=Decimal(minimum_c),
            maximum_c=Decimal(maximum_c),
        )
    except (DomainError, ValueError) as exc:
        return _guard(session, "warehouse", lambda: wms_views.temperature_page(str(exc)))
    return RedirectResponse("/warehouse/temperature", status_code=303)


@rt("/warehouse/temperature/readings")
def post(session, zone_id: int = 0, temperature_c: str = "", note: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        WarehouseService(db.postgres_database()).record_temperature(
            zone_id, company_id=company_id, measured_at=datetime.now(timezone.utc),
            temperature_c=Decimal(temperature_c), actor=_user(session), note=note,
        )
    except (DomainError, ValueError) as exc:
        return _guard(session, "warehouse", lambda: wms_views.temperature_page(str(exc)))
    return RedirectResponse("/warehouse/temperature", status_code=303)


@rt("/warehouse/temperature/release")
def post(session, excursion_id: int = 0, reason: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Response("PostgreSQL company is not configured", status_code=400)
    try:
        WarehouseService(db.postgres_database()).release_temperature_excursion(
            excursion_id, company_id=company_id,
            actor=_user(session), reason=reason,
        )
    except DomainError as exc:
        return _guard(session, "warehouse", lambda: wms_views.temperature_page(str(exc)))
    return RedirectResponse("/warehouse/temperature", status_code=303)


@rt("/warehouse/query")
def get(session):
    return _guard(session, "warehouse_query", wms_views.query_lab)


@rt("/warehouse/query/run")
def post(session, sql: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Div(P("PostgreSQL company is not configured."), cls="card")
    try:
        result = WarehouseQueryService(db.postgres_database()).run(
            sql, company_id=company_id
        )
    except WarehouseQueryError as exc:
        return Div(P(str(exc)), cls="card")
    return wms_views.query_result(result)


@rt("/warehouse/query/ask")
async def post(session, question: str = ""):
    if not _user(session):
        return Response("Unauthorized", status_code=401)
    company_id = _warehouse_company()
    if company_id is None:
        return Div(P("PostgreSQL company is not configured."), cls="card")
    try:
        result = await wms_ai.ask_warehouse(question, session, company_id)
    except WarehouseQueryError as exc:
        return Div(P(str(exc)), cls="card")
    except Exception as exc:  # Provider errors must not expose credentials.
        return Div(P(f"AI query could not run ({type(exc).__name__})."), cls="card")
    return wms_views.query_result(result, question=question)


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
            byok.usage_banner(session),
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
        async for chunk in ai.stream_chat(message, session):
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
