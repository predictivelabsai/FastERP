"""Accounting invariants and integration-stub smoke tests."""
from __future__ import annotations

import db
import seed


def _fresh_database(tmp_path, monkeypatch):
    path = tmp_path / "accounting.sqlite"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    seed.build()
    return path


def test_seeded_ledger_is_balanced(tmp_path, monkeypatch):
    _fresh_database(tmp_path, monkeypatch)
    totals = db.gl_totals()
    assert totals["balanced"]
    assert len(db.account_rows()) == 22
    assert len(db.expense_rows()) == 22
    assert len(db.project_rows()) == 4
    statement = db.balance_sheet()
    assets = sum(x["balance"] for x in statement["Assets"])
    liabilities_and_equity = sum(
        x["balance"] for section in ("Liabilities", "Equity") for x in statement[section])
    assert round(assets, 2) == round(liabilities_and_equity, 2)


def test_posting_expense_preserves_balance(tmp_path, monkeypatch):
    _fresh_database(tmp_path, monkeypatch)
    supplier = db.scalar("SELECT id FROM suppliers LIMIT 1")
    tax = db.scalar("SELECT id FROM tax_codes WHERE code='UK20'")
    unit = db.scalar("SELECT id FROM business_units WHERE code='UK'")

    expense_id = db.create_expense(
        supplier, "Software Expense", "Integration test", 100, tax, "GBP", unit)

    assert expense_id
    assert db.one("SELECT total FROM expenses WHERE id=?", (expense_id,))["total"] == 120
    assert db.gl_totals()["balanced"]


def test_manual_journal_must_balance(tmp_path, monkeypatch):
    _fresh_database(tmp_path, monkeypatch)
    assert db.create_journal(
        "2026-07-28", "Unbalanced",
        [("Bank", 100, 0, None, None), ("Other Income", 0, 90, None, None)]
    ) is None
    assert db.create_journal(
        "2026-07-28", "Balanced",
        [("Bank", 100, 0, None, None), ("Other Income", 0, 100, None, None)]
    )
    assert db.gl_totals()["balanced"]
