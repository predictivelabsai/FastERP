"""FastERP public reads and token-gated integration writes."""

from __future__ import annotations

import json
import os
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import db

from .api_core import (
    PostgresBackend,
    Resource,
    SQLiteBackend,
    create_sqlite_api,
)

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
    Resource(
        "items",
        "items",
        "Items",
        "Company-scoped item master data available to commerce connectors.",
        search_fields=("code", "name", "item_group"),
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


class CommerceOrderLine(BaseModel):
    sku: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=240)
    variant: str = Field(default="", max_length=180)
    quantity: int = Field(gt=0, le=10000)
    unit_price_minor: int = Field(ge=0)


class CommerceOrder(BaseModel):
    source: str = Field(pattern=r"^[a-z0-9_-]+$", max_length=40)
    source_id: str = Field(min_length=1, max_length=64)
    number: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=320)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    total_minor: int = Field(ge=0)
    payment_status: str = Field(max_length=40)
    shipping_address: dict = Field(default_factory=dict)
    lines: list[CommerceOrderLine] = Field(min_length=1)


commerce_bearer = HTTPBearer(auto_error=False)
commerce_credentials = Depends(commerce_bearer)


def require_fastshop_token(
    credentials: HTTPAuthorizationCredentials | None = commerce_credentials,
) -> None:
    configured = os.getenv("FASTSHOP_CONNECTOR_TOKEN", "")
    if not configured:
        raise HTTPException(status_code=503, detail="FastShop connector is not configured")
    supplied = credentials.credentials if credentials else ""
    if not secrets.compare_digest(configured, supplied):
        raise HTTPException(status_code=401, detail="Valid FastShop connector token required")


@api.post(
    "/v1/commerce/orders",
    status_code=202,
    tags=["Commerce integrations"],
    dependencies=[Depends(require_fastshop_token)],
)
def receive_commerce_order(
    order: CommerceOrder,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ],
    company_header: Annotated[
        str | None, Header(alias="X-FastERP-Company")
    ] = None,
):
    """Stage a FastShop order exactly once for ERP mapping and reconciliation."""

    try:
        company_id = int(company_header) if company_header else db.current_company_id()
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid FastERP company") from exc
    if db.using_postgres() and not company_id:
        raise HTTPException(status_code=422, detail="FastERP company is required")
    company_id = company_id or 1
    payload = json.dumps(order.model_dump(), separators=(",", ":"))
    existing = db.one(
        """SELECT id,status,erp_order_id FROM commerce_order_ingress
             WHERE company_id=? AND (idempotency_key=? OR (source=? AND source_id=?))""",
        (company_id, idempotency_key, order.source, order.source_id),
    )
    if existing:
        return {
            "id": existing["id"],
            "status": existing["status"],
            "erp_order_id": existing.get("erp_order_id"),
            "duplicate": True,
        }
    if db.using_postgres():
        with db.postgres_database().transaction() as connection:
            row = connection.execute(
                """INSERT INTO commerce_order_ingress
                       (company_id,source,source_id,idempotency_key,payload_json)
                     VALUES (%s,%s,%s,%s,%s::jsonb)
                  RETURNING id,status,erp_order_id""",
                (company_id, order.source, order.source_id, idempotency_key, payload),
            ).fetchone()
    else:
        with db.cursor() as connection:
            cursor = connection.execute(
                """INSERT INTO commerce_order_ingress
                       (company_id,source,source_id,idempotency_key,payload_json)
                     VALUES (?,?,?,?,?)""",
                (company_id, order.source, order.source_id, idempotency_key, payload),
            )
            row = {
                "id": cursor.lastrowid,
                "status": "Received",
                "erp_order_id": None,
            }
    return {**dict(row), "duplicate": False}


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
