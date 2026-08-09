"""Sales quotation and purchase request/RFQ conversion workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .accounting import ZERO, amount
from .database import Database
from .documents import audit, next_code, require_state
from .errors import DocumentStateError, DomainError
from .inventory import number


@dataclass(frozen=True)
class QuoteLine:
    item_id: int
    warehouse_id: int
    quantity: Decimal
    unit_price: Decimal
    description: str | None = None
    uom_id: int | None = None
    tax_code_id: int | None = None


@dataclass(frozen=True)
class QuoteConversionLine:
    quote_line_id: int
    quantity: Decimal


@dataclass(frozen=True)
class RequestLine:
    item_id: int
    warehouse_id: int
    quantity: Decimal
    description: str | None = None
    uom_id: int | None = None


@dataclass(frozen=True)
class RfqLine:
    request_line_id: int
    quantity: Decimal


@dataclass(frozen=True)
class SupplierQuoteLine:
    rfq_line_id: int
    quantity: Decimal
    unit_price: Decimal


class PreorderService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_sales_quote(
        self,
        *,
        company_id: int,
        customer_id: int,
        quote_date: date,
        valid_until: date | None,
        currency: str,
        exchange_rate: Decimal,
        lines: list[QuoteLine],
        actor: str,
        code: str | None = None,
        note: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Sales quotation requires at least one line")
        with self.database.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM customers WHERE id=%s AND company_id=%s AND active=true",
                (customer_id, company_id),
            ).fetchone():
                raise DomainError("Active customer does not belong to company")
            code = code or next_code(connection, company_id, "Sales Quote", prefix="QUO-")
            prepared = []
            net_total = ZERO
            tax_total = ZERO
            for line in lines:
                self._item_warehouse(connection, company_id, line.item_id, line.warehouse_id)
                qty = number(line.quantity)
                rate = number(line.unit_price)
                if qty <= ZERO or rate < ZERO:
                    raise DomainError("Quote quantity must be positive and rate non-negative")
                tax_rate = ZERO
                if line.tax_code_id:
                    tax = connection.execute(
                        "SELECT rate FROM tax_codes WHERE id=%s AND company_id=%s AND active=true",
                        (line.tax_code_id, company_id),
                    ).fetchone()
                    if not tax:
                        raise DomainError("Quote tax code does not belong to company")
                    tax_rate = amount(tax["rate"])
                net = amount(qty * rate)
                tax_amount = amount(net * tax_rate / 100)
                net_total += net
                tax_total += tax_amount
                prepared.append((line, qty, rate, net, tax_amount))
            quote_id = connection.execute(
                """INSERT INTO sales_quotes (
                       company_id,code,customer_id,quote_date,valid_until,currency,
                       exchange_rate,net_total,tax_total,total,document_state,status,note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Draft','Open',%s)
                   RETURNING id""",
                (
                    company_id, code, customer_id, quote_date, valid_until, currency,
                    amount(exchange_rate), net_total, tax_total,
                    amount(net_total + tax_total), note,
                ),
            ).fetchone()["id"]
            for line_number, (line, qty, rate, net, tax_amount) in enumerate(prepared, 1):
                connection.execute(
                    """INSERT INTO sales_quote_items (
                           quote_id,line_number,item_id,warehouse_id,description,uom_id,
                           qty,rate,net_amount,tax_amount,amount,tax_code_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        quote_id, line_number, line.item_id, line.warehouse_id,
                        line.description, line.uom_id, qty, rate, net, tax_amount,
                        amount(net + tax_amount), line.tax_code_id,
                    ),
                )
            audit(
                connection, company_id=company_id, entity_type="Sales Quote",
                entity_id=quote_id, event_type="Created", actor=actor,
                next_state="Draft",
            )
            return quote_id

    def post_sales_quote(self, quote_id: int, *, actor: str) -> None:
        with self.database.transaction() as connection:
            quote = connection.execute(
                "SELECT * FROM sales_quotes WHERE id=%s FOR UPDATE", (quote_id,)
            ).fetchone()
            if not quote:
                raise DomainError("Sales quote not found")
            require_state(quote, "Draft", "Approved")
            connection.execute(
                """UPDATE sales_quotes SET document_state='Posted',posted_at=now(),
                       posted_by=%s,row_version=row_version+1,updated_at=now() WHERE id=%s""",
                (actor, quote_id),
            )
            audit(
                connection, company_id=quote["company_id"], entity_type="Sales Quote",
                entity_id=quote_id, event_type="Posted", actor=actor,
                previous_state=quote["document_state"], next_state="Posted",
            )

    def convert_sales_quote(
        self,
        quote_id: int,
        *,
        order_date: date,
        delivery_date: date | None,
        lines: list[QuoteConversionLine],
        actor: str,
        code: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Quote conversion requires at least one line")
        with self.database.transaction() as connection:
            quote = connection.execute(
                "SELECT * FROM sales_quotes WHERE id=%s FOR UPDATE", (quote_id,)
            ).fetchone()
            if not quote:
                raise DomainError("Sales quote not found")
            require_state(quote, "Posted")
            if quote["status"] in {"Closed", "Cancelled", "Expired", "Lost", "Ordered"}:
                raise DocumentStateError(f"Quote status {quote['status']} cannot be converted")
            code = code or next_code(connection, quote["company_id"], "Sales Order", prefix="SO-")
            prepared = []
            total = ZERO
            for request in lines:
                source = connection.execute(
                    "SELECT * FROM sales_quote_items WHERE id=%s AND quote_id=%s FOR UPDATE",
                    (request.quote_line_id, quote_id),
                ).fetchone()
                qty = number(request.quantity)
                if not source or qty <= ZERO or source["ordered_qty"] + qty > source["qty"]:
                    raise DomainError("Quote conversion exceeds open quotation quantity")
                line_total = amount(qty * source["rate"])
                total += line_total
                prepared.append((source, qty, line_total))
            order_id = connection.execute(
                """INSERT INTO sales_orders (
                       company_id,code,customer_id,order_date,delivery_date,status,
                       currency,exchange_rate,total,document_state,quote_id,posted_at,posted_by)
                   VALUES (%s,%s,%s,%s,%s,'Confirmed',%s,%s,%s,'Posted',%s,now(),%s)
                   RETURNING id""",
                (
                    quote["company_id"], code, quote["customer_id"], order_date,
                    delivery_date, quote["currency"], quote["exchange_rate"],
                    total, quote_id, actor,
                ),
            ).fetchone()["id"]
            for line_number, (source, qty, line_total) in enumerate(prepared, 1):
                connection.execute(
                    """INSERT INTO sales_order_items (
                           order_id,line_number,item_id,warehouse_id,description,qty,rate,
                           amount,tax_code_id,uom_id,stock_qty,quote_item_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        order_id, line_number, source["item_id"], source["warehouse_id"],
                        source["description"], qty, source["rate"], line_total,
                        source["tax_code_id"], source["uom_id"], qty, source["id"],
                    ),
                )
                connection.execute(
                    "UPDATE sales_quote_items SET ordered_qty=ordered_qty+%s WHERE id=%s",
                    (qty, source["id"]),
                )
            remaining = connection.execute(
                "SELECT count(*) AS value FROM sales_quote_items WHERE quote_id=%s AND ordered_qty<qty",
                (quote_id,),
            ).fetchone()["value"]
            connection.execute(
                "UPDATE sales_quotes SET status=%s,updated_at=now() WHERE id=%s",
                ("Partly Ordered" if remaining else "Ordered", quote_id),
            )
            audit(
                connection, company_id=quote["company_id"], entity_type="Sales Order",
                entity_id=order_id, event_type="Posted From Quote", actor=actor,
                next_state="Posted", details={"quote_id": quote_id},
            )
            return order_id

    def create_purchase_request(
        self,
        *,
        company_id: int,
        request_date: date,
        required_by: date | None,
        lines: list[RequestLine],
        actor: str,
        code: str | None = None,
        note: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Purchase request requires at least one line")
        with self.database.transaction() as connection:
            code = code or next_code(connection, company_id, "Purchase Request", prefix="REQ-")
            request_id = connection.execute(
                """INSERT INTO purchase_requests (
                       company_id,code,request_date,required_by,document_state,status,
                       requested_by,note)
                   VALUES (%s,%s,%s,%s,'Posted','Open',%s,%s) RETURNING id""",
                (company_id, code, request_date, required_by, actor, note),
            ).fetchone()["id"]
            for line_number, line in enumerate(lines, 1):
                self._item_warehouse(connection, company_id, line.item_id, line.warehouse_id)
                qty = number(line.quantity)
                if qty <= ZERO:
                    raise DomainError("Requested quantity must be positive")
                connection.execute(
                    """INSERT INTO purchase_request_items (
                           purchase_request_id,line_number,item_id,warehouse_id,uom_id,
                           qty,description)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        request_id, line_number, line.item_id, line.warehouse_id,
                        line.uom_id, qty, line.description,
                    ),
                )
            audit(
                connection, company_id=company_id, entity_type="Purchase Request",
                entity_id=request_id, event_type="Posted", actor=actor,
                next_state="Posted",
            )
            return request_id

    def create_rfq(
        self,
        request_id: int,
        *,
        request_date: date,
        response_due: date | None,
        supplier_ids: list[int],
        lines: list[RfqLine],
        actor: str,
        code: str | None = None,
    ) -> int:
        if not supplier_ids or not lines:
            raise DomainError("RFQ requires suppliers and lines")
        with self.database.transaction() as connection:
            request = connection.execute(
                "SELECT * FROM purchase_requests WHERE id=%s FOR UPDATE", (request_id,)
            ).fetchone()
            if not request:
                raise DomainError("Purchase request not found")
            require_state(request, "Posted")
            code = code or next_code(connection, request["company_id"], "Request For Quote", prefix="RFQ-")
            rfq_id = connection.execute(
                """INSERT INTO requests_for_quote (
                       company_id,code,request_date,response_due,document_state,status)
                   VALUES (%s,%s,%s,%s,'Posted','Open') RETURNING id""",
                (request["company_id"], code, request_date, response_due),
            ).fetchone()["id"]
            for line_number, line in enumerate(lines, 1):
                source = connection.execute(
                    """SELECT * FROM purchase_request_items
                        WHERE id=%s AND purchase_request_id=%s""",
                    (line.request_line_id, request_id),
                ).fetchone()
                qty = number(line.quantity)
                if not source or qty <= ZERO or qty > source["qty"] - source["ordered_qty"]:
                    raise DomainError("RFQ quantity exceeds open request quantity")
                connection.execute(
                    """INSERT INTO rfq_items (
                           rfq_id,line_number,purchase_request_item_id,item_id,
                           warehouse_id,uom_id,qty,description)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        rfq_id, line_number, source["id"], source["item_id"],
                        source["warehouse_id"], source["uom_id"], qty,
                        source["description"],
                    ),
                )
            for supplier_id in set(supplier_ids):
                if not connection.execute(
                    "SELECT 1 FROM suppliers WHERE id=%s AND company_id=%s AND active=true",
                    (supplier_id, request["company_id"]),
                ).fetchone():
                    raise DomainError("RFQ supplier does not belong to company")
                connection.execute(
                    "INSERT INTO rfq_suppliers(rfq_id,supplier_id,sent_at) VALUES (%s,%s,now())",
                    (rfq_id, supplier_id),
                )
            connection.execute(
                "UPDATE purchase_requests SET status='RFQ Created',updated_at=now() WHERE id=%s",
                (request_id,),
            )
            audit(
                connection, company_id=request["company_id"], entity_type="Request For Quote",
                entity_id=rfq_id, event_type="Posted", actor=actor,
                next_state="Posted", details={"purchase_request_id": request_id},
            )
            return rfq_id

    def record_supplier_quote(
        self,
        rfq_id: int,
        *,
        supplier_id: int,
        quote_date: date,
        valid_until: date | None,
        currency: str,
        exchange_rate: Decimal,
        lines: list[SupplierQuoteLine],
        actor: str,
        code: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Supplier quotation requires lines")
        with self.database.transaction() as connection:
            rfq = connection.execute(
                "SELECT * FROM requests_for_quote WHERE id=%s FOR UPDATE", (rfq_id,)
            ).fetchone()
            if not rfq or not connection.execute(
                "SELECT 1 FROM rfq_suppliers WHERE rfq_id=%s AND supplier_id=%s",
                (rfq_id, supplier_id),
            ).fetchone():
                raise DomainError("Supplier was not invited to this RFQ")
            require_state(rfq, "Posted")
            code = code or next_code(connection, rfq["company_id"], "Supplier Quotation", prefix="SQUO-")
            prepared = []
            total = ZERO
            for line in lines:
                source = connection.execute(
                    "SELECT * FROM rfq_items WHERE id=%s AND rfq_id=%s",
                    (line.rfq_line_id, rfq_id),
                ).fetchone()
                qty = number(line.quantity)
                rate = number(line.unit_price)
                if not source or qty <= ZERO or qty > source["qty"] or rate < ZERO:
                    raise DomainError("Supplier quote line is invalid")
                line_total = amount(qty * rate)
                total += line_total
                prepared.append((source, qty, rate, line_total))
            quote_id = connection.execute(
                """INSERT INTO supplier_quotations (
                       company_id,code,rfq_id,supplier_id,quote_date,valid_until,
                       currency,exchange_rate,total,document_state,status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'Posted','Open') RETURNING id""",
                (
                    rfq["company_id"], code, rfq_id, supplier_id, quote_date,
                    valid_until, currency, amount(exchange_rate), total,
                ),
            ).fetchone()["id"]
            for line_number, (source, qty, rate, line_total) in enumerate(prepared, 1):
                connection.execute(
                    """INSERT INTO supplier_quotation_items (
                           supplier_quotation_id,line_number,rfq_item_id,item_id,uom_id,
                           qty,rate,amount)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        quote_id, line_number, source["id"], source["item_id"],
                        source["uom_id"], qty, rate, line_total,
                    ),
                )
            connection.execute(
                "UPDATE rfq_suppliers SET responded_at=now() WHERE rfq_id=%s AND supplier_id=%s",
                (rfq_id, supplier_id),
            )
            connection.execute(
                "UPDATE requests_for_quote SET status='Responses Received',updated_at=now() WHERE id=%s",
                (rfq_id,),
            )
            audit(
                connection, company_id=rfq["company_id"], entity_type="Supplier Quotation",
                entity_id=quote_id, event_type="Posted", actor=actor,
                next_state="Posted", details={"rfq_id": rfq_id},
            )
            return quote_id

    def award_supplier_quote(
        self,
        quote_id: int,
        *,
        order_date: date,
        actor: str,
        code: str | None = None,
    ) -> int:
        with self.database.transaction() as connection:
            quote = connection.execute(
                "SELECT * FROM supplier_quotations WHERE id=%s FOR UPDATE", (quote_id,)
            ).fetchone()
            if not quote:
                raise DomainError("Supplier quotation not found")
            require_state(quote, "Posted")
            if quote["status"] not in {"Open", "Accepted"}:
                raise DocumentStateError(f"Supplier quote status {quote['status']} cannot be awarded")
            code = code or next_code(connection, quote["company_id"], "Purchase Order", prefix="PO-")
            source_lines = connection.execute(
                """SELECT quote_line.*,rfq_line.warehouse_id,rfq_line.description,
                          rfq_line.purchase_request_item_id
                    FROM supplier_quotation_items quote_line
                    JOIN rfq_items rfq_line ON rfq_line.id=quote_line.rfq_item_id
                   WHERE quote_line.supplier_quotation_id=%s ORDER BY quote_line.line_number""",
                (quote_id,),
            ).fetchall()
            order_id = connection.execute(
                """INSERT INTO purchase_orders (
                       company_id,code,supplier_id,order_date,status,currency,
                       exchange_rate,total,document_state,supplier_quotation_id,
                       posted_at,posted_by)
                   VALUES (%s,%s,%s,%s,'Ordered',%s,%s,%s,'Posted',%s,now(),%s)
                   RETURNING id""",
                (
                    quote["company_id"], code, quote["supplier_id"], order_date,
                    quote["currency"], quote["exchange_rate"], quote["total"],
                    quote_id, actor,
                ),
            ).fetchone()["id"]
            request_ids = set()
            for line_number, source in enumerate(source_lines, 1):
                connection.execute(
                    """INSERT INTO purchase_order_items (
                           po_id,line_number,item_id,warehouse_id,description,qty,rate,
                           amount,uom_id,stock_qty)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        order_id, line_number, source["item_id"], source["warehouse_id"],
                        source["description"], source["qty"], source["rate"],
                        source["amount"], source["uom_id"], source["qty"],
                    ),
                )
                if source["purchase_request_item_id"]:
                    request_id = connection.execute(
                        """UPDATE purchase_request_items SET ordered_qty=ordered_qty+%s
                            WHERE id=%s RETURNING purchase_request_id""",
                        (source["qty"], source["purchase_request_item_id"]),
                    ).fetchone()["purchase_request_id"]
                    request_ids.add(request_id)
            connection.execute(
                "UPDATE supplier_quotations SET status='Ordered',updated_at=now() WHERE id=%s",
                (quote_id,),
            )
            connection.execute(
                "UPDATE requests_for_quote SET status='Awarded',updated_at=now() WHERE id=%s",
                (quote["rfq_id"],),
            )
            for request_id in request_ids:
                remaining = connection.execute(
                    "SELECT count(*) AS value FROM purchase_request_items WHERE purchase_request_id=%s AND ordered_qty<qty",
                    (request_id,),
                ).fetchone()["value"]
                connection.execute(
                    "UPDATE purchase_requests SET status=%s,updated_at=now() WHERE id=%s",
                    ("Partly Ordered" if remaining else "Ordered", request_id),
                )
            audit(
                connection, company_id=quote["company_id"], entity_type="Purchase Order",
                entity_id=order_id, event_type="Posted From Supplier Quotation",
                actor=actor, next_state="Posted", details={"supplier_quotation_id": quote_id},
            )
            return order_id

    @staticmethod
    def _item_warehouse(connection, company_id, item_id, warehouse_id):
        if not connection.execute(
            """SELECT 1 FROM items item JOIN warehouses warehouse ON warehouse.id=%s
                WHERE item.id=%s AND item.company_id=%s AND warehouse.company_id=%s
                  AND item.active=true AND warehouse.active=true""",
            (warehouse_id, item_id, company_id, company_id),
        ).fetchone():
            raise DomainError("Item or warehouse does not belong to company")
