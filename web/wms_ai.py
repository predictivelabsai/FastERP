"""FastBI-style warehouse text-to-SQL through the existing BYOK query gate."""

from __future__ import annotations

import re

import byok
import db
from fasterp.warehouse_query import WarehouseQueryService, WarehouseQueryError


SQL_SYSTEM = """You answer warehouse inventory questions by writing PostgreSQL SQL.
Return exactly one SELECT statement, without markdown or explanation. Use only
these tenant-filtered reporting views and columns:

{schema}

Rules: never use other schemas or tables; use only COUNT, SUM, AVG, MIN, MAX,
ROUND, COALESCE, DATE_TRUNC, EXTRACT and CURRENT_DATE when a function is needed;
do not use CTEs; alias aggregates clearly; use dates and quantities from the views; order time series;
limit broad detail queries to 100 rows. FEFO means earliest eligible expiry,
while FIFO cost valuation is a separate concept. If the question cannot be
answered from these views, return SELECT 'Unavailable from warehouse reports'
AS message FROM wms_stock_report LIMIT 1."""


def _extract_sql(content: str) -> str:
    text = content.strip()
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.I | re.S)
    if match:
        text = match.group(1).strip()
    match = re.search(r"\bSELECT\b.*", text, re.I | re.S)
    if not match:
        raise WarehouseQueryError("The model did not return a SELECT query")
    return match.group(0).strip().rstrip(";")


async def ask_warehouse(question: str, session, company_id: int):
    """Generate SQL, validate and execute it, then charge one successful query."""
    question = (question or "").strip()
    if not question:
        raise WarehouseQueryError("Enter a warehouse question")
    if len(question) > 2000:
        raise WarehouseQueryError("Question is too long")
    gate = byok.begin_query(session)
    if gate.blocked:
        raise WarehouseQueryError(gate.gate_markdown)
    service = WarehouseQueryService(db.postgres_database())
    from langchain_core.messages import HumanMessage, SystemMessage

    response = await gate.llm.ainvoke([
        SystemMessage(content=SQL_SYSTEM.format(schema=service.schema_prompt())),
        HumanMessage(content=question),
    ])
    content = response.content
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict)
                          else str(part) for part in content)
    result = service.run(_extract_sql(str(content)), company_id=company_id)
    gate.commit()
    return result
