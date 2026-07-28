::: cover

# FastERP User Guide

![FastERP sign in](guide/screenshots/01-login.png)

**Sell, ship, invoice, get paid — and keep balanced books.**

Self-contained synthetic demonstration · No Intuit connection

:::

---

## Operations dashboard

![Operations dashboard](guide/screenshots/02-dashboard.png)

Start at the daily cockpit for paid revenue, receivables, inventory value and
open orders. The charts highlight order status, aging debt and low-stock items.
Collapse the AI rail whenever you need a wider working area.

---

## Sales orders

![Sales orders](guide/screenshots/03-orders.png)

Open **Selling → Sales Orders** to filter the order book by status or search for
a customer or reference. Each row exposes delivery timing, workflow state and
total value.

---

## Order-to-cash workflow

![Sales order detail](guide/screenshots/04-order-detail.png)

An order detail page combines customer data, line items, totals and the next
transactional action. Move eligible orders through Confirm, Deliver and Invoice;
delivery adjusts stock and invoicing creates balanced accounting entries.

---

## Invoices and receivables

![Invoices](guide/screenshots/05-invoices.png)

Use **Invoices (AR)** to review outstanding, partly paid, paid and overdue
invoices. Record a payment from an order or invoice workflow to clear Accounts
Receivable and increase Cash.

---

## Items and stock

![Items and stock](guide/screenshots/06-items.png)

The stock register shows item codes, groups, selling rates, quantities, values
and reorder status. Filter by item group or search the catalog to investigate
availability before confirming demand.

---

## Suppliers

![Suppliers](guide/screenshots/07-suppliers.png)

The supplier register summarizes territory, purchase-order count and total
spend. Add synthetic suppliers here before creating a purchasing transaction.

---

## Purchase order and goods receipt

![Purchase order](guide/screenshots/08-purchase-order.png)

A purchase order records supplier, line items and workflow status. Receiving an
ordered PO increases stock and posts Inventory against Accounts Payable, linking
procurement to the general ledger.

---

## Accounting overview

![Accounting overview](guide/screenshots/09-accounting.png)

Open **Accounting → Overview** for cash, receivables, payables and net income.
Review the Profit & Loss snapshot, active projects and latest postings, or start
a new expense or manual journal.

---

## Chart of accounts

![Chart of accounts](guide/screenshots/10-accounts.png)

The 22-account chart groups assets, liabilities, equity, income, cost of sales
and operating expenses. Select an account to trace its postings in the General
Ledger.

---

## Record an expense

![New expense](guide/screenshots/11-expense.png)

Choose a supplier and expense category, then enter the net amount, tax code and
currency. Optional business-unit and project dimensions flow to ledger lines and
profitability reporting. A note can retain approval or receipt context.

---

## Post a journal entry

![New journal entry](guide/screenshots/12-journal.png)

Enter a date and memo, then add at least two account lines. Each line may carry a
business unit and project. FastERP rejects the journal unless total debits equal
total credits.

---

## General ledger

![General ledger](guide/screenshots/13-ledger.png)

The Trial Balance summarizes debits, credits and normal-side balances. Filter
ledger entries by account and use shared references such as `INV-7042`,
`EXP-8001` and `JE-9001` to follow linked transactions.

---

## Projects and business units

![Projects](guide/screenshots/14-projects.png)

Projects combine customer, owning business unit, status, budget, revenue, costs
and margin. Dimensioned expenses and journals update project costs immediately
without requiring separate ledgers.

---

## Financial reports

![Profit and loss](guide/screenshots/15-reports.png)

Switch between Profit & Loss, Balance Sheet, Trial Balance and Sales Tax. Reports
are accrual-basis, presented in GBP and derived solely from posted double-entry
transactions.

---

## Accounting setup and attachments

![Accounting setup](guide/screenshots/16-setup.png)

Setup lists GBP, EUR, USD and CAD rates, tax codes, business units and synthetic
receipt attachments. The rates and tax treatment are illustrative; the receipt
images contain no real supplier or payment information.

---

## Integration API and Swagger

![Swagger API](guide/screenshots/17-api.png)

Run `.venv/bin/uvicorn api_app:app --port 5012` and open
`http://localhost:5012/docs`. The read-mostly FastAPI stub documents accounts,
invoices, expenses, projects, reports and webhook examples. Invoice POSTs
validate previews without posting to the books.
