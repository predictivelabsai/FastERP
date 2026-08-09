"""PostgreSQL accounting and inventory invariant tests."""

from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg import sql

from fasterp.accounting import AccountingService, PostingLine
from fasterp.config import DatabaseSettings
from fasterp.database import Database
from fasterp.errors import ImbalanceError, InsufficientStockError, PeriodLockedError
from fasterp.inventory import InventoryLine, InventoryService
from fasterp.preorders import (
    PreorderService,
    QuoteConversionLine,
    QuoteLine,
    RequestLine,
    RfqLine,
    SupplierQuoteLine,
)
from fasterp.purchasing import (
    DebitNoteLine,
    PurchaseInvoiceLine,
    PurchaseOrderLine,
    PurchasingService,
    ReceiptReturnLine,
    ReceiptLine,
    SupplierPaymentAllocation,
)
from fasterp.sales import (
    CreditLine,
    DeliveryLine,
    InvoiceLine,
    OrderLine,
    PaymentAllocation,
    ReturnLine,
    SalesService,
)
from migration.apply import ApplyContext
from migration.mapping import normalizers_for
from migration.transactions import (
    apply_sales_delivery,
    apply_sales_invoice,
    apply_sales_order,
)
from scripts.migrate_postgres import apply_migrations


@pytest.fixture(scope="module")
def pg_domain():
    load_dotenv()
    database_url = os.getenv("DB_URL")
    if not database_url:
        pytest.skip("DB_URL is not configured")
    schema = f"fast_erp_kernel_{uuid.uuid4().hex[:12]}"
    apply_migrations(database_url, schema)
    database = Database(
        DatabaseSettings(database_url, schema, pool_min_size=1, pool_max_size=3)
    )
    try:
        with database.transaction() as connection:
            company_id = connection.execute(
                """INSERT INTO companies
                       (code,name,country_code,local_currency,timezone)
                   VALUES ('TEST','Test Company','GB','GBP','Europe/London')
                   RETURNING id"""
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO fiscal_periods
                       (company_id,code,starts_on,ends_on,status,locked_at,locked_by)
                       VALUES (%s,'FY26','2026-01-01','2026-12-31','Open',NULL,NULL),
                              (%s,'FY25','2025-01-01','2025-12-31','Locked',now(),'tester')""",
                (company_id, company_id),
            )
            account_ids = {}
            for code, name, kind, side in (
                ("1000", "Cash", "Asset", "Debit"),
                ("1100", "Accounts Receivable", "Asset", "Debit"),
                ("1200", "Inventory", "Asset", "Debit"),
                ("1300", "Purchase Tax Receivable", "Asset", "Debit"),
                ("2000", "Accounts Payable", "Liability", "Credit"),
                ("2050", "Goods Received Not Invoiced", "Liability", "Credit"),
                ("4000", "Sales Revenue", "Income", "Credit"),
                ("4900", "Exchange Gain", "Income", "Credit"),
                ("5000", "Cost of Goods Sold", "Expense", "Debit"),
                ("6000", "Purchase Expense", "Expense", "Debit"),
                ("6900", "Exchange Loss", "Expense", "Debit"),
                ("2100", "Sales Tax Payable", "Liability", "Credit"),
            ):
                account_ids[code] = connection.execute(
                    """INSERT INTO accounts
                           (company_id,code,name,account_type,normal_side)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (company_id, code, name, kind, side),
                ).fetchone()["id"]
            plant_id = connection.execute(
                "INSERT INTO plants(company_id,code,name) VALUES (%s,'P1','Plant') RETURNING id",
                (company_id,),
            ).fetchone()["id"]
            warehouse_id = connection.execute(
                """INSERT INTO warehouses(company_id,plant_id,code,name)
                   VALUES (%s,%s,'W1','Warehouse') RETURNING id""",
                (company_id, plant_id),
            ).fetchone()["id"]
            items = {}
            for code, method in (
                ("MA", "Moving Average"), ("FIFO", "FIFO"),
                ("NEG", "Moving Average"), ("SALE", "Moving Average")
                , ("BUY", "Moving Average")
            ):
                items[code] = connection.execute(
                    """INSERT INTO items
                           (company_id,code,name,uom,valuation_method)
                       VALUES (%s,%s,%s,'Each',%s) RETURNING id""",
                    (company_id, code, code, method),
                ).fetchone()["id"]
            partner_id = connection.execute(
                """INSERT INTO business_partners
                       (company_id,code,name,default_currency)
                   VALUES (%s,'C-1','Customer','EUR') RETURNING id""",
                (company_id,),
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO business_partner_roles(partner_id,role) VALUES (%s,'Customer')",
                (partner_id,),
            )
            customer_id = connection.execute(
                """INSERT INTO customers(company_id,code,name,currency,partner_id)
                   VALUES (%s,'C-1','Customer','EUR',%s) RETURNING id""",
                (company_id, partner_id),
            ).fetchone()["id"]
            supplier_partner_id = connection.execute(
                """INSERT INTO business_partners
                       (company_id,code,name,default_currency)
                   VALUES (%s,'S-1','Supplier','EUR') RETURNING id""",
                (company_id,),
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO business_partner_roles(partner_id,role) VALUES (%s,'Supplier')",
                (supplier_partner_id,),
            )
            supplier_id = connection.execute(
                """INSERT INTO suppliers(company_id,code,name,currency,partner_id)
                   VALUES (%s,'S-1','Supplier','EUR',%s) RETURNING id""",
                (company_id, supplier_partner_id),
            ).fetchone()["id"]
            tax_id = connection.execute(
                """INSERT INTO tax_codes
                       (company_id,code,name,rate,sales_account_id,purchase_account_id)
                   VALUES (%s,'VAT20','VAT 20',20,%s,%s) RETURNING id""",
                (company_id, account_ids["2100"], account_ids["1300"]),
            ).fetchone()["id"]
            usd_bank = connection.execute(
                """INSERT INTO bank_accounts(company_id,code,name,currency,gl_account_id)
                   VALUES (%s,'USD','USD Bank','USD',%s) RETURNING id""",
                (company_id, account_ids["1000"]),
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO company_accounting_settings (
                       company_id,receivable_account_id,payable_account_id,
                       inventory_account_id,cogs_account_id,sales_account_id,
                       sales_tax_account_id,purchase_tax_account_id,
                       purchase_account_id,goods_received_not_invoiced_account_id,
                       exchange_gain_account_id,
                       exchange_loss_account_id,default_bank_account_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    company_id, account_ids["1100"], account_ids["2000"],
                    account_ids["1200"], account_ids["5000"], account_ids["4000"],
                    account_ids["2100"], account_ids["1300"], account_ids["6000"],
                    account_ids["2050"], account_ids["4900"], account_ids["6900"],
                    usd_bank,
                ),
            )
        yield {
            "database": database,
            "url": database_url,
            "schema": schema,
            "company": company_id,
            "accounts": account_ids,
            "warehouse": warehouse_id,
            "items": items,
            "customer": customer_id,
            "partner": partner_id,
            "tax": tax_id,
            "usd_bank": usd_bank,
            "supplier": supplier_id,
            "supplier_partner": supplier_partner_id,
        }
    finally:
        database.close()
        assert schema.startswith("fast_erp_kernel_")
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def test_accounting_posting_is_balanced_idempotent_and_reversible(pg_domain):
    service = AccountingService(pg_domain["database"])
    accounts = pg_domain["accounts"]
    lines = [
        PostingLine(accounts["1100"], debit=Decimal("120")),
        PostingLine(accounts["4000"], credit=Decimal("120")),
    ]
    batch = service.post_voucher(
        company_id=pg_domain["company"], voucher_type="Sales Invoice",
        voucher_id=100, voucher_code="INV-100", posting_date=date(2026, 8, 1),
        lines=lines, actor="tester",
    )
    assert service.post_voucher(
        company_id=pg_domain["company"], voucher_type="Sales Invoice",
        voucher_id=100, voucher_code="INV-100", posting_date=date(2026, 8, 1),
        lines=lines, actor="tester",
    ) == batch
    with pg_domain["database"].connection() as connection:
        totals = connection.execute(
            "SELECT sum(debit) AS debit, sum(credit) AS credit FROM gl_entries WHERE posting_batch_id=%s",
            (batch,),
        ).fetchone()
        assert totals == {"debit": Decimal("120.0000"), "credit": Decimal("120.0000")}
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute("UPDATE gl_entries SET memo='changed' WHERE posting_batch_id=%s", (batch,))
        connection.rollback()
    reversal = service.reverse_batch(
        batch, reversal_voucher_type="Sales Invoice Cancellation",
        reversal_voucher_id=101, reversal_voucher_code="CANCEL-INV-100",
        posting_date=date(2026, 8, 2), actor="tester",
    )
    with pg_domain["database"].connection() as connection:
        net = connection.execute(
            """SELECT sum(debit-credit) AS value FROM gl_entries
                WHERE posting_batch_id IN (%s,%s) AND account_id=%s""",
            (batch, reversal, accounts["1100"]),
        ).fetchone()["value"]
        assert net == Decimal("0.0000")


def test_accounting_rejects_imbalance_and_locked_period(pg_domain):
    service = AccountingService(pg_domain["database"])
    accounts = pg_domain["accounts"]
    with pytest.raises(ImbalanceError):
        service.post_voucher(
            company_id=pg_domain["company"], voucher_type="Bad", voucher_id=1,
            voucher_code="BAD-1", posting_date=date(2026, 8, 1), actor="tester",
            lines=[PostingLine(accounts["1000"], debit=Decimal("10")),
                   PostingLine(accounts["4000"], credit=Decimal("9"))],
        )
    with pytest.raises(PeriodLockedError):
        service.post_voucher(
            company_id=pg_domain["company"], voucher_type="Old", voucher_id=1,
            voucher_code="OLD-1", posting_date=date(2025, 8, 1), actor="tester",
            lines=[PostingLine(accounts["1000"], debit=Decimal("10")),
                   PostingLine(accounts["4000"], credit=Decimal("10"))],
        )


def test_moving_average_inventory_and_idempotent_voucher(pg_domain):
    service = InventoryService(pg_domain["database"])
    common = dict(
        company_id=pg_domain["company"], event_type="Receipt",
        voucher_type="Purchase Receipt", event_date=date(2026, 8, 1), actor="tester",
    )
    first = service.post_event(
        **common, voucher_id=200, voucher_code="PR-200",
        lines=[InventoryLine(pg_domain["items"]["MA"], pg_domain["warehouse"], Decimal("10"), Decimal("5"))],
    )
    assert service.post_event(
        **common, voucher_id=200, voucher_code="PR-200",
        lines=[InventoryLine(pg_domain["items"]["MA"], pg_domain["warehouse"], Decimal("10"), Decimal("5"))],
    ) == first
    service.post_event(
        **common, voucher_id=201, voucher_code="PR-201",
        lines=[InventoryLine(pg_domain["items"]["MA"], pg_domain["warehouse"], Decimal("10"), Decimal("7"))],
    )
    service.post_event(
        company_id=pg_domain["company"], event_type="Delivery",
        voucher_type="Sales Delivery", voucher_id=202, voucher_code="DN-202",
        event_date=date(2026, 8, 2), actor="tester",
        lines=[InventoryLine(pg_domain["items"]["MA"], pg_domain["warehouse"], Decimal("-5"))],
    )
    balance = pg_domain["database"].one(
        "SELECT quantity,inventory_value,valuation_rate FROM inventory_balances WHERE item_id=%s AND warehouse_id=%s",
        (pg_domain["items"]["MA"], pg_domain["warehouse"]),
    )
    assert balance == {
        "quantity": Decimal("15.000000"),
        "inventory_value": Decimal("90.000000"),
        "valuation_rate": Decimal("6.000000"),
    }


def test_fifo_consumption_and_negative_stock_rejection(pg_domain):
    service = InventoryService(pg_domain["database"])
    fifo = pg_domain["items"]["FIFO"]
    for voucher, qty, rate in ((300, "10", "5"), (301, "10", "7")):
        service.post_event(
            company_id=pg_domain["company"], event_type="Receipt",
            voucher_type="Purchase Receipt", voucher_id=voucher,
            voucher_code=f"PR-{voucher}", event_date=date(2026, 8, 1), actor="tester",
            lines=[InventoryLine(fifo, pg_domain["warehouse"], Decimal(qty), Decimal(rate))],
        )
    service.post_event(
        company_id=pg_domain["company"], event_type="Delivery",
        voucher_type="Sales Delivery", voucher_id=302, voucher_code="DN-302",
        event_date=date(2026, 8, 2), actor="tester",
        lines=[InventoryLine(fifo, pg_domain["warehouse"], Decimal("-12"))],
    )
    balance = pg_domain["database"].one(
        "SELECT quantity,inventory_value,valuation_rate FROM inventory_balances WHERE item_id=%s AND warehouse_id=%s",
        (fifo, pg_domain["warehouse"]),
    )
    assert balance == {
        "quantity": Decimal("8.000000"),
        "inventory_value": Decimal("56.000000"),
        "valuation_rate": Decimal("7.000000"),
    }
    with pytest.raises(InsufficientStockError):
        service.post_event(
            company_id=pg_domain["company"], event_type="Delivery",
            voucher_type="Sales Delivery", voucher_id=303, voucher_code="DN-303",
            event_date=date(2026, 8, 2), actor="tester",
            lines=[InventoryLine(pg_domain["items"]["NEG"], pg_domain["warehouse"], Decimal("-1"))],
        )
    assert pg_domain["database"].scalar(
        "SELECT count(*) FROM inventory_events WHERE voucher_id=303"
    ) == 0


def test_order_to_cash_partial_delivery_invoice_and_multicurrency_payment(pg_domain):
    database = pg_domain["database"]
    inventory = InventoryService(database)
    sales = SalesService(database)
    item = pg_domain["items"]["SALE"]
    warehouse = pg_domain["warehouse"]
    inventory.post_event(
        company_id=pg_domain["company"], event_type="Opening",
        voucher_type="Opening Stock", voucher_id=500, voucher_code="OPEN-500",
        event_date=date(2026, 7, 31), actor="tester",
        lines=[InventoryLine(item, warehouse, Decimal("10"), Decimal("5"))],
    )
    order = sales.create_order(
        company_id=pg_domain["company"], customer_id=pg_domain["customer"],
        order_date=date(2026, 8, 1), delivery_date=date(2026, 8, 5),
        currency="EUR", exchange_rate=Decimal("0.8"), actor="tester",
        lines=[OrderLine(
            item, warehouse, Decimal("10"), Decimal("10"),
            tax_code_id=pg_domain["tax"],
        )],
    )
    sales.post_order(order, actor="approver")
    order_line = database.scalar(
        "SELECT id FROM sales_order_items WHERE order_id=%s", (order,)
    )
    delivery = sales.deliver(
        order, delivery_date=date(2026, 8, 2), actor="tester",
        lines=[DeliveryLine(order_line, Decimal("4"))],
    )
    assert database.scalar("SELECT status FROM sales_orders WHERE id=%s", (order,)) == "Partly Delivered"
    assert database.scalar(
        "SELECT quantity FROM inventory_balances WHERE item_id=%s AND warehouse_id=%s",
        (item, warehouse),
    ) == Decimal("6.000000")
    delivery_line = database.scalar(
        "SELECT id FROM sales_delivery_items WHERE delivery_id=%s", (delivery,)
    )
    invoice = sales.invoice(
        order, invoice_date=date(2026, 8, 3), actor="tester",
        lines=[InvoiceLine(order_line, Decimal("4"), delivery_line)],
    )
    assert database.one(
        "SELECT total,base_total,outstanding_amount,status FROM invoices WHERE id=%s",
        (invoice,),
    ) == {
        "total": Decimal("48.0000"),
        "base_total": Decimal("38.4000"),
        "outstanding_amount": Decimal("48.0000"),
        "status": "Unpaid",
    }
    payment = sales.receive_payment(
        company_id=pg_domain["company"], customer_id=pg_domain["customer"],
        payment_date=date(2026, 8, 4), currency="USD",
        exchange_rate=Decimal("0.75"), payment_amount=Decimal("52"),
        bank_account_id=pg_domain["usd_bank"], actor="tester",
        allocations=[PaymentAllocation(
            invoice, payment_amount=Decimal("52"), invoice_amount=Decimal("48"),
            base_amount=Decimal("39"),
        )],
    )
    assert payment
    assert database.one(
        "SELECT outstanding_amount,paid,status FROM invoices WHERE id=%s", (invoice,)
    ) == {"outstanding_amount": Decimal("0.0000"), "paid": Decimal("48.0000"), "status": "Paid"}
    allocation = database.one(
        "SELECT payment_amount,invoice_amount,base_amount,exchange_gain_loss FROM payment_allocations WHERE payment_id=%s",
        (payment,),
    )
    assert allocation == {
        "payment_amount": Decimal("52.0000"),
        "invoice_amount": Decimal("48.0000"),
        "base_amount": Decimal("39.0000"),
        "exchange_gain_loss": Decimal("0.6000"),
    }
    assert database.one(
        """SELECT sum(debit) AS debit,sum(credit) AS credit
             FROM gl_entries WHERE voucher_type='Customer Payment' AND voucher_id=%s""",
        (payment,),
    ) == {"debit": Decimal("39.0000"), "credit": Decimal("39.0000")}

    sales_return = sales.return_delivery(
        delivery, return_date=date(2026, 8, 5), actor="tester",
        lines=[ReturnLine(delivery_line, Decimal("2"))],
    )
    assert sales_return
    assert database.scalar(
        "SELECT quantity FROM inventory_balances WHERE item_id=%s AND warehouse_id=%s",
        (item, warehouse),
    ) == Decimal("8.000000")
    invoice_line = database.scalar(
        "SELECT id FROM invoice_items WHERE invoice_id=%s", (invoice,)
    )
    credit = sales.issue_credit_note(
        invoice, credit_date=date(2026, 8, 5), actor="tester",
        lines=[CreditLine(invoice_line, Decimal("2"))],
    )
    assert database.one(
        "SELECT invoice_type,total,outstanding_amount,status FROM invoices WHERE id=%s",
        (credit,),
    ) == {
        "invoice_type": "Credit Note",
        "total": Decimal("24.0000"),
        "outstanding_amount": Decimal("24.0000"),
        "status": "Unpaid",
    }
    assert database.scalar("SELECT status FROM sales_orders WHERE id=%s", (order,)) == "Partly Invoiced"
    assert database.one(
        """SELECT sum(debit) AS debit,sum(credit) AS credit
             FROM gl_entries WHERE voucher_type='Sales Credit Note' AND voucher_id=%s""",
        (credit,),
    ) == {"debit": Decimal("19.2000"), "credit": Decimal("19.2000")}


def test_procure_to_pay_partial_receipt_invoice_and_multicurrency_payment(pg_domain):
    database = pg_domain["database"]
    purchasing = PurchasingService(database)
    item = pg_domain["items"]["BUY"]
    warehouse = pg_domain["warehouse"]
    order = purchasing.create_order(
        company_id=pg_domain["company"], supplier_id=pg_domain["supplier"],
        order_date=date(2026, 8, 6), currency="EUR", exchange_rate=Decimal("0.8"),
        actor="tester", lines=[PurchaseOrderLine(
            item, warehouse, Decimal("10"), Decimal("8"), tax_code_id=pg_domain["tax"],
        )],
    )
    purchasing.post_order(order, actor="approver")
    order_line = database.scalar(
        "SELECT id FROM purchase_order_items WHERE po_id=%s", (order,)
    )
    receipt = purchasing.receive(
        order, receipt_date=date(2026, 8, 7), actor="tester",
        lines=[ReceiptLine(order_line, Decimal("4"))],
    )
    assert database.scalar("SELECT status FROM purchase_orders WHERE id=%s", (order,)) == "Partly Received"
    assert database.one(
        "SELECT quantity,inventory_value FROM inventory_balances WHERE item_id=%s AND warehouse_id=%s",
        (item, warehouse),
    ) == {"quantity": Decimal("4.000000"), "inventory_value": Decimal("25.600000")}
    receipt_line = database.scalar(
        "SELECT id FROM purchase_receipt_items WHERE receipt_id=%s", (receipt,)
    )
    invoice = purchasing.invoice(
        order, invoice_date=date(2026, 8, 8), actor="tester",
        lines=[PurchaseInvoiceLine(order_line, Decimal("4"), receipt_line)],
    )
    assert database.one(
        "SELECT total,base_total,outstanding_amount,status FROM purchase_invoices WHERE id=%s",
        (invoice,),
    ) == {
        "total": Decimal("38.4000"), "base_total": Decimal("30.7200"),
        "outstanding_amount": Decimal("38.4000"), "status": "Unpaid",
    }
    assert database.one(
        """SELECT sum(debit) AS debit,sum(credit) AS credit FROM gl_entries
             WHERE voucher_type='Purchase Invoice' AND voucher_id=%s""",
        (invoice,),
    ) == {"debit": Decimal("30.7200"), "credit": Decimal("30.7200")}
    payment = purchasing.pay_supplier(
        company_id=pg_domain["company"], supplier_id=pg_domain["supplier"],
        payment_date=date(2026, 8, 9), currency="USD",
        exchange_rate=Decimal("0.75"), payment_amount=Decimal("41.6"),
        bank_account_id=pg_domain["usd_bank"], actor="tester",
        allocations=[SupplierPaymentAllocation(
            invoice, payment_amount=Decimal("41.6"),
            invoice_amount=Decimal("38.4"), base_amount=Decimal("31.2"),
        )],
    )
    assert database.scalar(
        "SELECT status FROM purchase_invoices WHERE id=%s", (invoice,)
    ) == "Paid"
    assert database.one(
        """SELECT sum(debit) AS debit,sum(credit) AS credit FROM gl_entries
             WHERE voucher_type='Supplier Payment' AND voucher_id=%s""",
        (payment,),
    ) == {"debit": Decimal("31.2000"), "credit": Decimal("31.2000")}

    purchase_return = purchasing.return_receipt(
        receipt, return_date=date(2026, 8, 10), actor="tester",
        lines=[ReceiptReturnLine(receipt_line, Decimal("2"))],
    )
    assert purchase_return
    assert database.one(
        "SELECT quantity,inventory_value FROM inventory_balances WHERE item_id=%s AND warehouse_id=%s",
        (item, warehouse),
    ) == {"quantity": Decimal("2.000000"), "inventory_value": Decimal("12.800000")}
    invoice_line = database.scalar(
        "SELECT id FROM purchase_invoice_items WHERE purchase_invoice_id=%s", (invoice,)
    )
    debit = purchasing.issue_debit_note(
        invoice, debit_date=date(2026, 8, 10), actor="tester",
        lines=[DebitNoteLine(invoice_line, Decimal("2"))],
    )
    assert database.one(
        "SELECT invoice_type,total,outstanding_amount,status FROM purchase_invoices WHERE id=%s",
        (debit,),
    ) == {
        "invoice_type": "Debit Note", "total": Decimal("19.2000"),
        "outstanding_amount": Decimal("19.2000"), "status": "Unpaid",
    }
    assert database.scalar(
        "SELECT status FROM purchase_orders WHERE id=%s", (order,)
    ) == "Partly Invoiced"
    assert database.one(
        """SELECT sum(debit) AS debit,sum(credit) AS credit FROM gl_entries
             WHERE voucher_type='Purchase Debit Note' AND voucher_id=%s""",
        (debit,),
    ) == {"debit": Decimal("15.3600"), "credit": Decimal("15.3600")}


def test_quote_and_rfq_convert_atomically_to_posted_orders(pg_domain):
    database = pg_domain["database"]
    service = PreorderService(database)
    item = pg_domain["items"]["BUY"]
    warehouse = pg_domain["warehouse"]
    quote = service.create_sales_quote(
        company_id=pg_domain["company"], customer_id=pg_domain["customer"],
        quote_date=date(2026, 8, 11), valid_until=date(2026, 9, 11),
        currency="EUR", exchange_rate=Decimal("0.8"), actor="tester",
        lines=[QuoteLine(
            item, warehouse, Decimal("5"), Decimal("12"),
            tax_code_id=pg_domain["tax"],
        )],
    )
    service.post_sales_quote(quote, actor="approver")
    quote_line = database.scalar(
        "SELECT id FROM sales_quote_items WHERE quote_id=%s", (quote,)
    )
    sales_order = service.convert_sales_quote(
        quote, order_date=date(2026, 8, 12), delivery_date=date(2026, 8, 20),
        actor="tester", lines=[QuoteConversionLine(quote_line, Decimal("2"))],
    )
    assert database.one(
        "SELECT document_state,status,total,quote_id FROM sales_orders WHERE id=%s",
        (sales_order,),
    ) == {
        "document_state": "Posted", "status": "Confirmed",
        "total": Decimal("24.0000"), "quote_id": quote,
    }
    assert database.scalar("SELECT status FROM sales_quotes WHERE id=%s", (quote,)) == "Partly Ordered"

    request = service.create_purchase_request(
        company_id=pg_domain["company"], request_date=date(2026, 8, 11),
        required_by=date(2026, 9, 1), actor="tester",
        lines=[RequestLine(item, warehouse, Decimal("10"))],
    )
    request_line = database.scalar(
        "SELECT id FROM purchase_request_items WHERE purchase_request_id=%s", (request,)
    )
    rfq = service.create_rfq(
        request, request_date=date(2026, 8, 12), response_due=date(2026, 8, 19),
        supplier_ids=[pg_domain["supplier"]], actor="tester",
        lines=[RfqLine(request_line, Decimal("6"))],
    )
    rfq_line = database.scalar("SELECT id FROM rfq_items WHERE rfq_id=%s", (rfq,))
    supplier_quote = service.record_supplier_quote(
        rfq, supplier_id=pg_domain["supplier"], quote_date=date(2026, 8, 13),
        valid_until=date(2026, 9, 13), currency="EUR",
        exchange_rate=Decimal("0.8"), actor="tester",
        lines=[SupplierQuoteLine(rfq_line, Decimal("6"), Decimal("7"))],
    )
    purchase_order = service.award_supplier_quote(
        supplier_quote, order_date=date(2026, 8, 14), actor="approver"
    )
    assert database.one(
        "SELECT document_state,status,total,supplier_quotation_id FROM purchase_orders WHERE id=%s",
        (purchase_order,),
    ) == {
        "document_state": "Posted", "status": "Ordered",
        "total": Decimal("42.0000"), "supplier_quotation_id": supplier_quote,
    }
    assert database.scalar(
        "SELECT status FROM purchase_requests WHERE id=%s", (request,)
    ) == "Partly Ordered"
    assert database.scalar(
        "SELECT status FROM requests_for_quote WHERE id=%s", (rfq,)
    ) == "Awarded"


def test_sap_transaction_handlers_use_atomic_domain_services(pg_domain):
    database = pg_domain["database"]
    normalizers = normalizers_for("mock_sap")
    InventoryService(database).post_event(
        company_id=pg_domain["company"], event_type="Opening",
        voucher_type="Migration Test Opening", voucher_id=9900,
        voucher_code="MIGRATION-TEST-OPENING", event_date=date(2026, 8, 14),
        lines=[InventoryLine(
            pg_domain["items"]["SALE"], pg_domain["warehouse"],
            Decimal("5"), Decimal("5"),
        )], actor="tester",
    )
    with database.transaction() as connection:
        source_id = connection.execute(
            """INSERT INTO migration_sources
                   (company_id,name,connector_type,source_company_db)
               VALUES (%s,'Kernel SAP Transaction Test','mock_sap','TEST')
               RETURNING id""",
            (pg_domain["company"],),
        ).fetchone()["id"]
        run_id = connection.execute(
            """INSERT INTO migration_runs
                   (source_id,mode,status,history_from,history_to,requested_by)
               VALUES (%s,'dry_run','Applying','2025-01-01','2026-12-31','tester')
               RETURNING id""",
            (source_id,),
        ).fetchone()["id"]
        context = ApplyContext(run_id, source_id, pg_domain["company"])
        source_order = {
            "DocEntry": 9900, "DocNum": 19900, "CardCode": "C-1",
            "DocDate": "2026-08-15", "DocDueDate": "2026-08-20",
            "DocCurrency": "EUR", "DocRate": "0.8", "DocumentStatus": "bost_Open",
            "DocumentLines": [{
                "LineNum": 0, "ItemCode": "SALE", "WarehouseCode": "W1",
                "Quantity": "1", "UnitPrice": "20",
            }],
        }
        normalized, issues = normalizers["Orders"](source_order)
        assert not issues
        order = apply_sales_order(connection, context, normalized)
        connection.execute(
            """INSERT INTO migration_crosswalks
                   (source_id,source_object,source_key,target_table,target_id,payload_hash,
                    first_run_id,last_run_id)
               VALUES (%s,'Orders','9900','sales_orders',%s,'test',%s,%s)""",
            (source_id, order.entity_id, run_id, run_id),
        )
        delivery_payload = {
            "DocEntry": 9901, "DocNum": 19901, "CardCode": "C-1",
            "DocDate": "2026-08-16", "DocCurrency": "EUR", "DocRate": "0.8",
            "DocumentLines": [{
                "LineNum": 0, "ItemCode": "SALE", "WarehouseCode": "W1",
                "Quantity": "1", "UnitPrice": "20", "BaseEntry": 9900,
                "BaseLine": 0,
            }],
        }
        normalized, issues = normalizers["DeliveryNotes"](delivery_payload)
        assert not issues
        delivery = apply_sales_delivery(connection, context, normalized)
        assert delivery.entity_type == "sales_deliveries"
        invoice_payload = {
            **delivery_payload, "DocEntry": 9902, "DocNum": 19902,
            "DocDate": "2026-08-17", "DocDueDate": "2026-09-17",
        }
        normalized, issues = normalizers["Invoices"](invoice_payload)
        assert not issues
        invoice = apply_sales_invoice(connection, context, normalized)
        assert invoice.entity_type == "invoices"
        assert connection.execute(
            "SELECT status FROM sales_orders WHERE id=%s", (order.entity_id,)
        ).fetchone()["status"] == "Invoiced"
        assert connection.execute(
            """SELECT sum(debit)=sum(credit) AS balanced FROM gl_entries
                WHERE voucher_type='Sales Invoice' AND voucher_id=%s""",
            (invoice.entity_id,),
        ).fetchone()["balanced"]
