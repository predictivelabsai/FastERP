"""Public, screenshot-led overview of FastERP capabilities."""

from __future__ import annotations

from dataclasses import dataclass

from fasthtml.common import (
    A, Article, Body, Div, Figcaption, Figure, Footer, H1, H2, H3,
    Head, Html, Img, Li, Link, Main, Meta, Nav, Ol, P, Script,
    Section, Span, Style, Title,
)

from .account_auth import AUTH_CSS, AUTH_JS, auth_modal
from .landing import CSS as LANDING_CSS, FAVICON, public_nav
from .seo import seo_meta


@dataclass(frozen=True)
class Feature:
    slug: str
    title: str
    group: str
    description: str
    image: str
    alt: str


FEATURES = (
    Feature("operations-dashboard", "Operations dashboard", "Overview",
            "See paid revenue, open orders, receivables and low stock together. Use the summaries to decide which workflow needs attention first.",
            "02-dashboard.png", "FastERP operations dashboard with summary cards and charts"),
    Feature("sales-orders", "Sales order book", "Selling",
            "Filter and search customer orders by status. The order list shows value and delivery timing before you open the full transaction.",
            "03-orders.png", "FastERP sales orders list"),
    Feature("order-workflow", "Order-to-cash workflow", "Selling",
            "Follow an order from confirmation through delivery and invoicing. Each action updates the related stock or financial records.",
            "04-order-detail.png", "FastERP sales order detail and workflow actions"),
    Feature("invoices", "Invoices and receivables", "Selling",
            "Review unpaid, partly paid and settled invoices. Record payments against the invoice so the receivable balance stays current.",
            "05-invoices.png", "FastERP invoice and receivables list"),
    Feature("items-stock", "Items and stock", "Inventory & buying",
            "Search item codes, groups and quantities in the stock register. Reorder signals help buyers find items running low.",
            "06-items.png", "FastERP items and stock register"),
    Feature("suppliers", "Supplier management", "Inventory & buying",
            "Keep supplier details and purchasing activity together. Open a supplier record before placing or reviewing a purchase order.",
            "07-suppliers.png", "FastERP supplier register"),
    Feature("purchase-orders", "Purchase orders and receipt", "Inventory & buying",
            "Create purchase orders with item quantities and prices, then record received stock. Lot, serial and expiry details can be captured during receipt.",
            "08-purchase-order.png", "FastERP purchase order with receipt action"),
    Feature("inventory-control", "Inventory Control", "Inventory & buying",
            "Monitor on-hand, available, reserved and held quantities across warehouses. Expiry, FEFO allocation and temperature alerts lead into stock and lot details.",
            "19-inventory-control.png", "FastERP Inventory Control dashboard with stock and expiry charts"),
    Feature("inventory-query", "Inventory Query Lab", "Inventory & buying",
            "Ask inventory questions in plain language or run a guarded read-only query. Results appear as a table and a chart when the data supports one.",
            "20-inventory-query.png", "FastERP Inventory Query Lab with SQL and chart results"),
    Feature("accounting-overview", "Accounting overview", "Finance",
            "See cash, receivables, payables and net income in one accounting workspace. Jump to reports or the latest postings for more detail.",
            "09-accounting.png", "FastERP accounting overview"),
    Feature("chart-of-accounts", "Chart of accounts", "Finance",
            "Browse the accounts used for assets, liabilities, income and costs. Open an account to follow its transactions in the general ledger.",
            "10-accounts.png", "FastERP chart of accounts"),
    Feature("expenses", "Expenses", "Finance",
            "Record a supplier expense with category, tax and optional business dimensions. The posting flows into financial and project reporting.",
            "11-expense.png", "FastERP expense entry form"),
    Feature("journals", "Journal entries", "Finance",
            "Post manual adjustments with dated, balanced debit and credit lines. Add a memo and dimensions when the entry needs a clearer audit trail.",
            "12-journal.png", "FastERP journal entry form"),
    Feature("general-ledger", "General ledger", "Finance",
            "Inspect the trial balance and individual account postings. Voucher references connect a ledger entry back to its business transaction.",
            "13-ledger.png", "FastERP general ledger and trial balance"),
    Feature("projects", "Projects and business units", "Finance",
            "Track budget, revenue, cost and margin by project. Shared dimensions connect ordinary postings to project performance.",
            "14-projects.png", "FastERP projects overview"),
    Feature("financial-reports", "Financial reports", "Finance",
            "Review profit and loss, balance sheet, trial balance and sales tax from posted transactions. Change report views without exporting data first.",
            "15-reports.png", "FastERP profit and loss report"),
    Feature("accounting-setup", "Accounting setup", "Finance",
            "Manage currencies, tax codes and the accounts that power transaction posting. Setup keeps the operational workflows tied to balanced books.",
            "16-setup.png", "FastERP accounting setup"),
    Feature("ai-copilot", "AI copilot", "Platform",
            "Ask about orders, stock and financial activity from within the workspace. Grounded answers and commands use the application's live data and access rules.",
            "18-ai-copilot.png", "FastERP workspace with AI copilot conversation"),
    Feature("integration-api", "Integration API", "Platform",
            "Explore documented endpoints and schemas for connecting FastERP to other systems. API documentation gives developers concrete request examples.",
            "17-api.png", "FastERP integration API documentation"),
)


FEATURES_CSS = """
html{scroll-behavior:smooth}
.ft-hero{max-width:1180px;margin:auto;padding:78px 24px 48px}
.ft-hero h1{font-size:clamp(42px,6vw,68px);line-height:1.07;letter-spacing:-.05em;margin:15px 0 18px}
.ft-hero p{max-width:740px;font-size:18px;line-height:1.65;color:var(--muted);margin:0}
.ft-count{display:inline-block;margin-top:20px;padding:7px 13px;border-radius:999px;background:var(--tint);color:var(--accent);font-size:13px;font-weight:700}
.ft-layout{max-width:1180px;margin:0 auto;padding:0 24px 90px;display:grid;grid-template-columns:235px minmax(0,1fr);gap:42px;align-items:start}
.ft-toc{position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto;border:1px solid var(--line);border-radius:18px;padding:20px;background:#fff}
.ft-toc h2{font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin:0 0 14px}
.ft-toc ol{list-style:none;padding:0;margin:0}.ft-toc li{margin:0}.ft-toc a{display:block;padding:6px 8px;border-radius:7px;color:#364152;text-decoration:none;font-size:13px;line-height:1.4}
.ft-toc a:hover,.ft-toc a:focus{background:var(--tint);color:var(--accent)}
.ft-toc-group{display:block;margin:15px 8px 5px;color:var(--accent);font-size:10px;font-weight:800;letter-spacing:.1em;text-transform:uppercase}
.ft-content{min-width:0}.ft-group-title{font-size:14px;text-transform:uppercase;letter-spacing:.12em;color:var(--accent);margin:48px 0 4px;scroll-margin-top:24px}
.ft-feature{padding:28px 0 48px;border-bottom:1px solid var(--line);scroll-margin-top:24px}.ft-feature:first-of-type{padding-top:0}
.ft-feature h3{font-size:clamp(24px,3vw,34px);letter-spacing:-.035em;line-height:1.15;margin:6px 0 12px}.ft-feature p{font-size:16px;line-height:1.68;color:var(--muted);max-width:720px;margin:0 0 22px}
.ft-num{font-size:12px;font-weight:800;color:var(--accent);letter-spacing:.1em;text-transform:uppercase}
.ft-shot{margin:0;border:1px solid var(--line);border-radius:16px;overflow:hidden;background:#f4f6fa;box-shadow:0 16px 40px rgba(17,24,39,.07)}
.ft-shot a{display:block;line-height:0}.ft-shot img{display:block;width:100%;height:auto;object-fit:contain}.ft-shot figcaption{padding:11px 15px;color:var(--muted);font-size:12px;line-height:1.4;background:#fff}
.ft-footer-cta{max-width:1180px;margin:0 auto 60px;padding:0 24px}.ft-footer-cta>div{padding:32px;border-radius:20px;background:var(--tint)}.ft-footer-cta h2{margin:0 0 10px;font-size:26px}.ft-footer-cta p{color:var(--muted);line-height:1.6;margin:0 0 20px}
@media(max-width:850px){.ft-layout{grid-template-columns:1fr;gap:22px}.ft-toc{position:static;max-height:none}.ft-toc ol{columns:2}.ft-group-title{margin-top:36px}}
@media(max-width:560px){.ft-hero{padding-top:52px}.ft-toc ol{columns:1}.ft-feature{padding-bottom:34px}.ft-footer-cta>div{padding:24px}}
"""


def features_page(*, signed_in: bool = False):
    toc_items = []
    articles = []
    current_group = None
    for index, feature in enumerate(FEATURES, 1):
        if feature.group != current_group:
            current_group = feature.group
            toc_items.append(Li(Span(current_group, cls="ft-toc-group")))
            articles.append(H2(current_group, id=f"group-{index}", cls="ft-group-title"))
        toc_items.append(Li(A(feature.title, href=f"#{feature.slug}")))
        image_url = f"/static/features/{feature.image}"
        articles.append(Article(
            Span(f"Feature {index:02d}", cls="ft-num"),
            H3(feature.title),
            P(feature.description),
            Figure(
                A(Img(src=image_url, alt=feature.alt, loading="lazy",
                      width="1280", height="720"), href=image_url,
                  target="_blank", rel="noopener noreferrer",
                  aria_label=f"Open full screenshot: {feature.title}"),
                Figcaption(f"{feature.title} · example FastERP screen. Open image to enlarge."),
                cls="ft-shot",
            ), id=feature.slug, cls="ft-feature",
        ))
    return Html(
        Head(
            Title("Features · FastERP"),
            Meta(charset="utf-8"),
            Meta(name="viewport", content="width=device-width, initial-scale=1"),
            Meta(name="description", content="Explore FastERP's sales, warehouse, accounting, reporting, AI and integration features with screenshots."),
            *seo_meta(path="/features", title="Features · FastERP",
                      description="Explore FastERP features with product screenshots and practical descriptions."),
            Link(rel="icon", type="image/svg+xml", href=FAVICON),
            Style(LANDING_CSS + FEATURES_CSS + AUTH_CSS),
        ),
        Body(
            public_nav(signed_in=signed_in),
            Main(
                Section(
                    Span("Explore FastERP", cls="lp-kicker"),
                    H1("See how the work fits together."),
                    P("Browse the screens behind sales, purchasing, warehouse control, accounting and integrations. Each example uses synthetic demonstration data."),
                    Span(f"{len(FEATURES)} features · screenshots and explanations",
                         cls="ft-count"), cls="ft-hero",
                ),
                Div(
                    Nav(H2("On this page"), Ol(*toc_items),
                        aria_label="Features table of contents", cls="ft-toc"),
                    Div(*articles, cls="ft-content"),
                    cls="ft-layout",
                ),
                Section(Div(H2("Ready to explore the workspace?"),
                            P("Sign in to follow these workflows with your own company data."),
                            A("Open workspace", href="/", cls="lp-primary")),
                        cls="ft-footer-cta"),
            ),
            Footer(Span("FastERP is part of the open-source FastSME suite."),
                   A("View on GitHub ↗", href="https://github.com/predictivelabsai/FastERP",
                     target="_blank", rel="noopener noreferrer"), cls="lp-footer"),
            auth_modal("FastERP") if not signed_in else None,
            Script(AUTH_JS) if not signed_in else None,
        ),
    )
