# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

FastERP is a server-rendered, HTMX-driven ERP built with **FastHTML** (Python 3.12).
It models one deep vertical — Order-to-Cash + Procure-to-Stock + Inventory +
Accounting — plus a warehouse (WMS) subsystem and a one-time ERP migration pipeline
(SAP Business One / ERPNext → FastERP). All shipped data is deterministic synthetic
data. The web app runs on port **5011**.

## Commands

```bash
# Setup
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.sample .env

# Run the web app (self-seeds SQLite on first boot). http://localhost:5011
.venv/bin/python web_app.py
# Login: admin@fasterp.example / FastERP2026$  (override via .env)

# Rebuild the local SQLite demo dataset from scratch
.venv/bin/python seed.py

# Standalone integration API (FastAPI + Swagger). http://localhost:5012/docs
.venv/bin/uvicorn api_app:app --port 5012

# Tests (pytest)
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_accounting.py::test_invoice_posts_balanced_entry   # single test
.venv/bin/python -m pytest -q --collect-only                                             # list tests

# Syntax check before submitting (no linter/formatter is configured)
.venv/bin/python -m compileall web_app.py db.py seed.py web fasterp migration scripts

# PostgreSQL runtime: set DB_URL in .env, then
.venv/bin/python scripts/migrate_postgres.py                       # apply append-only schema migrations
.venv/bin/python -m scripts.seed_postgres --launch-date 2026-08-09 # deterministic three-company fixture

# One-time ERP migration CLI (register | run | status | snapshot | failback)
.venv/bin/python -m scripts.migrate_erp --help
```

**Testing note:** many tests in `tests/test_wms.py`, `tests/test_postgres_kernels.py`,
and parts of `tests/test_migration_pipeline.py` require a live PostgreSQL via `DB_URL`;
they call `pytest.skip("DB_URL is not configured")` when it is unset, so a bare
`pytest` run silently skips them. Run against a real Postgres to exercise the
`fasterp/` domain and WMS. Migration-runner static/unit checks and the accounting/API
tests run without a database.

## Architecture

### Two coexisting data layers — know which one you are in

This is the single most important thing to understand before editing.

1. **`db.py` — legacy application facade (SQLite-first).** A large module of raw-SQL
   helpers (`kpis()`, `orders_by_status()`, `snapshot()`, invoicing, payments, stock
   moves…) that powers the original web views and the AI assistant. It defaults to a
   local **SQLite** file (`fasterp.sqlite`) and self-seeds on boot via `seed.py`. When
   `DB_URL` is set it routes the *same* DB-API-placeholder SQL to PostgreSQL through a
   translation shim (`_pg_sql`, `using_postgres()`, `postgres_database()`). It hard-codes
   a deterministic "today" (`TODAY = date(2026, 6, 11)`) so KPI/aging output is stable.

2. **`fasterp/` — PostgreSQL-first typed domain services (newer).** Dataclass-typed
   services (`accounting`, `inventory`, `sales`, `purchasing`, `preorders`, `documents`,
   `warehouse*`) built on a pooled `Database` (`fasterp/database.py`) configured from
   `DatabaseSettings` (`fasterp/config.py`). `Database` is a per-settings **singleton**
   owning one bounded `psycopg` connection pool per process; use its `connection()` /
   transaction context managers and schema-qualified SQL (`DB_SCHEMA`, default
   `fast_erp`). Domain errors live in `fasterp/errors.py` (`DomainError`,
   `InsufficientStockError`). This layer is Postgres-only — there is no SQLite path.

New relational features (WMS, migration) live in `fasterp/` on Postgres. The classic
order-to-cash screens still go through `db.py`. Match the layer the surrounding code
already uses rather than mixing them.

### Entry points

- **`web_app.py`** — the FastHTML app. Owns routing (`@rt`), session auth (`_guard`),
  SSE streaming AI chat, and boot/seed. `fast_app(pico=False, hdrs=[Style(LAYOUT_CSS)])`;
  mounts the FastAPI app at `/api`. Wires in `web/*` renderers, `byok`, and the WMS.
  Keep route handlers thin — persistence/business logic belongs in `db.py` or `fasterp/`.
- **`api_app.py`** — standalone FastAPI integration surface (company-scoped resources,
  Swagger). Public reads need no credential; bearer-token writes are gated by
  `FASTSME_API_TOKEN`. `web/api.py` + `web/api_core.py` define the resources and the
  SQLite/Postgres backends behind them.

### Presentation (`web/`)

Server-side rendering only; HTMX (not a JS framework) drives interactivity — status
filters are GET links, line-item totals compute server-side. `layout.py` is the shared
3-pane shell + CSS tokens + chat JS. `views.py` renders the ERP screens, `accounting.py`
the finance workspace, `ai.py` the grounded chat + slash-commands (`/sales`, `/ar`,
`/stock`, `/top`, `/buying`, `/gl` — these work with **no API key**). `landing.py`,
`features.py`, `seo.py`, `developer.py` are the public marketing/dev pages;
`account_auth.py` + `google_auth.py` handle local accounts and Google SSO.

### WMS subsystem

`fasterp/warehouse.py` (opening stock, FEFO/FEFO reservations, temperature-controlled
lots), `warehouse_reports.py`, and `warehouse_query.py`. The **query layer is a
security boundary**: `WarehouseQueryService` parses candidate SQL with `sqlglot` and
only permits read-only `SELECT`s over the four tenant-scoped reporting *views*
(`wms_stock_report`, `wms_movement_report`, `wms_lot_report`, `wms_temperature_report`)
with an allowlisted function set — everything else raises `WarehouseQueryError`. The AI
"query lab" (`web/wms_ai.py`) generates SQL, runs it through this guard, and only
charges/returns on valid results. `web/wms_views.py` and `web/wms_charts.py` render it.

### Migration pipeline (`migration/` + `scripts/migrate_erp.py`)

Source-neutral one-time ERP cutover. `migration/connectors/` implements
`SourceConnector` for SAP Business One / ECC / S4 OData, ERPNext REST, and CSV bundles
(`mock_sap.py` for tests). `orchestrator.py` drives extract → stage → map → apply →
reconcile with gated cutover and recorded failback. Connector secrets are resolved only
from env prefixes (e.g. `SAP_B1_*`, `ERP_NEXT_*`) and are **never** persisted in
migration metadata. Runbook: `docs/SAP_BUSINESS_ONE_MIGRATION_PLAN.md`.

### PostgreSQL schema (`migrations/postgres/`)

**Append-only, numbered** SQL migrations (`0001_…` … `0017_…`, contiguous — a test
enforces this). Never edit a shipped migration; add the next-numbered file.
`scripts/migrate_postgres.py` applies them and records versions + checksums.

### BYOK (`byok/`)

Vendored "bring your own API key" package shared across the FastSME suite: a per-org
free-query gate, encrypted per-org LLM key storage, a LangChain chat-model factory
(xAI / OpenAI / Anthropic / Google), and self-registering `/byok` routes. Host wiring is
`byok.register(...)` + `byok.begin_query(session)` at the AI entrypoint. Keep its public
surface small (`register`, `begin_query`, `usage`, `usage_banner`, `org_for`).

## Conventions

- **Accounting is automatic and immutable.** Operational events post balanced double-entry
  automatically (invoicing → revenue + COGS at `COGS_RATIO`; payment → clears AR; goods
  receipt → inventory + AP). Preserve this invariant; `tests/test_accounting.py` guards it.
- Prefer parameterized SQL (`WHERE id=?` for `db.py`, `%s`/`psycopg.sql` for `fasterp/`)
  over interpolation. Match the placeholder style of the layer you are in.
- PEP 8, four-space indent, `snake_case` / `UPPER_SNAKE_CASE`, short module docstrings.
  No formatter is configured — match adjacent code.
- Do not commit `.env`, `fasterp.sqlite`, `byok/byok.sqlite`, caches, or `.venv`.
- Reference docs for deep dives: `README.md`, `SKILLS.md` (capability + migration
  playbook), `docs/ROADMAP.md`, `docs/WMS_OPERATIONS.md`, `docs/WMS_IMPLEMENTATION_PLAN.md`,
  and `AGENTS.md`.
```
