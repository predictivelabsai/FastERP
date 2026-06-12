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
| **Suppliers** | `Supplier` | `suppliers` (territory, spend, PO count) |
| **Purchase orders** | `Purchase Order` / `Purchase Order Item` | `purchase_orders` + items, multi-line create form |
| **Goods receipt** | `Purchase Receipt` | "Receive" action → stock In + GL posting |
| **General ledger** | `GL Entry` | `gl_entries` double-entry + trial balance |
| **AI assistant** | *(not upstream)* | grounded ops/finance Q&A |

## Near-term roadmap 🔜 (complete the O2C loop)

1. ✅ **Write the flow** (done) — confirm →
   **Delivery Note** (decrements stock) → **Sales Invoice** → **Payment Entry**
   (updates AR), each as an HTMX action.
2. ✅ **Buying side** (done) — `Supplier`, `Purchase Order` (multi-line create),
   `Purchase Receipt` ("Receive" → stock In). The inbound half that replenishes stock.
3. ✅ **Basic GL** (done) — a 6-account double-entry ledger. Invoices post
   Dr AR / Cr Sales Revenue (+ Dr COGS / Cr Inventory at est. cost); payments post
   Dr Cash / Cr AR; receipts post Dr Inventory / Cr AP. Trial-balance view, always balanced.
4. **Quotations** — `Quotation` → convert to Sales Order.
5. **Pricing** — `Price List`, `Pricing Rule` (per-customer / volume pricing).
6. **Multi-warehouse** — `Warehouse`, per-bin stock (currently one global stock qty).
7. **P&L statement** — roll the GL into a period income statement (revenue − COGS).

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
breadth into something shallow. The flow is now **fully transactional** on both
sides — sell (confirm → deliver → invoice → pay, stock and AR updating) and buy
(order → receive, stock and AP updating) — with every financial event posting to
a balanced double-entry **general ledger**. The GL uses an estimated cost ratio
(`COGS_RATIO`) to relieve inventory, since this slice has no per-item cost field.
Note the `fasthtml-oss-migrations` initiative deliberately splits ERPNext's
bundled CRM / HR / Helpdesk into **FastCRM / FastHRM / FastHelpdesk** rather than
reproducing the monolith.
