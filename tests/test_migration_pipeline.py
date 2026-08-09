"""End-to-end source discovery, staging, apply, and reconciliation tests."""

from __future__ import annotations

import os
import uuid
from datetime import date

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg import sql

from fasterp.config import DatabaseSettings
from fasterp.database import Database
from fasterp.errors import DomainError
from migration.apply import Applier
from migration.connectors import MockSapConnector
from migration.mapping import normalizers_for
from migration.masters import master_handlers
from migration.reconcile import Reconciler
from migration.staging import MigrationRunService
from migration.validation import Validator
from scripts.migrate_postgres import apply_migrations


@pytest.fixture(scope="module")
def pipeline_db():
    load_dotenv()
    database_url = os.getenv("DB_URL")
    if not database_url:
        pytest.skip("DB_URL is not configured")
    schema = f"fast_erp_pipeline_{uuid.uuid4().hex[:12]}"
    apply_migrations(database_url, schema)
    database = Database(DatabaseSettings(database_url, schema, 1, 3))
    try:
        with database.transaction() as connection:
            company = connection.execute(
                """INSERT INTO companies(code,name,country_code,local_currency)
                   VALUES ('PIPE','Pipeline Company','GB','GBP') RETURNING id"""
            ).fetchone()["id"]
        yield database, company
    finally:
        database.close()
        assert schema.startswith("fast_erp_pipeline_")
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _fixtures(price="10"):
    return {
        "Items": [
            {"ItemCode": f"I-{number}", "ItemName": f"Item {number}", "Price": price}
            for number in range(5)
        ],
        "Unsupported": [{"Key": "U-1", "Value": "preserve me"}],
    }


def _run(database, company, source_id, fixtures):
    connector = MockSapConnector(fixtures, keys={"Items": "ItemCode", "Unsupported": "Key"})
    runs = MigrationRunService(database)
    run = runs.create_run(
        source_id=source_id, history_from=date(2025, 1, 1),
        history_to=date(2026, 8, 16), requested_by="tester",
    )
    capabilities = runs.discover(run, connector, actor="tester")
    assert len(capabilities) == 2
    assert runs.extract(
        run, connector, ["Items", "Unsupported"], actor="tester", page_size=2
    ) == 6
    # Re-extracting the completed sources creates batch evidence but no duplicate
    # staging rows because the source keys and canonical payload hashes agree.
    assert runs.extract(
        run, connector, ["Items", "Unsupported"], actor="tester", page_size=2,
        restart_completed=True,
    ) == 6
    assert database.scalar(
        "SELECT count(*) FROM migration_staging_records WHERE run_id=%s", (run,)
    ) == 6
    warnings, errors = Validator(
        database, normalizers_for("mock_sap")
    ).validate(run, actor="tester")
    assert (warnings, errors) == (0, 0)
    runs.approve(run, approver="approver")
    return run


def test_pipeline_is_resumable_idempotent_archival_and_reconciled(pipeline_db):
    database, company = pipeline_db
    runs = MigrationRunService(database)
    source = runs.create_source(
        name="Synthetic SAP", connector_type="mock_sap", company_id=company,
        source_company_db="PIPE",
    )
    run = _run(database, company, source, _fixtures())
    handlers, order = master_handlers("mock_sap")
    applied, archived = Applier(database, handlers, order).apply(run, actor="tester")
    assert (applied, archived) == (5, 1)
    assert Reconciler(database).reconcile_counts(run, actor="tester")
    assert database.one(
        "SELECT status,applied_count,error_count FROM migration_runs WHERE id=%s", (run,)
    ) == {"status": "Completed", "applied_count": 5, "error_count": 0}
    assert database.scalar("SELECT count(*) FROM items WHERE company_id=%s", (company,)) == 5
    assert database.scalar("SELECT count(*) FROM migration_crosswalks WHERE source_id=%s", (source,)) == 5
    assert database.scalar("SELECT count(*) FROM external_references WHERE source_id=%s", (source,)) == 5
    assert database.scalar("SELECT count(*) FROM migration_archived_objects WHERE source_id=%s", (source,)) == 1
    assert database.scalar("SELECT count(*) FROM migration_apply_manifests WHERE run_id=%s", (run,)) == 6

    # A second run of the exact source links the existing crosswalks rather than
    # inserting duplicate items.
    replay = _run(database, company, source, _fixtures())
    assert Applier(database, handlers, order).apply(
        replay, actor="tester"
    ) == (5, 1)
    assert Reconciler(database).reconcile_counts(replay, actor="tester")
    assert database.scalar("SELECT count(*) FROM items WHERE company_id=%s", (company,)) == 5

    # A changed source payload is intentionally not silently merged after an
    # approved crosswalk; it requires an explicit new migration decision.
    changed = _run(database, company, source, _fixtures(price="11"))
    with pytest.raises(DomainError, match="Applied source changed"):
        Applier(database, handlers, order).apply(
            changed, actor="tester"
        )


def test_financial_reconciliation_pack_can_complete_exact_snapshot(pipeline_db):
    database, company = pipeline_db
    runs = MigrationRunService(database)
    source = runs.create_source(
        name="Financial Reconciliation Source", connector_type="mock_sap",
        company_id=company, source_company_db="PIPE-FIN",
    )
    run = runs.create_run(
        source_id=source, history_from=date(2025, 1, 1),
        history_to=date(2026, 8, 16), mode="cutover", requested_by="tester",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE migration_runs SET status='Reconciling' WHERE id=%s", (run,)
        )
    reconciler = Reconciler(database)
    expected = reconciler.target_snapshot(company, as_of=date(2026, 8, 16))
    assert reconciler.reconcile_counts(run, actor="tester", complete=False)
    assert reconciler.reconcile_financials(run, expected, actor="tester")
    assert database.scalar(
        "SELECT status FROM migration_runs WHERE id=%s", (run,)
    ) == "Completed"
    assert database.scalar(
        "SELECT count(*) FROM migration_reconciliation_artifacts WHERE run_id=%s",
        (run,),
    ) == 1


def test_opening_trial_balance_is_balanced_and_idempotent(pipeline_db):
    database, company = pipeline_db
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO accounts(company_id,code,name,account_type,normal_side)
               VALUES (%s,'1000','Opening Cash','Asset','Debit'),
                      (%s,'2000','Opening Equity','Equity','Credit')""",
            (company, company),
        )
        plant = connection.execute(
            """INSERT INTO plants(company_id,code,name)
               VALUES (%s,'OPEN','Opening Plant') RETURNING id""",
            (company,),
        ).fetchone()["id"]
        connection.execute(
            """INSERT INTO warehouses(company_id,plant_id,code,name)
               VALUES (%s,%s,'OPEN','Opening Warehouse')""",
            (company, plant),
        )
        connection.execute(
            """INSERT INTO items
                   (company_id,code,name,uom,valuation_method,tracks_serials,
                    tracks_batches,tracks_expiry)
               VALUES (%s,'OPEN-ITEM','Opening Item','Each','Moving Average',
                       true,true,true)""",
            (company,),
        )
    runs = MigrationRunService(database)
    source = runs.create_source(
        name="Opening Balance Source", connector_type="mock_sap",
        company_id=company, source_company_db="PIPE-OPEN",
    )
    run = runs.create_run(
        source_id=source, history_from=date(2026, 1, 1),
        history_to=date(2026, 8, 16), mode="cutover", requested_by="tester",
    )
    reconciler = Reconciler(database)
    first = reconciler.apply_opening_trial_balance(
        run, {"1000": "125", "2000": "-125"}, actor="tester"
    )
    second = reconciler.apply_opening_trial_balance(
        run, {"1000": "125", "2000": "-125"}, actor="tester"
    )
    assert first == second
    assert database.scalar(
        "SELECT count(*) FROM gl_entries WHERE posting_batch_id=%s", (first,)
    ) == 2
    with pytest.raises(DomainError, match="net to zero"):
        reconciler.apply_opening_trial_balance(
            run, {"1000": "125"}, actor="tester"
        )
    controls = {
        "OPEN-ITEM|OPEN": {
            "quantity": "2", "value": "20",
            "batches": [{"code": "B-1", "quantity": "2", "expires_on": "2027-01-01"}],
            "serials": [
                {"code": "S-1", "batch_code": "B-1"},
                {"code": "S-2", "batch_code": "B-1"},
            ],
        }
    }
    inventory_first = reconciler.apply_opening_inventory(run, controls, actor="tester")
    inventory_second = reconciler.apply_opening_inventory(run, controls, actor="tester")
    assert inventory_first == inventory_second
    assert database.scalar(
        "SELECT count(*) FROM inventory_tracking_entries WHERE ledger_entry_id IN "
        "(SELECT id FROM inventory_ledger_entries WHERE event_id=%s)",
        (inventory_first,),
    ) == 3
    with pytest.raises(DomainError, match="controls changed"):
        reconciler.apply_opening_inventory(
            run,
            {**controls, "OPEN-ITEM|OPEN": {**controls["OPEN-ITEM|OPEN"], "value": "22"}},
            actor="tester",
        )
