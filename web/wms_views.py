"""Warehouse inventory dashboards, stock drilldowns and SQL lab."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fasthtml.common import (
    A, Button, Div, Form, H1, H3, Input, Label, Option, P, Pre, Select,
    Span, Table, Tbody, Td, Textarea, Th, Thead, Tr,
)

import db
from fasterp.warehouse_query import QueryResult, WarehouseQueryService
from fasterp.warehouse_reports import WarehouseReports
from web.layout import kpi_card
from web import wms_charts


def _title(title: str, subtitle: str = "", *actions):
    return Div(Div(H1(title), P(subtitle, cls="sub") if subtitle else None),
               Div(*actions), cls="page-title")


def _company():
    if not db.using_postgres():
        return None
    try:
        return db.current_company_id()
    except RuntimeError:
        return None


def _unavailable():
    return (_title("Warehouse inventory"), Div(
        P("Configure a company in the PostgreSQL fast_erp schema to use warehouse inventory."),
        cls="card"))


def _metric(label, value, href, detail="", tone=""):
    return A(kpi_card(label, value, detail, tone), href=href,
             style="color:inherit;text-decoration:none;")


def dashboard(warehouse_code: str = "", start: str = "", end: str = ""):
    company = _company()
    if company is None:
        return _unavailable()
    reports = WarehouseReports(db.postgres_database())
    end_date = date.fromisoformat(end) if end else date.today()
    start_date = date.fromisoformat(start) if start else end_date - timedelta(days=30)
    data = reports.dashboard(company, warehouse_code or None,
                             start_date=start_date, end_date=end_date)
    totals, expiry = data["totals"], data["expiry"]
    warehouses = data["warehouses"]
    filter_form = Form(
        Select(Option("All warehouses", value=""), *[
            Option(f"{row['code']} · {row['name']}", value=row["code"],
                   selected="selected" if row["code"] == warehouse_code else None)
            for row in warehouses
        ], name="warehouse", cls="wms-select"),
        Input(type="date", name="start", value=start_date.isoformat(),
              aria_label="Movements from", cls="wms-input"),
        Input(type="date", name="end", value=end_date.isoformat(),
              aria_label="Movements to", cls="wms-input"),
        Button("Apply", cls="btn", type="submit"),
        method="get", action="/warehouse", cls="toolbar",
    )
    suffix = f"&warehouse={warehouse_code}" if warehouse_code else ""
    cards = Div(
        _metric("On hand", totals["on_hand"], f"/warehouse/stock?filter=all{suffix}"),
        _metric("Available", totals["available"], f"/warehouse/stock?filter=available{suffix}", tone="ok"),
        _metric("Reserved", totals["reserved"], f"/warehouse/stock?filter=all{suffix}"),
        _metric("Held", totals["held"], f"/warehouse/stock?filter=held{suffix}", tone="danger"),
        cls="kpi-grid",
    )
    expiry_cards = Div(
        _metric("Expiring in 7 days", expiry["days_7"], f"/warehouse/stock?filter=expiring&days=7{suffix}"),
        _metric("Expiring in 30 days", expiry["days_30"], f"/warehouse/stock?filter=expiring&days=30{suffix}"),
        _metric("Expiring in 90 days", expiry["days_90"], f"/warehouse/stock?filter=expiring&days=90{suffix}"),
        _metric("Orders to allocate", data["blocked_orders"], "/warehouse/reservations"),
        cls="kpi-grid",
    )
    daily = data["daily"]
    dates = [str(row["event_date"]) for row in daily]
    incoming = [[day, row["incoming"] or 0] for day, row in zip(dates, daily)]
    outgoing = [[day, row["outgoing"] or 0] for day, row in zip(dates, daily)]
    by_warehouse = [[row["warehouse_code"], row["on_hand"]]
                    for row in data["by_warehouse"]]
    charts = Div(
        wms_charts.chart_card("Receipts · selected dates", "wms-incoming",
                              ["Date", "Quantity"], incoming, chart_type="line"),
        wms_charts.chart_card("Issues · selected dates", "wms-outgoing",
                              ["Date", "Quantity"], outgoing, chart_type="line"),
        cls="grid-2",
    )
    stock_chart = wms_charts.chart_card(
        "On hand by warehouse", "wms-warehouses", ["Warehouse", "Quantity"],
        by_warehouse,
    )
    low_stock = Table(
        Thead(Tr(Th("Item"), Th("On hand", cls="num"),
                 Th("Reorder at", cls="num"))),
        Tbody(*[
            Tr(Td(row["name"]), Td(str(row["stock_qty"]), cls="num"),
               Td(str(row["reorder_level"]), cls="num"))
            for row in data["low_stock"]
        ] or [Tr(Td("No items below reorder level", colspan="3"))]),
        cls="tbl",
    )
    due = []
    now = datetime.now(timezone.utc)
    for zone in data["zones"]:
        last = zone["last_reading"]
        hours = (now-last).total_seconds()/3600 if last else None
        if zone["has_excursion"] or hours is None or hours >= zone["reading_interval_hours"]:
            due.append(zone)
    temperature = Table(
        Thead(Tr(Th("Zone"), Th("Last reading"), Th("Status"))),
        Tbody(*[
            Tr(Td(zone["name"]), Td(str(zone["last_reading"] or "Never")),
               Td("Excursion open" if zone["has_excursion"] else "Reading due"))
            for zone in due
        ] or [Tr(Td("All readings current", colspan="3"))]), cls="tbl",
    )
    blockers = data["allocation_blockers"]
    blocked_table = Table(
        Thead(Tr(Th("Order"), Th("Item"), Th("Needed", cls="num"),
                 Th("Eligible", cls="num"), Th("Reason"))),
        Tbody(*[
            Tr(Td(A(row["order_code"], href=f"/orders/{row['order_id']}")),
               Td(row["item_code"]), Td(str(row["needed"]), cls="num"),
               Td(str(row["eligible"]), cls="num"),
               Td("Expiry / shelf life" if row["available_now"] >= row["needed"]
                  else "Insufficient available stock"))
            for row in blockers
        ] or [Tr(Td("No blocked allocations", colspan="5"))]), cls="tbl",
    )
    discrepancies = reports.reconciliation(company)
    return (
        _title("Inventory Control", "Stock, lots, expiry, movements and temperature.",
               A("Ask inventory data", href="/warehouse/query", cls="btn primary"),
               A("Import opening stock", href="/warehouse/opening", cls="btn"),
               A("Movements", href="/warehouse/movements", cls="btn")),
        filter_form, cards, expiry_cards,
        Div(charts, stock_chart),
        Div(
            Div(Div(H3("Below reorder"), cls="card-header"), low_stock, cls="card"),
            Div(Div(H3("Temperature attention"), cls="card-header"), temperature,
                A("Record a reading", href="/warehouse/temperature", cls="btn"), cls="card"),
            cls="grid-2",
        ),
        Div(Div(H3(f"Allocation blockers · {blockers[0]['total_blockers'] if blockers else 0}"),
                cls="card-header"), blocked_table, cls="card"),
        Div(P("Stock projections match the inventory ledger." if not discrepancies else
              f"Attention: {len(discrepancies)} stock balances need reconciliation."),
            cls="card"),
    )


def stock_list(filter_name: str = "all", warehouse_code: str = "", days: int = 30):
    company = _company()
    if company is None:
        return _unavailable()
    reports = WarehouseReports(db.postgres_database())
    stock = reports.stock_rows(company, warehouse_code=warehouse_code or None,
                              filter_name=filter_name, days=days)
    filters = Div(*[
        A(name.title(), href=f"/warehouse/stock?filter={name}",
          cls="active" if name == filter_name else "")
        for name in ("all", "available", "held", "expiring", "low")
    ], cls="seg")
    table = Table(
        Thead(Tr(Th("Item"), Th("Warehouse"), Th("Location"), Th("Lot"),
                 Th("Serial"), Th("Expiry"), Th("Status"),
                 Th("On hand", cls="num"), Th("Available", cls="num"), Th("Action"))),
        Tbody(*[
            Tr(Td(row["item_code"]), Td(row["warehouse_code"]),
               Td(row["location_code"]),
               Td(A(row["lot_code"], href=f"/warehouse/lots/{row['batch_id']}")
                  if row["batch_id"] else "—"),
               Td(row["serial_code"] or "—"), Td(str(row["expiry_date"] or "—")),
               Td(row["disposition"]), Td(str(row["on_hand"]), cls="num"),
               Td(str(row["available"]), cls="num"),
               Td(A("Move / release", href=f"/warehouse/stock/{row['stock_slice_id']}/transfer"),
                  " · ", A("Count", href=f"/warehouse/stock/{row['stock_slice_id']}/count"),
                  *( [" · ", A("Identify", href=f"/warehouse/stock/{row['stock_slice_id']}/identify")]
                     if row["disposition"] == "Hold" and row["batch_id"] is None
                     and row["serial_number_id"] is None else [])))
            for row in stock
        ] or [Tr(Td("No matching stock", colspan="10"))]), cls="tbl",
    )
    return (_title("Stock by location and lot", f"{len(stock)} stock slices",
                   A("← Inventory Control", href="/warehouse", cls="btn")),
            filters, Div(table, cls="card"))


def movement_list(warehouse_code: str = ""):
    company = _company()
    if company is None:
        return _unavailable()
    rows = WarehouseReports(db.postgres_database()).movements(
        company, warehouse_code=warehouse_code or None
    )
    table = Table(
        Thead(Tr(Th("Date"), Th("Type"), Th("Voucher"), Th("Item"),
                 Th("Warehouse"), Th("Location"), Th("Lot"),
                 Th("Status"), Th("Change", cls="num"))),
        Tbody(*[
            Tr(Td(str(row["event_date"])), Td(row["event_type"]),
               Td(row["voucher_code"]), Td(row["item_code"]),
               Td(row["warehouse_code"]), Td(row["location_code"]),
               Td(row["lot_code"] or "—"), Td(row["disposition"]),
               Td(str(row["quantity_change"]), cls="num"))
            for row in rows
        ] or [Tr(Td("No movements yet", colspan="9"))]), cls="tbl",
    )
    return (_title("Inventory movements", f"Latest {len(rows)} physical movements",
                   A("← Inventory Control", href="/warehouse", cls="btn")),
            Div(table, cls="card"))


def lot_trace(batch_id: int, message: str = ""):
    company = _company()
    if company is None:
        return _unavailable()
    data = WarehouseReports(db.postgres_database()).lot_trace(company, batch_id)
    lot = data["lot"]
    movements = Table(
        Thead(Tr(Th("Date"), Th("Event"), Th("Voucher"), Th("Location"),
                 Th("Customer / supplier"), Th("Change", cls="num"))),
        Tbody(*[
            Tr(Td(str(row["event_date"])), Td(row["event_type"]),
               Td(row["voucher_code"]),
               Td(f"{row['warehouse_code']} / {row['location_code']}"),
               Td(row["customer_name"] or row["supplier_name"] or "—"),
               Td(str(row["quantity_change"]), cls="num"))
            for row in data["movements"]
        ] or [Tr(Td("No movement history", colspan="6"))]), cls="tbl",
    )
    locations = Table(
        Thead(Tr(Th("Warehouse"), Th("Location"), Th("Status"),
                 Th("Quantity", cls="num"))),
        Tbody(*[
            Tr(Td(row["warehouse_code"]), Td(row["location_code"]),
               Td(row["disposition"]), Td(str(row["quantity"]), cls="num"))
            for row in data["locations"]
        ] or [Tr(Td("No stock on hand", colspan="4"))]), cls="tbl",
    )
    hold_form = Form(
        Input(type="hidden", name="batch_id", value=str(batch_id)),
        Input(name="reason", placeholder="Reason for quarantine or recall",
              required=True, cls="wms-input"),
        Button("Hold this lot", type="submit", cls="btn"),
        method="post", action="/warehouse/holds", cls="inline-form",
    )
    active_holds = Div(*[
        Form(
            Span(f"{hold['reason']} · {hold['created_by']} · {hold['created_at']}"),
            Input(name="reason", placeholder="Release review reason", required=True,
                  cls="wms-input"),
            Button("Release hold", type="submit", cls="btn"),
            method="post", action=f"/warehouse/holds/{hold['id']}/release",
            cls="inline-form",
        ) for hold in data["holds"]
    ])
    amend_form = Form(
        Label("Manufactured on"),
        Input(name="manufactured_on", type="date",
              value=str(lot["manufactured_on"] or ""), cls="wms-input"),
        Label("Expires on"),
        Input(name="expires_on", type="date",
              value=str(lot["expiry_date"] or ""), cls="wms-input"),
        Label("Evidence / correction reason"),
        Input(name="reason", required=True, cls="wms-input"),
        Button("Correct lot dates", type="submit", cls="btn"),
        method="post", action=f"/warehouse/lots/{batch_id}/amend",
        cls="wms-receipt-fields",
    )
    amendment_table = Table(
        Thead(Tr(Th("Changed at"), Th("Expiry before"), Th("Expiry after"),
                 Th("Operator"), Th("Reason"))),
        Tbody(*[
            Tr(Td(str(row["amended_at"])), Td(str(row["old_expires_on"] or "—")),
               Td(str(row["new_expires_on"] or "—")), Td(row["amended_by"]),
               Td(row["reason"]))
            for row in data["amendments"]
        ] or [Tr(Td("No date corrections", colspan="5"))]), cls="tbl",
    )
    return (_title(f"Lot {lot['lot_code']}",
                   f"{lot['item_name']} · expires {lot['expiry_date'] or 'not set'}",
                   A("← Stock", href="/warehouse/stock", cls="btn")),
            P(message, cls="paid-note") if message else None,
            Div(kpi_card("On hand", lot["on_hand"]),
                kpi_card("Reserved", lot["reserved"]),
                kpi_card("On hold", "Yes" if lot["on_hold"] else "No"),
                cls="kpi-grid"),
            Div(Div(H3("Current locations"), cls="card-header"), locations, cls="card"),
            Div(Div(H3("Receipt to delivery history"), cls="card-header"),
                movements, cls="card"),
            Div(Div(H3("Quarantine / recall"), cls="card-header"),
                hold_form, active_holds, cls="card"),
            Div(Div(H3("Audited lot date corrections"), cls="card-header"),
                amend_form, amendment_table, cls="card"))


def query_lab():
    company = _company()
    if company is None:
        return _unavailable()
    schema = WarehouseQueryService(db.postgres_database()).schema_prompt()
    examples = [
        "SELECT warehouse_code, SUM(on_hand) AS units FROM wms_stock_report GROUP BY warehouse_code ORDER BY units DESC",
        "SELECT lot_code, expiry_date, SUM(on_hand) AS units FROM wms_stock_report WHERE expiry_date IS NOT NULL GROUP BY lot_code, expiry_date ORDER BY expiry_date LIMIT 20",
        "SELECT event_date, SUM(quantity_change) AS net_movement FROM wms_movement_report GROUP BY event_date ORDER BY event_date",
    ]
    return (
        _title("Inventory Query Lab", "Ask a question or run read-only SQL on tenant-scoped warehouse views.",
               A("← Inventory Control", href="/warehouse", cls="btn")),
        Div(Div(H3("Ask the data"), cls="card-header"),
            Form(Input(name="question", placeholder="Which lots expire in the next 30 days?",
                       cls="wms-input", required=True),
                 Button("Generate SQL and chart", type="submit", cls="btn primary"),
                 hx_post="/warehouse/query/ask", hx_target="#wms-query-result",
                 hx_swap="innerHTML", hx_indicator="#wms-query-progress",
                 cls="inline-form"),
            Div("Generating and validating a read-only inventory query…",
                id="wms-query-progress", cls="wms-query-progress htmx-indicator"),
            cls="card"),
        Div(Div(H3("SQL"), cls="card-header"),
            Form(Textarea(examples[0], name="sql", cls="wms-sqlbox"),
                 Button("Run read-only query", type="submit", cls="btn primary"),
                 hx_post="/warehouse/query/run", hx_target="#wms-query-result",
                 hx_swap="innerHTML"), cls="card"),
        Div(id="wms-query-result"),
        Div(Div(H3("Example queries"), cls="card-header"),
            *[Pre(example) for example in examples], cls="card"),
        Div(Div(H3("Available views"), cls="card-header"), Pre(schema), cls="card"),
    )


def query_result(result: QueryResult, *, question: str = ""):
    numeric = next((index for index in range(1, len(result.columns))
                    if result.rows and all(isinstance(row[index], (int, float, Decimal))
                                           for row in result.rows[:10])), None)
    chart = ()
    if numeric is not None:
        chart = (wms_charts.chart_card(
            "Chart", "wms-ad-hoc-chart", result.columns, result.rows[:50],
            chart_type="line" if any(term in result.columns[0].lower()
                                     for term in ("date", "month", "week")) else "bar",
            y_col=result.columns[numeric],
        ),)
    return Div(
        P(f"Generated from: {question}", cls="sub") if question else None,
        Div(Div(H3("Executed SQL"), cls="card-header"), Pre(result.sql), cls="card"),
        *chart,
        Div(Div(H3("Result"), cls="card-header"),
            wms_charts.result_table(result.columns, result.rows), cls="card"),
    )


def reservations_page(message: str = ""):
    company = _company()
    if company is None:
        return _unavailable()
    lines = WarehouseReports(db.postgres_database()).reservation_lines(company)
    table = Table(
        Thead(Tr(Th("Order"), Th("Customer"), Th("Item"), Th("Warehouse"),
                 Th("Remaining", cls="num"), Th("Reserved", cls="num"), Th("Action"))),
        Tbody(*[
            Tr(Td(A(row["order_code"], href=f"/orders/{row['order_id']}")),
               Td(row["customer_name"] or "—"), Td(row["item_code"]),
               Td(row["warehouse_code"]), Td(str(row["remaining"]), cls="num"),
               Td(str(row["reserved"]), cls="num"),
               Td(Form(Input(type="hidden", name="order_line_id",
                             value=str(row["order_line_id"])),
                       Button("Reserve FEFO", cls="btn primary", type="submit"),
                       method="post", action="/warehouse/reservations"),
                  " · ", A("Choose lot", href=f"/warehouse/reservations/{row['order_line_id']}/override")))
            for row in lines
        ] or [Tr(Td("No open order lines", colspan="7"))]), cls="tbl",
    )
    return (_title("FEFO reservations", "Allocate eligible lots to open sales orders.",
                   A("← Inventory Control", href="/warehouse", cls="btn")),
            P(message, cls="paid-note") if message else None,
            Div(table, cls="card"))


def reservation_override_page(order_line_id: int, message: str = ""):
    company = _company()
    if company is None:
        return _unavailable()
    database = db.postgres_database()
    order_line = database.one(
        """SELECT line.*,ord.code AS order_code,ord.delivery_date,
                  item.code AS item_code,warehouse.code AS warehouse_code,
                  COALESCE(customer.min_remaining_shelf_life_days,0) AS shelf_days
             FROM sales_order_items line
             JOIN sales_orders ord ON ord.id=line.order_id
             JOIN items item ON item.id=line.item_id
             JOIN warehouses warehouse ON warehouse.id=line.warehouse_id
             LEFT JOIN customers customer ON customer.id=ord.customer_id
            WHERE line.id=%s AND ord.company_id=%s""",
        (order_line_id, company),
    )
    if not order_line:
        return (_title("Order line not found"),)
    candidates = database.rows(
        """SELECT slice.id,slice.quantity-slice.reserved_qty AS available,
                  location.code AS location_code,batch.batch_code AS lot_code,
                  batch.expires_on
             FROM stock_slices slice
             JOIN warehouse_locations location ON location.id=slice.location_id
             JOIN items item ON item.id=slice.item_id
             LEFT JOIN batches batch ON batch.id=slice.batch_id
            WHERE slice.company_id=%s AND slice.item_id=%s
              AND slice.warehouse_id=%s AND slice.disposition='Available'
              AND slice.quantity>slice.reserved_qty AND location.pickable=true
              AND location.active=true
              AND (NOT item.tracks_expiry OR batch.expires_on IS NOT NULL)
              AND (batch.expires_on IS NULL OR batch.expires_on >=
                   COALESCE(%s::date,current_date)+%s)
              AND NOT EXISTS (SELECT 1 FROM lot_holds hold
                              WHERE hold.batch_id=slice.batch_id
                                AND hold.released_at IS NULL)
              AND NOT EXISTS (SELECT 1 FROM temperature_excursions excursion
                              WHERE excursion.zone_id=location.temperature_zone_id
                                AND excursion.status='Open')
            ORDER BY batch.expires_on NULLS LAST,slice.first_received_at,slice.id""",
        (company, order_line["item_id"], order_line["warehouse_id"],
         order_line["delivery_date"], order_line["shelf_days"]),
    )
    form = Form(
        Input(type="hidden", name="order_line_id", value=str(order_line_id)),
        Label("Eligible lot / location"),
        Select(*[Option(
            f"{row['lot_code'] or 'Untracked'} · {row['location_code']} · "
            f"{row['available']} available · expires {row['expires_on'] or 'n/a'}",
            value=str(row["id"]),
        ) for row in candidates], name="stock_slice_id", cls="wms-select"),
        Label("Quantity"),
        Input(name="quantity", type="number", min="0.000001", step="0.000001",
              required=True, cls="wms-input"),
        Label("Reason for bypassing FEFO order"),
        Input(name="reason", required=True, cls="wms-input"),
        Button("Reserve selected stock", type="submit", cls="btn primary"),
        method="post", action="/warehouse/reservations/override",
        cls="wms-receipt-fields",
    )
    return (_title(f"Choose lot for {order_line['order_code']}",
                   f"{order_line['item_code']} · {order_line['warehouse_code']} · "
                   f"customer shelf life {order_line['shelf_days']} days",
                   A("← Reservations", href="/warehouse/reservations", cls="btn")),
            P(message, cls="paid-note") if message else None,
            Div(form if candidates else P("No eligible stock for this line."),
                cls="card"))


def temperature_page(message: str = ""):
    company = _company()
    if company is None:
        return _unavailable()
    database = db.postgres_database()
    zones = database.rows(
        """SELECT zone.*,warehouse.code AS warehouse_code
             FROM temperature_zones zone
             JOIN warehouses warehouse ON warehouse.id=zone.warehouse_id
            WHERE zone.company_id=%s ORDER BY warehouse.code,zone.code""",
        (company,),
    )
    warehouses = database.rows(
        "SELECT id,code,name FROM warehouses WHERE company_id=%s AND active=true ORDER BY code",
        (company,),
    )
    readings = database.rows(
        """SELECT reading.*,zone.code AS zone_code
             FROM temperature_readings reading
             JOIN temperature_zones zone ON zone.id=reading.zone_id
            WHERE reading.company_id=%s ORDER BY measured_at DESC LIMIT 30""",
        (company,),
    )
    excursions = database.rows(
        """SELECT excursion.*,zone.code AS zone_code,reading.temperature_c
             FROM temperature_excursions excursion
             JOIN temperature_zones zone ON zone.id=excursion.zone_id
             JOIN temperature_readings reading ON reading.id=excursion.reading_id
            WHERE excursion.company_id=%s AND excursion.status='Open'
            ORDER BY excursion.opened_at DESC""",
        (company,),
    )
    affected = database.rows(
        """SELECT excursion.id AS excursion_id,zone.code AS zone_code,
                  item.code AS item_code,batch.id AS batch_id,
                  batch.batch_code AS lot_code,location.code AS location_code,
                  slice.quantity
             FROM temperature_excursions excursion
             JOIN temperature_zones zone ON zone.id=excursion.zone_id
             JOIN warehouse_locations location ON location.temperature_zone_id=zone.id
             JOIN stock_slices slice ON slice.location_id=location.id AND slice.quantity>0
             JOIN items item ON item.id=slice.item_id
             LEFT JOIN batches batch ON batch.id=slice.batch_id
            WHERE excursion.company_id=%s AND excursion.status='Open'
            ORDER BY zone.code,item.code,location.code LIMIT 200""",
        (company,),
    )
    zone_form = Form(
        Select(*[Option(f"{row['code']} · {row['name']}", value=str(row["id"]))
                 for row in warehouses], name="warehouse_id", cls="wms-select"),
        Input(name="code", placeholder="Zone code", required=True, cls="wms-input"),
        Input(name="name", placeholder="Zone name", required=True, cls="wms-input"),
        Input(name="minimum_c", type="number", step="0.001", placeholder="Min °C",
              required=True, cls="wms-input"),
        Input(name="maximum_c", type="number", step="0.001", placeholder="Max °C",
              required=True, cls="wms-input"),
        Button("Add zone", type="submit", cls="btn"),
        method="post", action="/warehouse/temperature/zones", cls="inline-form",
    )
    reading_form = Form(
        Select(*[Option(f"{row['warehouse_code']} / {row['code']}", value=str(row["id"]))
                 for row in zones], name="zone_id", cls="wms-select"),
        Input(name="temperature_c", type="number", step="0.001", required=True,
              placeholder="Reading °C", cls="wms-input"),
        Input(name="note", placeholder="Notes", cls="wms-input"),
        Button("Record reading", type="submit", cls="btn primary"),
        method="post", action="/warehouse/temperature/readings", cls="inline-form",
    )
    excursion_table = Table(
        Thead(Tr(Th("Zone"), Th("Reading"), Th("Opened"), Th("Review"))),
        Tbody(*[
            Tr(Td(row["zone_code"]), Td(f"{row['temperature_c']} °C"),
               Td(str(row["opened_at"])),
               Td(Form(Input(type="hidden", name="excursion_id", value=str(row["id"])),
                       Input(name="reason", placeholder="Review reason", required=True,
                             cls="wms-input"),
                       Button("Release", cls="btn", type="submit"),
                       method="post", action="/warehouse/temperature/release",
                       cls="inline-form")))
            for row in excursions
        ] or [Tr(Td("No open excursions", colspan="4"))]), cls="tbl",
    )
    reading_table = Table(
        Thead(Tr(Th("Zone"), Th("Measured at"), Th("Temperature"), Th("Operator"))),
        Tbody(*[
            Tr(Td(row["zone_code"]), Td(str(row["measured_at"])),
               Td(f"{row['temperature_c']} °C"), Td(row["recorded_by"]))
            for row in readings
        ] or [Tr(Td("No readings yet", colspan="4"))]), cls="tbl",
    )
    affected_table = Table(
        Thead(Tr(Th("Zone"), Th("Item"), Th("Lot"), Th("Location"),
                 Th("On hand", cls="num"))),
        Tbody(*[
            Tr(Td(row["zone_code"]), Td(row["item_code"]),
               Td(A(row["lot_code"], href=f"/warehouse/lots/{row['batch_id']}")
                  if row["batch_id"] else "—"),
               Td(row["location_code"]), Td(str(row["quantity"]), cls="num"))
            for row in affected
        ] or [Tr(Td("No stock affected", colspan="5"))]), cls="tbl",
    )
    return (_title("Temperature control", "Manual readings and review of affected stock.",
                   A("← Inventory Control", href="/warehouse", cls="btn")),
            P(message, cls="paid-note") if message else None,
            Div(Div(H3("Create temperature zone"), cls="card-header"), zone_form, cls="card"),
            Div(Div(H3("Record a reading"), cls="card-header"), reading_form, cls="card"),
            Div(Div(H3("Open excursions"), cls="card-header"), excursion_table, cls="card"),
            Div(Div(H3("Affected stock on hold"), cls="card-header"),
                affected_table, cls="card"),
            Div(Div(H3("Recent readings"), cls="card-header"), reading_table, cls="card"))


def locations_page(message: str = ""):
    company = _company()
    if company is None:
        return _unavailable()
    database = db.postgres_database()
    warehouses = database.rows(
        "SELECT id,code,name FROM warehouses WHERE company_id=%s AND active=true ORDER BY code",
        (company,),
    )
    locations = database.rows(
        """SELECT location.*,warehouse.code AS warehouse_code,
                  zone.code AS zone_code
             FROM warehouse_locations location
             JOIN warehouses warehouse ON warehouse.id=location.warehouse_id
             LEFT JOIN temperature_zones zone ON zone.id=location.temperature_zone_id
            WHERE location.company_id=%s ORDER BY warehouse.code,location.code""",
        (company,),
    )
    zones = database.rows(
        "SELECT id,code FROM temperature_zones WHERE company_id=%s AND active=true",
        (company,),
    )
    form = Form(
        Select(*[Option(row["code"], value=str(row["id"])) for row in warehouses],
               name="warehouse_id", cls="wms-select"),
        Input(name="code", placeholder="Bin code", required=True, cls="wms-input"),
        Input(name="name", placeholder="Location name", required=True, cls="wms-input"),
        Select(Option("No temperature zone", value=""),
               *[Option(row["code"], value=str(row["id"])) for row in zones],
               name="temperature_zone_id", cls="wms-select"),
        Button("Add location", type="submit", cls="btn primary"),
        method="post", action="/warehouse/locations", cls="inline-form",
    )
    table = Table(
        Thead(Tr(Th("Warehouse"), Th("Code"), Th("Name"), Th("Type"),
                 Th("Temperature zone"))),
        Tbody(*[
            Tr(Td(row["warehouse_code"]), Td(row["code"]), Td(row["name"]),
               Td(row["location_type"]), Td(row["zone_code"] or "—"))
            for row in locations
        ] or [Tr(Td("No locations yet", colspan="5"))]), cls="tbl",
    )
    return (_title("Warehouse locations", "Configure bins and their temperature zones.",
                   A("← Inventory Control", href="/warehouse", cls="btn")),
            P(message, cls="paid-note") if message else None,
            Div(Div(H3("Add location"), cls="card-header"), form, cls="card"),
            Div(table, cls="card"))


def receipt_form(purchase_order_id: int, message: str = ""):
    company = _company()
    if company is None:
        return _unavailable()
    database = db.postgres_database()
    order = database.one(
        "SELECT * FROM purchase_orders WHERE id=%s AND company_id=%s",
        (purchase_order_id, company),
    )
    if not order:
        return (_title("Purchase order not found"),)
    lines = database.rows(
        """SELECT line.*,item.code AS item_code,item.name AS item_name,
                  item.tracks_batches,item.tracks_serials,item.tracks_expiry,
                  warehouse.code AS warehouse_code
             FROM purchase_order_items line
             JOIN items item ON item.id=line.item_id
             JOIN warehouses warehouse ON warehouse.id=line.warehouse_id
            WHERE line.po_id=%s AND line.qty-line.received_qty+line.returned_qty>0
            ORDER BY line.line_number""",
        (purchase_order_id,),
    )
    locations = database.rows(
        """SELECT location.id,location.warehouse_id,location.code,
                  warehouse.code AS warehouse_code
             FROM warehouse_locations location
             JOIN warehouses warehouse ON warehouse.id=location.warehouse_id
            WHERE location.company_id=%s AND location.active=true
            ORDER BY warehouse.code,location.code""",
        (company,),
    )
    cards = []
    for line in lines:
        prefix = f"line_{line['id']}_"
        loc_options = [Option("Default location", value="")]
        loc_options.extend(
            Option(location["code"], value=str(location["id"]))
            for location in locations if location["warehouse_id"] == line["warehouse_id"]
        )
        controls = [
            Input(type="hidden", name=prefix+"order_line_id", value=str(line["id"])),
            Label("Accepted quantity"),
            Input(name=prefix+"accepted", type="number", min="0", step="0.000001",
                  value=str(line["qty"]-line["received_qty"]+line["returned_qty"]),
                  cls="wms-input"),
            Label("Rejected quantity"),
            Input(name=prefix+"rejected", type="number", min="0", step="0.000001",
                  value="0", cls="wms-input"),
            Label("Location"),
            Select(*loc_options, name=prefix+"location_id", cls="wms-select"),
        ]
        if line["tracks_batches"]:
            controls.extend([
                Label("Lot number"),
                Input(name=prefix+"lot_code", placeholder="Supplier lot", cls="wms-input"),
                Label("Manufactured on"),
                Input(name=prefix+"manufactured_on", type="date", cls="wms-input"),
                Label("Expires on"),
                Input(name=prefix+"expires_on", type="date", cls="wms-input",
                      required=line["tracks_expiry"]),
            ])
        if line["tracks_serials"]:
            controls.extend([
                Label("Serial numbers"),
                Textarea(name=prefix+"serials", cls="wms-sqlbox",
                         placeholder="One serial per line or separated by commas"),
            ])
        cards.append(Div(
            Div(H3(f"{line['item_code']} · {line['item_name']}"), cls="card-header"),
            P(f"{line['warehouse_code']} · {line['qty']-line['received_qty']+line['returned_qty']} remaining",
              cls="sub"),
            Div(*controls, cls="wms-receipt-fields"), cls="card",
        ))
    return (_title(f"Receive {order['code']}",
                   "Enter lot, expiry and serial details before stock is posted.",
                   A("← Purchase order", href=f"/purchase/{purchase_order_id}", cls="btn")),
            P(message, cls="paid-note") if message else None,
            Form(*cards, Button("Post receipt", type="submit", cls="btn primary"),
                 method="post", action=f"/purchase/{purchase_order_id}/receive")
            if cards else Div(P("This purchase order has no quantity left to receive."), cls="card"))


def transfer_page(stock_slice_id: int, message: str = ""):
    company = _company()
    if company is None:
        return _unavailable()
    database = db.postgres_database()
    source = database.one(
        """SELECT slice.*,item.code AS item_code,location.code AS location_code,
                  batch.batch_code AS lot_code
             FROM stock_slices slice
             JOIN items item ON item.id=slice.item_id
             JOIN warehouse_locations location ON location.id=slice.location_id
             LEFT JOIN batches batch ON batch.id=slice.batch_id
            WHERE slice.id=%s AND slice.company_id=%s""",
        (stock_slice_id, company),
    )
    if not source:
        return (_title("Stock slice not found"),)
    locations = database.rows(
        """SELECT location.id,location.code,location.name,
                  warehouse.code AS warehouse_code
             FROM warehouse_locations location
             JOIN warehouses warehouse ON warehouse.id=location.warehouse_id
            WHERE location.company_id=%s AND location.active=true
            ORDER BY warehouse.code,location.code""",
        (company,),
    )
    form = Form(
        Input(type="hidden", name="stock_slice_id", value=str(stock_slice_id)),
        Label("Quantity"),
        Input(name="quantity", type="number", min="0.000001", step="0.000001",
              max=str(source["quantity"]-source["reserved_qty"]), required=True,
              cls="wms-input"),
        Label("Destination"),
        Select(*[Option(f"{row['warehouse_code']} / {row['code']} · {row['name']}",
                        value=str(row["id"]))
                 for row in locations], name="destination_location_id", cls="wms-select"),
        Label("New status"),
        Select(*[Option(status, value=status,
                        selected="selected" if status == source["disposition"] else None)
                 for status in ("Available", "QC", "Hold", "Rejected", "Damaged")],
               name="disposition", cls="wms-select"),
        Label("Reason"),
        Input(name="reason", required=True, placeholder="Putaway, QC release, damage…",
              cls="wms-input"),
        Button("Move stock", type="submit", cls="btn primary"),
        method="post", action="/warehouse/stock/transfer",
        cls="wms-receipt-fields",
    )
    return (_title(f"Move {source['item_code']}",
                   f"{source['location_code']} · lot {source['lot_code'] or 'none'} · "
                   f"{source['quantity']-source['reserved_qty']} unreserved",
                   A("← Stock", href="/warehouse/stock", cls="btn")),
            P(message, cls="paid-note") if message else None,
            Div(form, cls="card"))


def count_page(stock_slice_id: int, message: str = ""):
    company = _company()
    if company is None:
        return _unavailable()
    database = db.postgres_database()
    source = database.one(
        """SELECT slice.*,item.code AS item_code,location.code AS location_code,
                  batch.batch_code AS lot_code
             FROM stock_slices slice
             JOIN items item ON item.id=slice.item_id
             JOIN warehouse_locations location ON location.id=slice.location_id
             LEFT JOIN batches batch ON batch.id=slice.batch_id
            WHERE slice.id=%s AND slice.company_id=%s""",
        (stock_slice_id, company),
    )
    if not source:
        return (_title("Stock slice not found"),)
    accounts = database.rows(
        """SELECT id,code,name FROM accounts
            WHERE company_id=%s AND account_type='Expense' AND active=true
            ORDER BY code""",
        (company,),
    )
    form = Form(
        Input(type="hidden", name="stock_slice_id", value=str(stock_slice_id)),
        Label("Counted quantity"),
        Input(name="counted_quantity", type="number", min="0", step="0.000001",
              value=str(source["quantity"]), required=True, cls="wms-input"),
        Label("Adjustment expense account"),
        Select(*[Option(f"{row['code']} · {row['name']}", value=str(row["id"]))
                 for row in accounts], name="adjustment_account_id", cls="wms-select"),
        Label("Reason"),
        Input(name="reason", required=True,
              placeholder="Cycle count, damage, recount…", cls="wms-input"),
        Button("Post count", type="submit", cls="btn primary"),
        method="post", action="/warehouse/stock/count",
        cls="wms-receipt-fields",
    )
    return (_title(f"Count {source['item_code']}",
                   f"{source['location_code']} · lot {source['lot_code'] or 'none'} · "
                   f"expected {source['quantity']}",
                   A("← Stock", href="/warehouse/stock", cls="btn")),
            P(message, cls="paid-note") if message else None,
            Div(form, cls="card"))


def opening_import_page(message: str = ""):
    company = _company()
    if company is None:
        return _unavailable()
    accounts = db.postgres_database().rows(
        """SELECT id,code,name FROM accounts WHERE company_id=%s
            AND account_type='Equity' AND active=true ORDER BY code""",
        (company,),
    )
    example = ("item_code,warehouse_code,location_code,quantity,unit_cost,"
               "batch_code,manufactured_on,expires_on,serial_code,disposition\n"
               "FOOD,W1,DEFAULT,5,10,LOT-01,2026-09-01,2027-03-01,,Available")
    form = Form(
        Label("Opening equity account"),
        Select(*[Option(f"{row['code']} · {row['name']}", value=str(row["id"]))
                 for row in accounts], name="offset_account_id", cls="wms-select"),
        Label("CSV rows (maximum 1000)"),
        Textarea(example, name="csv_text", cls="wms-sqlbox", rows="9", required=True),
        Label("Reason"),
        Input(name="reason", placeholder="Initial audited opening balance",
              required=True, cls="wms-input"),
        Button("Import and post", type="submit", cls="btn primary"),
        method="post", action="/warehouse/opening", cls="wms-receipt-fields",
    )
    return (_title("Opening stock import",
                   "Post verified lots, serials, quantities and value before the first item posting.",
                   A("← Inventory Control", href="/warehouse", cls="btn")),
            P(message, cls="paid-note") if message else None,
            Div(form, cls="card"))


def identify_page(stock_slice_id: int, message: str = ""):
    company = _company()
    if company is None:
        return _unavailable()
    database = db.postgres_database()
    source = database.one(
        """SELECT slice.*,item.code AS item_code,item.tracks_batches,
                  item.tracks_serials,item.tracks_expiry
             FROM stock_slices slice
             JOIN items item ON item.id=slice.item_id
            WHERE slice.id=%s AND slice.company_id=%s""",
        (stock_slice_id, company),
    )
    if not source:
        return (_title("Stock slice not found"),)
    locations = database.rows(
        """SELECT id,code,name FROM warehouse_locations
            WHERE company_id=%s AND warehouse_id=%s AND active=true ORDER BY code""",
        (company, source["warehouse_id"]),
    )
    fields = [
        Input(type="hidden", name="stock_slice_id", value=str(stock_slice_id)),
        Label("Quantity to identify"),
        Input(name="quantity", type="number", min="0.000001", step="0.000001",
              max=str(source["quantity"]), required=True, cls="wms-input"),
        Label("Verified location"),
        Select(*[Option(f"{row['code']} · {row['name']}", value=str(row["id"]))
                 for row in locations], name="location_id", cls="wms-select"),
    ]
    if source["tracks_batches"]:
        fields.extend([
            Label("Lot code"), Input(name="batch_code", required=True, cls="wms-input"),
            Label("Manufactured on"), Input(name="manufactured_on", type="date", cls="wms-input"),
            Label("Expires on"), Input(name="expires_on", type="date",
                                      required=source["tracks_expiry"], cls="wms-input"),
        ])
    if source["tracks_serials"]:
        fields.extend([
            Label("Serial number"), Input(name="serial_code", required=True,
                                          cls="wms-input"),
        ])
    fields.extend([
        Label("Evidence / reason"),
        Input(name="reason", required=True, placeholder="Verified against supplier label",
              cls="wms-input"),
        Button("Identify into hold", type="submit", cls="btn primary"),
    ])
    return (_title(f"Identify held {source['item_code']}",
                   f"{source['quantity']} unidentified units. Identified units stay on hold until review.",
                   A("← Stock", href="/warehouse/stock", cls="btn")),
            P(message, cls="paid-note") if message else None,
            Div(Form(*fields, method="post", action="/warehouse/stock/identify",
                     cls="wms-receipt-fields"), cls="card"))
