# Skills

Capability reference for FastERP + the shared **Frappe → FastHTML migration
playbook** (same recipe across `fasthtml-oss-migrations`; see `FastCRM/SKILLS.md`).

---

## Part 1 — FastERP capabilities

**Entry:** `python web_app.py` → http://localhost:5011
(login `admin@fasterp.example` / `FastERP2026$`).

### Pages

| View | Route | What it shows |
|---|---|---|
| Dashboard | `/` | revenue, receivables, inventory value, AR aging, low stock |
| Sales Orders | `/orders?status=&q=` | order list; `/orders/{id}` = line items + invoice |
| Invoices (AR) | `/invoices?status=` | receivables with outstanding + status |
| Items & Stock | `/items?group=&q=` | inventory with value + reorder flag |
| Customers | `/customers` | accounts ranked by outstanding |
| AI Assistant | `/ai` | ops/finance chat (right rail) |

### Data model (`db.py`)

`customers · items · sales_orders (+ sales_order_items) · invoices · stock_moves`.
`kpis()` computes revenue/receivables/inventory; `orders_by_status()` drives the
funnel. Rebuild with `python seed.py` (delivered/invoiced orders generate
invoices + stock-out moves so totals reconcile).

### AI (`web/ai.py`)

Grounded chat over `snapshot()` (revenue, AR aging, stock, orders). Slash-commands
(no key): `/sales`, `/ar`, `/stock`, `/top`.

---

## Part 2 — Frappe → FastHTML migration playbook

1. **Mine the schema** — `python scripts/frappe_doctype_to_schema.py /tmp/frappe-erpnext`.
2. **Scope to one flow** — ERPNext is 527 doctypes; pick a single coherent
   vertical (order-to-cash) and keep it deep rather than reproducing the monolith
   shallowly. Defer the rest in `docs/ROADMAP.md`.
3. **FastHTML shell** — `fast_app(pico=False, hdrs=[Style(CSS)])`; `page()`
   wrapper; `_guard()` auth.
4. **HTMX over JS** — segmented status filters are GET links; line-item totals are
   computed server-side.
5. **Internally consistent synthetic data** — derive invoices + stock moves from
   order status so KPIs reconcile; fixed RNG seed; self-seed on boot.
6. **LLM, key-optional** — reuse `_provider_stream`; slash-commands work with no key.
7. **Capture the demo** — Playwright MCP → frames → `build_demo_gif.sh`.
8. **Ship deploy paths** — `.env.sample`, `Dockerfile`, `docker-compose.yml`.

### Reusable assets

| File | Reuse |
|---|---|
| `scripts/frappe_doctype_to_schema.py` | DocType JSON → SQLite DDL |
| `scripts/build_demo_gif.sh` | frames → demo GIF |
| `web/layout.py` | 3-pane shell + CSS tokens + SSE chat JS |
| `web/ai.py` `_provider_stream()` | 4-provider streaming chat |
