# FastERP

**FastERP** is an open-source **ERP** built with [FastHTML](https://fastht.ml) —
a server-side, HTMX-driven ERP with behavior informed by public ERP workflows
and an independently designed Python/PostgreSQL implementation. It covers
**Order-to-Cash, Procure-to-Stock, Inventory and Accounting** with deterministic
synthetic data. Python-first, no JavaScript framework, with an AI assistant
grounded in the live demo company.

*Sell, ship, invoice, get paid.* Runs on port **5011**.

> **Synthetic demo data only by default.** SQLite uses `seed.py`; PostgreSQL uses
> the versioned three-company fixture in `scripts/seed_postgres.py`.

## Demo

![FastERP walkthrough](docs/demo/fasterp-walkthrough.gif)

**[Download the FastERP product deck (PDF)](docs/FastERP_user_guide_2026-07-28.pdf)**
· [PowerPoint](docs/FastERP_user_guide_2026-07-28.pptx)
· [Web-friendly guide](docs/FastERP_user_guide_2026-07-28.md)

**[Laadi alla eestikeelne tootejuhend (PDF)](docs/FastERP_user_guide_2026-07-28_ee.pdf)**
· [PowerPoint](docs/FastERP_user_guide_2026-07-28_ee.pptx)
· [Markdown](docs/FastERP_user_guide_2026-07-28_ee.md)

## Quickstart (native)

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.sample .env          # add an LLM key for free-form AI chat
.venv/bin/python web_app.py  # http://localhost:5011  (self-seeds on first boot)
```

Login: `admin@fasterp.example` / `FastERP2026$`. Rebuild data with
`.venv/bin/python seed.py`.

For PostgreSQL, set `DB_URL`, then run:

```bash
.venv/bin/python scripts/migrate_postgres.py
.venv/bin/python -m scripts.seed_postgres --launch-date 2026-08-09
```

The SAP Business One/ERPNext CLI supports source registration, discovery,
resumable dry runs, gated cutover, reconciliation snapshots, status, and
recorded failback:

```bash
.venv/bin/python -m scripts.migrate_erp --help
```

For a real cutover, provision each empty target company, register its SAP B1 or
ERPNext source, run repeatable dry runs, and supply approved opening/final
control totals to the gated cutover command. See the
[migration runbook](docs/SAP_BUSINESS_ONE_MIGRATION_PLAN.md).

Run the optional integration API and open Swagger:

```bash
.venv/bin/uvicorn api_app:app --port 5012  # http://localhost:5012/docs
```

## Run with Docker

```bash
docker compose up --build      # http://localhost:5011
```

`Dockerfile` (python:3.12-slim, port 5011) seeds on first boot;
`docker-compose.yml` mounts a `fasterp-data` volume at `/data`.

## Module tour

- **Dashboard** (`/`) — revenue (paid), receivables (with overdue), inventory
  value and low-stock count; sales orders by status, **AR aging**, monthly
  invoiced sales, and low-stock items.
- **Sales Orders** (`/orders`) — filter by status; open an order for its **line
  items**, totals, and linked invoice.
- **Invoices (AR)** (`/invoices`) — accounts-receivable list with outstanding
  amounts and status (Unpaid / Partly Paid / Paid / **Overdue**).
- **Items & Stock** (`/items`) — inventory by group with stock levels, value, and
  a **reorder flag**.
- **Customers** (`/customers`) — accounts ranked by outstanding balance.
- **Buying** (`/suppliers`, `/purchase`) — suppliers, multi-line purchase orders,
  goods receipt, stock increases and Accounts Payable postings.
- **Accounting** (`/accounting`) — finance KPIs, a 22-account chart, categorized
  expenses, balanced manual journals and a filterable general ledger.
- **Dimensions** — GBP/EUR/USD, tax codes, business units, projects, notes,
  custom fields, transaction links and synthetic receipt attachments.
- **Reports** (`/accounting/reports`) — Profit & Loss, Balance Sheet, Trial
  Balance and sales-tax summaries; project pages show budget, costs and margin.
- **Integration API** (`/api`) — company-scoped FastAPI resources for accounts,
  invoices, expenses, projects, reports and webhook payloads. Swagger includes
  example schemas; invoice POSTs validate previews without posting.
- **AI Assistant** (right rail) — ops/finance Q&A grounded in a live snapshot;
  slash-commands `/sales`, `/ar`, `/stock`, `/top`, `/buying` and `/gl` work
  with **no API key**.

## Accounting model

Operational events post balanced, immutable entries automatically: invoicing books revenue
and cost of sales, payments clear receivables, goods receipt creates inventory
and payables, and expenses retain tax, currency, business-unit and project
dimensions. This is illustrative accounting software using synthetic data—not a
QuickBooks/Intuit integration or a production bookkeeping system.

```bash
.venv/bin/python -m pytest -q         # accounting and API invariants
bash scripts/build_demo_gif.sh        # rebuild README walkthrough
bash scripts/build_user_guide.sh      # regenerate PDF and PowerPoint guides
```

See the [dated user guide](docs/FastERP_user_guide_2026-07-28.md), also provided
as [PDF](docs/FastERP_user_guide_2026-07-28.pdf) and
[PowerPoint](docs/FastERP_user_guide_2026-07-28.pptx).

## Scope

ERPNext spans Accounting, Selling, Buying, Stock, Manufacturing, Assets,
Projects and more (~527 doctypes). FastERP deliberately models **one coherent
flow — order to cash, with the inventory that backs it**. The full module map and
what's deferred is in **[docs/ROADMAP.md](docs/ROADMAP.md)**.

## Architecture

```
web_app.py        FastHTML routes, auth, SSE chat, boot
api_app.py        standalone FastAPI integration surface
db.py             PostgreSQL runtime facade with SQLite development fallback
fasterp/          typed accounting, inventory, sales, purchasing, and RFQ services
migration/        SAP B1/ERPNext connectors, staging, apply, and reconciliation
migrations/       append-only PostgreSQL schema migrations
seed.py           deterministic SQLite fallback fixture
scripts/seed_postgres.py  deterministic three-company PostgreSQL fixture
web/layout.py     3-pane shell, CSS, chat JS
web/views.py      dashboard, orders, invoices, items, customers renderers
web/accounting.py accounting workspace, forms, dimensions and reports
web/ai.py         grounded chat + slash-commands
tests/            accounting invariants and API contract smoke tests
```

See **[SKILLS.md](SKILLS.md)** for the capability reference + migration playbook.
Part of the
[`fasthtml-oss-migrations`](https://github.com/predictivelabsai/fasthtml-oss-migrations)
initiative.

## Licence

MIT.
