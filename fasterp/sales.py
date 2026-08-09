"""Order-to-cash documents using the shared inventory and accounting kernels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .accounting import AccountingService, PostingLine, ZERO, amount
from .database import Database
from .documents import audit, next_code, require_state
from .errors import AllocationError, DocumentStateError, DomainError
from .inventory import InventoryLine, InventoryService, number


@dataclass(frozen=True)
class OrderLine:
    item_id: int
    warehouse_id: int
    quantity: Decimal
    unit_price: Decimal
    description: str | None = None
    uom_id: int | None = None
    tax_code_id: int | None = None
    delivery_tolerance_percent: Decimal = ZERO
    billing_tolerance_percent: Decimal = ZERO


@dataclass(frozen=True)
class DeliveryLine:
    order_line_id: int
    quantity: Decimal


@dataclass(frozen=True)
class InvoiceLine:
    order_line_id: int
    quantity: Decimal
    delivery_line_id: int | None = None


@dataclass(frozen=True)
class PaymentAllocation:
    invoice_id: int
    payment_amount: Decimal
    invoice_amount: Decimal
    base_amount: Decimal


@dataclass(frozen=True)
class ReturnLine:
    delivery_line_id: int
    quantity: Decimal


@dataclass(frozen=True)
class CreditLine:
    invoice_line_id: int
    quantity: Decimal


class SalesService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.accounting = AccountingService(database)
        self.inventory = InventoryService(database)

    def create_order(
        self,
        *,
        company_id: int,
        customer_id: int,
        order_date: date,
        delivery_date: date | None,
        currency: str,
        exchange_rate: Decimal,
        lines: list[OrderLine],
        actor: str,
        code: str | None = None,
        note: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Sales order requires at least one line")
        exchange = amount(exchange_rate)
        if exchange <= ZERO:
            raise DomainError("Exchange rate must be positive")
        with self.database.transaction() as connection:
            customer = connection.execute(
                "SELECT id FROM customers WHERE id=%s AND company_id=%s AND active=true",
                (customer_id, company_id),
            ).fetchone()
            if not customer:
                raise DomainError("Active customer does not belong to company")
            code = code or next_code(
                connection, company_id, "Sales Order", prefix="SO-"
            )
            normalized = []
            total = ZERO
            for line in lines:
                qty = number(line.quantity)
                rate = number(line.unit_price)
                if qty <= ZERO or rate < ZERO:
                    raise DomainError("Order quantity must be positive and rate non-negative")
                self._validate_item_warehouse(
                    connection, company_id, line.item_id, line.warehouse_id
                )
                line_total = amount(qty * rate)
                total += line_total
                normalized.append((line, qty, rate, line_total))
            order_id = connection.execute(
                """INSERT INTO sales_orders (
                       company_id,code,customer_id,order_date,delivery_date,status,
                       currency,exchange_rate,total,note,document_state)
                   VALUES (%s,%s,%s,%s,%s,'Draft',%s,%s,%s,%s,'Draft')
                   RETURNING id""",
                (
                    company_id, code, customer_id, order_date, delivery_date,
                    currency, exchange, total, note,
                ),
            ).fetchone()["id"]
            for line_number, (line, qty, rate, line_total) in enumerate(normalized, 1):
                connection.execute(
                    """INSERT INTO sales_order_items (
                           order_id,line_number,item_id,warehouse_id,description,qty,rate,
                           amount,tax_code_id,uom_id,stock_qty,
                           delivery_tolerance_percent,billing_tolerance_percent)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        order_id, line_number, line.item_id, line.warehouse_id,
                        line.description, qty, rate, line_total, line.tax_code_id,
                        line.uom_id, qty, amount(line.delivery_tolerance_percent),
                        amount(line.billing_tolerance_percent),
                    ),
                )
            audit(
                connection, company_id=company_id, entity_type="Sales Order",
                entity_id=order_id, event_type="Created", actor=actor,
                next_state="Draft",
            )
            return order_id

    def post_order(self, order_id: int, *, actor: str) -> None:
        with self.database.transaction() as connection:
            order = connection.execute(
                "SELECT * FROM sales_orders WHERE id=%s FOR UPDATE", (order_id,)
            ).fetchone()
            if not order:
                raise DomainError("Sales order not found")
            require_state(order, "Draft", "Approved")
            line_count = connection.execute(
                "SELECT count(*) AS value FROM sales_order_items WHERE order_id=%s AND qty>0",
                (order_id,),
            ).fetchone()["value"]
            if not line_count:
                raise DomainError("Sales order has no valid lines")
            connection.execute(
                """UPDATE sales_orders
                      SET document_state='Posted',status='Confirmed',posted_at=now(),
                          posted_by=%s,row_version=row_version+1,updated_at=now()
                    WHERE id=%s""",
                (actor, order_id),
            )
            audit(
                connection, company_id=order["company_id"], entity_type="Sales Order",
                entity_id=order_id, event_type="Posted", actor=actor,
                previous_state=order["document_state"], next_state="Posted",
                revision=order["revision"],
            )

    def deliver(
        self,
        order_id: int,
        *,
        delivery_date: date,
        lines: list[DeliveryLine],
        actor: str,
        code: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Delivery requires at least one line")
        with self.database.transaction() as connection:
            order = connection.execute(
                "SELECT * FROM sales_orders WHERE id=%s FOR UPDATE", (order_id,)
            ).fetchone()
            if not order:
                raise DomainError("Sales order not found")
            require_state(order, "Posted")
            if order["status"] in {"Closed", "Cancelled", "On Hold"}:
                raise DocumentStateError(f"Order status {order['status']} cannot be delivered")
            code = code or next_code(
                connection, order["company_id"], "Sales Delivery", prefix="DN-"
            )
            delivery_id = connection.execute(
                """INSERT INTO sales_deliveries (
                       company_id,code,customer_id,document_kind,delivery_date,
                       document_state,status,posted_at,posted_by)
                   VALUES (%s,%s,%s,'Delivery',%s,'Posted','To Bill',now(),%s)
                   RETURNING id""",
                (
                    order["company_id"], code, order["customer_id"],
                    delivery_date, actor,
                ),
            ).fetchone()["id"]
            inventory_lines = []
            for line_number, request in enumerate(lines, 1):
                source = connection.execute(
                    """SELECT line.*,item.inventory_item FROM sales_order_items line
                        JOIN items item ON item.id=line.item_id
                        WHERE line.id=%s AND line.order_id=%s FOR UPDATE OF line""",
                    (request.order_line_id, order_id),
                ).fetchone()
                if not source:
                    raise DomainError("Delivery line does not belong to sales order")
                qty = number(request.quantity)
                remaining = number(source["qty"] - source["delivered_qty"] + source["returned_qty"])
                maximum = number(source["qty"] * (Decimal("1") + source["delivery_tolerance_percent"] / 100))
                if qty <= ZERO or source["delivered_qty"] + qty - source["returned_qty"] > maximum:
                    raise DomainError(
                        f"Delivery quantity {qty} exceeds remaining/tolerance for order line {source['id']}"
                    )
                delivery_line_id = connection.execute(
                    """INSERT INTO sales_delivery_items (
                           delivery_id,line_number,sales_order_item_id,item_id,warehouse_id,
                           uom_id,qty,stock_qty)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        delivery_id, line_number, source["id"], source["item_id"],
                        source["warehouse_id"], source["uom_id"], qty, qty,
                    ),
                ).fetchone()["id"]
                connection.execute(
                    "UPDATE sales_order_items SET delivered_qty=delivered_qty+%s WHERE id=%s",
                    (qty, source["id"]),
                )
                if source["inventory_item"]:
                    inventory_lines.append(
                        InventoryLine(
                            source["item_id"], source["warehouse_id"], -qty,
                            source_line_type="Sales Delivery Item",
                            source_line_id=delivery_line_id,
                        )
                    )
            if inventory_lines:
                event_id = self.inventory.post_event(
                    company_id=order["company_id"], event_type="Delivery",
                    voucher_type="Sales Delivery", voucher_id=delivery_id,
                    voucher_code=code, event_date=delivery_date,
                    lines=inventory_lines, actor=actor, connection=connection,
                )
                cogs = -connection.execute(
                    "SELECT sum(value_change) AS value FROM inventory_ledger_entries WHERE event_id=%s",
                    (event_id,),
                ).fetchone()["value"]
                settings = self._account_settings(connection, order["company_id"])
                self.accounting.post_voucher(
                    company_id=order["company_id"], voucher_type="Sales Delivery",
                    voucher_id=delivery_id, voucher_code=code,
                    posting_date=delivery_date, actor=actor, connection=connection,
                    lines=[
                        PostingLine(settings["cogs_account_id"], debit=cogs),
                        PostingLine(settings["inventory_account_id"], credit=cogs),
                    ],
                )
            self._refresh_order_status(connection, order_id)
            audit(
                connection, company_id=order["company_id"], entity_type="Sales Delivery",
                entity_id=delivery_id, event_type="Posted", actor=actor,
                next_state="Posted", details={"sales_order_id": order_id},
            )
            return delivery_id

    def invoice(
        self,
        order_id: int,
        *,
        invoice_date: date,
        lines: list[InvoiceLine],
        actor: str,
        due_date: date | None = None,
        code: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Invoice requires at least one line")
        with self.database.transaction() as connection:
            order = connection.execute(
                "SELECT * FROM sales_orders WHERE id=%s FOR UPDATE", (order_id,)
            ).fetchone()
            if not order:
                raise DomainError("Sales order not found")
            require_state(order, "Posted")
            customer = connection.execute(
                "SELECT * FROM customers WHERE id=%s", (order["customer_id"],)
            ).fetchone()
            if not customer or not customer["partner_id"]:
                raise DomainError("Customer is not linked to a business partner")
            code = code or next_code(
                connection, order["company_id"], "Sales Invoice", prefix="INV-"
            )
            prepared = []
            net_total = ZERO
            tax_total = ZERO
            for line_number, request in enumerate(lines, 1):
                source = connection.execute(
                    """SELECT line.*,tax.rate AS tax_rate,tax.sales_account_id AS tax_account
                        FROM sales_order_items line
                        LEFT JOIN tax_codes tax ON tax.id=line.tax_code_id
                        WHERE line.id=%s AND line.order_id=%s FOR UPDATE OF line""",
                    (request.order_line_id, order_id),
                ).fetchone()
                if not source:
                    raise DomainError("Invoice line does not belong to sales order")
                qty = number(request.quantity)
                maximum = number(source["qty"] * (Decimal("1") + source["billing_tolerance_percent"] / 100))
                if qty <= ZERO or source["invoiced_qty"] + qty > maximum:
                    raise DomainError("Invoice quantity exceeds order billing tolerance")
                delivery_line = None
                if request.delivery_line_id:
                    delivery_line = connection.execute(
                        """SELECT line.* FROM sales_delivery_items line
                            JOIN sales_deliveries delivery ON delivery.id=line.delivery_id
                            WHERE line.id=%s AND line.sales_order_item_id=%s
                              AND delivery.document_state='Posted' FOR UPDATE OF line""",
                        (request.delivery_line_id, source["id"]),
                    ).fetchone()
                    if not delivery_line or delivery_line["billed_qty"] + qty > delivery_line["qty"]:
                        raise DomainError("Invoice quantity exceeds delivered unbilled quantity")
                net = amount(qty * source["rate"])
                tax = amount(net * amount(source["tax_rate"] or 0) / 100)
                total = amount(net + tax)
                net_total += net
                tax_total += tax
                prepared.append((line_number, request, source, delivery_line, qty, net, tax, total))
            total = amount(net_total + tax_total)
            exchange = amount(order["exchange_rate"])
            base_net = amount(net_total * exchange)
            base_tax = amount(tax_total * exchange)
            base_total = amount(total * exchange)
            due = due_date or invoice_date + timedelta(days=30)
            invoice_id = connection.execute(
                """INSERT INTO invoices (
                       company_id,code,order_id,customer_id,invoice_date,due_date,
                       currency,exchange_rate,net_total,tax_total,total,paid,status,
                       document_state,posted_at,posted_by,base_net_total,base_tax_total,
                       base_total,outstanding_amount)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,'Unpaid',
                           'Posted',now(),%s,%s,%s,%s,%s) RETURNING id""",
                (
                    order["company_id"], code, order_id, order["customer_id"],
                    invoice_date, due, order["currency"], exchange, net_total,
                    tax_total, total, actor, base_net, base_tax, base_total, total,
                ),
            ).fetchone()["id"]
            tax_postings: dict[int, Decimal] = {}
            for line_number, request, source, delivery_line, qty, net, tax, line_total in prepared:
                invoice_line_id = connection.execute(
                    """INSERT INTO invoice_items (
                           invoice_id,line_number,item_id,warehouse_id,description,qty,rate,
                           net_amount,tax_amount,amount,tax_code_id,sales_order_item_id,
                           delivery_item_id,uom_id,stock_qty,base_net_amount,
                           base_tax_amount,base_amount)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       RETURNING id""",
                    (
                        invoice_id, line_number, source["item_id"], source["warehouse_id"],
                        source["description"], qty, source["rate"], net, tax, line_total,
                        source["tax_code_id"], source["id"], request.delivery_line_id,
                        source["uom_id"], qty, amount(net * exchange),
                        amount(tax * exchange), amount(line_total * exchange),
                    ),
                ).fetchone()["id"]
                connection.execute(
                    "UPDATE sales_order_items SET invoiced_qty=invoiced_qty+%s WHERE id=%s",
                    (qty, source["id"]),
                )
                if delivery_line:
                    connection.execute(
                        "UPDATE sales_delivery_items SET billed_qty=billed_qty+%s WHERE id=%s",
                        (qty, delivery_line["id"]),
                    )
                if tax:
                    tax_account = source["tax_account"]
                    if tax_account:
                        tax_postings[tax_account] = tax_postings.get(tax_account, ZERO) + amount(tax * exchange)
            settings = self._account_settings(connection, order["company_id"])
            posting_lines = [
                PostingLine(
                    settings["receivable_account_id"], debit=base_total,
                    account_currency=order["currency"], account_debit=total,
                    transaction_currency=order["currency"], transaction_debit=total,
                    party_id=customer["partner_id"], due_date=due,
                ),
                PostingLine(
                    settings["sales_account_id"], credit=base_net,
                    transaction_currency=order["currency"], transaction_credit=net_total,
                ),
            ]
            if base_tax:
                if tax_postings:
                    posting_lines.extend(
                        PostingLine(account_id, credit=value)
                        for account_id, value in tax_postings.items()
                    )
                elif settings["sales_tax_account_id"]:
                    posting_lines.append(
                        PostingLine(settings["sales_tax_account_id"], credit=base_tax)
                    )
                else:
                    raise DomainError("Sales tax account is not configured")
            self.accounting.post_voucher(
                company_id=order["company_id"], voucher_type="Sales Invoice",
                voucher_id=invoice_id, voucher_code=code, posting_date=invoice_date,
                lines=posting_lines, actor=actor, connection=connection,
            )
            connection.execute(
                """INSERT INTO party_ledger_entries (
                       company_id,partner_id,partner_role,posting_date,due_date,
                       voucher_type,voucher_id,voucher_code,currency,debit,base_debit)
                   VALUES (%s,%s,'Customer',%s,%s,'Sales Invoice',%s,%s,%s,%s,%s)""",
                (
                    order["company_id"], customer["partner_id"], invoice_date,
                    due, invoice_id, code, order["currency"], total, base_total,
                ),
            )
            self._refresh_order_status(connection, order_id)
            audit(
                connection, company_id=order["company_id"], entity_type="Sales Invoice",
                entity_id=invoice_id, event_type="Posted", actor=actor,
                next_state="Posted", details={"sales_order_id": order_id},
            )
            return invoice_id

    def receive_payment(
        self,
        *,
        company_id: int,
        customer_id: int,
        payment_date: date,
        currency: str,
        exchange_rate: Decimal,
        payment_amount: Decimal,
        allocations: list[PaymentAllocation],
        actor: str,
        bank_account_id: int | None = None,
        code: str | None = None,
        reference_number: str | None = None,
    ) -> int:
        payment_total = amount(payment_amount)
        exchange = amount(exchange_rate)
        if payment_total <= ZERO or exchange <= ZERO:
            raise AllocationError("Payment amount and exchange rate must be positive")
        if sum((amount(row.payment_amount) for row in allocations), ZERO) > payment_total:
            raise AllocationError("Payment allocations exceed payment amount")
        with self.database.transaction() as connection:
            customer = connection.execute(
                "SELECT * FROM customers WHERE id=%s AND company_id=%s AND active=true",
                (customer_id, company_id),
            ).fetchone()
            if not customer or not customer["partner_id"]:
                raise DomainError("Customer is not linked to a business partner")
            settings = self._account_settings(connection, company_id)
            bank_id = bank_account_id or settings["default_bank_account_id"]
            bank = connection.execute(
                "SELECT * FROM bank_accounts WHERE id=%s AND company_id=%s AND active=true",
                (bank_id, company_id),
            ).fetchone() if bank_id else None
            if not bank:
                raise DomainError("Active bank account is not configured")
            code = code or next_code(
                connection, company_id, "Customer Payment", prefix="PAY-"
            )
            base_total = amount(payment_total * exchange)
            payment_id = connection.execute(
                """INSERT INTO payments (
                       company_id,code,customer_id,payment_date,currency,exchange_rate,
                       amount,status,document_state,payment_type,bank_account_id,
                       base_amount,unallocated_amount,reference_number,posted_at,posted_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'Posted','Posted','Customer Receipt',
                           %s,%s,%s,%s,now(),%s) RETURNING id""",
                (
                    company_id, code, customer_id, payment_date, currency, exchange,
                    payment_total, bank_id, base_total,
                    payment_total - sum((amount(row.payment_amount) for row in allocations), ZERO),
                    reference_number, actor,
                ),
            ).fetchone()["id"]
            payment_ledger_id = connection.execute(
                """INSERT INTO party_ledger_entries (
                       company_id,partner_id,partner_role,posting_date,voucher_type,
                       voucher_id,voucher_code,currency,credit,base_credit,is_advance)
                   VALUES (%s,%s,'Customer',%s,'Customer Payment',%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (
                    company_id, customer["partner_id"], payment_date, payment_id,
                    code, currency, payment_total, base_total, not bool(allocations),
                ),
            ).fetchone()["id"]
            original_base_total = ZERO
            allocated_payment_base = ZERO
            gain_loss = ZERO
            for allocation in allocations:
                invoice = connection.execute(
                    """SELECT invoice.*,customer.partner_id FROM invoices invoice
                        JOIN customers customer ON customer.id=invoice.customer_id
                        WHERE invoice.id=%s AND invoice.company_id=%s
                          AND invoice.customer_id=%s AND invoice.document_state='Posted'
                        FOR UPDATE OF invoice""",
                    (allocation.invoice_id, company_id, customer_id),
                ).fetchone()
                invoice_amount = amount(allocation.invoice_amount)
                pay_amount = amount(allocation.payment_amount)
                allocation_base = amount(allocation.base_amount)
                if not invoice or invoice_amount <= ZERO or pay_amount <= ZERO or allocation_base <= ZERO:
                    raise AllocationError("Payment allocation is invalid")
                if invoice_amount > amount(invoice["outstanding_amount"]):
                    raise AllocationError(f"Allocation exceeds outstanding invoice {invoice['code']}")
                original_base = amount(invoice_amount * invoice["exchange_rate"])
                difference = amount(allocation_base - original_base)
                invoice_ledger = connection.execute(
                    """SELECT id FROM party_ledger_entries
                        WHERE company_id=%s AND voucher_type='Sales Invoice'
                          AND voucher_id=%s AND partner_role='Customer'""",
                    (company_id, invoice["id"]),
                ).fetchone()
                if not invoice_ledger:
                    raise AllocationError("Invoice has no party-ledger entry")
                connection.execute(
                    """INSERT INTO payment_allocations (
                           payment_id,invoice_id,amount,payment_amount,invoice_amount,
                           base_amount,exchange_gain_loss)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        payment_id, invoice["id"], invoice_amount, pay_amount,
                        invoice_amount, allocation_base, difference,
                    ),
                )
                connection.execute(
                    """INSERT INTO party_ledger_allocations (
                           company_id,source_entry_id,target_entry_id,allocation_date,
                           amount,base_amount,exchange_gain_loss)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        company_id, payment_ledger_id, invoice_ledger["id"],
                        payment_date, invoice_amount, allocation_base, difference,
                    ),
                )
                outstanding = amount(invoice["outstanding_amount"] - invoice_amount)
                paid = amount(invoice["total"] - outstanding)
                status = "Paid" if outstanding == ZERO else "Partly Paid"
                connection.execute(
                    """UPDATE invoices SET outstanding_amount=%s,paid=%s,status=%s,updated_at=now()
                        WHERE id=%s""",
                    (outstanding, paid, status, invoice["id"]),
                )
                original_base_total += original_base
                allocated_payment_base += allocation_base
                gain_loss += difference
            unallocated_base = amount(base_total - allocated_payment_base)
            ar_credit = amount(original_base_total + unallocated_base)
            posting_lines = [
                PostingLine(
                    bank["gl_account_id"], debit=base_total,
                    account_currency=currency, account_debit=payment_total,
                    transaction_currency=currency, transaction_debit=payment_total,
                ),
                PostingLine(
                    settings["receivable_account_id"], credit=ar_credit,
                    party_id=customer["partner_id"],
                ),
            ]
            if gain_loss > ZERO:
                if not settings["exchange_gain_account_id"]:
                    raise DomainError("Exchange gain account is not configured")
                posting_lines.append(
                    PostingLine(settings["exchange_gain_account_id"], credit=gain_loss)
                )
            elif gain_loss < ZERO:
                if not settings["exchange_loss_account_id"]:
                    raise DomainError("Exchange loss account is not configured")
                posting_lines.append(
                    PostingLine(settings["exchange_loss_account_id"], debit=-gain_loss)
                )
            self.accounting.post_voucher(
                company_id=company_id, voucher_type="Customer Payment",
                voucher_id=payment_id, voucher_code=code, posting_date=payment_date,
                lines=posting_lines, actor=actor, connection=connection,
            )
            audit(
                connection, company_id=company_id, entity_type="Customer Payment",
                entity_id=payment_id, event_type="Posted", actor=actor,
                next_state="Posted",
            )
            return payment_id

    def return_delivery(
        self,
        delivery_id: int,
        *,
        return_date: date,
        lines: list[ReturnLine],
        actor: str,
        code: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Sales return requires at least one line")
        with self.database.transaction() as connection:
            original = connection.execute(
                "SELECT * FROM sales_deliveries WHERE id=%s FOR UPDATE", (delivery_id,)
            ).fetchone()
            if not original or original["document_kind"] != "Delivery":
                raise DomainError("Original sales delivery not found")
            require_state(original, "Posted")
            code = code or next_code(
                connection, original["company_id"], "Sales Return", prefix="RET-"
            )
            return_id = connection.execute(
                """INSERT INTO sales_deliveries (
                       company_id,code,customer_id,document_kind,delivery_date,
                       document_state,status,return_against_id,posted_at,posted_by)
                   VALUES (%s,%s,%s,'Return',%s,'Posted','Return',%s,now(),%s)
                   RETURNING id""",
                (
                    original["company_id"], code, original["customer_id"],
                    return_date, delivery_id, actor,
                ),
            ).fetchone()["id"]
            inventory_lines = []
            order_ids = set()
            for line_number, request in enumerate(lines, 1):
                source = connection.execute(
                    """SELECT delivery_line.*,order_line.order_id,
                              ledger.outgoing_rate,ledger.valuation_rate
                        FROM sales_delivery_items delivery_line
                        JOIN sales_order_items order_line ON order_line.id=delivery_line.sales_order_item_id
                        LEFT JOIN inventory_event_lines event_line
                          ON event_line.source_line_type='Sales Delivery Item'
                         AND event_line.source_line_id=delivery_line.id
                        LEFT JOIN inventory_ledger_entries ledger ON ledger.event_line_id=event_line.id
                       WHERE delivery_line.id=%s AND delivery_line.delivery_id=%s
                       FOR UPDATE OF delivery_line,order_line""",
                    (request.delivery_line_id, delivery_id),
                ).fetchone()
                qty = number(request.quantity)
                if not source or qty <= ZERO or source["returned_qty"] + qty > source["qty"]:
                    raise DomainError("Sales return quantity exceeds delivered quantity")
                return_line_id = connection.execute(
                    """INSERT INTO sales_delivery_items (
                           delivery_id,line_number,sales_order_item_id,item_id,warehouse_id,
                           uom_id,qty,stock_qty,unit_cost)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        return_id, line_number, source["sales_order_item_id"],
                        source["item_id"], source["warehouse_id"], source["uom_id"],
                        qty, qty, source["outgoing_rate"] or source["valuation_rate"],
                    ),
                ).fetchone()["id"]
                connection.execute(
                    "UPDATE sales_delivery_items SET returned_qty=returned_qty+%s WHERE id=%s",
                    (qty, source["id"]),
                )
                connection.execute(
                    "UPDATE sales_order_items SET returned_qty=returned_qty+%s WHERE id=%s",
                    (qty, source["sales_order_item_id"]),
                )
                inventory_lines.append(
                    InventoryLine(
                        source["item_id"], source["warehouse_id"], qty,
                        source["outgoing_rate"] or source["valuation_rate"],
                        source_line_type="Sales Return Item", source_line_id=return_line_id,
                    )
                )
                order_ids.add(source["order_id"])
            event = self.inventory.post_event(
                company_id=original["company_id"], event_type="Sales Return",
                voucher_type="Sales Return", voucher_id=return_id, voucher_code=code,
                event_date=return_date, lines=inventory_lines, actor=actor,
                connection=connection,
            )
            returned_value = connection.execute(
                "SELECT sum(value_change) AS value FROM inventory_ledger_entries WHERE event_id=%s",
                (event,),
            ).fetchone()["value"]
            settings = self._account_settings(connection, original["company_id"])
            self.accounting.post_voucher(
                company_id=original["company_id"], voucher_type="Sales Return",
                voucher_id=return_id, voucher_code=code, posting_date=return_date,
                actor=actor, connection=connection,
                lines=[
                    PostingLine(settings["inventory_account_id"], debit=returned_value),
                    PostingLine(settings["cogs_account_id"], credit=returned_value),
                ],
            )
            for order_id in order_ids:
                self._refresh_order_status(connection, order_id)
            audit(
                connection, company_id=original["company_id"], entity_type="Sales Return",
                entity_id=return_id, event_type="Posted", actor=actor,
                next_state="Posted", details={"return_against_id": delivery_id},
            )
            return return_id

    def issue_credit_note(
        self,
        invoice_id: int,
        *,
        credit_date: date,
        lines: list[CreditLine],
        actor: str,
        code: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Credit note requires at least one line")
        with self.database.transaction() as connection:
            original = connection.execute(
                """SELECT invoice.*,customer.partner_id FROM invoices invoice
                    JOIN customers customer ON customer.id=invoice.customer_id
                    WHERE invoice.id=%s FOR UPDATE OF invoice""",
                (invoice_id,),
            ).fetchone()
            if not original or original["invoice_type"] != "Invoice":
                raise DomainError("Original sales invoice not found")
            require_state(original, "Posted")
            code = code or next_code(
                connection, original["company_id"], "Sales Credit Note", prefix="CN-"
            )
            prepared = []
            net_total = ZERO
            tax_total = ZERO
            for line_number, request in enumerate(lines, 1):
                source = connection.execute(
                    """SELECT line.*,tax.sales_account_id AS tax_account
                        FROM invoice_items line
                        LEFT JOIN tax_codes tax ON tax.id=line.tax_code_id
                        WHERE line.id=%s AND line.invoice_id=%s FOR UPDATE OF line""",
                    (request.invoice_line_id, invoice_id),
                ).fetchone()
                qty = number(request.quantity)
                if not source or qty <= ZERO or source["credited_qty"] + qty > source["qty"]:
                    raise DomainError("Credit quantity exceeds invoiced quantity")
                unit_net = amount(source["net_amount"] / source["qty"])
                unit_tax = amount(source["tax_amount"] / source["qty"])
                net = amount(unit_net * qty)
                tax = amount(unit_tax * qty)
                total = amount(net + tax)
                net_total += net
                tax_total += tax
                prepared.append((line_number, source, qty, net, tax, total))
            total = amount(net_total + tax_total)
            exchange = amount(original["exchange_rate"])
            base_net = amount(net_total * exchange)
            base_tax = amount(tax_total * exchange)
            base_total = amount(total * exchange)
            credit_id = connection.execute(
                """INSERT INTO invoices (
                       company_id,code,order_id,customer_id,invoice_date,due_date,
                       currency,exchange_rate,net_total,tax_total,total,paid,status,
                       document_state,posted_at,posted_by,base_net_total,base_tax_total,
                       base_total,outstanding_amount,invoice_type,return_against_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,'Unpaid','Posted',
                           now(),%s,%s,%s,%s,%s,'Credit Note',%s) RETURNING id""",
                (
                    original["company_id"], code, original["order_id"],
                    original["customer_id"], credit_date, credit_date,
                    original["currency"], exchange, net_total, tax_total, total,
                    actor, base_net, base_tax, base_total, total, invoice_id,
                ),
            ).fetchone()["id"]
            tax_postings: dict[int, Decimal] = {}
            order_lines = set()
            for line_number, source, qty, net, tax, line_total in prepared:
                connection.execute(
                    """INSERT INTO invoice_items (
                           invoice_id,line_number,item_id,warehouse_id,description,qty,rate,
                           net_amount,tax_amount,amount,tax_code_id,sales_order_item_id,
                           delivery_item_id,uom_id,stock_qty,base_net_amount,
                           base_tax_amount,base_amount)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        credit_id, line_number, source["item_id"], source["warehouse_id"],
                        source["description"], qty, source["rate"], net, tax, line_total,
                        source["tax_code_id"], source["sales_order_item_id"],
                        source["delivery_item_id"], source["uom_id"], qty,
                        amount(net * exchange), amount(tax * exchange),
                        amount(line_total * exchange),
                    ),
                )
                connection.execute(
                    "UPDATE invoice_items SET credited_qty=credited_qty+%s WHERE id=%s",
                    (qty, source["id"]),
                )
                if source["sales_order_item_id"]:
                    connection.execute(
                        """UPDATE sales_order_items
                              SET invoiced_qty=GREATEST(invoiced_qty-%s,0) WHERE id=%s""",
                        (qty, source["sales_order_item_id"]),
                    )
                    order_lines.add(source["sales_order_item_id"])
                if tax and source["tax_account"]:
                    tax_postings[source["tax_account"]] = tax_postings.get(source["tax_account"], ZERO) + amount(tax * exchange)
            settings = self._account_settings(connection, original["company_id"])
            postings = [
                PostingLine(settings["sales_account_id"], debit=base_net),
                PostingLine(
                    settings["receivable_account_id"], credit=base_total,
                    party_id=original["partner_id"],
                ),
            ]
            if base_tax:
                if tax_postings:
                    postings.extend(
                        PostingLine(account_id, debit=value)
                        for account_id, value in tax_postings.items()
                    )
                elif settings["sales_tax_account_id"]:
                    postings.append(
                        PostingLine(settings["sales_tax_account_id"], debit=base_tax)
                    )
                else:
                    raise DomainError("Sales tax account is not configured")
            self.accounting.post_voucher(
                company_id=original["company_id"], voucher_type="Sales Credit Note",
                voucher_id=credit_id, voucher_code=code, posting_date=credit_date,
                lines=postings, actor=actor, connection=connection,
            )
            credit_ledger = connection.execute(
                """INSERT INTO party_ledger_entries (
                       company_id,partner_id,partner_role,posting_date,due_date,
                       voucher_type,voucher_id,voucher_code,currency,credit,base_credit)
                   VALUES (%s,%s,'Customer',%s,%s,'Sales Credit Note',%s,%s,%s,%s,%s)
                   RETURNING id""",
                (
                    original["company_id"], original["partner_id"], credit_date,
                    credit_date, credit_id, code, original["currency"], total, base_total,
                ),
            ).fetchone()["id"]
            allocation = min(total, amount(original["outstanding_amount"]))
            if allocation > ZERO:
                original_ledger = connection.execute(
                    """SELECT id FROM party_ledger_entries
                        WHERE company_id=%s AND voucher_type='Sales Invoice'
                          AND voucher_id=%s AND partner_role='Customer'""",
                    (original["company_id"], invoice_id),
                ).fetchone()
                allocation_base = amount(allocation * exchange)
                connection.execute(
                    """INSERT INTO party_ledger_allocations (
                           company_id,source_entry_id,target_entry_id,allocation_date,
                           amount,base_amount,exchange_gain_loss)
                       VALUES (%s,%s,%s,%s,%s,%s,0)""",
                    (
                        original["company_id"], credit_ledger, original_ledger["id"],
                        credit_date, allocation, allocation_base,
                    ),
                )
                original_outstanding = amount(original["outstanding_amount"] - allocation)
                credit_outstanding = amount(total - allocation)
                connection.execute(
                    """UPDATE invoices SET outstanding_amount=%s,
                           paid=total-%s,status=%s,updated_at=now() WHERE id=%s""",
                    (
                        original_outstanding, original_outstanding,
                        "Paid" if original_outstanding == ZERO else "Partly Paid",
                        invoice_id,
                    ),
                )
                connection.execute(
                    """UPDATE invoices SET outstanding_amount=%s,paid=%s,status=%s,
                           updated_at=now() WHERE id=%s""",
                    (
                        credit_outstanding, allocation,
                        "Paid" if credit_outstanding == ZERO else "Partly Paid",
                        credit_id,
                    ),
                )
            if original["order_id"]:
                self._refresh_order_status(connection, original["order_id"])
            audit(
                connection, company_id=original["company_id"], entity_type="Sales Credit Note",
                entity_id=credit_id, event_type="Posted", actor=actor,
                next_state="Posted", details={"return_against_id": invoice_id},
            )
            return credit_id

    @staticmethod
    def _validate_item_warehouse(connection, company_id, item_id, warehouse_id):
        row = connection.execute(
            """SELECT item.id FROM items item JOIN warehouses warehouse ON warehouse.id=%s
                WHERE item.id=%s AND item.company_id=%s AND warehouse.company_id=%s
                  AND item.active=true AND warehouse.active=true""",
            (warehouse_id, item_id, company_id, company_id),
        ).fetchone()
        if not row:
            raise DomainError("Item or warehouse does not belong to company")

    @staticmethod
    def _account_settings(connection, company_id):
        settings = connection.execute(
            "SELECT * FROM company_accounting_settings WHERE company_id=%s",
            (company_id,),
        ).fetchone()
        if not settings:
            raise DomainError("Company accounting settings are not configured")
        return settings

    @staticmethod
    def _refresh_order_status(connection, order_id):
        rows = connection.execute(
            "SELECT qty,delivered_qty,returned_qty,invoiced_qty FROM sales_order_items WHERE order_id=%s",
            (order_id,),
        ).fetchall()
        net_delivered = sum((row["delivered_qty"] - row["returned_qty"] for row in rows), ZERO)
        ordered = sum((row["qty"] for row in rows), ZERO)
        invoiced = sum((row["invoiced_qty"] for row in rows), ZERO)
        if invoiced >= ordered and ordered > ZERO:
            status = "Invoiced"
        elif invoiced > ZERO:
            status = "Partly Invoiced"
        elif net_delivered >= ordered and ordered > ZERO:
            status = "Delivered"
        elif net_delivered > ZERO:
            status = "Partly Delivered"
        else:
            status = "Confirmed"
        connection.execute(
            "UPDATE sales_orders SET status=%s,updated_at=now() WHERE id=%s",
            (status, order_id),
        )
