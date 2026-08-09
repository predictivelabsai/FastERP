"""Accounting workspace views for FastERP."""
from __future__ import annotations

from html import escape

from fasthtml.common import (
    A, Button, Div, Form, H1, H3, Input, NotStr, P, Span, Strong,
    Table, Tbody, Td, Th, Thead, Tr,
)

import db
from web.layout import kpi_card, money


def _title(title, sub="", *actions):
    return Div(Div(H1(title), P(sub, cls="sub") if sub else None),
               Div(*actions) if actions else None, cls="page-title")


def _select(name, rows, value_key, label_key, prompt, required=False):
    req = " required" if required else ""
    opts = [f'<option value="">{escape(prompt)}</option>']
    opts += [f'<option value="{escape(str(r[value_key]))}">{escape(str(r[label_key]))}</option>'
             for r in rows]
    return NotStr(f'<select name="{escape(name)}"{req}>{"".join(opts)}</select>')


def overview():
    k = db.accounting_kpis()
    pnl = db.profit_and_loss()
    income = sum(r["amount"] for r in pnl if r["section"] == "Income")
    costs = sum(r["amount"] for r in pnl if r["section"] == "Expenses")
    recent = db.gl_entries(limit=8)
    projects = db.project_rows()[:4]
    return (
        _title("Accounting Overview", "Books, tax and performance across the synthetic business.",
               A("+ New expense", href="/accounting/expenses/new", cls="btn"),
               A("+ Journal entry", href="/accounting/journals/new", cls="btn primary")),
        Div(kpi_card("Cash & bank", money(k["cash"]), "Available book balance", "ok"),
            kpi_card("Receivables", money(k["receivable"]), "Customer balances"),
            kpi_card("Payables", money(k["payable"]), "Supplier balances"),
            kpi_card("Net income", money(k["net_income"]), f"{money(k['revenue'])} revenue",
                     "ok" if k["net_income"] >= 0 else "danger"), cls="kpi-grid"),
        Div(
            Div(Div(H3("Profit & loss snapshot"), A("Full report →", href="/accounting/reports?report=pnl"),
                    cls="card-header"),
                Div(Span("Income"), Strong(money(income)), cls="summary-row"),
                Div(Span("Cost & expenses"), Strong(money(costs)), cls="summary-row"),
                Div(Span("Operating result"), Strong(money(income-costs)), cls="summary-row total"),
                cls="card"),
            Div(Div(H3("Active projects"), A("All projects →", href="/accounting/projects"),
                    cls="card-header"),
                *[Div(Div(Strong(p["name"]), Span(p["status"], cls="pill active")),
                     P(f"{p['business_unit']} · Budget {money(p['budget'])}", cls="sub"),
                     cls="project-row") for p in projects], cls="card"), cls="grid-2"),
        Div(Div(H3("Recent postings"), A("Open general ledger →", href="/ledger"),
                cls="card-header"),
            Table(Thead(Tr(Th("Date"), Th("Account"), Th("Reference"), Th("Debit", cls="num"),
                           Th("Credit", cls="num"))),
                  Tbody(*[Tr(Td(r["entry_date"]), Td(r["account"]), Td(r["ref"]),
                             Td(money(r["debit"]) if r["debit"] else "—", cls="num"),
                             Td(money(r["credit"]) if r["credit"] else "—", cls="num"))
                          for r in recent]), cls="tbl"), cls="card"),
    )


def chart_of_accounts():
    accounts = db.account_rows()
    return (
        _title("Chart of Accounts", f"{len(accounts)} active accounts"),
        Div(Table(Thead(Tr(Th("Code"), Th("Account"), Th("Type"), Th("Normal"),
                            Th("Debits", cls="num"), Th("Credits", cls="num"))),
                  Tbody(*[Tr(Td(a["code"]), Td(A(a["name"], href=f"/ledger?account={a['name']}")),
                             Td(a["account_type"]), Td(a["normal_side"]),
                             Td(money(a["debit"]), cls="num"), Td(money(a["credit"]), cls="num"))
                          for a in accounts]), cls="tbl"), cls="card"),
    )


def expenses():
    data = db.expense_rows()
    return (
        _title("Expenses", "Categorized, taxed and allocated supplier spending.",
               A("+ New expense", href="/accounting/expenses/new", cls="btn primary")),
        Div(Table(Thead(Tr(Th("Date"), Th("Expense"), Th("Supplier"), Th("Category"),
                            Th("Unit / project"), Th("Currency"), Th("Total", cls="num"))),
                  Tbody(*[Tr(Td(x["expense_date"]), Td(Strong(x["code"]), P(x["description"], cls="sub")),
                             Td(x["supplier"] or "—"), Td(x["category"]),
                             Td(f"{x['business_unit'] or '—'} / {x['project'] or '—'}"),
                             Td(x["currency"]), Td(f"{x['total']:,.2f}", cls="num"))
                          for x in data]), cls="tbl"), cls="card"),
    )


def expense_form():
    suppliers = db.suppliers()
    units = db.business_unit_rows()
    projects = db.project_dimension_rows()
    taxes = db.tax_code_rows(with_label=True)
    currencies = db.rows("SELECT *, code || ' · ' || name label FROM currencies ORDER BY code")
    categories = [{"name": n} for n in db.OPERATING_EXPENSES]
    return (
        _title("New Expense", "Record a supplier cost and post it to the general ledger.",
               A("← Expenses", href="/accounting/expenses", cls="btn")),
        Form(
            Div(Div(P("Supplier", cls="label"), _select("supplier_id", suppliers, "id", "name", "Select supplier")),
                Div(P("Category", cls="label"), _select("category", categories, "name", "name",
                                                       "Select expense account", True)), cls="form-grid"),
            Div(Div(P("Description", cls="label"), Input(name="description", required=True,
                                                        placeholder="What was purchased?")),
                Div(P("Net amount", cls="label"), Input(name="net_amount", type="number", min="0.01",
                                                       step="0.01", required=True, placeholder="0.00")),
                cls="form-grid"),
            Div(Div(P("Tax", cls="label"), _select("tax_code_id", taxes, "id", "label", "No tax")),
                Div(P("Currency", cls="label"), _select("currency", currencies, "code", "label",
                                                       "Select currency", True)), cls="form-grid"),
            Div(Div(P("Business unit", cls="label"), _select("business_unit_id", units, "id", "name",
                                                            "Unallocated")),
                Div(P("Project", cls="label"), _select("project_id", projects, "id", "name",
                                                      "No project")), cls="form-grid"),
            P("Note", cls="label"), Input(name="note", placeholder="Approval or receipt note"),
            Div(Button("Post expense", cls="btn primary", type="submit"),
                A("Cancel", href="/accounting/expenses", cls="btn"), cls="form-actions"),
            method="post", action="/accounting/expenses/new", cls="card accounting-form"),
    )


def journals():
    data = db.journal_rows()
    return (
        _title("Journal Entries", "Balanced manual adjustments and opening entries.",
               A("+ New journal", href="/accounting/journals/new", cls="btn primary")),
        Div(Table(Thead(Tr(Th("Date"), Th("Journal"), Th("Memo"), Th("Lines", cls="num"),
                            Th("Total debit", cls="num"), Th("Status"))),
                  Tbody(*[Tr(Td(j["entry_date"]), Td(A(j["code"], href=f"/ledger?account=All")),
                             Td(j["memo"]), Td(j["lines"], cls="num"), Td(money(j["total"]), cls="num"),
                             Td(Span(j["status"], cls="pill paid"))) for j in data]), cls="tbl"), cls="card"),
    )


def journal_form(error=""):
    accounts = [{"name": n} for n in db.ACCOUNTS]
    units = db.business_unit_rows()
    projects = db.project_dimension_rows()
    line_blocks = []
    for n in range(4):
        line_blocks.append(Div(
            _select(f"account_{n}", accounts, "name", "name", "Select account"),
            Input(name=f"debit_{n}", type="number", step="0.01", min="0", placeholder="Debit"),
            Input(name=f"credit_{n}", type="number", step="0.01", min="0", placeholder="Credit"),
            _select(f"unit_{n}", units, "id", "name", "Business unit"),
            _select(f"project_{n}", projects, "id", "name", "Project"),
            cls="journal-line"))
    return (
        _title("New Journal Entry", "Debits must equal credits before posting.",
               A("← Journals", href="/accounting/journals", cls="btn")),
        Form(P(error, cls="error") if error else None,
             Div(Div(P("Date", cls="label"), Input(name="entry_date", type="date",
                                                   value=db.TODAY.isoformat(), required=True)),
                 Div(P("Memo", cls="label"), Input(name="memo", required=True,
                                                   placeholder="Reason for adjustment")), cls="form-grid"),
             Div(Strong("Account"), Strong("Debit"), Strong("Credit"), Strong("Unit"), Strong("Project"),
                 cls="journal-line journal-head"),
             *line_blocks,
             Div(Button("Post balanced journal", cls="btn primary", type="submit"),
                 A("Cancel", href="/accounting/journals", cls="btn"), cls="form-actions"),
             method="post", action="/accounting/journals/new", cls="card accounting-form"),
    )


def projects():
    data = db.project_rows()
    return (
        _title("Projects", "Budgets and profitability using accounting dimensions."),
        Div(Table(Thead(Tr(Th("Project"), Th("Customer"), Th("Business unit"), Th("Status"),
                            Th("Budget", cls="num"), Th("Revenue", cls="num"),
                            Th("Costs", cls="num"), Th("Margin", cls="num"))),
                  Tbody(*[Tr(Td(Strong(p["code"]), P(p["name"], cls="sub")), Td(p["customer"]),
                             Td(p["business_unit"]), Td(Span(p["status"], cls="pill active")),
                             Td(money(p["budget"]), cls="num"), Td(money(p["revenue"]), cls="num"),
                             Td(money(p["costs"]), cls="num"),
                             Td(money(p["revenue"]-p["costs"]), cls="num"))
                          for p in data]), cls="tbl"), cls="card"),
    )


def reports(report="pnl"):
    tabs = Div(*[A(label, href=f"/accounting/reports?report={key}",
                   cls="active" if report == key else "")
                 for key, label in (("pnl", "Profit & Loss"), ("balance", "Balance Sheet"),
                                    ("trial", "Trial Balance"), ("tax", "Sales Tax"))], cls="seg")
    if report == "balance":
        sections = db.balance_sheet()
        body = Div(*[Div(H3(name),
                         Table(Tbody(*[Tr(Td(r["account"]), Td(money(r["balance"]), cls="num"))
                                       for r in vals],
                                     Tr(Td(Strong(f"Total {name}")),
                                        Td(Strong(money(sum(x["balance"] for x in vals))), cls="num"))),
                               cls="tbl"), cls="card") for name, vals in sections.items()], cls="grid-3")
        title = "Balance Sheet"
    elif report == "trial":
        data = db.trial_balance()
        body = Div(Table(Thead(Tr(Th("Account"), Th("Normal"), Th("Debit", cls="num"),
                                    Th("Credit", cls="num"), Th("Balance", cls="num"))),
                         Tbody(*[Tr(Td(r["account"]), Td(r["normal"]), Td(money(r["debit"]), cls="num"),
                                    Td(money(r["credit"]), cls="num"), Td(money(r["balance"]), cls="num"))
                                 for r in data]), cls="tbl"), cls="card")
        title = "Trial Balance"
    elif report == "tax":
        data = db.tax_summary()
        body = Div(Table(Thead(Tr(Th("Period"), Th("Taxable purchases", cls="num"),
                                    Th("Input tax", cls="num"))),
                         Tbody(*[Tr(Td(r["period"]), Td(money(r["taxable"]), cls="num"),
                                    Td(money(r["input_tax"]), cls="num")) for r in data]), cls="tbl"), cls="card")
        title = "Sales Tax Summary"
    else:
        data = db.profit_and_loss()
        income = sum(r["amount"] for r in data if r["section"] == "Income")
        costs = sum(r["amount"] for r in data if r["section"] == "Expenses")
        body = Div(Table(Thead(Tr(Th("Section"), Th("Account"), Th("Amount", cls="num"))),
                         Tbody(*[Tr(Td(r["section"]), Td(r["account"]), Td(money(r["amount"]), cls="num"))
                                 for r in data],
                               Tr(Td(Strong("Net income"), colspan="2"),
                                  Td(Strong(money(income-costs)), cls="num"))), cls="tbl"), cls="card")
        title = "Profit & Loss"
    currency = db.current_company()["local_currency"] if db.using_postgres() else "GBP"
    return _title(title, f"Accrual-basis report in {currency} from posted transactions."), tabs, body


def settings():
    currencies = db.currency_rows()
    units = db.business_unit_rows(order_by="code")
    taxes = db.tax_code_rows()
    attachments = db.attachment_rows()
    return (
        _title("Accounting Setup", "Currencies, tax codes, business units and seeded attachments."),
        Div(
            Div(H3("Currencies"), *[Div(Strong(c["code"]), Span(c["name"]),
                                         Span(f"1 {c['code']} = {c['rate_to_local']:.2f} {c['local_currency']}"), cls="setup-row")
                                      for c in currencies], cls="card"),
            Div(H3("Business units"), *[Div(Strong(u["code"]), Span(u["name"]), Span(u["region"]),
                                            cls="setup-row") for u in units], cls="card"),
            Div(H3("Tax codes"), *[Div(Strong(t["code"]), Span(t["name"]), Span(f"{t['rate']:.1f}%"),
                                       cls="setup-row") for t in taxes], cls="card"), cls="grid-3"),
        Div(H3("Attachments & notes"),
            *[Div(A(a["filename"], href=f"/{a['path']}", target="_blank"), Span(a["entity_type"]),
                  Span(a["note"]), cls="setup-row") for a in attachments], cls="card"),
    )
