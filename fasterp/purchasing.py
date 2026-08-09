"""Procure-to-pay documents using shared inventory and accounting kernels."""

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
class PurchaseOrderLine:
    item_id: int
    warehouse_id: int
    quantity: Decimal
    unit_price: Decimal
    description: str | None = None
    uom_id: int | None = None
    tax_code_id: int | None = None
    receipt_tolerance_percent: Decimal = ZERO
    billing_tolerance_percent: Decimal = ZERO


@dataclass(frozen=True)
class ReceiptLine:
    order_line_id: int
    accepted_quantity: Decimal
    rejected_quantity: Decimal = ZERO
    rejected_warehouse_id: int | None = None


@dataclass(frozen=True)
class PurchaseInvoiceLine:
    order_line_id: int
    quantity: Decimal
    receipt_line_id: int | None = None
    expense_account_id: int | None = None


@dataclass(frozen=True)
class SupplierPaymentAllocation:
    invoice_id: int
    payment_amount: Decimal
    invoice_amount: Decimal
    base_amount: Decimal


@dataclass(frozen=True)
class ReceiptReturnLine:
    receipt_line_id: int
    quantity: Decimal
    rejected: bool = False


@dataclass(frozen=True)
class DebitNoteLine:
    invoice_line_id: int
    quantity: Decimal


class PurchasingService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.accounting = AccountingService(database)
        self.inventory = InventoryService(database)

    def create_order(
        self,
        *,
        company_id: int,
        supplier_id: int,
        order_date: date,
        currency: str,
        exchange_rate: Decimal,
        lines: list[PurchaseOrderLine],
        actor: str,
        code: str | None = None,
        note: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Purchase order requires at least one line")
        exchange = amount(exchange_rate)
        if exchange <= ZERO:
            raise DomainError("Exchange rate must be positive")
        with self.database.transaction() as connection:
            supplier = connection.execute(
                "SELECT id FROM suppliers WHERE id=%s AND company_id=%s AND active=true",
                (supplier_id, company_id),
            ).fetchone()
            if not supplier:
                raise DomainError("Active supplier does not belong to company")
            code = code or next_code(
                connection, company_id, "Purchase Order", prefix="PO-"
            )
            total = ZERO
            normalized = []
            for line in lines:
                qty = number(line.quantity)
                rate = number(line.unit_price)
                if qty <= ZERO or rate < ZERO:
                    raise DomainError("Purchase quantity must be positive and rate non-negative")
                self._validate_item_warehouse(
                    connection, company_id, line.item_id, line.warehouse_id
                )
                line_total = amount(qty * rate)
                total += line_total
                normalized.append((line, qty, rate, line_total))
            order_id = connection.execute(
                """INSERT INTO purchase_orders (
                       company_id,code,supplier_id,order_date,status,currency,
                       exchange_rate,total,note,document_state)
                   VALUES (%s,%s,%s,%s,'Draft',%s,%s,%s,%s,'Draft') RETURNING id""",
                (
                    company_id, code, supplier_id, order_date, currency,
                    exchange, total, note,
                ),
            ).fetchone()["id"]
            for line_number, (line, qty, rate, line_total) in enumerate(normalized, 1):
                connection.execute(
                    """INSERT INTO purchase_order_items (
                           po_id,line_number,item_id,warehouse_id,description,qty,rate,
                           amount,tax_code_id,uom_id,stock_qty,receipt_tolerance_percent,
                           billing_tolerance_percent)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        order_id, line_number, line.item_id, line.warehouse_id,
                        line.description, qty, rate, line_total, line.tax_code_id,
                        line.uom_id, qty, amount(line.receipt_tolerance_percent),
                        amount(line.billing_tolerance_percent),
                    ),
                )
            audit(
                connection, company_id=company_id, entity_type="Purchase Order",
                entity_id=order_id, event_type="Created", actor=actor,
                next_state="Draft",
            )
            return order_id

    def post_order(self, order_id: int, *, actor: str) -> None:
        with self.database.transaction() as connection:
            order = connection.execute(
                "SELECT * FROM purchase_orders WHERE id=%s FOR UPDATE", (order_id,)
            ).fetchone()
            if not order:
                raise DomainError("Purchase order not found")
            require_state(order, "Draft", "Approved")
            if not connection.execute(
                "SELECT 1 FROM purchase_order_items WHERE po_id=%s LIMIT 1", (order_id,)
            ).fetchone():
                raise DomainError("Purchase order has no lines")
            connection.execute(
                """UPDATE purchase_orders
                      SET document_state='Posted',status='Ordered',posted_at=now(),
                          posted_by=%s,row_version=row_version+1,updated_at=now()
                    WHERE id=%s""",
                (actor, order_id),
            )
            audit(
                connection, company_id=order["company_id"], entity_type="Purchase Order",
                entity_id=order_id, event_type="Posted", actor=actor,
                previous_state=order["document_state"], next_state="Posted",
            )

    def receive(
        self,
        order_id: int,
        *,
        receipt_date: date,
        lines: list[ReceiptLine],
        actor: str,
        code: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Purchase receipt requires at least one line")
        with self.database.transaction() as connection:
            order = connection.execute(
                "SELECT * FROM purchase_orders WHERE id=%s FOR UPDATE", (order_id,)
            ).fetchone()
            if not order:
                raise DomainError("Purchase order not found")
            require_state(order, "Posted")
            if order["status"] in {"Closed", "Cancelled", "On Hold"}:
                raise DocumentStateError(f"Purchase order status {order['status']} cannot be received")
            code = code or next_code(
                connection, order["company_id"], "Purchase Receipt", prefix="PR-"
            )
            receipt_id = connection.execute(
                """INSERT INTO purchase_receipts (
                       company_id,code,supplier_id,document_kind,receipt_date,
                       document_state,status,posted_at,posted_by)
                   VALUES (%s,%s,%s,'Receipt',%s,'Posted','To Bill',now(),%s)
                   RETURNING id""",
                (
                    order["company_id"], code, order["supplier_id"],
                    receipt_date, actor,
                ),
            ).fetchone()["id"]
            inventory_lines = []
            for line_number, request in enumerate(lines, 1):
                source = connection.execute(
                    """SELECT line.*,item.inventory_item FROM purchase_order_items line
                        JOIN items item ON item.id=line.item_id
                        WHERE line.id=%s AND line.po_id=%s FOR UPDATE OF line""",
                    (request.order_line_id, order_id),
                ).fetchone()
                accepted = number(request.accepted_quantity)
                rejected = number(request.rejected_quantity)
                total_qty = accepted + rejected
                maximum = number(source["qty"] * (Decimal("1") + source["receipt_tolerance_percent"] / 100)) if source else ZERO
                if not source or total_qty <= ZERO or source["received_qty"] + total_qty - source["returned_qty"] > maximum:
                    raise DomainError("Receipt quantity exceeds purchase-order tolerance")
                if rejected > ZERO and not request.rejected_warehouse_id:
                    raise DomainError("Rejected quantity requires a rejected warehouse")
                if request.rejected_warehouse_id:
                    self._validate_item_warehouse(
                        connection, order["company_id"], source["item_id"],
                        request.rejected_warehouse_id,
                    )
                base_unit_cost = number(source["rate"] * order["exchange_rate"])
                receipt_line_id = connection.execute(
                    """INSERT INTO purchase_receipt_items (
                           receipt_id,line_number,purchase_order_item_id,item_id,
                           accepted_warehouse_id,rejected_warehouse_id,uom_id,
                           accepted_qty,rejected_qty,stock_qty,unit_cost)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        receipt_id, line_number, source["id"], source["item_id"],
                        source["warehouse_id"], request.rejected_warehouse_id,
                        source["uom_id"], accepted, rejected, total_qty, base_unit_cost,
                    ),
                ).fetchone()["id"]
                connection.execute(
                    "UPDATE purchase_order_items SET received_qty=received_qty+%s WHERE id=%s",
                    (total_qty, source["id"]),
                )
                if source["inventory_item"]:
                    if accepted > ZERO:
                        inventory_lines.append(
                            InventoryLine(
                                source["item_id"], source["warehouse_id"], accepted,
                                base_unit_cost, source_line_type="Purchase Receipt Item",
                                source_line_id=receipt_line_id,
                            )
                        )
                    if rejected > ZERO:
                        inventory_lines.append(
                            InventoryLine(
                                source["item_id"], request.rejected_warehouse_id,
                                rejected, base_unit_cost,
                                source_line_type="Purchase Receipt Rejected Item",
                                source_line_id=receipt_line_id,
                            )
                        )
            if inventory_lines:
                event = self.inventory.post_event(
                    company_id=order["company_id"], event_type="Receipt",
                    voucher_type="Purchase Receipt", voucher_id=receipt_id,
                    voucher_code=code, event_date=receipt_date,
                    lines=inventory_lines, actor=actor, connection=connection,
                )
                received_value = connection.execute(
                    "SELECT sum(value_change) AS value FROM inventory_ledger_entries WHERE event_id=%s",
                    (event,),
                ).fetchone()["value"]
                settings = self._account_settings(connection, order["company_id"])
                if not settings["goods_received_not_invoiced_account_id"]:
                    raise DomainError("Goods-received-not-invoiced account is not configured")
                self.accounting.post_voucher(
                    company_id=order["company_id"], voucher_type="Purchase Receipt",
                    voucher_id=receipt_id, voucher_code=code, posting_date=receipt_date,
                    actor=actor, connection=connection,
                    lines=[
                        PostingLine(settings["inventory_account_id"], debit=received_value),
                        PostingLine(settings["goods_received_not_invoiced_account_id"], credit=received_value),
                    ],
                )
            self._refresh_order_status(connection, order_id)
            audit(
                connection, company_id=order["company_id"], entity_type="Purchase Receipt",
                entity_id=receipt_id, event_type="Posted", actor=actor,
                next_state="Posted", details={"purchase_order_id": order_id},
            )
            return receipt_id

    def invoice(
        self,
        order_id: int,
        *,
        invoice_date: date,
        lines: list[PurchaseInvoiceLine],
        actor: str,
        due_date: date | None = None,
        code: str | None = None,
        supplier_reference: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Purchase invoice requires at least one line")
        with self.database.transaction() as connection:
            order = connection.execute(
                "SELECT * FROM purchase_orders WHERE id=%s FOR UPDATE", (order_id,)
            ).fetchone()
            if not order:
                raise DomainError("Purchase order not found")
            require_state(order, "Posted")
            supplier = connection.execute(
                "SELECT * FROM suppliers WHERE id=%s", (order["supplier_id"],)
            ).fetchone()
            if not supplier or not supplier["partner_id"]:
                raise DomainError("Supplier is not linked to a business partner")
            code = code or next_code(
                connection, order["company_id"], "Purchase Invoice", prefix="PINV-"
            )
            prepared = []
            net_total = ZERO
            tax_total = ZERO
            for line_number, request in enumerate(lines, 1):
                source = connection.execute(
                    """SELECT line.*,item.inventory_item,tax.rate AS tax_rate,
                              tax.purchase_account_id AS tax_account
                        FROM purchase_order_items line
                        JOIN items item ON item.id=line.item_id
                        LEFT JOIN tax_codes tax ON tax.id=line.tax_code_id
                        WHERE line.id=%s AND line.po_id=%s FOR UPDATE OF line""",
                    (request.order_line_id, order_id),
                ).fetchone()
                qty = number(request.quantity)
                maximum = number(source["qty"] * (Decimal("1") + source["billing_tolerance_percent"] / 100)) if source else ZERO
                if not source or qty <= ZERO or source["invoiced_qty"] + qty > maximum:
                    raise DomainError("Purchase invoice quantity exceeds order tolerance")
                receipt = None
                if request.receipt_line_id:
                    receipt = connection.execute(
                        """SELECT line.* FROM purchase_receipt_items line
                            JOIN purchase_receipts receipt ON receipt.id=line.receipt_id
                            WHERE line.id=%s AND line.purchase_order_item_id=%s
                              AND receipt.document_state='Posted' FOR UPDATE OF line""",
                        (request.receipt_line_id, source["id"]),
                    ).fetchone()
                    if not receipt or receipt["billed_qty"] + qty > receipt["accepted_qty"] + receipt["rejected_qty"]:
                        raise DomainError("Purchase invoice exceeds received unbilled quantity")
                net = amount(qty * source["rate"])
                tax = amount(net * amount(source["tax_rate"] or 0) / 100)
                total = amount(net + tax)
                net_total += net
                tax_total += tax
                prepared.append((line_number, request, source, receipt, qty, net, tax, total))
            exchange = amount(order["exchange_rate"])
            total = amount(net_total + tax_total)
            base_net = amount(net_total * exchange)
            base_tax = amount(tax_total * exchange)
            base_total = amount(total * exchange)
            due = due_date or invoice_date + timedelta(days=30)
            invoice_id = connection.execute(
                """INSERT INTO purchase_invoices (
                       company_id,code,supplier_id,supplier_reference,invoice_date,due_date,
                       currency,exchange_rate,net_total,tax_total,total,base_net_total,
                       base_tax_total,base_total,outstanding_amount,document_state,status,
                       posted_at,posted_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           'Posted','Unpaid',now(),%s) RETURNING id""",
                (
                    order["company_id"], code, order["supplier_id"], supplier_reference,
                    invoice_date, due, order["currency"], exchange, net_total,
                    tax_total, total, base_net, base_tax, base_total, total, actor,
                ),
            ).fetchone()["id"]
            settings = self._account_settings(connection, order["company_id"])
            debit_postings: dict[int, Decimal] = {}
            tax_postings: dict[int, Decimal] = {}
            for line_number, request, source, receipt, qty, net, tax, line_total in prepared:
                base_line_net = amount(net * exchange)
                base_line_tax = amount(tax * exchange)
                expense_account = request.expense_account_id or settings["purchase_account_id"]
                connection.execute(
                    """INSERT INTO purchase_invoice_items (
                           purchase_invoice_id,line_number,purchase_order_item_id,
                           receipt_item_id,item_id,warehouse_id,expense_account_id,
                           description,uom_id,qty,stock_qty,rate,net_amount,tax_amount,
                           amount,base_net_amount,base_tax_amount,base_amount,tax_code_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        invoice_id, line_number, source["id"], request.receipt_line_id,
                        source["item_id"], source["warehouse_id"], expense_account,
                        source["description"], source["uom_id"], qty, qty,
                        source["rate"], net, tax, line_total, base_line_net,
                        base_line_tax, amount(line_total * exchange), source["tax_code_id"],
                    ),
                )
                connection.execute(
                    "UPDATE purchase_order_items SET invoiced_qty=invoiced_qty+%s WHERE id=%s",
                    (qty, source["id"]),
                )
                if receipt:
                    connection.execute(
                        "UPDATE purchase_receipt_items SET billed_qty=billed_qty+%s WHERE id=%s",
                        (qty, receipt["id"]),
                    )
                if source["inventory_item"] and receipt:
                    accrual = amount(qty * receipt["unit_cost"])
                    account = settings["goods_received_not_invoiced_account_id"]
                    if not account:
                        raise DomainError("Goods-received-not-invoiced account is not configured")
                    debit_postings[account] = debit_postings.get(account, ZERO) + accrual
                    variance = amount(base_line_net - accrual)
                    if variance:
                        if not expense_account:
                            raise DomainError("Purchase variance account is not configured")
                        debit_postings[expense_account] = debit_postings.get(expense_account, ZERO) + variance
                else:
                    if not expense_account:
                        raise DomainError("Purchase expense account is not configured")
                    debit_postings[expense_account] = debit_postings.get(expense_account, ZERO) + base_line_net
                if tax:
                    tax_account = source["tax_account"] or settings["purchase_tax_account_id"]
                    if not tax_account:
                        raise DomainError("Purchase tax account is not configured")
                    tax_postings[tax_account] = tax_postings.get(tax_account, ZERO) + base_line_tax
            posting_lines = [
                PostingLine(account_id, debit=value) if value > ZERO
                else PostingLine(account_id, credit=-value)
                for account_id, value in debit_postings.items() if value
            ]
            posting_lines.extend(
                PostingLine(account_id, debit=value)
                for account_id, value in tax_postings.items()
            )
            posting_lines.append(
                PostingLine(
                    settings["payable_account_id"], credit=base_total,
                    party_id=supplier["partner_id"], due_date=due,
                    transaction_currency=order["currency"], transaction_credit=total,
                )
            )
            self.accounting.post_voucher(
                company_id=order["company_id"], voucher_type="Purchase Invoice",
                voucher_id=invoice_id, voucher_code=code, posting_date=invoice_date,
                lines=posting_lines, actor=actor, connection=connection,
            )
            connection.execute(
                """INSERT INTO party_ledger_entries (
                       company_id,partner_id,partner_role,posting_date,due_date,
                       voucher_type,voucher_id,voucher_code,currency,credit,base_credit)
                   VALUES (%s,%s,'Supplier',%s,%s,'Purchase Invoice',%s,%s,%s,%s,%s)""",
                (
                    order["company_id"], supplier["partner_id"], invoice_date,
                    due, invoice_id, code, order["currency"], total, base_total,
                ),
            )
            self._refresh_order_status(connection, order_id)
            audit(
                connection, company_id=order["company_id"], entity_type="Purchase Invoice",
                entity_id=invoice_id, event_type="Posted", actor=actor,
                next_state="Posted", details={"purchase_order_id": order_id},
            )
            return invoice_id

    def pay_supplier(
        self,
        *,
        company_id: int,
        supplier_id: int,
        payment_date: date,
        currency: str,
        exchange_rate: Decimal,
        payment_amount: Decimal,
        allocations: list[SupplierPaymentAllocation],
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
            raise AllocationError("Supplier allocations exceed payment amount")
        with self.database.transaction() as connection:
            supplier = connection.execute(
                "SELECT * FROM suppliers WHERE id=%s AND company_id=%s AND active=true",
                (supplier_id, company_id),
            ).fetchone()
            if not supplier or not supplier["partner_id"]:
                raise DomainError("Supplier is not linked to a business partner")
            settings = self._account_settings(connection, company_id)
            bank_id = bank_account_id or settings["default_bank_account_id"]
            bank = connection.execute(
                "SELECT * FROM bank_accounts WHERE id=%s AND company_id=%s AND active=true",
                (bank_id, company_id),
            ).fetchone() if bank_id else None
            if not bank:
                raise DomainError("Active bank account is not configured")
            code = code or next_code(
                connection, company_id, "Supplier Payment", prefix="SPAY-"
            )
            base_total = amount(payment_total * exchange)
            payment_id = connection.execute(
                """INSERT INTO payments (
                       company_id,code,supplier_id,payment_date,currency,exchange_rate,
                       amount,status,document_state,payment_type,bank_account_id,
                       base_amount,unallocated_amount,reference_number,posted_at,posted_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'Posted','Posted','Supplier Payment',
                           %s,%s,%s,%s,now(),%s) RETURNING id""",
                (
                    company_id, code, supplier_id, payment_date, currency, exchange,
                    payment_total, bank_id, base_total,
                    payment_total - sum((amount(row.payment_amount) for row in allocations), ZERO),
                    reference_number, actor,
                ),
            ).fetchone()["id"]
            payment_ledger = connection.execute(
                """INSERT INTO party_ledger_entries (
                       company_id,partner_id,partner_role,posting_date,voucher_type,
                       voucher_id,voucher_code,currency,debit,base_debit,is_advance)
                   VALUES (%s,%s,'Supplier',%s,'Supplier Payment',%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (
                    company_id, supplier["partner_id"], payment_date, payment_id,
                    code, currency, payment_total, base_total, not bool(allocations),
                ),
            ).fetchone()["id"]
            original_base_total = ZERO
            allocated_payment_base = ZERO
            gain_loss = ZERO
            for allocation in allocations:
                invoice = connection.execute(
                    """SELECT invoice.*,supplier.partner_id FROM purchase_invoices invoice
                        JOIN suppliers supplier ON supplier.id=invoice.supplier_id
                        WHERE invoice.id=%s AND invoice.company_id=%s
                          AND invoice.supplier_id=%s AND invoice.document_state='Posted'
                        FOR UPDATE OF invoice""",
                    (allocation.invoice_id, company_id, supplier_id),
                ).fetchone()
                invoice_amount = amount(allocation.invoice_amount)
                pay_amount = amount(allocation.payment_amount)
                allocation_base = amount(allocation.base_amount)
                if not invoice or invoice_amount <= ZERO or pay_amount <= ZERO or allocation_base <= ZERO:
                    raise AllocationError("Supplier allocation is invalid")
                if invoice_amount > amount(invoice["outstanding_amount"]):
                    raise AllocationError("Allocation exceeds supplier invoice outstanding")
                original_base = amount(invoice_amount * invoice["exchange_rate"])
                difference = amount(allocation_base - original_base)
                invoice_ledger = connection.execute(
                    """SELECT id FROM party_ledger_entries
                        WHERE company_id=%s AND voucher_type='Purchase Invoice'
                          AND voucher_id=%s AND partner_role='Supplier'""",
                    (company_id, invoice["id"]),
                ).fetchone()
                connection.execute(
                    """INSERT INTO supplier_payment_allocations (
                           payment_id,purchase_invoice_id,amount,payment_amount,
                           invoice_amount,base_amount,exchange_gain_loss)
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
                        company_id, payment_ledger, invoice_ledger["id"], payment_date,
                        invoice_amount, allocation_base, difference,
                    ),
                )
                outstanding = amount(invoice["outstanding_amount"] - invoice_amount)
                status = "Paid" if outstanding == ZERO else "Partly Paid"
                connection.execute(
                    "UPDATE purchase_invoices SET outstanding_amount=%s,status=%s,updated_at=now() WHERE id=%s",
                    (outstanding, status, invoice["id"]),
                )
                original_base_total += original_base
                allocated_payment_base += allocation_base
                gain_loss += difference
            unallocated_base = amount(base_total - allocated_payment_base)
            ap_debit = amount(original_base_total + unallocated_base)
            postings = [
                PostingLine(settings["payable_account_id"], debit=ap_debit, party_id=supplier["partner_id"]),
                PostingLine(
                    bank["gl_account_id"], credit=base_total,
                    account_currency=currency, account_credit=payment_total,
                    transaction_currency=currency, transaction_credit=payment_total,
                ),
            ]
            if gain_loss > ZERO:
                if not settings["exchange_loss_account_id"]:
                    raise DomainError("Exchange loss account is not configured")
                postings.append(PostingLine(settings["exchange_loss_account_id"], debit=gain_loss))
            elif gain_loss < ZERO:
                if not settings["exchange_gain_account_id"]:
                    raise DomainError("Exchange gain account is not configured")
                postings.append(PostingLine(settings["exchange_gain_account_id"], credit=-gain_loss))
            self.accounting.post_voucher(
                company_id=company_id, voucher_type="Supplier Payment",
                voucher_id=payment_id, voucher_code=code, posting_date=payment_date,
                lines=postings, actor=actor, connection=connection,
            )
            audit(
                connection, company_id=company_id, entity_type="Supplier Payment",
                entity_id=payment_id, event_type="Posted", actor=actor,
                next_state="Posted",
            )
            return payment_id

    def return_receipt(
        self,
        receipt_id: int,
        *,
        return_date: date,
        lines: list[ReceiptReturnLine],
        actor: str,
        code: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Purchase return requires at least one line")
        with self.database.transaction() as connection:
            original = connection.execute(
                "SELECT * FROM purchase_receipts WHERE id=%s FOR UPDATE", (receipt_id,)
            ).fetchone()
            if not original or original["document_kind"] != "Receipt":
                raise DomainError("Original purchase receipt not found")
            require_state(original, "Posted")
            code = code or next_code(
                connection, original["company_id"], "Purchase Return", prefix="PRET-"
            )
            return_id = connection.execute(
                """INSERT INTO purchase_receipts (
                       company_id,code,supplier_id,document_kind,receipt_date,
                       document_state,status,return_against_id,posted_at,posted_by)
                   VALUES (%s,%s,%s,'Return',%s,'Posted','Return',%s,now(),%s)
                   RETURNING id""",
                (
                    original["company_id"], code, original["supplier_id"],
                    return_date, receipt_id, actor,
                ),
            ).fetchone()["id"]
            inventory_lines = []
            order_ids = set()
            for line_number, request in enumerate(lines, 1):
                source = connection.execute(
                    """SELECT receipt_line.*,order_line.po_id
                        FROM purchase_receipt_items receipt_line
                        JOIN purchase_order_items order_line
                          ON order_line.id=receipt_line.purchase_order_item_id
                       WHERE receipt_line.id=%s AND receipt_line.receipt_id=%s
                       FOR UPDATE OF receipt_line,order_line""",
                    (request.receipt_line_id, receipt_id),
                ).fetchone()
                qty = number(request.quantity)
                available = (
                    source["rejected_qty"] - source["rejected_returned_qty"]
                    if source and request.rejected
                    else source["accepted_qty"] - source["accepted_returned_qty"] if source else ZERO
                )
                if not source or qty <= ZERO or qty > available:
                    raise DomainError("Purchase return exceeds received quantity")
                warehouse_id = (
                    source["rejected_warehouse_id"]
                    if request.rejected else source["accepted_warehouse_id"]
                )
                return_line_id = connection.execute(
                    """INSERT INTO purchase_receipt_items (
                           receipt_id,line_number,purchase_order_item_id,item_id,
                           accepted_warehouse_id,rejected_warehouse_id,uom_id,
                           accepted_qty,rejected_qty,stock_qty,unit_cost)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        return_id, line_number, source["purchase_order_item_id"],
                        source["item_id"], None if request.rejected else warehouse_id,
                        warehouse_id if request.rejected else None, source["uom_id"],
                        ZERO if request.rejected else qty,
                        qty if request.rejected else ZERO, qty, source["unit_cost"],
                    ),
                ).fetchone()["id"]
                if request.rejected:
                    connection.execute(
                        """UPDATE purchase_receipt_items
                              SET rejected_returned_qty=rejected_returned_qty+%s,
                                  returned_qty=returned_qty+%s WHERE id=%s""",
                        (qty, qty, source["id"]),
                    )
                else:
                    connection.execute(
                        """UPDATE purchase_receipt_items
                              SET accepted_returned_qty=accepted_returned_qty+%s,
                                  returned_qty=returned_qty+%s WHERE id=%s""",
                        (qty, qty, source["id"]),
                    )
                connection.execute(
                    "UPDATE purchase_order_items SET returned_qty=returned_qty+%s WHERE id=%s",
                    (qty, source["purchase_order_item_id"]),
                )
                inventory_lines.append(
                    InventoryLine(
                        source["item_id"], warehouse_id, -qty,
                        forced_unit_cost=source["unit_cost"],
                        source_line_type="Purchase Return Item",
                        source_line_id=return_line_id,
                    )
                )
                order_ids.add(source["po_id"])
            event = self.inventory.post_event(
                company_id=original["company_id"], event_type="Purchase Return",
                voucher_type="Purchase Return", voucher_id=return_id,
                voucher_code=code, event_date=return_date, lines=inventory_lines,
                actor=actor, connection=connection,
            )
            returned_value = -connection.execute(
                "SELECT sum(value_change) AS value FROM inventory_ledger_entries WHERE event_id=%s",
                (event,),
            ).fetchone()["value"]
            settings = self._account_settings(connection, original["company_id"])
            if not settings["goods_received_not_invoiced_account_id"]:
                raise DomainError("Goods-received-not-invoiced account is not configured")
            self.accounting.post_voucher(
                company_id=original["company_id"], voucher_type="Purchase Return",
                voucher_id=return_id, voucher_code=code, posting_date=return_date,
                actor=actor, connection=connection,
                lines=[
                    PostingLine(settings["goods_received_not_invoiced_account_id"], debit=returned_value),
                    PostingLine(settings["inventory_account_id"], credit=returned_value),
                ],
            )
            for order_id in order_ids:
                self._refresh_order_status(connection, order_id)
            audit(
                connection, company_id=original["company_id"], entity_type="Purchase Return",
                entity_id=return_id, event_type="Posted", actor=actor,
                next_state="Posted", details={"return_against_id": receipt_id},
            )
            return return_id

    def issue_debit_note(
        self,
        invoice_id: int,
        *,
        debit_date: date,
        lines: list[DebitNoteLine],
        actor: str,
        code: str | None = None,
    ) -> int:
        if not lines:
            raise DomainError("Debit note requires at least one line")
        with self.database.transaction() as connection:
            original = connection.execute(
                """SELECT invoice.*,supplier.partner_id FROM purchase_invoices invoice
                    JOIN suppliers supplier ON supplier.id=invoice.supplier_id
                    WHERE invoice.id=%s FOR UPDATE OF invoice""",
                (invoice_id,),
            ).fetchone()
            if not original or original["invoice_type"] != "Invoice":
                raise DomainError("Original purchase invoice not found")
            require_state(original, "Posted")
            code = code or next_code(
                connection, original["company_id"], "Purchase Debit Note", prefix="DN-"
            )
            prepared = []
            net_total = ZERO
            tax_total = ZERO
            for line_number, request in enumerate(lines, 1):
                source = connection.execute(
                    """SELECT line.*,item.inventory_item,
                              tax.purchase_account_id AS tax_account,
                              receipt.unit_cost AS receipt_unit_cost
                        FROM purchase_invoice_items line
                        LEFT JOIN items item ON item.id=line.item_id
                        LEFT JOIN tax_codes tax ON tax.id=line.tax_code_id
                        LEFT JOIN purchase_receipt_items receipt ON receipt.id=line.receipt_item_id
                        WHERE line.id=%s AND line.purchase_invoice_id=%s
                        FOR UPDATE OF line""",
                    (request.invoice_line_id, invoice_id),
                ).fetchone()
                qty = number(request.quantity)
                if not source or qty <= ZERO or source["debited_qty"] + qty > source["qty"]:
                    raise DomainError("Debit-note quantity exceeds invoiced quantity")
                net = amount(source["net_amount"] / source["qty"] * qty)
                tax = amount(source["tax_amount"] / source["qty"] * qty)
                line_total = amount(net + tax)
                net_total += net
                tax_total += tax
                prepared.append((line_number, source, qty, net, tax, line_total))
            exchange = amount(original["exchange_rate"])
            total = amount(net_total + tax_total)
            base_net = amount(net_total * exchange)
            base_tax = amount(tax_total * exchange)
            base_total = amount(total * exchange)
            debit_id = connection.execute(
                """INSERT INTO purchase_invoices (
                       company_id,code,supplier_id,invoice_type,invoice_date,due_date,
                       currency,exchange_rate,net_total,tax_total,total,base_net_total,
                       base_tax_total,base_total,outstanding_amount,return_against_id,
                       document_state,status,posted_at,posted_by)
                   VALUES (%s,%s,%s,'Debit Note',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,'Posted','Unpaid',now(),%s) RETURNING id""",
                (
                    original["company_id"], code, original["supplier_id"], debit_date,
                    debit_date, original["currency"], exchange, net_total, tax_total,
                    total, base_net, base_tax, base_total, total, invoice_id, actor,
                ),
            ).fetchone()["id"]
            settings = self._account_settings(connection, original["company_id"])
            credit_postings: dict[int, Decimal] = {}
            tax_postings: dict[int, Decimal] = {}
            for line_number, source, qty, net, tax, line_total in prepared:
                connection.execute(
                    """INSERT INTO purchase_invoice_items (
                           purchase_invoice_id,line_number,purchase_order_item_id,
                           receipt_item_id,item_id,warehouse_id,expense_account_id,
                           description,uom_id,qty,stock_qty,rate,net_amount,tax_amount,
                           amount,base_net_amount,base_tax_amount,base_amount,tax_code_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        debit_id, line_number, source["purchase_order_item_id"],
                        source["receipt_item_id"], source["item_id"],
                        source["warehouse_id"], source["expense_account_id"],
                        source["description"], source["uom_id"], qty, qty,
                        source["rate"], net, tax, line_total, amount(net * exchange),
                        amount(tax * exchange), amount(line_total * exchange),
                        source["tax_code_id"],
                    ),
                )
                connection.execute(
                    "UPDATE purchase_invoice_items SET debited_qty=debited_qty+%s WHERE id=%s",
                    (qty, source["id"]),
                )
                if source["purchase_order_item_id"]:
                    connection.execute(
                        """UPDATE purchase_order_items
                              SET invoiced_qty=GREATEST(invoiced_qty-%s,0) WHERE id=%s""",
                        (qty, source["purchase_order_item_id"]),
                    )
                base_line_net = amount(net * exchange)
                if source["inventory_item"] and source["receipt_item_id"]:
                    accrual = amount(qty * source["receipt_unit_cost"])
                    account = settings["goods_received_not_invoiced_account_id"]
                    credit_postings[account] = credit_postings.get(account, ZERO) + accrual
                    variance = amount(base_line_net - accrual)
                    if variance:
                        account = source["expense_account_id"] or settings["purchase_account_id"]
                        credit_postings[account] = credit_postings.get(account, ZERO) + variance
                else:
                    account = source["expense_account_id"] or settings["purchase_account_id"]
                    credit_postings[account] = credit_postings.get(account, ZERO) + base_line_net
                if tax:
                    account = source["tax_account"] or settings["purchase_tax_account_id"]
                    tax_postings[account] = tax_postings.get(account, ZERO) + amount(tax * exchange)
            postings = [
                PostingLine(settings["payable_account_id"], debit=base_total, party_id=original["partner_id"])
            ]
            postings.extend(
                PostingLine(account, credit=value) if value > ZERO
                else PostingLine(account, debit=-value)
                for account, value in credit_postings.items() if value
            )
            postings.extend(PostingLine(account, credit=value) for account, value in tax_postings.items())
            self.accounting.post_voucher(
                company_id=original["company_id"], voucher_type="Purchase Debit Note",
                voucher_id=debit_id, voucher_code=code, posting_date=debit_date,
                lines=postings, actor=actor, connection=connection,
            )
            debit_ledger = connection.execute(
                """INSERT INTO party_ledger_entries (
                       company_id,partner_id,partner_role,posting_date,due_date,
                       voucher_type,voucher_id,voucher_code,currency,debit,base_debit)
                   VALUES (%s,%s,'Supplier',%s,%s,'Purchase Debit Note',%s,%s,%s,%s,%s)
                   RETURNING id""",
                (
                    original["company_id"], original["partner_id"], debit_date,
                    debit_date, debit_id, code, original["currency"], total, base_total,
                ),
            ).fetchone()["id"]
            allocation = min(total, amount(original["outstanding_amount"]))
            if allocation > ZERO:
                original_ledger = connection.execute(
                    """SELECT id FROM party_ledger_entries
                        WHERE company_id=%s AND voucher_type='Purchase Invoice'
                          AND voucher_id=%s AND partner_role='Supplier'""",
                    (original["company_id"], invoice_id),
                ).fetchone()
                allocation_base = amount(allocation * exchange)
                connection.execute(
                    """INSERT INTO party_ledger_allocations (
                           company_id,source_entry_id,target_entry_id,allocation_date,
                           amount,base_amount,exchange_gain_loss)
                       VALUES (%s,%s,%s,%s,%s,%s,0)""",
                    (
                        original["company_id"], debit_ledger, original_ledger["id"],
                        debit_date, allocation, allocation_base,
                    ),
                )
                original_outstanding = amount(original["outstanding_amount"] - allocation)
                debit_outstanding = amount(total - allocation)
                connection.execute(
                    "UPDATE purchase_invoices SET outstanding_amount=%s,status=%s,updated_at=now() WHERE id=%s",
                    (
                        original_outstanding,
                        "Paid" if original_outstanding == ZERO else "Partly Paid",
                        invoice_id,
                    ),
                )
                connection.execute(
                    "UPDATE purchase_invoices SET outstanding_amount=%s,status=%s,updated_at=now() WHERE id=%s",
                    (
                        debit_outstanding,
                        "Paid" if debit_outstanding == ZERO else "Partly Paid",
                        debit_id,
                    ),
                )
            order_ids = {
                row["po_id"] for row in connection.execute(
                    """SELECT DISTINCT po_id FROM purchase_order_items
                        WHERE id = ANY(%s)""",
                    ([source["purchase_order_item_id"] for _, source, *_ in prepared if source["purchase_order_item_id"]],),
                ).fetchall()
            } if any(source["purchase_order_item_id"] for _, source, *_ in prepared) else set()
            for order_id in order_ids:
                self._refresh_order_status(connection, order_id)
            audit(
                connection, company_id=original["company_id"], entity_type="Purchase Debit Note",
                entity_id=debit_id, event_type="Posted", actor=actor,
                next_state="Posted", details={"return_against_id": invoice_id},
            )
            return debit_id

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
            "SELECT qty,received_qty,returned_qty,invoiced_qty FROM purchase_order_items WHERE po_id=%s",
            (order_id,),
        ).fetchall()
        ordered = sum((row["qty"] for row in rows), ZERO)
        received = sum((row["received_qty"] - row["returned_qty"] for row in rows), ZERO)
        invoiced = sum((row["invoiced_qty"] for row in rows), ZERO)
        if invoiced >= ordered and ordered > ZERO:
            status = "Invoiced"
        elif invoiced > ZERO:
            status = "Partly Invoiced"
        elif received >= ordered and ordered > ZERO:
            status = "Received"
        elif received > ZERO:
            status = "Partly Received"
        else:
            status = "Ordered"
        connection.execute(
            "UPDATE purchase_orders SET status=%s,updated_at=now() WHERE id=%s",
            (status, order_id),
        )
