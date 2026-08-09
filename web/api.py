"""FastERP public reads and token-gated integration writes."""

from __future__ import annotations

import db

from .api_core import PostgresBackend, Resource, SQLiteBackend, create_sqlite_api


RESOURCES = (
    Resource(
        "accounts",
        "accounts",
        "Accounts",
        "Chart-of-accounts records used by the general ledger.",
        search_fields=("code", "name", "account_type"),
        primary_key="code",
    ),
    Resource(
        "customers",
        "customers",
        "Customers",
        "Customer master data shared by sales orders and invoices.",
        write_fields=("name", "territory", "credit_limit"),
        search_fields=("name", "territory"),
    ),
    Resource(
        "invoices",
        "invoices",
        "Invoices",
        "Issued sales invoices and their payment status.",
        search_fields=("code", "status"),
    ),
    Resource(
        "expenses",
        "expenses",
        "Expenses",
        "Posted supplier expenses, tax, currency, and accounting dimensions.",
        search_fields=("code", "category", "description", "status"),
    ),
    Resource(
        "projects",
        "projects",
        "Projects",
        "Projects used as reporting dimensions across the ledger.",
        search_fields=("code", "name", "status"),
    ),
)

backend = (
    PostgresBackend(db.postgres_database(), RESOURCES)
    if db.using_postgres()
    else SQLiteBackend(db.DB_PATH, RESOURCES, initialize=db.init_schema)
)
api = create_sqlite_api(
    product="FastERP",
    version="1.0.0",
    description="Open integration access to the FastERP synthetic accounting workspace.",
    base_url="https://erp.fastsme.com",
    backend=backend,
    resources=RESOURCES,
)


@api.get("/v1/reports/profit-and-loss", tags=["Reports"])
def profit_and_loss(
    business_unit_id: int | None = None,
    project_id: int | None = None,
):
    """Return income, expenses, and net income for optional dimensions."""

    data = db.profit_and_loss(business_unit_id, project_id)
    income = sum(row["amount"] for row in data if row["section"] == "Income")
    expenses = sum(row["amount"] for row in data if row["section"] == "Expenses")
    currency = db.current_company()["local_currency"] if db.using_postgres() else "GBP"
    return {
        "currency": currency,
        "rows": data,
        "net_income": round(income - expenses, 2),
    }


@api.get("/v1/reports/trial-balance", tags=["Reports"])
def trial_balance():
    """Return the current account balances and balance check."""

    totals = db.gl_totals()
    currency = db.current_company()["local_currency"] if db.using_postgres() else "GBP"
    return {
        "currency": currency,
        "balanced": totals["balanced"],
        "rows": db.trial_balance(),
    }
