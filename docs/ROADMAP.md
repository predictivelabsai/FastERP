# FastERP Roadmap — ERPNext feature comparison

[ERPNext](https://github.com/frappe/erpnext) is ~527 doctypes spanning the whole
enterprise. FastERP models **one coherent vertical — Order-to-Cash + the stock
that backs it** — and maps the rest here.

## Implemented ✅ (Selling + Stock)

| Capability | Upstream doctype(s) | FastERP |
|---|---|---|
| Customers | `Customer` | `customers` (territory, credit limit) |
| Items | `Item` | `items` (group, UOM, rate, stock, reorder) |
| Sales orders | `Sales Order` / `Sales Order Item` | `sales_orders` + `sales_order_items` |
| Sales invoices | `Sales Invoice` | `invoices` (total, paid, status) |
| **AR aging** | `Payment Entry` / GL | invoice status Unpaid/Partly Paid/Paid/Overdue |
| Stock movements | `Stock Ledger Entry` | `stock_moves` (In/Out) |
| Reorder signal | `Item.reorder_level` | low-stock flag + dashboard list |
| **AI assistant** | *(not upstream)* | grounded ops/finance Q&A |

## Near-term roadmap 🔜 (complete the O2C loop)

1. ✅ **Write the flow** (done) — confirm →
   **Delivery Note** (decrements stock) → **Sales Invoice** → **Payment Entry**
   (updates AR), each as an HTMX action.
2. **Quotations** — `Quotation` → convert to Sales Order.
3. **Buying side** — `Supplier`, `Purchase Order`, `Purchase Receipt`
   (the inbound half that replenishes stock; `stock_moves` already has 'In').
4. **Pricing** — `Price List`, `Pricing Rule` (per-customer / volume pricing).
5. **Multi-warehouse** — `Warehouse`, per-bin stock (currently one global stock qty).
6. **Basic GL** — post invoices/payments to a simple `GL Entry` ledger and show a
   P&L / receivables-vs-payables summary.

## Later / out-of-scope 🗓️

The rest of ERPNext, deferred for a focused demonstrator:

- **Accounting core** — full Chart of Accounts, Journal Entries, Cost Centers,
  Accounting Dimensions, financial statements, tax templates.
- **Manufacturing** — BOM, Work Order, Job Card, capacity planning.
- **Assets** — `Asset`, depreciation schedules, maintenance.
- **Projects / Timesheets**, **Payroll** (see FastHRM), **CRM** (see FastCRM),
  **Support** (see FastHelpdesk) — ERPNext bundles these; the migrations split
  them into focused apps.
- **Subcontracting, Quality, Maintenance, Loans**, multi-company consolidation.

## Design notes

FastERP keeps a **single coherent flow** legible rather than thinning ERPNext's
breadth into something shallow. The headline next step is making the flow
**transactional** (deliver → invoice → pay, with stock and AR updating) — the
demonstrator currently presents a consistent end-state read-only. Note the
`fasthtml-oss-migrations` initiative deliberately splits ERPNext's bundled CRM /
HR / Helpdesk into **FastCRM / FastHRM / FastHelpdesk** rather than reproducing
the monolith.
