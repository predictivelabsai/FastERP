"""FastAPI integration surface for future FastERP connectors.

Run with: uvicorn api_app:app --port 5012
Swagger UI: http://localhost:5012/docs
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field

import db


@asynccontextmanager
async def lifespan(_app):
    db.init_schema()
    yield

app = FastAPI(
    title="FastERP Integration API",
    version="0.1.0",
    description=(
        "A self-contained, read-mostly integration stub for the synthetic FastERP demo. "
        "Its resource shapes are inspired by common small-business accounting workflows; "
        "it does not connect to or implement the Intuit API."
    ),
    contact={"name": "FastERP Contributors"},
    license_info={"name": "MIT"},
    lifespan=lifespan,
)


class InvoiceLine(BaseModel):
    item_code: str = Field(examples=["ITM-1005"])
    description: str = Field(examples=["Smart Thermostat"])
    quantity: float = Field(gt=0, examples=[4])
    unit_price: float = Field(ge=0, examples=[189.50])
    tax_code: str = Field(default="UK20", examples=["UK20"])


class InvoiceDraft(BaseModel):
    customer_id: int = Field(examples=[1])
    currency: str = Field(default="GBP", min_length=3, max_length=3, examples=["EUR"])
    business_unit: str | None = Field(default=None, examples=["EU"])
    project_code: str | None = Field(default=None, examples=["PRJ-101"])
    note: str | None = Field(default=None, examples=["Created by a future commerce connector"])
    lines: list[InvoiceLine]


class WebhookEvent(BaseModel):
    event: Literal["invoice.created", "invoice.paid", "expense.created"]
    resource_id: str = Field(examples=["INV-7042"])
    occurred_at: str = Field(examples=["2026-07-28T09:30:00Z"])


@app.get("/", tags=["System"])
def root():
    return {"name": app.title, "version": app.version, "swagger": "/docs", "openapi": "/openapi.json"}


@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "database": db.DB_PATH, "synthetic_data": True}


@app.get("/v1/accounts", tags=["Accounting"])
def list_accounts():
    return {"data": db.account_rows()}


@app.get("/v1/invoices", tags=["Sales"])
def list_invoices(
    status: Annotated[str | None, Query(examples=["Overdue"])] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    sql = """SELECT i.*, c.name customer FROM invoices i
             LEFT JOIN customers c ON c.id=i.customer_id"""
    params: tuple = ()
    if status:
        sql += " WHERE i.status=?"
        params = (status,)
    sql += " ORDER BY i.invoice_date DESC LIMIT ?"
    return {"data": db.rows(sql, params + (limit,)), "next_cursor": None}


@app.post("/v1/invoices", status_code=202, tags=["Sales"])
def propose_invoice(invoice: InvoiceDraft):
    """Validate a future integration payload without posting it to the books."""
    net = round(sum(line.quantity * line.unit_price for line in invoice.lines), 2)
    return {
        "status": "accepted_for_preview",
        "posted": False,
        "preview": {"customer_id": invoice.customer_id, "currency": invoice.currency,
                    "net_total": net, "line_count": len(invoice.lines)},
        "message": "Stub endpoint only; use the FastERP UI to post accounting transactions.",
    }


@app.get("/v1/expenses", tags=["Purchases"])
def list_expenses(limit: Annotated[int, Query(ge=1, le=200)] = 50):
    return {"data": db.expense_rows(limit)}


@app.get("/v1/projects", tags=["Dimensions"])
def list_projects():
    return {"data": db.project_rows()}


@app.get("/v1/reports/profit-and-loss", tags=["Reports"])
def profit_and_loss(
    business_unit_id: int | None = None,
    project_id: int | None = None,
):
    data = db.profit_and_loss(business_unit_id, project_id)
    income = sum(x["amount"] for x in data if x["section"] == "Income")
    expenses = sum(x["amount"] for x in data if x["section"] == "Expenses")
    return {"currency": "GBP", "rows": data, "net_income": round(income - expenses, 2)}


@app.get("/v1/reports/trial-balance", tags=["Reports"])
def trial_balance():
    totals = db.gl_totals()
    return {"currency": "GBP", "balanced": totals["balanced"], "rows": db.trial_balance()}


@app.post("/v1/webhooks/example", tags=["Integration examples"])
def receive_webhook(event: WebhookEvent):
    """Demonstrate the intended payload envelope for a future connector."""
    return {"received": True, "event": event.event, "resource_id": event.resource_id}
