"""Operational transaction handlers for SAP Business One and ERPNext imports."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
import hashlib

from psycopg.types.json import Jsonb

from fasterp.accounting import AccountingService, PostingLine
from fasterp.documents import next_code
from fasterp.errors import DomainError
from fasterp.inventory import InventoryLine, InventoryService
from fasterp.preorders import (
    PreorderService, QuoteLine, RequestLine, RfqLine, SupplierQuoteLine,
)
from fasterp.purchasing import (
    DebitNoteLine,
    PurchaseInvoiceLine,
    PurchaseOrderLine,
    PurchasingService,
    ReceiptLine,
    ReceiptReturnLine,
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

from .apply import ApplyContext, ApplyResult, Handler
from .connectors.base import canonical_hash
from .tracking import apply_tracking


class _BoundDatabase:
    """Let domain services join the applier's atomic transaction."""

    def __init__(self, connection) -> None:
        self._connection = connection

    @contextmanager
    def transaction(self):
        yield self._connection

    @contextmanager
    def connection(self):
        yield self._connection

    def rows(self, query, params=()):
        return list(self._connection.execute(query, params).fetchall())

    def one(self, query, params=()):
        return self._connection.execute(query, params).fetchone()

    def scalar(self, query, params=()):
        row = self.one(query, params)
        return next(iter(row.values())) if row else None


SAP_ORDER = [
    "Quotations", "Orders", "DeliveryNotes", "Returns", "Invoices", "CreditNotes",
    "IncomingPayments", "PurchaseRequests", "PurchaseQuotations",
    "PurchaseOrders", "PurchaseDeliveryNotes", "PurchaseReturns",
    "PurchaseInvoices", "PurchaseCreditNotes", "OutgoingPayments", "JournalEntries",
]
ERPNEXT_ORDER = [
    "Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "Payment Entry",
    "Material Request", "Request for Quotation", "Supplier Quotation",
    "Purchase Order", "Purchase Receipt", "Purchase Invoice",
    "Journal Entry", "Stock Entry", "Stock Reconciliation",
]


def transaction_handlers(connector_type: str) -> tuple[dict[str, Handler], list[str]]:
    if connector_type in {"sap_business_one_odata_v4", "mock_sap"}:
        handlers = {
            "Orders": apply_sales_order,
            "DeliveryNotes": apply_sales_delivery,
            "Invoices": apply_sales_invoice,
            "IncomingPayments": apply_payment,
            "PurchaseOrders": apply_purchase_order,
            "PurchaseDeliveryNotes": apply_purchase_receipt,
            "PurchaseInvoices": apply_purchase_invoice,
            "OutgoingPayments": apply_payment,
            "JournalEntries": apply_journal,
            "Quotations": apply_sales_quote,
            "Returns": apply_sales_return,
            "CreditNotes": apply_credit_note,
            "PurchaseReturns": apply_purchase_return,
            "PurchaseCreditNotes": apply_debit_note,
            "PurchaseRequests": apply_purchase_request,
            "PurchaseQuotations": apply_supplier_quote,
        }
        return handlers, SAP_ORDER
    if connector_type == "erpnext_rest":
        handlers = {
            "Sales Order": apply_sales_order,
            "Delivery Note": apply_sales_delivery,
            "Sales Invoice": apply_sales_invoice,
            "Payment Entry": apply_payment,
            "Purchase Order": apply_purchase_order,
            "Purchase Receipt": apply_purchase_receipt,
            "Purchase Invoice": apply_purchase_invoice,
            "Journal Entry": apply_journal,
            "Stock Entry": apply_stock_entry,
            "Stock Reconciliation": apply_stock_entry,
            "Quotation": apply_sales_quote,
            "Material Request": apply_purchase_request,
            "Request for Quotation": apply_rfq,
            "Supplier Quotation": apply_supplier_quote,
        }
        return handlers, ERPNEXT_ORDER
    return {}, []


def full_handlers(connector_type: str) -> tuple[dict[str, Handler], list[str]]:
    from .masters import master_handlers

    masters, master_order = master_handlers(connector_type)
    transactions, transaction_order = transaction_handlers(connector_type)
    return {**masters, **transactions}, master_order + transaction_order


def _company(context: ApplyContext) -> int:
    if context.company_id is None:
        raise DomainError("Migration source must be assigned to a target company")
    return context.company_id


def _day(payload: dict, field: str = "posting_date") -> date:
    value = payload.get(field)
    if not value:
        raise DomainError(f"Migration payload requires {field}")
    return date.fromisoformat(str(value))


def _source_type(connection, context: ApplyContext) -> str:
    return connection.execute(
        "SELECT connector_type FROM migration_sources WHERE id=%s", (context.source_id,)
    ).fetchone()["connector_type"]


def _crosswalk(connection, context: ApplyContext, objects: tuple[str, ...], key: str) -> int:
    target = _try_crosswalk(connection, context, objects, key)
    if target is None:
        raise DomainError(f"Source dependency {objects}/{key} has not been applied")
    return target


def _try_crosswalk(connection, context: ApplyContext, objects: tuple[str, ...], key: str) -> int | None:
    row = connection.execute(
        """SELECT target_id FROM migration_crosswalks
            WHERE source_id=%s AND source_object=ANY(%s) AND source_key=%s
            ORDER BY id DESC LIMIT 1""",
        (context.source_id, list(objects), str(key)),
    ).fetchone()
    return row["target_id"] if row else None


def _lookup(connection, table: str, company_id: int, code: str | None) -> int:
    if not code:
        raise DomainError(f"{table} source code is required")
    row = connection.execute(
        f"SELECT id FROM {table} WHERE company_id=%s AND code=%s AND active=true",
        (company_id, code),
    ).fetchone()
    if not row:
        raise DomainError(f"No active {table} mapping for {code}")
    return row["id"]


def _tax(connection, company_id: int, code: str | None) -> int | None:
    if not code:
        return None
    row = connection.execute(
        "SELECT id FROM tax_codes WHERE company_id=%s AND code=%s AND active=true",
        (company_id, code),
    ).fetchone()
    return row["id"] if row else None


def _track_event(connection, voucher_type: str, voucher_id: int, payload: dict) -> None:
    event = connection.execute(
        """SELECT id FROM inventory_events
            WHERE voucher_type=%s AND voucher_id=%s ORDER BY id DESC LIMIT 1""",
        (voucher_type, voucher_id),
    ).fetchone()
    if event:
        apply_tracking(connection, event["id"], payload["lines"])


def _archive_source(connection, context: ApplyContext, payload: dict, reason: str) -> ApplyResult:
    row = connection.execute(
        """INSERT INTO migration_archived_objects
               (run_id,source_id,source_object,source_key,source_document_no,payload,
                payload_hash,archive_reason)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (
            context.run_id, context.source_id, payload["kind"], payload["source_key"],
            payload.get("source_document_no"), Jsonb(payload), canonical_hash(payload), reason,
        ),
    ).fetchone()
    return ApplyResult("migration_archived_objects", row["id"], "Archive")


def _archive_cancelled(connection, context: ApplyContext, payload: dict) -> ApplyResult | None:
    if not payload.get("cancelled"):
        return None
    return _archive_source(connection, context, payload, "Cancelled source document")


def _archive_closed_preorder(connection, context, payload):
    status = str(payload.get("source_status") or "").lower()
    if status not in {"bost_close", "closed", "expired", "lost", "cancelled"}:
        return None
    return _archive_source(connection, context, payload, "Closed source pre-order document")


def _sales_lines(connection, company_id: int, payload: dict) -> list[OrderLine]:
    return [OrderLine(
        _lookup(connection, "items", company_id, line.get("item_code")),
        _lookup(connection, "warehouses", company_id, line.get("warehouse_code")),
        Decimal(line["quantity"]), Decimal(line["unit_price"]),
        description=line.get("description"),
        tax_code_id=_tax(connection, company_id, line.get("tax_code")),
    ) for line in payload["lines"]]


def apply_sales_quote(connection, context, payload):
    archived = _archive_cancelled(connection, context, payload) or _archive_closed_preorder(
        connection, context, payload
    )
    if archived:
        return archived
    company = _company(context)
    customer = _lookup(connection, "customers", company, payload.get("partner_code"))
    local_currency = connection.execute(
        "SELECT local_currency FROM companies WHERE id=%s", (company,)
    ).fetchone()["local_currency"]
    service = PreorderService(_BoundDatabase(connection))
    entity = service.create_sales_quote(
        company_id=company, customer_id=customer, quote_date=_day(payload),
        valid_until=date.fromisoformat(payload["due_date"]) if payload.get("due_date") else None,
        currency=payload.get("currency") or local_currency,
        exchange_rate=Decimal(payload.get("exchange_rate") or "1"),
        lines=[QuoteLine(
            _lookup(connection, "items", company, line.get("item_code")),
            _lookup(connection, "warehouses", company, line.get("warehouse_code")),
            Decimal(line["quantity"]), Decimal(line["unit_price"]),
            description=line.get("description"),
            tax_code_id=_tax(connection, company, line.get("tax_code")),
        ) for line in payload["lines"]], actor="migration",
    )
    service.post_sales_quote(entity, actor="migration")
    return ApplyResult("sales_quotes", entity)


def _purchase_request_lines(connection, company, payload):
    return [RequestLine(
        _lookup(connection, "items", company, line.get("item_code")),
        _lookup(connection, "warehouses", company, line.get("warehouse_code")),
        Decimal(line["quantity"]), description=line.get("description"),
    ) for line in payload["lines"]]


def apply_purchase_request(connection, context, payload):
    archived = _archive_cancelled(connection, context, payload) or _archive_closed_preorder(
        connection, context, payload
    )
    if archived:
        return archived
    company = _company(context)
    entity = PreorderService(_BoundDatabase(connection)).create_purchase_request(
        company_id=company, request_date=_day(payload),
        required_by=date.fromisoformat(payload["due_date"]) if payload.get("due_date") else None,
        lines=_purchase_request_lines(connection, company, payload),
        actor="migration",
    )
    return ApplyResult("purchase_requests", entity)


def _request_for_preorder(connection, context, payload, service):
    base_keys = {line.get("base_key") for line in payload["lines"] if line.get("base_key")}
    if len(base_keys) == 1:
        request_id = _try_crosswalk(
            connection, context, ("PurchaseRequests", "Material Request"),
            base_keys.pop(),
        )
        if request_id:
            return request_id
    company = _company(context)
    return service.create_purchase_request(
        company_id=company, request_date=_day(payload),
        required_by=date.fromisoformat(payload["due_date"]) if payload.get("due_date") else None,
        lines=_purchase_request_lines(connection, company, payload),
        actor="migration",
    )


def _create_rfq_for_payload(connection, context, payload, service, supplier_ids):
    request_id = _request_for_preorder(connection, context, payload, service)
    request_lines = connection.execute(
        """SELECT id,line_number,qty FROM purchase_request_items
            WHERE purchase_request_id=%s ORDER BY line_number""",
        (request_id,),
    ).fetchall()
    by_number = {row["line_number"]: row for row in request_lines}
    requests = []
    for line in payload["lines"]:
        source = by_number.get(line.get("base_line_number") or line["line_number"])
        if not source:
            source = by_number.get(line["line_number"])
        if not source:
            raise DomainError("RFQ purchase-request line is missing")
        requests.append(RfqLine(source["id"], min(Decimal(line["quantity"]), source["qty"])))
    return service.create_rfq(
        request_id, request_date=_day(payload),
        response_due=date.fromisoformat(payload["due_date"]) if payload.get("due_date") else None,
        supplier_ids=supplier_ids, lines=requests, actor="migration",
    )


def apply_rfq(connection, context, payload):
    archived = _archive_cancelled(connection, context, payload) or _archive_closed_preorder(
        connection, context, payload
    )
    if archived:
        return archived
    company = _company(context)
    suppliers = [
        _lookup(connection, "suppliers", company, code)
        for code in payload.get("supplier_codes") or []
    ]
    if not suppliers:
        raise DomainError("RFQ requires at least one mapped supplier")
    service = PreorderService(_BoundDatabase(connection))
    entity = _create_rfq_for_payload(
        connection, context, payload, service, suppliers
    )
    return ApplyResult("requests_for_quote", entity)


def apply_supplier_quote(connection, context, payload):
    archived = _archive_cancelled(connection, context, payload) or _archive_closed_preorder(
        connection, context, payload
    )
    if archived:
        return archived
    company = _company(context)
    supplier_id = _lookup(connection, "suppliers", company, payload.get("partner_code"))
    service = PreorderService(_BoundDatabase(connection))
    base_keys = {line.get("base_key") for line in payload["lines"] if line.get("base_key")}
    rfq_id = None
    if len(base_keys) == 1:
        rfq_id = _try_crosswalk(
            connection, context, ("Request for Quotation",), base_keys.pop()
        )
    if rfq_id is None:
        rfq_id = _create_rfq_for_payload(
            connection, context, payload, service, [supplier_id]
        )
    rfq_lines = connection.execute(
        "SELECT id,line_number,qty FROM rfq_items WHERE rfq_id=%s ORDER BY line_number",
        (rfq_id,),
    ).fetchall()
    by_number = {row["line_number"]: row for row in rfq_lines}
    lines = []
    for line in payload["lines"]:
        source = by_number.get(line.get("base_line_number") or line["line_number"])
        if not source:
            source = by_number.get(line["line_number"])
        if not source:
            raise DomainError("Supplier quotation RFQ line is missing")
        lines.append(SupplierQuoteLine(
            source["id"], min(Decimal(line["quantity"]), source["qty"]),
            Decimal(line["unit_price"]),
        ))
    local_currency = connection.execute(
        "SELECT local_currency FROM companies WHERE id=%s", (company,)
    ).fetchone()["local_currency"]
    entity = service.record_supplier_quote(
        rfq_id, supplier_id=supplier_id, quote_date=_day(payload),
        valid_until=date.fromisoformat(payload["due_date"]) if payload.get("due_date") else None,
        currency=payload.get("currency") or local_currency,
        exchange_rate=Decimal(payload.get("exchange_rate") or "1"),
        lines=lines, actor="migration",
    )
    return ApplyResult("supplier_quotations", entity)


def apply_sales_order(connection, context, payload):
    archived = _archive_cancelled(connection, context, payload)
    if archived:
        return archived
    company = _company(context)
    customer = _lookup(connection, "customers", company, payload.get("partner_code"))
    service = SalesService(_BoundDatabase(connection))
    order_id = service.create_order(
        company_id=company, customer_id=customer, order_date=_day(payload),
        delivery_date=date.fromisoformat(payload["due_date"]) if payload.get("due_date") else None,
        currency=payload.get("currency") or connection.execute(
            "SELECT local_currency FROM companies WHERE id=%s", (company,)
        ).fetchone()["local_currency"],
        exchange_rate=Decimal(payload.get("exchange_rate") or "1"),
        lines=_sales_lines(connection, company, payload), actor="migration",
    )
    if payload.get("source_status") not in {"Draft", "bost_OpenDraft"}:
        service.post_order(order_id, actor="migration")
    return ApplyResult("sales_orders", order_id)


def _base_document(connection, context, payload, sap_object: str, erpnext_object: str) -> int:
    keys = {line.get("base_key") for line in payload["lines"] if line.get("base_key")}
    if len(keys) != 1:
        raise DomainError(f"{payload['kind']} must reference exactly one base document")
    return _crosswalk(connection, context, (sap_object, erpnext_object), keys.pop())


def apply_sales_delivery(connection, context, payload):
    if payload.get("kind") == "Sales Return":
        return apply_sales_return(connection, context, payload)
    archived = _archive_cancelled(connection, context, payload)
    if archived:
        return archived
    order_id = _base_document(connection, context, payload, "Orders", "Sales Order")
    requests = []
    for line in payload["lines"]:
        source = connection.execute(
            "SELECT id FROM sales_order_items WHERE order_id=%s AND line_number=%s",
            (order_id, line["base_line_number"]),
        ).fetchone()
        if not source:
            raise DomainError("Sales delivery base line is missing")
        requests.append(DeliveryLine(source["id"], Decimal(line["quantity"])))
    entity = SalesService(_BoundDatabase(connection)).deliver(
        order_id, delivery_date=_day(payload), lines=requests, actor="migration"
    )
    _track_event(connection, "Sales Delivery", entity, payload)
    return ApplyResult("sales_deliveries", entity)


def apply_sales_invoice(connection, context, payload):
    if payload.get("kind") == "Credit Note":
        return apply_credit_note(connection, context, payload)
    archived = _archive_cancelled(connection, context, payload)
    if archived:
        return archived
    base_keys = {line.get("base_key") for line in payload["lines"] if line.get("base_key")}
    if len(base_keys) != 1:
        raise DomainError("Sales invoice must reference exactly one order or delivery")
    base_key = base_keys.pop()
    order_id = _try_crosswalk(connection, context, ("Orders", "Sales Order"), base_key)
    delivery_id = None
    if order_id is None:
        delivery_id = _crosswalk(
            connection, context, ("DeliveryNotes", "Delivery Note"), base_key
        )
    lines = []
    for line in payload["lines"]:
        if delivery_id:
            delivered = connection.execute(
                """SELECT delivery_line.id,delivery_line.sales_order_item_id,
                          order_line.order_id
                     FROM sales_delivery_items delivery_line
                     JOIN sales_order_items order_line
                       ON order_line.id=delivery_line.sales_order_item_id
                    WHERE delivery_line.delivery_id=%s AND delivery_line.line_number=%s""",
                (delivery_id, line["base_line_number"]),
            ).fetchone()
            source = {"id": delivered["sales_order_item_id"]} if delivered else None
            if delivered:
                order_id = delivered["order_id"]
        else:
            source = connection.execute(
                "SELECT id FROM sales_order_items WHERE order_id=%s AND line_number=%s",
                (order_id, line["base_line_number"]),
            ).fetchone()
            delivered = None
        if not source:
            raise DomainError("Sales invoice base line is missing")
        delivered = delivered or connection.execute(
            """SELECT delivery_line.id FROM sales_delivery_items delivery_line
                JOIN sales_deliveries delivery ON delivery.id=delivery_line.delivery_id
                WHERE delivery_line.sales_order_item_id=%s
                  AND delivery.document_state='Posted'
                  AND delivery_line.billed_qty<delivery_line.qty
                ORDER BY delivery.id LIMIT 1""",
            (source["id"],),
        ).fetchone()
        lines.append(InvoiceLine(
            source["id"], Decimal(line["quantity"]),
            delivered["id"] if delivered else None,
        ))
    entity = SalesService(_BoundDatabase(connection)).invoice(
        order_id, invoice_date=_day(payload),
        due_date=date.fromisoformat(payload["due_date"]) if payload.get("due_date") else None,
        lines=lines, actor="migration",
    )
    return ApplyResult("invoices", entity)


def apply_purchase_order(connection, context, payload):
    archived = _archive_cancelled(connection, context, payload)
    if archived:
        return archived
    company = _company(context)
    supplier = _lookup(connection, "suppliers", company, payload.get("partner_code"))
    service = PurchasingService(_BoundDatabase(connection))
    entity = service.create_order(
        company_id=company, supplier_id=supplier, order_date=_day(payload),
        currency=payload.get("currency") or connection.execute(
            "SELECT local_currency FROM companies WHERE id=%s", (company,)
        ).fetchone()["local_currency"],
        exchange_rate=Decimal(payload.get("exchange_rate") or "1"),
        lines=[PurchaseOrderLine(
            _lookup(connection, "items", company, line.get("item_code")),
            _lookup(connection, "warehouses", company, line.get("warehouse_code")),
            Decimal(line["quantity"]), Decimal(line["unit_price"]),
            description=line.get("description"),
            tax_code_id=_tax(connection, company, line.get("tax_code")),
        ) for line in payload["lines"]], actor="migration",
    )
    if payload.get("source_status") not in {"Draft", "bost_OpenDraft"}:
        service.post_order(entity, actor="migration")
    return ApplyResult("purchase_orders", entity)


def apply_purchase_receipt(connection, context, payload):
    if payload.get("kind") == "Purchase Return":
        return apply_purchase_return(connection, context, payload)
    archived = _archive_cancelled(connection, context, payload)
    if archived:
        return archived
    order_id = _base_document(connection, context, payload, "PurchaseOrders", "Purchase Order")
    requests = []
    for line in payload["lines"]:
        source = connection.execute(
            "SELECT id FROM purchase_order_items WHERE po_id=%s AND line_number=%s",
            (order_id, line["base_line_number"]),
        ).fetchone()
        if not source:
            raise DomainError("Purchase receipt base line is missing")
        requests.append(ReceiptLine(source["id"], Decimal(line["quantity"])))
    entity = PurchasingService(_BoundDatabase(connection)).receive(
        order_id, receipt_date=_day(payload), lines=requests, actor="migration"
    )
    _track_event(connection, "Purchase Receipt", entity, payload)
    return ApplyResult("purchase_receipts", entity)


def apply_purchase_invoice(connection, context, payload):
    if payload.get("kind") == "Debit Note":
        return apply_debit_note(connection, context, payload)
    archived = _archive_cancelled(connection, context, payload)
    if archived:
        return archived
    base_keys = {line.get("base_key") for line in payload["lines"] if line.get("base_key")}
    if len(base_keys) != 1:
        raise DomainError("Purchase invoice must reference exactly one order or receipt")
    base_key = base_keys.pop()
    order_id = _try_crosswalk(
        connection, context, ("PurchaseOrders", "Purchase Order"), base_key
    )
    receipt_id = None
    if order_id is None:
        receipt_id = _crosswalk(
            connection, context, ("PurchaseDeliveryNotes", "Purchase Receipt"), base_key
        )
    lines = []
    for line in payload["lines"]:
        if receipt_id:
            receipt = connection.execute(
                """SELECT receipt_line.id,receipt_line.purchase_order_item_id,
                          order_line.po_id
                     FROM purchase_receipt_items receipt_line
                     JOIN purchase_order_items order_line
                       ON order_line.id=receipt_line.purchase_order_item_id
                    WHERE receipt_line.receipt_id=%s AND receipt_line.line_number=%s""",
                (receipt_id, line["base_line_number"]),
            ).fetchone()
            source = {"id": receipt["purchase_order_item_id"]} if receipt else None
            if receipt:
                order_id = receipt["po_id"]
        else:
            source = connection.execute(
                "SELECT id FROM purchase_order_items WHERE po_id=%s AND line_number=%s",
                (order_id, line["base_line_number"]),
            ).fetchone()
            receipt = None
        if not source:
            raise DomainError("Purchase invoice base line is missing")
        receipt = receipt or connection.execute(
            """SELECT receipt_line.id FROM purchase_receipt_items receipt_line
                JOIN purchase_receipts receipt ON receipt.id=receipt_line.receipt_id
                WHERE receipt_line.purchase_order_item_id=%s
                  AND receipt.document_state='Posted'
                  AND receipt_line.billed_qty<receipt_line.accepted_qty+receipt_line.rejected_qty
                ORDER BY receipt.id LIMIT 1""",
            (source["id"],),
        ).fetchone()
        lines.append(PurchaseInvoiceLine(
            source["id"], Decimal(line["quantity"]),
            receipt_line_id=receipt["id"] if receipt else None,
        ))
    entity = PurchasingService(_BoundDatabase(connection)).invoice(
        order_id, invoice_date=_day(payload),
        due_date=date.fromisoformat(payload["due_date"]) if payload.get("due_date") else None,
        lines=lines, actor="migration", supplier_reference=payload.get("supplier_reference"),
    )
    return ApplyResult("purchase_invoices", entity)


def apply_sales_return(connection, context, payload):
    archived = _archive_cancelled(connection, context, payload)
    if archived:
        return archived
    delivery_id = _base_document(
        connection, context, payload, "DeliveryNotes", "Delivery Note"
    )
    requests = []
    for line in payload["lines"]:
        source = connection.execute(
            "SELECT id FROM sales_delivery_items WHERE delivery_id=%s AND line_number=%s",
            (delivery_id, line["base_line_number"]),
        ).fetchone()
        if not source:
            raise DomainError("Sales return base delivery line is missing")
        requests.append(ReturnLine(source["id"], Decimal(line["quantity"])))
    entity = SalesService(_BoundDatabase(connection)).return_delivery(
        delivery_id, return_date=_day(payload), lines=requests, actor="migration"
    )
    _track_event(connection, "Sales Return", entity, payload)
    return ApplyResult("sales_deliveries", entity)


def apply_credit_note(connection, context, payload):
    archived = _archive_cancelled(connection, context, payload)
    if archived:
        return archived
    invoice_id = _base_document(connection, context, payload, "Invoices", "Sales Invoice")
    requests = []
    for line in payload["lines"]:
        source = connection.execute(
            "SELECT id FROM invoice_items WHERE invoice_id=%s AND line_number=%s",
            (invoice_id, line["base_line_number"]),
        ).fetchone()
        if not source:
            raise DomainError("Credit-note base invoice line is missing")
        requests.append(CreditLine(source["id"], Decimal(line["quantity"])))
    entity = SalesService(_BoundDatabase(connection)).issue_credit_note(
        invoice_id, credit_date=_day(payload), lines=requests, actor="migration"
    )
    return ApplyResult("invoices", entity)


def apply_purchase_return(connection, context, payload):
    archived = _archive_cancelled(connection, context, payload)
    if archived:
        return archived
    receipt_id = _base_document(
        connection, context, payload, "PurchaseDeliveryNotes", "Purchase Receipt"
    )
    requests = []
    for line in payload["lines"]:
        source = connection.execute(
            "SELECT id FROM purchase_receipt_items WHERE receipt_id=%s AND line_number=%s",
            (receipt_id, line["base_line_number"]),
        ).fetchone()
        if not source:
            raise DomainError("Purchase return base receipt line is missing")
        requests.append(ReceiptReturnLine(source["id"], Decimal(line["quantity"])))
    entity = PurchasingService(_BoundDatabase(connection)).return_receipt(
        receipt_id, return_date=_day(payload), lines=requests, actor="migration"
    )
    _track_event(connection, "Purchase Return", entity, payload)
    return ApplyResult("purchase_receipts", entity)


def apply_debit_note(connection, context, payload):
    archived = _archive_cancelled(connection, context, payload)
    if archived:
        return archived
    invoice_id = _base_document(
        connection, context, payload, "PurchaseInvoices", "Purchase Invoice"
    )
    requests = []
    for line in payload["lines"]:
        source = connection.execute(
            "SELECT id FROM purchase_invoice_items WHERE purchase_invoice_id=%s AND line_number=%s",
            (invoice_id, line["base_line_number"]),
        ).fetchone()
        if not source:
            raise DomainError("Debit-note base invoice line is missing")
        requests.append(DebitNoteLine(source["id"], Decimal(line["quantity"])))
    entity = PurchasingService(_BoundDatabase(connection)).issue_debit_note(
        invoice_id, debit_date=_day(payload), lines=requests, actor="migration"
    )
    return ApplyResult("purchase_invoices", entity)


def apply_payment(connection, context, payload):
    company = _company(context)
    is_customer = payload["kind"] == "Customer Receipt"
    party_table = "customers" if is_customer else "suppliers"
    party = _lookup(connection, party_table, company, payload.get("partner_code"))
    invoice_objects = (
        ("Invoices", "Sales Invoice") if is_customer
        else ("PurchaseInvoices", "Purchase Invoice")
    )
    allocations = []
    for row in payload.get("allocations") or []:
        target = _crosswalk(connection, context, invoice_objects, row["document_key"])
        table = "invoices" if is_customer else "purchase_invoices"
        invoice = connection.execute(
            f"SELECT * FROM {table} WHERE id=%s", (target,)
        ).fetchone()
        invoice_amount = min(Decimal(row["amount"]), Decimal(invoice["outstanding_amount"]))
        base = invoice_amount * Decimal(invoice["exchange_rate"])
        allocation_type = PaymentAllocation if is_customer else SupplierPaymentAllocation
        allocations.append(allocation_type(target, invoice_amount, invoice_amount, base))
    currency = payload.get("currency") or connection.execute(
        "SELECT local_currency FROM companies WHERE id=%s", (company,)
    ).fetchone()["local_currency"]
    bank = connection.execute(
        """SELECT id FROM bank_accounts WHERE company_id=%s AND currency=%s AND active=true
            ORDER BY id LIMIT 1""",
        (company, currency),
    ).fetchone()
    common = dict(
        company_id=company, payment_date=_day(payload), currency=currency,
        exchange_rate=Decimal(payload.get("exchange_rate") or "1"),
        payment_amount=Decimal(payload["amount"]), allocations=allocations,
        actor="migration", bank_account_id=bank["id"] if bank else None,
        reference_number=payload.get("reference_number"),
    )
    if is_customer:
        entity = SalesService(_BoundDatabase(connection)).receive_payment(
            customer_id=party, **common
        )
    else:
        entity = PurchasingService(_BoundDatabase(connection)).pay_supplier(
            supplier_id=party, **common
        )
    return ApplyResult("payments", entity)


def apply_journal(connection, context, payload):
    company = _company(context)
    clean = []
    for row in payload["lines"]:
        account = connection.execute(
            "SELECT id FROM accounts WHERE company_id=%s AND (code=%s OR name=%s)",
            (company, row["account_code"], row["account_code"]),
        ).fetchone()
        if not account:
            raise DomainError(f"Journal account is not mapped: {row['account_code']}")
        clean.append((account["id"], Decimal(row["debit"]), Decimal(row["credit"]), row.get("memo")))
    debit = sum((row[1] for row in clean), Decimal("0"))
    credit = sum((row[2] for row in clean), Decimal("0"))
    if debit <= 0 or debit != credit:
        raise DomainError("Source journal is not balanced")
    document_code = next_code(connection, company, "Journal Entry", prefix="JE-")
    code = connection.execute(
        """INSERT INTO journal_entries(company_id,code,entry_date,memo,status,document_state,
                                         posted_at,posted_by)
           VALUES (%s,%s,%s,%s,'Posted','Posted',now(),'migration') RETURNING id,code""",
        (company, document_code, _day(payload), payload.get("memo")),
    ).fetchone()
    for number, row in enumerate(clean, 1):
        connection.execute(
            """INSERT INTO journal_lines
                   (journal_id,line_number,account_id,debit,credit,memo)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (code["id"], number, *row),
        )
    AccountingService(_BoundDatabase(connection)).post_voucher(
        company_id=company, voucher_type="Journal Entry", voucher_id=code["id"],
        voucher_code=code["code"], posting_date=_day(payload), actor="migration",
        lines=[PostingLine(account, debit=debit, credit=credit, memo=memo)
               for account, debit, credit, memo in clean], connection=connection,
    )
    return ApplyResult("journal_entries", code["id"])


def apply_stock_entry(connection, context, payload):
    if not payload.get("lines"):
        return _archive_source(
            connection, context, payload, "Stock reconciliation has no adjustments"
        )
    company = _company(context)
    event = InventoryService(_BoundDatabase(connection)).post_event(
        company_id=company, event_type=payload.get("event_type") or "Adjustment",
        voucher_type="Migrated Stock Entry", voucher_id=int(payload["source_key"].split("-")[-1])
        if str(payload["source_key"]).split("-")[-1].isdigit()
        else int(hashlib.sha256(str(payload["source_key"]).encode()).hexdigest()[:12], 16),
        voucher_code=f"MIG-STOCK-{payload['source_document_no']}", event_date=_day(payload),
        lines=[InventoryLine(
            _lookup(connection, "items", company, row.get("item_code")),
            _lookup(connection, "warehouses", company, row.get("warehouse_code")),
            Decimal(row["quantity"]), Decimal(row["unit_cost"]) if Decimal(row["quantity"]) > 0 else None,
        ) for row in payload["lines"]], actor="migration", connection=connection,
    )
    apply_tracking(connection, event, payload["lines"])
    return ApplyResult("inventory_events", event)
