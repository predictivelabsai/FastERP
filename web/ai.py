"""FastERP AI — grounded chat + slash-commands over the order-to-cash data."""
from __future__ import annotations

import json
import os

import db
from web.layout import money

PROVIDER = os.getenv("MODEL_PROVIDER", "xai")
MODEL = os.getenv("MODEL_NAME", "grok-4-1-fast-reasoning")


def snapshot() -> str:
    k = db.kpis()
    obs = db.orders_by_status()
    tb = db.trial_balance()
    payable = next((r["balance"] for r in tb if r["account"] == "Accounts Payable"), 0)
    company_id = db.current_company_id()
    if company_id is not None:
        open_po = db.scalar(
            "SELECT COUNT(*) FROM purchase_orders WHERE company_id=? AND status='Ordered'",
            (company_id,),
        ) or 0
        n_sup = db.scalar(
            "SELECT COUNT(*) FROM suppliers WHERE company_id=?", (company_id,)
        ) or 0
    else:
        open_po = db.scalar("SELECT COUNT(*) FROM purchase_orders WHERE status='Ordered'") or 0
        n_sup = db.scalar("SELECT COUNT(*) FROM suppliers") or 0
    lines = [
        "ERP SNAPSHOT (synthetic order-to-cash + inventory + buying + GL):",
        f"- Revenue (paid invoices): {money(k['revenue'])}. Receivables outstanding: {money(k['receivable'])} "
        f"(of which {money(k['overdue'])} overdue).",
        f"- Inventory value: {money(k['inventory_value'])}; {k['low_stock']} items below reorder. "
        f"Open orders: {k['open_orders']}. Customers: {k['customers']}.",
        f"- Buying: {n_sup} suppliers, {open_po} purchase orders awaiting receipt. "
        f"Accounts Payable outstanding: {money(payable)}.",
        "Sales orders by status: " + ", ".join(f"{o['status']} {o['count']} ({money(o['value'])})" for o in obs),
        "Trial balance (account: balance): " + ", ".join(
            f"{r['account']} {money(r['balance'])} {r['normal']}" for r in tb),
    ]
    return "\n".join(lines)


SYSTEM_PROMPT = """You are the FastERP assistant, embedded in an open-source ERP (order-to-cash + stock).
Help operations & finance understand sales orders, receivables and inventory. Be concise;
use Markdown (short tables, bold figures). All data is synthetic — never claim it's real.
Base answers on the ERP SNAPSHOT below; if something isn't there, say so."""


def _table(headers, rows_):
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows_:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def handle_command(text):
    if not text.startswith("/"):
        return None
    parts = text[1:].split()
    cmd = parts[0].lower() if parts else ""
    if cmd in ("help", "?"):
        return ("**FastERP shortcuts**\n\n- `/sales` — orders by status\n- `/ar` — receivables aging\n"
                "- `/stock` — low-stock items\n- `/top` — top customers by outstanding\n"
                "- `/buying` — open purchase orders\n- `/gl` — trial balance\n\nOr ask a question in plain English.")
    if cmd == "sales":
        return "**Sales orders by status**\n\n" + _table(
            ["Status", "Orders", "Value"], [[o["status"], o["count"], money(o["value"])] for o in db.orders_by_status()])
    if cmd == "ar":
        company_id = db.current_company_id()
        where = "company_id=? AND status!='Paid'" if company_id is not None else "status!='Paid'"
        r = db.rows(
            f"""SELECT status, COUNT(*) n, ROUND(SUM(total-paid)) due
                  FROM invoices WHERE {where} GROUP BY status""",
            (company_id,) if company_id is not None else (),
        )
        if not r:
            return "No outstanding receivables. 🎉"
        return "**Receivables aging**\n\n" + _table(["Status", "Invoices", "Outstanding"],
                                                    [[x["status"], x["n"], money(x["due"])] for x in r])
    if cmd == "stock":
        company_id = db.current_company_id()
        where = "company_id=? AND stock_qty<=reorder_level" if company_id is not None else "stock_qty<=reorder_level"
        r = db.rows(
            f"""SELECT name, stock_qty, reorder_level, uom FROM items
                  WHERE {where} ORDER BY stock_qty LIMIT 15""",
            (company_id,) if company_id is not None else (),
        )
        if not r:
            return "All items above reorder level. 🎉"
        return "**Low-stock items**\n\n" + _table(["Item", "In stock", "Reorder at"],
                                                  [[x["name"], f"{x['stock_qty']:.0f} {x['uom']}", f"{x['reorder_level']:.0f}"] for x in r])
    if cmd == "top":
        company_id = db.current_company_id()
        company_where = "i.company_id=? AND " if company_id is not None else ""
        r = db.rows(
            f"""SELECT c.name, ROUND(SUM(i.total-i.paid)) due FROM invoices i
                  JOIN customers c ON c.id=i.customer_id
                 WHERE {company_where}i.status!='Paid'
                 GROUP BY c.id ORDER BY due DESC LIMIT 10""",
            (company_id,) if company_id is not None else (),
        )
        return "**Top customers by outstanding**\n\n" + _table(["Customer", "Outstanding"],
                                                              [[x["name"], money(x["due"])] for x in r])
    if cmd in ("buying", "po", "purchase"):
        company_id = db.current_company_id()
        company_where = "po.company_id=? AND " if company_id is not None else ""
        r = db.rows(
            f"""SELECT po.code, s.name supplier, po.status, po.total FROM purchase_orders po
                  LEFT JOIN suppliers s ON s.id=po.supplier_id
                 WHERE {company_where}po.status='Ordered'
                 ORDER BY po.total DESC LIMIT 15""",
            (company_id,) if company_id is not None else (),
        )
        if not r:
            return "No purchase orders awaiting receipt. 🎉"
        return "**Purchase orders awaiting receipt**\n\n" + _table(
            ["PO", "Supplier", "Total"], [[x["code"], x["supplier"], money(x["total"])] for x in r])
    if cmd in ("gl", "ledger", "tb"):
        tb = db.trial_balance()
        t = db.gl_totals()
        body = _table(["Account", "Debit", "Credit", "Balance"],
                      [[r["account"], money(r["debit"]), money(r["credit"]), f"{money(r['balance'])} {r['normal']}"]
                       for r in tb])
        flag = "✅ balanced" if t["balanced"] else "⚠ out of balance"
        return f"**Trial balance** ({flag})\n\n" + body
    return f"Unknown command `/{cmd}`. Try `/help`."


async def stream_chat(message):
    cmd = handle_command(message)
    if cmd is not None:
        yield f"data: {json.dumps({'token': cmd})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"
        return
    system = SYSTEM_PROMPT + "\n\n" + snapshot()
    try:
        async for tok in _provider_stream(system, message):
            yield f"data: {json.dumps({'token': tok})}\n\n"
    except Exception as e:  # noqa: BLE001
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
    yield f"data: {json.dumps({'done': True})}\n\n"


async def _provider_stream(system, message):
    import httpx
    provider, model = PROVIDER, MODEL
    if provider in ("xai", "openai"):
        url = "https://api.x.ai/v1/chat/completions" if provider == "xai" else "https://api.openai.com/v1/chat/completions"
        key = os.getenv("XAI_API_KEY" if provider == "xai" else "OPENAI_API_KEY", "")
        if not key:
            yield _no_key(provider); return
        async with httpx.AsyncClient(timeout=90) as client:
            async with client.stream("POST", url, headers={"Authorization": f"Bearer {key}"},
                                     json={"model": model, "stream": True,
                                           "messages": [{"role": "system", "content": system},
                                                        {"role": "user", "content": message}]}) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            tok = json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                            if tok: yield tok
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass
    elif provider == "anthropic":
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            yield _no_key(provider); return
        async with httpx.AsyncClient(timeout=90) as client:
            async with client.stream("POST", "https://api.anthropic.com/v1/messages",
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                                     json={"model": model, "max_tokens": 1500, "stream": True, "system": system,
                                           "messages": [{"role": "user", "content": message}]}) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            ev = json.loads(line[6:])
                            if ev.get("type") == "content_block_delta":
                                tok = ev.get("delta", {}).get("text", "")
                                if tok: yield tok
                        except json.JSONDecodeError:
                            pass
    elif provider == "google":
        key = os.getenv("GOOGLE_API_KEY", "")
        if not key:
            yield _no_key(provider); return
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse&key={key}"
        async with httpx.AsyncClient(timeout=90) as client:
            async with client.stream("POST", url, json={"system_instruction": {"parts": [{"text": system}]},
                                                        "contents": [{"role": "user", "parts": [{"text": message}]}]}) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            tok = json.loads(line[6:])["candidates"][0]["content"]["parts"][0].get("text", "")
                            if tok: yield tok
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass
    else:
        yield "No LLM provider configured. Slash-commands like /ar work without a key."


def _no_key(provider):
    env = {"xai": "XAI_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "google": "GOOGLE_API_KEY"}[provider]
    return (f"⚠ No **{env}** set, so free-form chat is disabled. Add it to `.env` and restart. "
            "Slash-commands (`/sales`, `/ar`, `/stock`) work without any key.")
