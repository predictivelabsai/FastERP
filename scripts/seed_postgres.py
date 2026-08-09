"""Build the deterministic, company-scoped PostgreSQL demonstration fixture.

The fixture deliberately uses the production posting services for operational
documents. It creates three companies (GBP, EUR, USD), 1,000 items and three
dual-role business partners per company, all nine company/document currency
combinations, 1,000 opening inventory ledger rows, and exactly 1,000 balanced
GL rows per company.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from fasterp.config import DatabaseSettings
from fasterp.database import Database
from fasterp.purchasing import (
    PurchaseInvoiceLine,
    PurchaseOrderLine,
    PurchasingService,
    ReceiptLine,
    SupplierPaymentAllocation,
)
from fasterp.sales import (
    DeliveryLine,
    InvoiceLine,
    OrderLine,
    PaymentAllocation,
    SalesService,
)


FIXTURE_VERSION = "postgres-demo-v1"
RANDOM_SEED = 20260809
COMPANIES = (
    ("DEMO-GBP", "FastERP UK Demo", "GB", "GBP", "Europe/London"),
    ("DEMO-EUR", "FastERP EU Demo", "EE", "EUR", "Europe/Tallinn"),
    ("DEMO-USD", "FastERP US Demo", "US", "USD", "America/New_York"),
)
CURRENCIES = ("GBP", "EUR", "USD")
FX_TO_LOCAL = {
    "GBP": {"GBP": "1", "EUR": "0.86", "USD": "0.78"},
    "EUR": {"GBP": "1.16", "EUR": "1", "USD": "0.91"},
    "USD": {"GBP": "1.28", "EUR": "1.10", "USD": "1"},
}
ACCOUNT_ROWS = (
    ("1000", "Bank", "Asset", "Debit"),
    ("1010", "Cash", "Asset", "Debit"),
    ("1100", "Accounts Receivable", "Asset", "Debit"),
    ("1200", "Inventory", "Asset", "Debit"),
    ("1300", "Prepaid Expenses", "Asset", "Debit"),
    ("1310", "Purchase Tax Receivable", "Asset", "Debit"),
    ("1500", "Equipment", "Asset", "Debit"),
    ("2000", "Accounts Payable", "Liability", "Credit"),
    ("2050", "Goods Received Not Invoiced", "Liability", "Credit"),
    ("2100", "Sales Tax Payable", "Liability", "Credit"),
    ("2200", "Accrued Expenses", "Liability", "Credit"),
    ("3000", "Owner's Equity", "Equity", "Credit"),
    ("3100", "Retained Earnings", "Equity", "Credit"),
    ("4000", "Sales Revenue", "Income", "Credit"),
    ("4100", "Service Revenue", "Income", "Credit"),
    ("4200", "Other Income", "Income", "Credit"),
    ("4900", "Exchange Gain", "Income", "Credit"),
    ("5000", "Cost of Goods Sold", "Expense", "Debit"),
    ("6000", "Purchase Expense", "Expense", "Debit"),
    ("6100", "Payroll Expense", "Expense", "Debit"),
    ("6200", "Rent Expense", "Expense", "Debit"),
    ("6300", "Software Expense", "Expense", "Debit"),
    ("6400", "Travel Expense", "Expense", "Debit"),
    ("6500", "Marketing Expense", "Expense", "Debit"),
    ("6600", "Professional Fees", "Expense", "Debit"),
    ("6700", "Utilities Expense", "Expense", "Debit"),
    ("6800", "Inventory Adjustment", "Expense", "Debit"),
    ("6900", "Exchange Loss", "Expense", "Debit"),
)


def _operational_date(launch_date: date, index: int) -> date:
    year = launch_date.year - 1 if index % 2 == 0 else launch_date.year
    return date(year, 1, 15) + timedelta(days=(index % 10) * 28)


def _create_masters(database: Database, launch_date: date) -> list[dict]:
    created: list[dict] = []
    with database.transaction() as connection:
        existing = connection.execute(
            "SELECT code FROM companies WHERE code=ANY(%s) ORDER BY code",
            ([row[0] for row in COMPANIES],),
        ).fetchall()
        if existing:
            codes = ", ".join(row["code"] for row in existing)
            raise RuntimeError(
                f"Synthetic companies already exist ({codes}); refusing a destructive reseed"
            )
        for company_code, company_name, country, local_currency, timezone in COMPANIES:
            company_id = connection.execute(
                """INSERT INTO companies
                       (code,name,country_code,local_currency,reporting_currency,timezone)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    company_code, company_name, country, local_currency,
                    local_currency, timezone,
                ),
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO fiscal_periods(company_id,code,starts_on,ends_on,status)
                   VALUES (%s,%s,%s,%s,'Open'),(%s,%s,%s,%s,'Open')""",
                (
                    company_id, f"FY{launch_date.year - 1}",
                    date(launch_date.year - 1, 1, 1), date(launch_date.year - 1, 12, 31),
                    company_id, f"FY{launch_date.year}",
                    date(launch_date.year, 1, 1), date(launch_date.year, 12, 31),
                ),
            )
            account_ids = {}
            for code, name, kind, side in ACCOUNT_ROWS:
                account_ids[name] = connection.execute(
                    """INSERT INTO accounts
                           (company_id,code,name,account_type,normal_side)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (company_id, code, name, kind, side),
                ).fetchone()["id"]
            plant_id = connection.execute(
                """INSERT INTO plants(company_id,code,name,country_code)
                   VALUES (%s,'MAIN','Main Plant',%s) RETURNING id""",
                (company_id, country),
            ).fetchone()["id"]
            warehouse_id = connection.execute(
                """INSERT INTO warehouses(company_id,plant_id,code,name)
                   VALUES (%s,%s,'MAIN','Main Warehouse') RETURNING id""",
                (company_id, plant_id),
            ).fetchone()["id"]
            unit_id = connection.execute(
                """INSERT INTO business_units(company_id,code,name,region)
                   VALUES (%s,'CORE','Core Operations',%s) RETURNING id""",
                (company_id, country),
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO tax_codes
                       (company_id,code,name,rate,recoverable,sales_account_id,
                        purchase_account_id,country_code)
                   VALUES (%s,'ZERO','Zero rated',0,true,%s,%s,%s)""",
                (
                    company_id, account_ids["Sales Tax Payable"],
                    account_ids["Purchase Tax Receivable"], country,
                ),
            )
            bank_ids = {}
            for currency in CURRENCIES:
                bank_ids[currency] = connection.execute(
                    """INSERT INTO bank_accounts
                           (company_id,code,name,currency,gl_account_id)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        company_id, f"BANK-{currency}", f"{currency} Demo Bank",
                        currency, account_ids["Bank"],
                    ),
                ).fetchone()["id"]
                if currency != local_currency:
                    connection.execute(
                        """INSERT INTO exchange_rates
                               (company_id,rate_date,from_currency,to_currency,rate)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (
                            company_id, launch_date, currency, local_currency,
                            FX_TO_LOCAL[local_currency][currency],
                        ),
                    )
            connection.execute(
                """INSERT INTO company_accounting_settings (
                       company_id,receivable_account_id,payable_account_id,
                       inventory_account_id,cogs_account_id,sales_account_id,
                       purchase_account_id,sales_tax_account_id,purchase_tax_account_id,
                       exchange_gain_account_id,exchange_loss_account_id,
                       goods_received_not_invoiced_account_id,
                       inventory_adjustment_account_id,default_bank_account_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    company_id, account_ids["Accounts Receivable"],
                    account_ids["Accounts Payable"], account_ids["Inventory"],
                    account_ids["Cost of Goods Sold"], account_ids["Sales Revenue"],
                    account_ids["Purchase Expense"], account_ids["Sales Tax Payable"],
                    account_ids["Purchase Tax Receivable"], account_ids["Exchange Gain"],
                    account_ids["Exchange Loss"], account_ids["Goods Received Not Invoiced"],
                    account_ids["Inventory Adjustment"], bank_ids[local_currency],
                ),
            )
            connection.execute(
                """INSERT INTO company_user_roles
                       (company_id,user_identifier,role_code,granted_by)
                   VALUES (%s,'admin@fasterp.example','preparer','fixture'),
                          (%s,'admin@fasterp.example','approver','fixture'),
                          (%s,'admin@fasterp.example','administrator','fixture')""",
                (company_id, company_id, company_id),
            )
            partners = []
            for number in range(1, 4):
                code = f"PARTNER-{number:03d}"
                partner_id = connection.execute(
                    """INSERT INTO business_partners
                           (company_id,code,name,default_currency,credit_limit)
                       VALUES (%s,%s,%s,%s,250000) RETURNING id""",
                    (
                        company_id, code, f"Synthetic Partner {number}",
                        CURRENCIES[(number - 1) % 3],
                    ),
                ).fetchone()["id"]
                connection.execute(
                    """INSERT INTO business_partner_roles(partner_id,role)
                       VALUES (%s,'Customer'),(%s,'Supplier')""",
                    (partner_id, partner_id),
                )
                customer_id = connection.execute(
                    """INSERT INTO customers
                           (company_id,code,name,territory,credit_limit,currency,partner_id,created)
                       VALUES (%s,%s,%s,%s,250000,%s,%s,%s) RETURNING id""",
                    (
                        company_id, code, f"Synthetic Partner {number}", country,
                        CURRENCIES[(number - 1) % 3], partner_id, launch_date,
                    ),
                ).fetchone()["id"]
                supplier_id = connection.execute(
                    """INSERT INTO suppliers
                           (company_id,code,name,territory,currency,partner_id,created)
                       VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        company_id, code, f"Synthetic Partner {number}", country,
                        CURRENCIES[(number - 1) % 3], partner_id, launch_date,
                    ),
                ).fetchone()["id"]
                partners.append((customer_id, supplier_id))
            item_values = []
            groups = ("Raw Material", "Components", "Finished Goods", "Consumables", "Packaging")
            for number in range(1, 1001):
                item_values.append((
                    company_id, f"{local_currency}-ITEM-{number:04d}",
                    f"Synthetic {groups[(number - 1) % len(groups)]} {number:04d}",
                    groups[(number - 1) % len(groups)], "Each",
                    Decimal("10") + Decimal(number % 97), Decimal("10"),
                ))
            with connection.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO items
                           (company_id,code,name,item_group,uom,rate,reorder_level,
                            valuation_method,inventory_item)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'Moving Average',true)""",
                    item_values,
                )
            item_ids = [
                row["id"] for row in connection.execute(
                    "SELECT id FROM items WHERE company_id=%s ORDER BY code", (company_id,)
                ).fetchall()
            ]
            created.append({
                "id": company_id,
                "code": company_code,
                "local_currency": local_currency,
                "warehouse_id": warehouse_id,
                "unit_id": unit_id,
                "accounts": account_ids,
                "banks": bank_ids,
                "partners": partners,
                "items": item_ids,
            })
    return created


def _seed_company(database: Database, company: dict, launch_date: date) -> dict:
    company_id = company["id"]
    warehouse_id = company["warehouse_id"]
    local_currency = company["local_currency"]
    sales = SalesService(database)
    purchasing = PurchasingService(database)

    opening_date = date(launch_date.year - 1, 1, 1)
    opening_code = f"OPEN-{company['code']}"
    with database.transaction() as connection:
        event_id = connection.execute(
            """INSERT INTO inventory_events
                   (company_id,event_type,event_date,sequence_no,voucher_type,
                    voucher_id,voucher_code,document_state,posted_at,posted_by)
               VALUES (%s,'Opening',%s,1,'Synthetic Opening Stock',%s,%s,
                       'Posted',now(),'fixture') RETURNING id""",
            (company_id, opening_date, company_id, opening_code),
        ).fetchone()["id"]
        connection.execute(
            """WITH ranked AS (
                   SELECT id,row_number() OVER (ORDER BY code) AS line_number
                     FROM items WHERE company_id=%s
               )
               INSERT INTO inventory_event_lines
                   (event_id,line_number,item_id,warehouse_id,quantity,unit_cost)
               SELECT %s,line_number,id,%s,50,8+(line_number %% 41)
                 FROM ranked""",
            (company_id, event_id, warehouse_id),
        )
        connection.execute(
            """INSERT INTO inventory_ledger_entries
                   (company_id,event_id,event_line_id,item_id,warehouse_id,posting_at,
                    sequence_no,quantity_change,quantity_after,incoming_rate,
                    valuation_rate,value_change,value_after,valuation_method)
               SELECT %s,%s,line.id,line.item_id,line.warehouse_id,
                      %s::date + time '00:00',line.line_number,50,50,line.unit_cost,
                      line.unit_cost,50*line.unit_cost,50*line.unit_cost,'Moving Average'
                 FROM inventory_event_lines line WHERE line.event_id=%s""",
            (company_id, event_id, opening_date, event_id),
        )
        connection.execute(
            """INSERT INTO inventory_balances
                   (company_id,item_id,warehouse_id,quantity,inventory_value,
                    valuation_rate,last_ledger_entry_id)
               SELECT company_id,item_id,warehouse_id,quantity_after,value_after,
                      valuation_rate,id
                 FROM inventory_ledger_entries WHERE event_id=%s""",
            (event_id,),
        )
        connection.execute(
            """INSERT INTO item_warehouse_stock
                   (item_id,warehouse_id,quantity,average_cost)
               SELECT item_id,warehouse_id,quantity_after,valuation_rate
                 FROM inventory_ledger_entries WHERE event_id=%s""",
            (event_id,),
        )
        connection.execute(
            """INSERT INTO stock_moves
                   (company_id,item_id,warehouse_id,move_date,direction,qty,unit_cost,ref)
               SELECT company_id,item_id,warehouse_id,%s,'In',quantity_after,
                      valuation_rate,%s
                 FROM inventory_ledger_entries WHERE event_id=%s""",
            (opening_date, opening_code, event_id),
        )
        connection.execute(
            "UPDATE items SET stock_qty=50,updated_at=now() WHERE company_id=%s",
            (company_id,),
        )

    sales_invoice_ids = []
    purchase_invoice_ids = []
    for index in range(6):
        posting_date = _operational_date(launch_date, index)
        currency = CURRENCIES[index % 3]
        exchange = Decimal(FX_TO_LOCAL[local_currency][currency])
        customer_id, supplier_id = company["partners"][index % 3]
        item_id = company["items"][index]
        price = Decimal("25") + Decimal(index)

        order_id = sales.create_order(
            company_id=company_id,
            customer_id=customer_id,
            order_date=posting_date,
            delivery_date=posting_date + timedelta(days=3),
            currency=currency,
            exchange_rate=exchange,
            lines=[OrderLine(item_id, warehouse_id, Decimal("1"), price)],
            actor="fixture",
        )
        sales.post_order(order_id, actor="fixture")
        order_line_id = database.scalar(
            "SELECT id FROM sales_order_items WHERE order_id=%s", (order_id,)
        )
        delivery_id = sales.deliver(
            order_id,
            delivery_date=posting_date + timedelta(days=1),
            lines=[DeliveryLine(order_line_id, Decimal("1"))],
            actor="fixture",
        )
        delivery_line_id = database.scalar(
            "SELECT id FROM sales_delivery_items WHERE delivery_id=%s", (delivery_id,)
        )
        invoice_id = sales.invoice(
            order_id,
            invoice_date=posting_date + timedelta(days=2),
            due_date=posting_date + timedelta(days=32),
            lines=[InvoiceLine(order_line_id, Decimal("1"), delivery_line_id)],
            actor="fixture",
        )
        sales_invoice_ids.append(invoice_id)
        invoice = database.one("SELECT * FROM invoices WHERE id=%s", (invoice_id,))
        if index < 4:
            sales.receive_payment(
                company_id=company_id,
                customer_id=customer_id,
                payment_date=posting_date + timedelta(days=5),
                currency=currency,
                exchange_rate=exchange,
                payment_amount=invoice["total"],
                allocations=[PaymentAllocation(
                    invoice_id, invoice["total"], invoice["total"], invoice["base_total"]
                )],
                actor="fixture",
                bank_account_id=company["banks"][currency],
            )
        elif invoice["due_date"] < launch_date:
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE invoices SET status='Overdue' WHERE id=%s", (invoice_id,)
                )

        purchase_order_id = purchasing.create_order(
            company_id=company_id,
            supplier_id=supplier_id,
            order_date=posting_date,
            currency=currency,
            exchange_rate=exchange,
            lines=[PurchaseOrderLine(item_id, warehouse_id, Decimal("1"), price - Decimal("8"))],
            actor="fixture",
        )
        purchasing.post_order(purchase_order_id, actor="fixture")
        purchase_line_id = database.scalar(
            "SELECT id FROM purchase_order_items WHERE po_id=%s", (purchase_order_id,)
        )
        receipt_id = purchasing.receive(
            purchase_order_id,
            receipt_date=posting_date + timedelta(days=1),
            lines=[ReceiptLine(purchase_line_id, Decimal("1"))],
            actor="fixture",
        )
        receipt_line_id = database.scalar(
            "SELECT id FROM purchase_receipt_items WHERE receipt_id=%s", (receipt_id,)
        )
        purchase_invoice_id = purchasing.invoice(
            purchase_order_id,
            invoice_date=posting_date + timedelta(days=2),
            due_date=posting_date + timedelta(days=32),
            lines=[PurchaseInvoiceLine(
                purchase_line_id, Decimal("1"), receipt_line_id=receipt_line_id
            )],
            actor="fixture",
            supplier_reference=f"SAP-REF-{index + 1:05d}",
        )
        purchase_invoice_ids.append(purchase_invoice_id)
        purchase_invoice = database.one(
            "SELECT * FROM purchase_invoices WHERE id=%s", (purchase_invoice_id,)
        )
        if index < 4:
            purchasing.pay_supplier(
                company_id=company_id,
                supplier_id=supplier_id,
                payment_date=posting_date + timedelta(days=5),
                currency=currency,
                exchange_rate=exchange,
                payment_amount=purchase_invoice["total"],
                allocations=[SupplierPaymentAllocation(
                    purchase_invoice_id, purchase_invoice["total"],
                    purchase_invoice["total"], purchase_invoice["base_total"],
                )],
                actor="fixture",
                bank_account_id=company["banks"][currency],
            )
        elif purchase_invoice["due_date"] < launch_date:
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE purchase_invoices SET status='Overdue' WHERE id=%s",
                    (purchase_invoice_id,),
                )

    existing_gl = database.scalar(
        "SELECT count(*) FROM gl_entries WHERE company_id=%s", (company_id,)
    )
    remaining = 1000 - existing_gl
    if remaining < 0 or remaining % 2:
        raise RuntimeError(
            f"Operational fixture produced {existing_gl} GL rows; cannot top up exactly to 1,000"
        )
    with database.transaction() as connection:
        journal_count = remaining // 2
        prefix = f"SYN-JE-{company['code']}-"
        connection.execute(
            """INSERT INTO journal_entries
                   (company_id,code,entry_date,memo,status,document_state,
                    posted_at,posted_by,transaction_currency,transaction_exchange_rate)
               SELECT %s,%s||lpad(series::text,4,'0'),
                      make_date(%s-(CASE WHEN series%%2=0 THEN 1 ELSE 0 END),
                                1+((series-1)%%12),1+((series-1)%%27)),
                      'Synthetic balanced activity','Posted','Posted',now(),
                      'fixture',%s,1
                 FROM generate_series(1,%s) series""",
            (company_id, prefix, launch_date.year, local_currency, journal_count),
        )
        connection.execute(
            """WITH selected AS (
                   SELECT id,100+(substring(code from '[0-9]+$')::integer%%73) AS value
                     FROM journal_entries WHERE company_id=%s AND code LIKE %s
               )
               INSERT INTO journal_lines
                   (journal_id,line_number,account_id,debit,credit,memo)
               SELECT id,1,%s,value,0,'Synthetic activity' FROM selected
               UNION ALL
               SELECT id,2,%s,0,value,'Synthetic activity' FROM selected""",
            (
                company_id, prefix + "%", company["accounts"]["Bank"],
                company["accounts"]["Other Income"],
            ),
        )
        connection.execute(
            """INSERT INTO posting_batches
                   (company_id,voucher_type,voucher_id,voucher_code,posting_date,status)
               SELECT company_id,'Journal Entry',id,code,entry_date,'Draft'
                 FROM journal_entries WHERE company_id=%s AND code LIKE %s""",
            (company_id, prefix + "%"),
        )
        connection.execute(
            """INSERT INTO gl_entries
                   (company_id,entry_date,account_id,debit,credit,ref,posting_batch_id,
                    line_number,voucher_type,voucher_id,voucher_code,memo)
               SELECT batch.company_id,batch.posting_date,line.account_id,line.debit,
                      line.credit,batch.voucher_code,batch.id,line.line_number,
                      'Journal Entry',journal.id,journal.code,line.memo
                 FROM posting_batches batch
                 JOIN journal_entries journal ON journal.id=batch.voucher_id
                 JOIN journal_lines line ON line.journal_id=journal.id
                WHERE batch.company_id=%s AND batch.voucher_type='Journal Entry'
                  AND journal.code LIKE %s""",
            (company_id, prefix + "%"),
        )
        connection.execute(
            """UPDATE posting_batches batch
                  SET status='Posted',posted_at=now(),posted_by='fixture'
                 FROM journal_entries journal
                WHERE journal.id=batch.voucher_id AND batch.company_id=%s
                  AND batch.voucher_type='Journal Entry' AND journal.code LIKE %s""",
            (company_id, prefix + "%"),
        )

    counts = {}
    with database.connection() as connection:
        for table in (
            "items", "business_partners", "sales_orders", "invoices",
            "purchase_orders", "purchase_invoices", "inventory_ledger_entries",
            "gl_entries",
        ):
            counts[table] = connection.execute(
                f"SELECT count(*) AS value FROM {table} WHERE company_id=%s",
                (company_id,),
            ).fetchone()["value"]
        balance = connection.execute(
            """SELECT COALESCE(sum(debit),0) AS debit,COALESCE(sum(credit),0) AS credit
                 FROM gl_entries WHERE company_id=%s""",
            (company_id,),
        ).fetchone()
    if counts["items"] != 1000 or counts["business_partners"] != 3:
        raise RuntimeError(f"Fixture master counts are wrong for {company['code']}: {counts}")
    if counts["inventory_ledger_entries"] < 1000 or counts["gl_entries"] != 1000:
        raise RuntimeError(f"Fixture row counts are wrong for {company['code']}: {counts}")
    if balance["debit"] != balance["credit"]:
        raise RuntimeError(f"Fixture ledger is out of balance for {company['code']}")
    return {**counts, "debit": str(balance["debit"]), "credit": str(balance["credit"])}


def seed(database: Database, launch_date: date) -> dict:
    if database.scalar(
        "SELECT count(*) FROM schema_migrations WHERE version=%s",
        ("0013_migration_master_idempotency",),
    ) != 1:
        raise RuntimeError("PostgreSQL migrations through 0013 are required")
    if database.scalar(
        "SELECT count(*) FROM synthetic_fixture_manifests WHERE fixture_version=%s",
        (FIXTURE_VERSION,),
    ):
        raise RuntimeError(f"Fixture {FIXTURE_VERSION} is already installed")
    companies = _create_masters(database, launch_date)
    expected = {
        "launch_date": launch_date.isoformat(),
        "cutover_window": {"start": "T0", "go_live": "T+7"},
        "cutover_date": (launch_date + timedelta(days=7)).isoformat(),
        "companies": {},
    }
    for company in companies:
        expected["companies"][company["code"]] = _seed_company(
            database, company, launch_date
        )
    canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO synthetic_fixture_manifests
                   (fixture_version,random_seed,company_count,items_per_company,
                    partners_per_company,rows_per_company,currencies,expected_results,
                    manifest_hash)
               VALUES (%s,%s,3,1000,3,1000,%s::jsonb,%s::jsonb,%s)""",
            (
                FIXTURE_VERSION, RANDOM_SEED, json.dumps(CURRENCIES),
                json.dumps(expected, sort_keys=True), digest,
            ),
        )
    return expected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--launch-date", type=date.fromisoformat, default=date.today(),
        help="Launch date used as T0; go-live is recorded as T+7 (default: today)",
    )
    parser.add_argument("--schema", help="Override DB_SCHEMA for a disposable rehearsal")
    args = parser.parse_args()
    load_dotenv()
    settings = DatabaseSettings.from_env()
    if args.schema:
        settings = DatabaseSettings(
            settings.url, args.schema, settings.pool_min_size, settings.pool_max_size
        )
    with Database(settings) as database:
        result = seed(database, args.launch_date)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
