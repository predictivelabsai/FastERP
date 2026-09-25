"""Warehouse stock identity, FEFO, returns, temperature, and query guard."""

from __future__ import annotations

import os
import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg import sql

from fasterp.config import DatabaseSettings
from fasterp.database import Database
from fasterp.errors import DomainError, InsufficientStockError
from fasterp.inventory import InventoryLine, InventoryService
from fasterp.sales import DeliveryLine, ReturnLine, SalesService
from fasterp.warehouse import OpeningStockRow, StockAllocation, WarehouseService
from fasterp.warehouse_query import WarehouseQueryError, WarehouseQueryService
from scripts.migrate_postgres import apply_migrations


@pytest.fixture(scope="module")
def wms():
    load_dotenv()
    url = os.getenv("DB_URL")
    if not url:
        pytest.skip("DB_URL is not configured")
    schema = f"fast_erp_wms_test_{uuid.uuid4().hex[:12]}"
    apply_migrations(url, schema)
    database = Database(DatabaseSettings(url, schema, pool_min_size=0, pool_max_size=3))
    try:
        with database.transaction() as connection:
            company = connection.execute(
                """INSERT INTO companies(code,name,country_code,local_currency,timezone)
                   VALUES ('WMS','WMS Test','GB','GBP','UTC') RETURNING id"""
            ).fetchone()["id"]
            warehouse = connection.execute(
                """INSERT INTO warehouses(company_id,code,name)
                   VALUES (%s,'W1','Main warehouse') RETURNING id""",
                (company,),
            ).fetchone()["id"]
            item = connection.execute(
                """INSERT INTO items(company_id,code,name,uom,tracks_batches,tracks_expiry)
                   VALUES (%s,'FOOD','Food','Each',true,true) RETURNING id""",
                (company,),
            ).fetchone()["id"]
            customer = connection.execute(
                """INSERT INTO customers(company_id,code,name,min_remaining_shelf_life_days)
                   VALUES (%s,'C1','Customer',10) RETURNING id""",
                (company,),
            ).fetchone()["id"]
            inventory_account = connection.execute(
                """INSERT INTO accounts(company_id,code,name,account_type,normal_side)
                   VALUES (%s,'1200','Inventory','Asset','Debit') RETURNING id""",
                (company,),
            ).fetchone()["id"]
            cogs_account = connection.execute(
                """INSERT INTO accounts(company_id,code,name,account_type,normal_side)
                   VALUES (%s,'5000','COGS','Expense','Debit') RETURNING id""",
                (company,),
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO company_accounting_settings
                   (company_id,inventory_account_id,cogs_account_id)
                   VALUES (%s,%s,%s)""",
                (company, inventory_account, cogs_account),
            )
            order = connection.execute(
                """INSERT INTO sales_orders
                   (company_id,code,customer_id,order_date,delivery_date,status,
                    currency,total,document_state)
                   VALUES (%s,'SO-WMS',%s,'2026-09-24','2026-09-25',
                           'Confirmed','GBP',60,'Posted') RETURNING id""",
                (company, customer),
            ).fetchone()["id"]
            order_line = connection.execute(
                """INSERT INTO sales_order_items
                   (order_id,line_number,item_id,warehouse_id,qty,rate,amount)
                   VALUES (%s,1,%s,%s,6,10,60) RETURNING id""",
                (order, item, warehouse),
            ).fetchone()["id"]
        yield {
            "database": database, "company": company, "warehouse": warehouse,
            "item": item, "customer": customer, "order": order,
            "order_line": order_line,
        }
    finally:
        database.close()
        with psycopg.connect(url) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema)
            ))


def test_fefo_returns_holds_temperature_and_reports(wms):
    database = wms["database"]
    inventory = InventoryService(database)
    warehouse = WarehouseService(database)
    sales = SalesService(database)
    queries = WarehouseQueryService(database)
    zone = warehouse.create_temperature_zone(
        company_id=wms["company"], warehouse_id=wms["warehouse"],
        code="COLD", name="Cold room", minimum_c=Decimal("2"),
        maximum_c=Decimal("8"),
    )
    location = warehouse.create_location(
        company_id=wms["company"], warehouse_id=wms["warehouse"],
        code="A-01", name="Cold shelf", actor="tester",
        temperature_zone_id=zone,
    )
    for voucher, code, amount, expiry in (
        (1, "EARLY", 3, date(2026, 10, 10)),
        (2, "LATE", 5, date(2026, 12, 1)),
    ):
        inventory.post_event(
            company_id=wms["company"], event_type="Receipt",
            voucher_type="Test Receipt", voucher_id=voucher,
            voucher_code=f"TR-{voucher}", event_date=date(2026, 9, 24),
            lines=[InventoryLine(
                wms["item"], wms["warehouse"], Decimal(amount), Decimal("10"),
                allocations=(StockAllocation(
                    Decimal(amount), location_id=location, batch_code=code,
                    expires_on=expiry,
                ),),
            )], actor="tester",
        )
    warehouse.set_customer_shelf_life(
        wms["customer"], company_id=wms["company"], days=20,
    )
    from fasterp.warehouse_reports import WarehouseReports
    blockers = WarehouseReports(database).dashboard(wms["company"])["allocation_blockers"]
    assert len(blockers) == 1
    assert blockers[0]["order_code"] == "SO-WMS"
    assert blockers[0]["needed"] == Decimal("6.000000")
    assert blockers[0]["eligible"] == Decimal("5.000000")
    assert blockers[0]["available_now"] == Decimal("8.000000")
    shelf_life_reservation = warehouse.reserve_order_line(
        wms["order_line"], company_id=wms["company"], actor="tester",
        requested_quantity=Decimal("3"),
    )
    late_id = database.scalar("SELECT id FROM batches WHERE batch_code='LATE'")
    assert [row["batch_id"] for row in shelf_life_reservation] == [late_id]
    amendment_id = warehouse.amend_lot_dates(
        late_id, company_id=wms["company"], manufactured_on=None,
        expires_on=date(2026, 12, 2), actor="tester",
        reason="Corrected from supplier certificate",
    )
    assert database.scalar(
        "SELECT count(*) FROM stock_reservations WHERE status='Active'"
    ) == 0
    assert database.scalar(
        "SELECT reason FROM warehouse_lot_amendments WHERE id=%s",
        (amendment_id,),
    ) == "Corrected from supplier certificate"
    warehouse.release_order_reservations(
        wms["order_line"], company_id=wms["company"],
        actor="tester", reason="Shelf-life test",
    )
    warehouse.set_customer_shelf_life(
        wms["customer"], company_id=wms["company"], days=10,
    )
    late_slice_id = database.scalar(
        "SELECT id FROM stock_slices WHERE batch_id=%s AND quantity>0",
        (late_id,),
    )
    override = warehouse.reserve_order_line(
        wms["order_line"], company_id=wms["company"], actor="tester",
        requested_quantity=Decimal("1"), preferred_slice_id=late_slice_id,
        override_reason="Customer approved alternate lot",
    )
    assert [row["batch_id"] for row in override] == [late_id]
    assert database.scalar("SELECT count(*) FROM warehouse_allocation_overrides") == 1
    warehouse.release_order_reservations(
        wms["order_line"], company_id=wms["company"],
        actor="tester", reason="Return to FEFO",
    )
    reserved = warehouse.reserve_order_line(
        wms["order_line"], company_id=wms["company"], actor="tester"
    )
    assert [row["quantity"] for row in reserved] == [Decimal("3.000000"), Decimal("3.000000")]
    delivery = sales.deliver(
        wms["order"], delivery_date=date(2026, 9, 25),
        lines=[DeliveryLine(wms["order_line"], Decimal("6"))], actor="tester",
    )
    delivery_line = database.scalar(
        "SELECT id FROM sales_delivery_items WHERE delivery_id=%s", (delivery,)
    )
    shipped = database.rows(
        """SELECT batch.batch_code,split.quantity_change
             FROM inventory_allocation_splits split
             JOIN batches batch ON batch.id=split.batch_id
             JOIN inventory_ledger_entries ledger ON ledger.id=split.ledger_entry_id
             JOIN inventory_events event ON event.id=ledger.event_id
            WHERE event.voucher_type='Sales Delivery' ORDER BY split.id"""
    )
    assert [(row["batch_code"], row["quantity_change"]) for row in shipped] == [
        ("EARLY", Decimal("-3.000000")), ("LATE", Decimal("-3.000000"))
    ]
    early_id = database.scalar("SELECT id FROM batches WHERE batch_code='EARLY'")
    returned = sales.return_delivery(
        delivery, return_date=date(2026, 9, 26),
        lines=[ReturnLine(
            delivery_line, Decimal("2"),
            (StockAllocation(Decimal("2"), batch_id=early_id),),
        )], actor="tester",
    )
    assert returned
    assert database.scalar(
        "SELECT sum(quantity) FROM stock_slices WHERE batch_id=%s AND disposition='QC'",
        (early_id,),
    ) == Decimal("2.000000")
    other_location = warehouse.create_location(
        company_id=wms["company"], warehouse_id=wms["warehouse"],
        code="QC-01", name="Review shelf", actor="tester",
    )
    qc_slice = database.scalar(
        "SELECT id FROM stock_slices WHERE batch_id=%s AND disposition='QC'",
        (early_id,),
    )
    warehouse.transfer_location(
        qc_slice, company_id=wms["company"],
        destination_location_id=other_location, amount=Decimal("1"),
        disposition="QC", actor="tester", reason="Move for review",
    )
    hold = warehouse.hold_lot(late_id, company_id=wms["company"],
                              actor="tester", reason="Recall")
    with pytest.raises(InsufficientStockError):
        warehouse.reserve_order_line(wms["order_line"], company_id=wms["company"],
                                     actor="tester")
    warehouse.release_lot_hold(hold, company_id=wms["company"],
                               actor="tester", reason="Cleared")
    warehouse.record_temperature(
        zone, company_id=wms["company"],
        measured_at=datetime(2026, 9, 26, 12, tzinfo=timezone.utc),
        temperature_c=Decimal("12"), actor="tester",
    )
    with pytest.raises(InsufficientStockError):
        warehouse.reserve_order_line(wms["order_line"], company_id=wms["company"],
                                     actor="tester")
    excursion_id = database.scalar("SELECT id FROM temperature_excursions")
    warehouse.release_temperature_excursion(
        excursion_id, company_id=wms["company"], actor="tester",
        reason="Product cleared after review"
    )
    trace = __import__("fasterp.warehouse_reports", fromlist=["WarehouseReports"])
    report = trace.WarehouseReports(database).lot_trace(wms["company"], early_id)
    assert any(row["customer_name"] == "Customer" for row in report["movements"])
    assert len([row for row in report["movements"] if row["event_type"] == "Transfer"]) == 2
    assert any(row["event_type"].startswith("Reservation") for row in report["movements"])
    assert trace.WarehouseReports(database).reconciliation(wms["company"]) == []
    with database.transaction() as connection:
        second_warehouse = connection.execute(
            """INSERT INTO warehouses(company_id,code,name)
               VALUES (%s,'W2','Overflow warehouse') RETURNING id""",
            (wms["company"],),
        ).fetchone()["id"]
    destination = warehouse.create_location(
        company_id=wms["company"], warehouse_id=second_warehouse,
        code="B-01", name="Overflow shelf", actor="tester",
    )
    late_slice = database.scalar(
        """SELECT id FROM stock_slices WHERE batch_id=%s AND quantity>0
            AND disposition='Available'""", (late_id,),
    )
    value_before = database.scalar(
        "SELECT sum(inventory_value) FROM inventory_balances WHERE company_id=%s",
        (wms["company"],),
    )
    warehouse.transfer_warehouse(
        late_slice, company_id=wms["company"],
        destination_location_id=destination, amount=Decimal("1"),
        actor="tester", reason="Overflow move",
    )
    assert database.scalar(
        "SELECT sum(inventory_value) FROM inventory_balances WHERE company_id=%s",
        (wms["company"],),
    ) == value_before
    assert trace.WarehouseReports(database).reconciliation(wms["company"]) == []
    scoped_dashboard = trace.WarehouseReports(database).dashboard(
        wms["company"], warehouse_code="W1"
    )
    assert [row["warehouse_code"] for row in scoped_dashboard["by_warehouse"]] == ["W1"]
    result = queries.run(
        "SELECT item_code, SUM(on_hand) AS quantity FROM wms_stock_report GROUP BY item_code",
        company_id=wms["company"],
    )
    assert result.rows == [["FOOD", Decimal("4.000000")]]
    assert queries.run("SELECT item_code FROM wms_stock_report",
                       company_id=wms["company"] + 100).rows == []
    count_id = warehouse.count_stock(
        qc_slice, company_id=wms["company"], counted_quantity=Decimal("0"),
        adjustment_account_id=database.scalar(
            "SELECT id FROM accounts WHERE company_id=%s AND code='5000'",
            (wms["company"],),
        ), actor="tester", reason="Cycle count shortage",
    )
    assert database.scalar(
        "SELECT variance_qty FROM warehouse_stock_counts WHERE id=%s", (count_id,)
    ) == Decimal("-1.000000")
    assert database.scalar(
        """SELECT count(*) FROM gl_entries
            WHERE voucher_type='Warehouse Stock Count' AND voucher_id=%s""",
        (count_id,),
    ) == 2
    assert trace.WarehouseReports(database).reconciliation(wms["company"]) == []


@pytest.mark.parametrize("statement", [
    "DELETE FROM wms_stock_report",
    "SELECT * FROM fast_erp.companies",
    "SELECT * FROM fast_erp.wms_stock_report",
    "SELECT pg_sleep(1) FROM wms_stock_report",
    "SELECT * FROM wms_stock_report FOR UPDATE",
    "SELECT * FROM wms_stock_report; SELECT * FROM wms_lot_report",
])
def test_query_guard_rejects_unsafe_sql(statement):
    from fasterp.warehouse_query import validate_query

    with pytest.raises(WarehouseQueryError):
        validate_query(statement)


def test_ai_query_generates_executes_and_charges_only_valid_results(wms, monkeypatch):
    from web import wms_ai

    class Model:
        content = ""

        async def ainvoke(self, messages):
            assert "wms_stock_report" in messages[0].content
            assert messages[1].content == "How many stock slices are there?"
            return SimpleNamespace(content=self.content)

    model = Model()
    charges = []
    gate = SimpleNamespace(blocked=False, llm=model,
                           commit=lambda: charges.append("charged"))
    monkeypatch.setattr(wms_ai.byok, "begin_query", lambda session: gate)
    monkeypatch.setattr(wms_ai.db, "postgres_database", lambda: wms["database"])

    model.content = "```sql\nSELECT COUNT(*) AS slices FROM wms_stock_report\n```"
    result = asyncio.run(wms_ai.ask_warehouse(
        "How many stock slices are there?", {"user": "tester"}, wms["company"]
    ))
    assert result.columns == ["slices"]
    assert len(result.rows) == 1
    assert result.rows[0][0] >= 0
    assert charges == ["charged"]

    model.content = "DELETE FROM wms_stock_report"
    with pytest.raises(WarehouseQueryError):
        asyncio.run(wms_ai.ask_warehouse(
            "How many stock slices are there?", {"user": "tester"}, wms["company"]
        ))
    assert charges == ["charged"]


def test_dashboard_and_query_lab_render(wms, monkeypatch):
    from fasthtml.common import to_xml
    from web import wms_views

    monkeypatch.setattr(wms_views, "_company", lambda: wms["company"])
    monkeypatch.setattr(wms_views.db, "postgres_database", lambda: wms["database"])
    for builder, marker in (
        (wms_views.dashboard, "Inventory Control"),
        (wms_views.query_lab, "Inventory Query Lab"),
        (wms_views.locations_page, "Warehouse locations"),
        (wms_views.temperature_page, "Temperature control"),
    ):
        html = "".join(to_xml(part) for part in builder() if part is not None)
        assert marker in html
        if builder is wms_views.query_lab:
            assert 'hx-indicator="#wms-query-progress"' in html


def test_controlled_opening_stock_import(wms):
    database = wms["database"]
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO items(company_id,code,name,uom,tracks_batches,tracks_expiry)
               VALUES (%s,'OPEN','Opening item','Each',true,true)""",
            (wms["company"],),
        )
        equity = connection.execute(
            """INSERT INTO accounts(company_id,code,name,account_type,normal_side)
               VALUES (%s,'3000','Opening equity','Equity','Credit') RETURNING id""",
            (wms["company"],),
        ).fetchone()["id"]
    row = OpeningStockRow(
        "OPEN", "W1", "DEFAULT", Decimal("2"), Decimal("5"),
        batch_code="OPEN-LOT", expires_on=date(2027, 3, 1),
    )
    service = WarehouseService(database)
    import_id = service.import_opening_stock(
        [row], company_id=wms["company"], offset_account_id=equity,
        actor="tester", reason="Initial verified balance",
    )
    assert database.scalar(
        """SELECT sum(quantity) FROM stock_slices slice
            JOIN items item ON item.id=slice.item_id WHERE item.code='OPEN'"""
    ) == Decimal("2.000000")
    assert database.scalar(
        """SELECT count(*) FROM gl_entries
            WHERE voucher_type='Warehouse Opening Import' AND voucher_id=%s""",
        (import_id,),
    ) == 2
    with pytest.raises(DomainError):
        service.import_opening_stock(
            [row], company_id=wms["company"], offset_account_id=equity,
            actor="tester", reason="Duplicate",
        )
    from fasterp.warehouse_reports import WarehouseReports
    assert WarehouseReports(database).reconciliation(wms["company"]) == []


def test_legacy_held_stock_identification(wms):
    database = wms["database"]
    with database.transaction() as connection:
        item_id = connection.execute(
            """INSERT INTO items(company_id,code,name,uom,tracks_batches,tracks_expiry)
               VALUES (%s,'LEGACY','Legacy item','Each',true,true)
               RETURNING id""", (wms["company"],),
        ).fetchone()["id"]
        location_id = connection.execute(
            """SELECT id FROM warehouse_locations
                WHERE company_id=%s AND warehouse_id=%s AND code='DEFAULT'""",
            (wms["company"], wms["warehouse"]),
        ).fetchone()["id"]
        connection.execute(
            """INSERT INTO inventory_balances
               (company_id,item_id,warehouse_id,quantity,inventory_value,valuation_rate)
               VALUES (%s,%s,%s,2,20,10)""",
            (wms["company"], item_id, wms["warehouse"]),
        )
        legacy_slice = connection.execute(
            """INSERT INTO stock_slices
               (company_id,item_id,warehouse_id,location_id,disposition,quantity)
               VALUES (%s,%s,%s,%s,'Hold',2) RETURNING id""",
            (wms["company"], item_id, wms["warehouse"], location_id),
        ).fetchone()["id"]
    service = WarehouseService(database)
    service.identify_legacy_stock(
        legacy_slice, company_id=wms["company"],
        allocation=StockAllocation(
            Decimal("1"), location_id=location_id,
            batch_code="LEG-01", expires_on=date(2027, 4, 1),
        ), actor="tester", reason="Matched supplier label",
    )
    batch_id = database.scalar("SELECT id FROM batches WHERE batch_code='LEG-01'")
    from fasterp.warehouse_reports import WarehouseReports
    trace = WarehouseReports(database).lot_trace(wms["company"], batch_id)
    assert any(row["event_type"] == "Identification" for row in trace["movements"])
    assert WarehouseReports(database).reconciliation(wms["company"]) == []


def test_concurrent_reservations_cannot_overallocate(wms):
    database = wms["database"]
    with database.transaction() as connection:
        item_id = connection.execute(
            """INSERT INTO items(company_id,code,name,uom)
               VALUES (%s,'CONC','Concurrency item','Each') RETURNING id""",
            (wms["company"],),
        ).fetchone()["id"]
        order_lines = []
        for number in (1, 2):
            order_id = connection.execute(
                """INSERT INTO sales_orders
                   (company_id,code,customer_id,order_date,delivery_date,status,
                    currency,total,document_state)
                   VALUES (%s,%s,%s,'2026-09-24','2026-09-25',
                           'Confirmed','GBP',40,'Posted') RETURNING id""",
                (wms["company"], f"SO-CONC-{number}", wms["customer"]),
            ).fetchone()["id"]
            order_lines.append(connection.execute(
                """INSERT INTO sales_order_items
                   (order_id,line_number,item_id,warehouse_id,qty,rate,amount)
                   VALUES (%s,1,%s,%s,4,10,40) RETURNING id""",
                (order_id, item_id, wms["warehouse"]),
            ).fetchone()["id"])
    InventoryService(database).post_event(
        company_id=wms["company"], event_type="Receipt",
        voucher_type="Concurrency Receipt", voucher_id=1,
        voucher_code="CONC-1", event_date=date(2026, 9, 24),
        lines=[InventoryLine(item_id, wms["warehouse"], Decimal("5"),
                             Decimal("10"))], actor="tester",
    )
    start = Barrier(2)

    def reserve(line_id):
        start.wait(timeout=10)
        try:
            WarehouseService(database).reserve_order_line(
                line_id, company_id=wms["company"], actor="tester",
            )
            return True
        except InsufficientStockError:
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(reserve, order_lines))
    assert sorted(outcomes) == [False, True]
    assert database.scalar(
        "SELECT sum(reserved_qty) FROM stock_slices WHERE item_id=%s", (item_id,)
    ) == Decimal("4.000000")
