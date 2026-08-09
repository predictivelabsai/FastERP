"""Operational apply handlers for source-neutral master records."""

from __future__ import annotations

from decimal import Decimal

from fasterp.errors import DomainError

from .apply import ApplyContext, ApplyResult, Handler


ACCOUNT_ROLE_COLUMNS = {
    "receivable": "receivable_account_id",
    "payable": "payable_account_id",
    "inventory": "inventory_account_id",
    "cogs": "cogs_account_id",
    "sales": "sales_account_id",
    "purchase": "purchase_account_id",
    "sales_tax": "sales_tax_account_id",
    "purchase_tax": "purchase_tax_account_id",
    "exchange_gain": "exchange_gain_account_id",
    "exchange_loss": "exchange_loss_account_id",
    "grni": "goods_received_not_invoiced_account_id",
}


def ensure_accounting_settings(database, company_id: int, configuration: dict) -> None:
    """Resolve approved account-role codes before transaction application."""

    required_columns = tuple(ACCOUNT_ROLE_COLUMNS.values()) + ("default_bank_account_id",)
    with database.transaction() as connection:
        existing = connection.execute(
            "SELECT * FROM company_accounting_settings WHERE company_id=%s",
            (company_id,),
        ).fetchone()
        if existing and all(existing[column] is not None for column in required_columns):
            return
        roles = configuration.get("account_roles") or {}
        missing_roles = sorted(set(ACCOUNT_ROLE_COLUMNS) - set(roles))
        if missing_roles or not roles.get("default_bank"):
            missing = missing_roles + ([] if roles.get("default_bank") else ["default_bank"])
            raise DomainError(
                "Migration source account_roles are missing: " + ", ".join(missing)
            )
        accounts = {
            row["code"]: row["id"]
            for row in connection.execute(
                "SELECT id,code FROM accounts WHERE company_id=%s AND active=true",
                (company_id,),
            ).fetchall()
        }
        unknown = sorted({str(value) for value in roles.values()} - set(accounts))
        if unknown:
            raise DomainError(
                "Migration account-role codes are not mapped: " + ", ".join(unknown)
            )
        company = connection.execute(
            "SELECT local_currency FROM companies WHERE id=%s", (company_id,)
        ).fetchone()
        bank_id = connection.execute(
            """INSERT INTO bank_accounts(company_id,code,name,currency,gl_account_id)
               VALUES (%s,'MIG-DEFAULT','Migration default bank',%s,%s)
               ON CONFLICT (company_id,code) DO UPDATE SET
                   currency=excluded.currency,gl_account_id=excluded.gl_account_id,
                   active=true
               RETURNING id""",
            (company_id, company["local_currency"], accounts[roles["default_bank"]]),
        ).fetchone()["id"]
        columns = list(ACCOUNT_ROLE_COLUMNS.values())
        values = [accounts[roles[role]] for role in ACCOUNT_ROLE_COLUMNS]
        assignments = ",".join(f"{column}=excluded.{column}" for column in columns)
        connection.execute(
            f"""INSERT INTO company_accounting_settings
                    (company_id,{','.join(columns)},default_bank_account_id)
                VALUES (%s,{','.join(['%s'] * len(columns))},%s)
                ON CONFLICT (company_id) DO UPDATE SET
                    {assignments},default_bank_account_id=excluded.default_bank_account_id,
                    updated_at=now()""",
            (company_id, *values, bank_id),
        )


def master_handlers(connector_type: str) -> tuple[dict[str, Handler], list[str]]:
    common = {
        "Currencies": apply_currency,
        "CurrencyRates": apply_exchange_rate,
        "ChartOfAccounts": apply_account,
        "ProfitCenters": apply_business_unit,
        "Warehouses": apply_warehouse,
        "BusinessPartners": apply_partner,
        "PaymentTermsTypes": apply_payment_terms,
        "UnitOfMeasurements": apply_uom,
        "PriceLists": apply_price_book,
        "Items": apply_item,
        "Projects": apply_project,
        "SalesTaxCodes": apply_tax,
    }
    if connector_type in {"sap_business_one_odata_v4", "mock_sap"}:
        return common, list(common)
    erpnext = {
        "Currency": apply_currency,
        "Currency Exchange": apply_exchange_rate,
        "Account": apply_account,
        "Cost Center": apply_business_unit,
        "Warehouse": apply_warehouse,
        "Customer": apply_partner,
        "Supplier": apply_partner,
        "Payment Terms Template": apply_payment_terms,
        "UOM": apply_uom,
        "Item": apply_item,
        "Item Price": apply_item_price,
        "Sales Taxes and Charges Template": apply_tax,
        "Purchase Taxes and Charges Template": apply_tax,
        "Project": apply_project,
    }
    return erpnext, list(erpnext)


def _company(context: ApplyContext) -> int:
    if context.company_id is None:
        raise DomainError("Migration source must be assigned to a target company")
    return context.company_id


def apply_currency(connection, context, payload):
    del context
    row = connection.execute(
        """INSERT INTO currencies(code,name,symbol,active)
           VALUES (%s,%s,%s,true)
           ON CONFLICT (code) DO UPDATE SET
               name=excluded.name,symbol=excluded.symbol,active=true,updated_at=now()
           RETURNING id""",
        (payload["code"], payload.get("name") or payload["code"], payload.get("symbol") or payload["code"]),
    ).fetchone()
    return ApplyResult("currencies", row["id"])


def apply_exchange_rate(connection, context, payload):
    company = _company(context)
    target = payload.get("to_currency") or connection.execute(
        "SELECT local_currency FROM companies WHERE id=%s", (company,)
    ).fetchone()["local_currency"]
    source = payload["from_currency"]
    if source == target:
        currency = connection.execute(
            "SELECT id FROM currencies WHERE code=%s", (source,)
        ).fetchone()
        if not currency:
            raise DomainError(f"Currency is not mapped: {source}")
        return ApplyResult("currencies", currency["id"], "Link")
    row = connection.execute(
        """INSERT INTO exchange_rates
               (company_id,rate_date,from_currency,to_currency,rate)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (company_id,rate_date,from_currency,to_currency) DO UPDATE SET
               rate=excluded.rate
           RETURNING id""",
        (company, payload["rate_date"], source, target, Decimal(payload["rate"])),
    ).fetchone()
    return ApplyResult("exchange_rates", row["id"])


def apply_account(connection, context, payload):
    company = _company(context)
    row = connection.execute(
        """INSERT INTO accounts(company_id,code,name,account_type,normal_side,active)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (company_id,code) DO UPDATE SET
               name=excluded.name,account_type=excluded.account_type,
               normal_side=excluded.normal_side,active=excluded.active,updated_at=now()
           RETURNING id""",
        (company, payload["code"], payload["name"], payload["account_type"], payload["normal_side"], payload.get("active", True)),
    ).fetchone()
    return ApplyResult("accounts", row["id"])


def apply_business_unit(connection, context, payload):
    company = _company(context)
    row = connection.execute(
        """INSERT INTO business_units(company_id,code,name,region,active)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (company_id,code) DO UPDATE SET
               name=excluded.name,region=excluded.region,active=excluded.active,
               updated_at=now()
           RETURNING id""",
        (
            company, payload["code"], payload.get("name") or payload["code"],
            payload.get("region"), payload.get("active", True),
        ),
    ).fetchone()
    return ApplyResult("business_units", row["id"])


def apply_warehouse(connection, context, payload):
    company = _company(context)
    plant = connection.execute(
        """INSERT INTO plants(company_id,code,name,active)
           VALUES (%s,%s,%s,true)
           ON CONFLICT (company_id,code) DO UPDATE SET name=excluded.name,active=true,updated_at=now()
           RETURNING id""",
        (company, payload.get("plant_code") or "DEFAULT", payload.get("plant_name") or "Default plant"),
    ).fetchone()["id"]
    row = connection.execute(
        """INSERT INTO warehouses(company_id,plant_id,code,name,active)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (company_id,code) DO UPDATE SET
               plant_id=excluded.plant_id,name=excluded.name,active=excluded.active,updated_at=now()
           RETURNING id""",
        (company, plant, payload["code"], payload.get("name") or payload["code"], payload.get("active", True)),
    ).fetchone()
    return ApplyResult("warehouses", row["id"])


def apply_partner(connection, context, payload):
    company = _company(context)
    row = connection.execute(
        """INSERT INTO business_partners
               (company_id,code,name,default_currency,tax_identifier,credit_limit,active)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (company_id,code) DO UPDATE SET
               name=excluded.name,default_currency=excluded.default_currency,
               tax_identifier=excluded.tax_identifier,credit_limit=excluded.credit_limit,
               active=excluded.active,updated_at=now()
           RETURNING id""",
        (company, payload["code"], payload["name"], payload.get("currency"), payload.get("tax_identifier"), Decimal(payload.get("credit_limit") or "0"), payload.get("active", True)),
    ).fetchone()
    partner = row["id"]
    for role in payload.get("roles", []):
        connection.execute(
            "INSERT INTO business_partner_roles(partner_id,role) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (partner, role),
        )
        if role == "Customer":
            connection.execute(
                """INSERT INTO customers(company_id,code,name,credit_limit,currency,active,partner_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (company_id,code) DO UPDATE SET
                       name=excluded.name,credit_limit=excluded.credit_limit,currency=excluded.currency,
                       active=excluded.active,partner_id=excluded.partner_id,updated_at=now()""",
                (company, payload["code"], payload["name"], Decimal(payload.get("credit_limit") or "0"), payload.get("currency"), payload.get("active", True), partner),
            )
        if role == "Supplier":
            connection.execute(
                """INSERT INTO suppliers(company_id,code,name,currency,active,partner_id)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (company_id,code) DO UPDATE SET
                       name=excluded.name,currency=excluded.currency,active=excluded.active,
                       partner_id=excluded.partner_id,updated_at=now()""",
                (company, payload["code"], payload["name"], payload.get("currency"), payload.get("active", True), partner),
            )
    for address in payload.get("addresses") or []:
        connection.execute(
            """INSERT INTO partner_addresses
                   (partner_id,address_type,label,address_1,address_2,city,
                    state_region,postal_code,country_code,is_default)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
            (
                partner, address.get("type") or "Other", address.get("label"),
                address.get("address_1"), address.get("address_2"),
                address.get("city"), address.get("state_region"),
                address.get("postal_code"), address.get("country_code"),
                address.get("is_default", False),
            ),
        )
    return ApplyResult("business_partners", partner)


def apply_payment_terms(connection, context, payload):
    company = _company(context)
    row = connection.execute(
        """INSERT INTO payment_terms(company_id,code,name,due_days,active)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (company_id,code) DO UPDATE SET
               name=excluded.name,due_days=excluded.due_days,active=excluded.active
           RETURNING id""",
        (
            company, payload["code"], payload.get("name") or payload["code"],
            payload.get("due_days", 0), payload.get("active", True),
        ),
    ).fetchone()
    return ApplyResult("payment_terms", row["id"])


def apply_uom(connection, context, payload):
    del context
    code = str(payload["code"]).upper()
    row = connection.execute(
        """INSERT INTO uoms(code,name,allows_fraction,active)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (code) DO UPDATE SET
               name=excluded.name,allows_fraction=excluded.allows_fraction,
               active=excluded.active
           RETURNING id""",
        (
            code, payload.get("name") or payload["code"],
            payload.get("allows_fraction", True), payload.get("active", True),
        ),
    ).fetchone()
    return ApplyResult("uoms", row["id"])


def apply_price_book(connection, context, payload):
    company = _company(context)
    currency = payload.get("currency") or connection.execute(
        "SELECT local_currency FROM companies WHERE id=%s", (company,)
    ).fetchone()["local_currency"]
    row = connection.execute(
        """INSERT INTO price_books(company_id,code,name,purpose,currency,active)
           VALUES (%s,%s,%s,%s,%s,%s)
           ON CONFLICT (company_id,code) DO UPDATE SET
               name=excluded.name,purpose=excluded.purpose,currency=excluded.currency,
               active=excluded.active,updated_at=now()
           RETURNING id""",
        (
            company, payload["code"], payload.get("name") or payload["code"],
            payload.get("purpose") or "Sales", currency, payload.get("active", True),
        ),
    ).fetchone()
    return ApplyResult("price_books", row["id"])


def _apply_item_price(connection, company, item_id, payload):
    book = apply_price_book(connection, ApplyContext(0, 0, company), {
        "code": payload["price_book_code"],
        "name": payload.get("price_book_name") or payload["price_book_code"],
        "purpose": payload.get("purpose") or "Sales",
        "currency": payload.get("currency"), "active": True,
    }).entity_id
    uom_id = None
    if payload.get("uom"):
        uom_id = apply_uom(connection, None, {
            "code": payload["uom"], "name": payload["uom"],
        }).entity_id
    values = (
        book, item_id, uom_id, Decimal(payload.get("minimum_qty") or "0"),
        Decimal(payload.get("unit_price") or "0"), payload.get("valid_from"),
        payload.get("valid_to"),
    )
    row = connection.execute(
        """INSERT INTO price_book_entries
               (price_book_id,item_id,uom_id,minimum_qty,unit_price,valid_from,valid_to)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT DO NOTHING RETURNING id""",
        values,
    ).fetchone()
    if not row:
        row = connection.execute(
            """SELECT id FROM price_book_entries
                WHERE price_book_id=%s AND item_id=%s
                  AND uom_id IS NOT DISTINCT FROM %s AND minimum_qty=%s
                  AND valid_from IS NOT DISTINCT FROM %s
                  AND valid_to IS NOT DISTINCT FROM %s""",
            (book, item_id, uom_id, values[3], values[5], values[6]),
        ).fetchone()
    return row["id"]


def apply_item_price(connection, context, payload):
    company = _company(context)
    item = connection.execute(
        "SELECT id FROM items WHERE company_id=%s AND code=%s",
        (company, payload["item_code"]),
    ).fetchone()
    if not item:
        raise DomainError(f"Item price item is not mapped: {payload['item_code']}")
    return ApplyResult(
        "price_book_entries", _apply_item_price(connection, company, item["id"], payload)
    )


def apply_item(connection, context, payload):
    company = _company(context)
    uom_code = str(payload.get("uom") or "Each").upper()
    uom = connection.execute(
        """INSERT INTO uoms(code,name) VALUES (%s,%s)
           ON CONFLICT (code) DO UPDATE SET name=excluded.name RETURNING id""",
        (uom_code, payload.get("uom") or "Each"),
    ).fetchone()["id"]
    row = connection.execute(
        """INSERT INTO items (
               company_id,code,name,item_group,uom,inventory_item,active,stock_uom_id,
               valuation_method,standard_cost,tracks_serials,tracks_batches,tracks_expiry)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (company_id,code) DO UPDATE SET
               name=excluded.name,item_group=excluded.item_group,uom=excluded.uom,
               inventory_item=excluded.inventory_item,active=excluded.active,
               stock_uom_id=excluded.stock_uom_id,valuation_method=excluded.valuation_method,
               standard_cost=excluded.standard_cost,tracks_serials=excluded.tracks_serials,
               tracks_batches=excluded.tracks_batches,tracks_expiry=excluded.tracks_expiry,
               updated_at=now()
           RETURNING id""",
        (company, payload["code"], payload["name"], payload.get("item_group"), payload.get("uom") or "Each", payload.get("inventory_item", True), payload.get("active", True), uom, payload.get("valuation_method") or "Moving Average", Decimal(payload.get("standard_cost") or "0"), payload.get("tracks_serials", False), payload.get("tracks_batches", False), payload.get("tracks_expiry", False)),
    ).fetchone()
    for price in payload.get("prices") or []:
        _apply_item_price(connection, company, row["id"], price)
    return ApplyResult("items", row["id"])


def apply_project(connection, context, payload):
    company = _company(context)
    row = connection.execute(
        """INSERT INTO projects(company_id,code,name,status)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (company_id,code) DO UPDATE SET
               name=excluded.name,status=excluded.status,updated_at=now()
           RETURNING id""",
        (company, payload["code"], payload.get("name") or payload["code"], payload.get("status") or "Active"),
    ).fetchone()
    return ApplyResult("projects", row["id"])


def apply_tax(connection, context, payload):
    company = _company(context)
    row = connection.execute(
        """INSERT INTO tax_codes(company_id,code,name,rate,recoverable)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (company_id,code) DO UPDATE SET
               name=excluded.name,rate=excluded.rate,recoverable=excluded.recoverable,
               updated_at=now() RETURNING id""",
        (company, payload["code"], payload.get("name") or payload["code"], Decimal(payload.get("rate") or "0"), payload.get("recoverable", False)),
    ).fetchone()
    return ApplyResult("tax_codes", row["id"])
