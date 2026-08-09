"""Register, inspect, dry-run, and cut over SAP B1 or ERPNext migrations."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fasterp.config import DatabaseSettings
from fasterp.database import Database
from migration.orchestrator import MigrationOrchestrator
from migration.reconcile import Reconciler
from migration.staging import MigrationRunService


def _json_file(path: str | None) -> dict | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    provision = commands.add_parser(
        "provision-company", help="Create an empty target company and fiscal periods"
    )
    provision.add_argument("--code", required=True)
    provision.add_argument("--name", required=True)
    provision.add_argument("--country", required=True)
    provision.add_argument("--currency", required=True, choices=("EUR", "GBP", "USD"))
    provision.add_argument("--timezone", required=True)
    provision.add_argument("--fiscal-year-start", type=int, default=1)
    provision.add_argument("--launch-date", type=date.fromisoformat, default=date.today())

    register = commands.add_parser("register", help="Create or update a non-secret source")
    register.add_argument("--name", required=True)
    register.add_argument(
        "--connector", required=True,
        choices=("sap_business_one_odata_v4", "erpnext_rest", "csv_bundle"),
    )
    register.add_argument("--company-code", required=True)
    register.add_argument("--base-url")
    register.add_argument("--source-company-db")
    register.add_argument("--credential-prefix")
    register.add_argument("--configuration", help="JSON object with non-secret options")

    run = commands.add_parser("run", help="Execute discovery/extract/validate or cutover")
    run.add_argument("--source", required=True)
    run.add_argument("--mode", choices=("dry_run", "cutover"), default="dry_run")
    run.add_argument("--launch-date", type=date.fromisoformat, default=date.today())
    run.add_argument("--actor", required=True)
    run.add_argument("--approver")
    run.add_argument("--page-size", type=int, default=100)
    run.add_argument("--include-unsupported", action="store_true")
    run.add_argument("--confirm-source-frozen", action="store_true")
    run.add_argument("--control-totals", help="Source financial snapshot JSON; required for cutover")
    run.add_argument("--backup-reference", help="Verified pre-cutover backup/restore reference")

    status = commands.add_parser("status", help="Show migration run status")
    status.add_argument("--run-id", type=int)

    rollback = commands.add_parser("failback", help="Record controlled external restore/failback")
    rollback.add_argument("--run-id", type=int, required=True)
    rollback.add_argument("--actor", required=True)
    rollback.add_argument("--restore-reference", required=True)
    rollback.add_argument("--confirm-traffic-stopped", action="store_true")

    snapshot = commands.add_parser("snapshot", help="Write target reconciliation snapshot")
    snapshot.add_argument("--company-code", required=True)
    snapshot.add_argument("--as-of", type=date.fromisoformat, required=True)
    snapshot.add_argument("--output", required=True)

    args = parser.parse_args()
    load_dotenv()
    with Database(DatabaseSettings.from_env()) as database:
        if args.command == "provision-company":
            if len(args.country.strip()) != 2:
                raise SystemExit("--country must be a two-letter ISO country code")
            if not 1 <= args.fiscal_year_start <= 12:
                raise SystemExit("--fiscal-year-start must be between 1 and 12")
            with database.transaction() as connection:
                company_id = connection.execute(
                    """INSERT INTO companies
                           (code,name,country_code,local_currency,reporting_currency,
                            timezone,fiscal_year_start)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (code) DO UPDATE SET
                           name=excluded.name,country_code=excluded.country_code,
                           local_currency=excluded.local_currency,
                           reporting_currency=excluded.reporting_currency,
                           timezone=excluded.timezone,
                           fiscal_year_start=excluded.fiscal_year_start,
                           active=true,updated_at=now()
                       RETURNING id""",
                    (
                        args.code, args.name, args.country.upper(), args.currency,
                        args.currency, args.timezone, args.fiscal_year_start,
                    ),
                ).fetchone()["id"]
                for year in (args.launch_date.year - 1, args.launch_date.year):
                    start = date(year, args.fiscal_year_start, 1)
                    if args.fiscal_year_start == 1:
                        end = date(year, 12, 31)
                    else:
                        end = date(year + 1, args.fiscal_year_start, 1) - timedelta(days=1)
                    connection.execute(
                        """INSERT INTO fiscal_periods
                               (company_id,code,starts_on,ends_on,status)
                           VALUES (%s,%s,%s,%s,'Open')
                           ON CONFLICT (company_id,code) DO UPDATE SET
                               starts_on=excluded.starts_on,ends_on=excluded.ends_on""",
                        (company_id, f"FY{year}", start, end),
                    )
            print(json.dumps({"company_id": company_id, "code": args.code}))
        elif args.command == "register":
            company = database.one(
                "SELECT id FROM companies WHERE code=%s AND active=true", (args.company_code,)
            )
            if not company:
                raise SystemExit(f"Active company not found: {args.company_code}")
            configuration = json.loads(args.configuration or "{}")
            source_id = MigrationRunService(database).create_source(
                name=args.name, connector_type=args.connector, company_id=company["id"],
                base_url=args.base_url, source_company_db=args.source_company_db,
                credential_env_prefix=args.credential_prefix,
                configuration=configuration,
            )
            print(json.dumps({"source_id": source_id, "name": args.name}))
        elif args.command == "run":
            result = MigrationOrchestrator(database).execute(
                source_name=args.source, launch_date=args.launch_date, actor=args.actor,
                mode=args.mode, approver=args.approver,
                source_control_totals=_json_file(args.control_totals),
                source_frozen=args.confirm_source_frozen,
                include_unsupported=args.include_unsupported, page_size=args.page_size,
                backup_reference=args.backup_reference,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "status":
            where = " WHERE run.id=%s" if args.run_id else ""
            params = (args.run_id,) if args.run_id else ()
            rows = database.rows(
                """SELECT run.id,source.name AS source,run.mode,run.status,
                          run.history_from,run.history_to,run.source_count,run.applied_count,
                          run.warning_count,run.error_count,run.created_at,run.completed_at
                     FROM migration_runs run JOIN migration_sources source ON source.id=run.source_id"""
                + where + " ORDER BY run.id DESC LIMIT 100",
                params,
            )
            print(json.dumps(rows, indent=2, default=str))
        elif args.command == "failback":
            MigrationOrchestrator(database).record_failback(
                args.run_id, actor=args.actor,
                restore_reference=args.restore_reference,
                traffic_stopped=args.confirm_traffic_stopped,
            )
            print(json.dumps({"run_id": args.run_id, "status": "Rolled Back"}))
        else:
            company = database.one(
                "SELECT id FROM companies WHERE code=%s AND active=true", (args.company_code,)
            )
            if not company:
                raise SystemExit(f"Active company not found: {args.company_code}")
            result = Reconciler(database).target_snapshot(company["id"], as_of=args.as_of)
            output = Path(args.output)
            output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(str(output.resolve()))


if __name__ == "__main__":
    main()
