"""Independent source-to-neutral normalization for supported ERP masters."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .validation import Issue, Normalizer, default_normalizer


def normalizers_for(connector_type: str) -> dict[str, Normalizer]:
    if connector_type in {"sap_business_one_odata_v4", "mock_sap"}:
        return {
            "Currencies": _sap_currency,
            "ChartOfAccounts": _sap_account,
            "Warehouses": _sap_warehouse,
            "BusinessPartners": _sap_partner,
            "Items": _sap_item,
            "Projects": _sap_project,
            "SalesTaxCodes": _sap_tax,
            "ProfitCenters": _sap_business_unit,
            "CurrencyRates": _sap_exchange_rate,
            "PaymentTermsTypes": _sap_payment_terms,
            "UnitOfMeasurements": _sap_uom,
            "PriceLists": _sap_price_book,
            "PurchaseRequests": lambda payload: _sap_document(payload, "Purchase Request"),
            "PurchaseQuotations": lambda payload: _sap_document(payload, "Supplier Quotation"),
            "Orders": lambda payload: _sap_document(payload, "Sales Order"),
            "DeliveryNotes": lambda payload: _sap_document(payload, "Sales Delivery"),
            "Invoices": lambda payload: _sap_document(payload, "Sales Invoice"),
            "IncomingPayments": lambda payload: _sap_payment(payload, "Customer Receipt"),
            "PurchaseOrders": lambda payload: _sap_document(payload, "Purchase Order"),
            "PurchaseDeliveryNotes": lambda payload: _sap_document(payload, "Purchase Receipt"),
            "PurchaseInvoices": lambda payload: _sap_document(payload, "Purchase Invoice"),
            "OutgoingPayments": lambda payload: _sap_payment(payload, "Supplier Payment"),
            "JournalEntries": _sap_journal,
            "Quotations": lambda payload: _sap_document(payload, "Sales Quote"),
            "Returns": lambda payload: _sap_document(payload, "Sales Return"),
            "CreditNotes": lambda payload: _sap_document(payload, "Credit Note"),
            "PurchaseReturns": lambda payload: _sap_document(payload, "Purchase Return"),
            "PurchaseCreditNotes": lambda payload: _sap_document(payload, "Debit Note"),
        }
    if connector_type == "erpnext_rest":
        return {
            "Currency": _erpnext_currency,
            "Account": _erpnext_account,
            "Warehouse": _erpnext_warehouse,
            "Customer": _erpnext_customer,
            "Supplier": _erpnext_supplier,
            "Item": _erpnext_item,
            "Project": _erpnext_project,
            "Cost Center": _erpnext_business_unit,
            "Currency Exchange": _erpnext_exchange_rate,
            "Payment Terms Template": _erpnext_payment_terms,
            "UOM": _erpnext_uom,
            "Item Price": _erpnext_item_price,
            "Sales Taxes and Charges Template": lambda payload: _erpnext_tax(payload, False),
            "Purchase Taxes and Charges Template": lambda payload: _erpnext_tax(payload, True),
            "Material Request": lambda payload: _erpnext_document(payload, "Purchase Request"),
            "Request for Quotation": lambda payload: _erpnext_document(payload, "Request For Quote"),
            "Supplier Quotation": lambda payload: _erpnext_document(payload, "Supplier Quotation"),
            "Sales Order": lambda payload: _erpnext_document(payload, "Sales Order"),
            "Delivery Note": lambda payload: _erpnext_document(payload, "Sales Delivery"),
            "Sales Invoice": lambda payload: _erpnext_document(payload, "Sales Invoice"),
            "Payment Entry": _erpnext_payment,
            "Purchase Order": lambda payload: _erpnext_document(payload, "Purchase Order"),
            "Purchase Receipt": lambda payload: _erpnext_document(payload, "Purchase Receipt"),
            "Purchase Invoice": lambda payload: _erpnext_document(payload, "Purchase Invoice"),
            "Journal Entry": _erpnext_journal,
            "Stock Entry": _erpnext_stock_entry,
            "Stock Reconciliation": _erpnext_stock_reconciliation,
            "Quotation": lambda payload: _erpnext_document(payload, "Sales Quote"),
        }
    return {}


def _text(payload: dict[str, Any], *names: str, required: bool = False) -> tuple[str | None, list[Issue]]:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return str(value).strip(), []
    if required:
        field = names[0]
        return None, [Issue("Error", "required_field", f"{field} is required", field)]
    return None, []


def _decimal(payload: dict[str, Any], *names: str, default: str = "0") -> tuple[str, list[Issue]]:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            try:
                return str(Decimal(str(value))), []
            except InvalidOperation:
                return default, [Issue("Error", "invalid_decimal", f"{name} must be numeric", name)]
    return default, []


def _base(payload: dict[str, Any]) -> tuple[dict[str, Any], list[Issue]]:
    normalized, issues = default_normalizer(payload)
    return normalized, issues


def _sap_currency(payload):
    code, issues = _text(payload, "Code", required=True)
    name, more = _text(payload, "Name")
    return {"code": (code or "").upper(), "name": name or code, "symbol": payload.get("InternationalDescription") or code}, issues + more


def _sap_account(payload):
    code, issues = _text(payload, "Code", required=True)
    name, more = _text(payload, "Name", required=True)
    raw_type = str(payload.get("AccountType") or payload.get("AcctType") or "Other")
    credit_types = {"_at_Revenues", "Revenue", "Liability", "Equity", "Income"}
    account_type = {
        "_at_Revenues": "Income", "_at_Expenses": "Expense",
        "_at_Other": "Other", "Revenue": "Income",
    }.get(raw_type, raw_type.replace("_at_", ""))
    return {
        "code": code, "name": name, "account_type": account_type,
        "normal_side": "Credit" if raw_type in credit_types else "Debit",
        "active": not bool(payload.get("FrozenFor") == "tYES"),
        "parent_code": payload.get("FatherNum"),
    }, issues + more


def _sap_warehouse(payload):
    code, issues = _text(payload, "WarehouseCode", required=True)
    name, more = _text(payload, "WarehouseName")
    return {
        "code": code, "name": name or code,
        "plant_code": str(payload.get("BusinessPlaceID") or "DEFAULT"),
        "plant_name": str(payload.get("BusinessPlaceName") or "Default plant"),
        "active": payload.get("Inactive") != "tYES",
    }, issues + more


def _sap_partner(payload):
    code, issues = _text(payload, "CardCode", required=True)
    name, more = _text(payload, "CardName", required=True)
    currency, currency_issues = _text(payload, "Currency")
    limit, limit_issues = _decimal(payload, "CreditLimit")
    card_type = payload.get("CardType")
    role = {"cCustomer": "Customer", "cSupplier": "Supplier"}.get(card_type)
    if not role:
        issues.append(Issue("Error", "unsupported_partner_role", f"Unsupported CardType {card_type}", "CardType"))
    addresses = []
    for row in payload.get("BPAddresses") or []:
        addresses.append({
            "type": "Shipping" if row.get("AddressType") == "bo_ShipTo" else "Billing",
            "label": row.get("AddressName"), "address_1": row.get("Street"),
            "address_2": row.get("Block"), "city": row.get("City"),
            "state_region": row.get("State"), "postal_code": row.get("ZipCode"),
            "country_code": row.get("Country"),
        })
    return {
        "code": code, "name": name, "roles": [role] if role else [],
        "currency": currency if currency and len(currency) == 3 else None,
        "credit_limit": limit, "active": payload.get("Frozen") != "tYES",
        "tax_identifier": payload.get("FederalTaxID"),
        "addresses": addresses,
    }, issues + more + currency_issues + limit_issues


def _sap_item(payload):
    code, issues = _text(payload, "ItemCode", required=True)
    name, more = _text(payload, "ItemName", required=True)
    valuation = {
        "bavMovingAverage": "Moving Average", "bavFIFO": "FIFO",
        "bavStandard": "Standard Cost", "MovingAverage": "Moving Average",
    }.get(payload.get("CostAccountingMethod"), "Moving Average")
    standard, standard_issues = _decimal(payload, "StandardPrice", "AvgStdPrice")
    prices = [{
        "price_book_code": f"SAP-{row.get('PriceList')}-{row.get('Currency') or 'LOCAL'}",
        "price_book_name": f"SAP price list {row.get('PriceList')}",
        "purpose": "Sales", "currency": row.get("Currency"),
        "unit_price": str(row.get("Price") or 0), "minimum_qty": "0",
    } for row in payload.get("ItemPrices") or [] if row.get("PriceList") is not None]
    return {
        "code": code, "name": name, "item_group": str(payload.get("ItemsGroupCode") or ""),
        "uom": payload.get("InventoryUOM") or "Each",
        "inventory_item": payload.get("InventoryItem") != "tNO",
        "active": payload.get("Valid") != "tNO",
        "valuation_method": valuation, "standard_cost": standard,
        "tracks_serials": payload.get("ManageSerialNumbers") == "tYES",
        "tracks_batches": payload.get("ManageBatchNumbers") == "tYES",
        "tracks_expiry": bool(payload.get("ShelfLife")),
        "prices": prices,
    }, issues + more + standard_issues


def _sap_project(payload):
    code, issues = _text(payload, "Code", required=True)
    name, more = _text(payload, "Name")
    return {"code": code, "name": name or code, "status": "Active"}, issues + more


def _sap_tax(payload):
    code, issues = _text(payload, "Code", required=True)
    name, more = _text(payload, "Name")
    rate, rate_issues = _decimal(payload, "Rate")
    return {"code": code, "name": name or code, "rate": rate, "recoverable": False}, issues + more + rate_issues


def _sap_business_unit(payload):
    code, issues = _text(payload, "CenterCode", required=True)
    return {
        "code": code, "name": payload.get("CenterName") or code,
        "region": payload.get("InWhichDimension"),
        "active": payload.get("Active") != "tNO",
    }, issues


def _sap_exchange_rate(payload):
    currency, issues = _text(payload, "Currency", required=True)
    rate, more = _decimal(payload, "Rate")
    rate_date = _iso_date(payload.get("Date"))
    if not rate_date:
        issues.append(Issue("Error", "invalid_date", "Date is required", "Date"))
    return {
        "from_currency": currency, "rate_date": rate_date, "rate": rate,
    }, issues + more


def _sap_payment_terms(payload):
    code, issues = _text(payload, "GroupNumber", required=True)
    due_days = int(payload.get("NumberOfAdditionalDays") or 0)
    return {
        "code": code, "name": payload.get("PaymentTermsGroupName") or code,
        "due_days": max(due_days, 0), "active": True,
    }, issues


def _sap_uom(payload):
    code, issues = _text(payload, "UomCode", "AbsEntry", required=True)
    return {
        "code": code, "name": payload.get("UomName") or code,
        "allows_fraction": True, "active": True,
    }, issues


def _sap_price_book(payload):
    code, issues = _text(payload, "PriceListNo", required=True)
    return {
        "code": f"SAP-{code}", "name": payload.get("PriceListName") or f"SAP price list {code}",
        "purpose": "Sales", "currency": payload.get("BasePriceListCurrency"),
        "active": payload.get("Active") != "tNO",
    }, issues


def _erpnext_currency(payload):
    code, issues = _text(payload, "name", required=True)
    return {"code": (code or "").upper(), "name": payload.get("currency_name") or code, "symbol": payload.get("symbol") or code}, issues


def _erpnext_account(payload):
    code, issues = _text(payload, "account_number", "name", required=True)
    name, more = _text(payload, "account_name", "name", required=True)
    root = payload.get("root_type") or "Asset"
    return {
        "code": code, "name": name, "account_type": payload.get("account_type") or root,
        "normal_side": "Credit" if root in {"Liability", "Equity", "Income"} else "Debit",
        "active": not bool(payload.get("disabled")), "parent_code": payload.get("parent_account"),
    }, issues + more


def _erpnext_warehouse(payload):
    code, issues = _text(payload, "name", required=True)
    return {
        "code": code, "name": payload.get("warehouse_name") or code,
        "plant_code": "DEFAULT", "plant_name": "Default plant",
        "active": not bool(payload.get("disabled")),
    }, issues


def _erpnext_partner(payload, role):
    code, issues = _text(payload, "name", required=True)
    name = payload.get("customer_name") or payload.get("supplier_name") or code
    return {
        "code": code, "name": name, "roles": [role],
        "currency": payload.get("default_currency"),
        "credit_limit": str(payload.get("credit_limit") or 0),
        "active": not bool(payload.get("disabled")),
        "tax_identifier": payload.get("tax_id"),
    }, issues


def _erpnext_customer(payload):
    return _erpnext_partner(payload, "Customer")


def _erpnext_supplier(payload):
    return _erpnext_partner(payload, "Supplier")


def _erpnext_item(payload):
    code, issues = _text(payload, "item_code", "name", required=True)
    name, more = _text(payload, "item_name", "name", required=True)
    valuation = payload.get("valuation_method") or "Moving Average"
    if valuation not in {"Moving Average", "FIFO", "Standard Cost"}:
        valuation = "Moving Average"
    return {
        "code": code, "name": name, "item_group": payload.get("item_group"),
        "uom": payload.get("stock_uom") or "Each",
        "inventory_item": bool(payload.get("is_stock_item", 1)),
        "active": not bool(payload.get("disabled")), "valuation_method": valuation,
        "standard_cost": str(payload.get("valuation_rate") or 0),
        "tracks_serials": bool(payload.get("has_serial_no")),
        "tracks_batches": bool(payload.get("has_batch_no")),
        "tracks_expiry": bool(payload.get("has_expiry_date")),
    }, issues + more


def _erpnext_project(payload):
    code, issues = _text(payload, "name", required=True)
    return {"code": code, "name": payload.get("project_name") or code, "status": payload.get("status") or "Open"}, issues


def _erpnext_business_unit(payload):
    code, issues = _text(payload, "name", required=True)
    return {
        "code": code, "name": payload.get("cost_center_name") or code,
        "region": None, "active": not bool(payload.get("disabled")),
    }, issues


def _erpnext_exchange_rate(payload):
    source, issues = _text(payload, "from_currency", required=True)
    target, more = _text(payload, "to_currency", required=True)
    rate, rate_issues = _decimal(payload, "exchange_rate")
    rate_date = _iso_date(payload.get("date") or payload.get("creation"))
    if not rate_date:
        issues.append(Issue("Error", "invalid_date", "Exchange-rate date is required", "date"))
    return {
        "from_currency": source, "to_currency": target,
        "rate_date": rate_date, "rate": rate,
    }, issues + more + rate_issues


def _erpnext_payment_terms(payload):
    code, issues = _text(payload, "name", required=True)
    rows = payload.get("terms") or []
    due_days = max((int(row.get("credit_days") or 0) for row in rows), default=0)
    return {"code": code, "name": code, "due_days": due_days, "active": True}, issues


def _erpnext_uom(payload):
    code, issues = _text(payload, "name", required=True)
    return {
        "code": code, "name": payload.get("uom_name") or code,
        "allows_fraction": not bool(payload.get("must_be_whole_number")),
        "active": not bool(payload.get("enabled") == 0),
    }, issues


def _erpnext_item_price(payload):
    key, issues = _text(payload, "name", required=True)
    item, more = _text(payload, "item_code", required=True)
    price, price_issues = _decimal(payload, "price_list_rate")
    return {
        "source_key": key, "item_code": item,
        "price_book_code": payload.get("price_list") or "STANDARD",
        "price_book_name": payload.get("price_list") or "Standard",
        "purpose": "Purchasing" if payload.get("buying") else "Sales",
        "currency": payload.get("currency"), "uom": payload.get("uom"),
        "minimum_qty": str(payload.get("min_qty") or 0), "unit_price": price,
        "valid_from": _iso_date(payload.get("valid_from")),
        "valid_to": _iso_date(payload.get("valid_upto")),
    }, issues + more + price_issues


def _erpnext_tax(payload, recoverable):
    code, issues = _text(payload, "name", required=True)
    rows = payload.get("taxes") or []
    rate = next((row.get("rate") for row in rows if row.get("rate") is not None), 0)
    try:
        normalized_rate = str(Decimal(str(rate)))
    except InvalidOperation:
        normalized_rate = "0"
        issues.append(Issue("Error", "invalid_decimal", "Tax rate must be numeric", "rate"))
    return {
        "code": code, "name": payload.get("title") or code,
        "rate": normalized_rate, "recoverable": recoverable,
    }, issues


def _iso_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)[:10]
    try:
        __import__("datetime").date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _sap_lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for index, line in enumerate(payload.get("DocumentLines") or []):
        serials = [{
            "code": row.get("InternalSerialNumber") or row.get("ManufacturerSerialNumber")
                    or row.get("SystemSerialNumber"),
            "expires_on": _iso_date(row.get("ExpiryDate")),
            "warranty_expires_on": _iso_date(row.get("WarrantyEnd")),
        } for row in line.get("SerialNumbers") or []]
        batches = [{
            "code": row.get("BatchNumber"),
            "quantity": str(row.get("Quantity") or 0),
            "manufactured_on": _iso_date(row.get("ManufacturingDate")),
            "expires_on": _iso_date(row.get("ExpiryDate")),
        } for row in line.get("BatchNumbers") or []]
        result.append({
            "line_number": int(line.get("LineNum", index)) + 1,
            "item_code": line.get("ItemCode"),
            "description": line.get("ItemDescription"),
            "warehouse_code": line.get("WarehouseCode"),
            "quantity": str(line.get("Quantity") or 0),
            "unit_price": str(line.get("UnitPrice", line.get("Price", 0)) or 0),
            "tax_code": line.get("TaxCode"),
            "base_object": line.get("BaseType"),
            "base_key": str(line["BaseEntry"]) if line.get("BaseEntry") is not None else None,
            "base_line_number": int(line.get("BaseLine", index)) + 1,
            "serials": [row for row in serials if row["code"] is not None],
            "batches": [row for row in batches if row["code"] is not None],
        })
    return result


def _sap_document(payload: dict[str, Any], kind: str):
    key, issues = _text(payload, "DocEntry", required=True)
    code = str(payload.get("DocNum") or key or "")
    lines = _sap_lines(payload)
    if not lines:
        issues.append(Issue("Error", "missing_lines", f"{kind} requires DocumentLines", "DocumentLines"))
    return {
        "kind": kind,
        "source_key": key,
        "source_document_no": code,
        "partner_code": payload.get("CardCode"),
        "posting_date": _iso_date(payload.get("DocDate")),
        "due_date": _iso_date(payload.get("DocDueDate")),
        "currency": payload.get("DocCurrency"),
        "exchange_rate": str(payload.get("DocRate") or 1),
        "source_status": payload.get("DocumentStatus"),
        "cancelled": payload.get("Cancelled") == "tYES",
        "supplier_reference": payload.get("NumAtCard"),
        "lines": lines,
    }, issues


def _sap_payment(payload: dict[str, Any], kind: str):
    key, issues = _text(payload, "DocEntry", required=True)
    allocations = []
    for row in payload.get("PaymentInvoices") or []:
        allocations.append({
            "document_key": str(row.get("DocEntry")),
            "amount": str(row.get("SumApplied") or row.get("AppliedFC") or 0),
        })
    amount_value = payload.get("DocTotal") or sum(
        Decimal(row["amount"]) for row in allocations
    )
    return {
        "kind": kind,
        "source_key": key,
        "source_document_no": str(payload.get("DocNum") or key or ""),
        "partner_code": payload.get("CardCode"),
        "posting_date": _iso_date(payload.get("DocDate")),
        "currency": payload.get("DocCurrency"),
        "exchange_rate": str(payload.get("DocRate") or 1),
        "amount": str(amount_value or 0),
        "reference_number": payload.get("Reference1") or payload.get("TransferReference"),
        "allocations": allocations,
    }, issues


def _sap_journal(payload: dict[str, Any]):
    key, issues = _text(payload, "JdtNum", "Number", required=True)
    lines = [{
        "line_number": int(row.get("Line_ID", index)) + 1,
        "account_code": row.get("AccountCode") or row.get("ShortName"),
        "debit": str(row.get("Debit") or 0),
        "credit": str(row.get("Credit") or 0),
        "memo": row.get("LineMemo"),
    } for index, row in enumerate(payload.get("JournalEntryLines") or [])]
    if len(lines) < 2:
        issues.append(Issue("Error", "missing_lines", "Journal requires at least two lines", "JournalEntryLines"))
    return {
        "kind": "Journal Entry", "source_key": key,
        "source_document_no": str(payload.get("Number") or key or ""),
        "posting_date": _iso_date(payload.get("ReferenceDate")),
        "memo": payload.get("Memo"), "lines": lines,
    }, issues


def _erpnext_lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for index, line in enumerate(payload.get("items") or []):
        serial_codes = [
            value.strip() for value in str(line.get("serial_no") or "").splitlines()
            if value.strip()
        ]
        batch_code = line.get("batch_no")
        result.append({
            "line_number": int(line.get("idx", index + 1)),
            "item_code": line.get("item_code"),
            "description": line.get("description"),
            "warehouse_code": line.get("warehouse"),
            "quantity": str(line.get("qty") or line.get("stock_qty") or 0),
            "unit_price": str(line.get("rate") or 0),
            "tax_code": None,
            "base_key": line.get("sales_order") or line.get("purchase_order")
                        or line.get("delivery_note") or line.get("purchase_receipt")
                        or line.get("material_request")
                        or line.get("request_for_quotation"),
            "base_line_number": int(line.get("so_detail_idx") or line.get("po_detail_idx")
                                    or line.get("idx", index + 1)),
            "serials": [{"code": code} for code in serial_codes],
            "batches": ([{
                "code": batch_code,
                "quantity": str(line.get("qty") or line.get("stock_qty") or 0),
                "expires_on": _iso_date(line.get("expiry_date")),
            }] if batch_code else []),
        })
    return result


def _erpnext_document(payload: dict[str, Any], kind: str):
    key, issues = _text(payload, "name", required=True)
    lines = _erpnext_lines(payload)
    if not lines:
        issues.append(Issue("Error", "missing_lines", f"{kind} requires items", "items"))
    if payload.get("taxes_and_charges"):
        for line in lines:
            line["tax_code"] = payload["taxes_and_charges"]
    if payload.get("is_return"):
        kind = {
            "Sales Delivery": "Sales Return", "Sales Invoice": "Credit Note",
            "Purchase Receipt": "Purchase Return", "Purchase Invoice": "Debit Note",
        }.get(kind, kind)
        return_against = payload.get("return_against")
        for line in lines:
            line["base_key"] = return_against
    return {
        "kind": kind, "source_key": key, "source_document_no": key,
        "partner_code": payload.get("customer") or payload.get("supplier"),
        "posting_date": _iso_date(payload.get("posting_date") or payload.get("transaction_date")),
        "due_date": _iso_date(payload.get("due_date") or payload.get("delivery_date")),
        "currency": payload.get("currency"),
        "exchange_rate": str(payload.get("conversion_rate") or 1),
        "source_status": payload.get("status"),
        "cancelled": int(payload.get("docstatus") or 0) == 2,
        "supplier_reference": payload.get("bill_no"), "lines": lines,
        "supplier_codes": [
            row.get("supplier") for row in payload.get("suppliers") or []
            if row.get("supplier")
        ],
    }, issues


def _erpnext_payment(payload: dict[str, Any]):
    payment_type = payload.get("payment_type")
    kind = "Customer Receipt" if payment_type == "Receive" else "Supplier Payment"
    key, issues = _text(payload, "name", required=True)
    allocations = [{
        "document_key": row.get("reference_name"),
        "amount": str(row.get("allocated_amount") or 0),
    } for row in payload.get("references") or []]
    return {
        "kind": kind, "source_key": key, "source_document_no": key,
        "partner_code": payload.get("party"),
        "posting_date": _iso_date(payload.get("posting_date")),
        "currency": payload.get("paid_from_account_currency") if kind == "Customer Receipt"
                    else payload.get("paid_to_account_currency"),
        "exchange_rate": str(payload.get("source_exchange_rate") or 1),
        "amount": str(payload.get("paid_amount") or 0),
        "reference_number": payload.get("reference_no"), "allocations": allocations,
    }, issues


def _erpnext_journal(payload: dict[str, Any]):
    key, issues = _text(payload, "name", required=True)
    lines = [{
        "line_number": int(row.get("idx", index + 1)), "account_code": row.get("account"),
        "debit": str(row.get("debit_in_account_currency") or row.get("debit") or 0),
        "credit": str(row.get("credit_in_account_currency") or row.get("credit") or 0),
        "memo": row.get("user_remark"),
    } for index, row in enumerate(payload.get("accounts") or [])]
    return {
        "kind": "Journal Entry", "source_key": key, "source_document_no": key,
        "posting_date": _iso_date(payload.get("posting_date")),
        "memo": payload.get("user_remark"), "lines": lines,
    }, issues


def _erpnext_stock_entry(payload: dict[str, Any]):
    key, issues = _text(payload, "name", required=True)
    lines = []
    for index, row in enumerate(payload.get("items") or []):
        quantity = Decimal(str(row.get("transfer_qty") or row.get("qty") or 0))
        direction = Decimal("1") if row.get("t_warehouse") else Decimal("-1")
        lines.append({
            "line_number": int(row.get("idx", index + 1)),
            "item_code": row.get("item_code"),
            "warehouse_code": row.get("t_warehouse") or row.get("s_warehouse"),
            "quantity": str(quantity * direction),
            "unit_cost": str(row.get("basic_rate") or row.get("valuation_rate") or 0),
            "serials": [
                {"code": value.strip()}
                for value in str(row.get("serial_no") or "").splitlines()
                if value.strip()
            ],
            "batches": ([{
                "code": row.get("batch_no"), "quantity": str(abs(quantity)),
                "expires_on": _iso_date(row.get("expiry_date")),
            }] if row.get("batch_no") else []),
        })
    return {
        "kind": "Stock Entry", "source_key": key, "source_document_no": key,
        "posting_date": _iso_date(payload.get("posting_date")),
        "event_type": "Receipt" if all(Decimal(row["quantity"]) > 0 for row in lines)
                      else "Delivery", "lines": lines,
    }, issues


def _erpnext_stock_reconciliation(payload: dict[str, Any]):
    key, issues = _text(payload, "name", required=True)
    lines = []
    for index, row in enumerate(payload.get("items") or []):
        target = Decimal(str(row.get("qty") or 0))
        current = Decimal(str(row.get("current_qty") or 0))
        difference = target - current
        if difference == 0:
            continue
        lines.append({
            "line_number": int(row.get("idx", index + 1)),
            "item_code": row.get("item_code"),
            "warehouse_code": row.get("warehouse"),
            "quantity": str(difference),
            "unit_cost": str(row.get("valuation_rate") or 0),
            "serials": [], "batches": [],
        })
    if not lines:
        issues.append(Issue(
            "Warning", "no_adjustments", "Stock reconciliation has no quantity differences"
        ))
    return {
        "kind": "Stock Reconciliation", "source_key": key,
        "source_document_no": key,
        "posting_date": _iso_date(payload.get("posting_date")),
        "event_type": "Adjustment", "lines": lines,
    }, issues
