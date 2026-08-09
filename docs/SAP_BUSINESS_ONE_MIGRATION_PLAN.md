# SAP Business One to FastERP migration and capability plan

## Final decisions

FastERP will support a one-time cutover from SAP Business One 10.0 through the
Service Layer OData v4 API. ERPNext is a behavioral reference only: FastERP
will use an independently designed PostgreSQL schema and Python domain services,
without copying ERPNext code, DocTypes, schema, fixtures, or framework patterns.

- Source: SAP Business One 10.0 Service Layer, OData v4 (`/b1s/v2`).
- Target: PostgreSQL schema `fast_erp` on the configured `DB_URL`.
- Direction: SAP Business One to FastERP only; no ongoing synchronization.
- Organisation: three company databases, with legal entity, plant, warehouse,
  tax, fiscal-year, and timezone settings discovered from the source.
- Currencies: EUR, GBP, and USD. Each company has a base currency and may transact
  in any of the three currencies.
- History: the current and previous fiscal years, plus every older open document,
  advance, balance, and referenced master needed for reconciliation.
- Identity: FastERP generates identity keys and document numbers. SAP `DocEntry`,
  `DocNum`, `CardCode`, and other keys remain unique secondary references.
- Localisation: implement only the countries and tax jurisdictions used by the
  three target companies.
- Inventory valuation: preserve the SAP valuation method per company/item. The
  first implementation may optimize for moving average only if discovery proves
  that all migrated stock uses it.
- Traceability: serial number, batch, and expiry behavior is required whenever
  any of those features appears in the source.
- Commercial scope: open quotations and RFQs are operationally migrated; closed
  ones are retained as read-only source snapshots.
- Corrections: returns, credit notes, and debit notes are required at launch.
- Controls: preparer, approver, and administrator roles, with accounting-period
  locks, are required at launch.
- Relative schedule: project launch is `T0`; production cutover is `T+7 calendar
  days`. Dates in run records are resolved from the actual `T0` timestamp.

The capacity and acceptance fixture is conservative and applies per company:

- 1,000 synthetic items;
- 3 synthetic business partners, exercising customer, supplier, and dual roles;
- 1,000 synthetic transactional or ledger rows;
- all USD/GBP/EUR source, base, and settlement currency combinations.

Across three companies, the rehearsal therefore exercises 3,000 item assignments,
9 partner assignments, and at least 3,000 transaction/ledger rows. Shared item
identities may be reused across companies, but company-specific accounting,
pricing, warehouse, tax, and valuation data remains separate.

## Implementation status (2026-08-09)

The application implementation is complete for the agreed connector/demo scope.
Migrations `0001` through `0013` are applied to the shared `fast_erp` schema.
The authoritative runtime uses `DB_URL`; SQLite remains only as an explicit
local fallback. The shared deterministic fixture records `T0=2026-08-09` and
`T+7=2026-08-16` and contains, per company, 1,000 items, three dual-role
partners, 1,000 balanced GL rows, and 1,012 valued inventory-ledger rows.

Implemented migration paths include SAP Business One Service Layer OData v4,
ERPNext/Frappe REST, CSV recovery bundles, and deterministic mocks. Transaction
application covers quotes, sales and purchase orders, delivery/receipt,
invoice, customer/supplier payment, returns, credit/debit notes, journals, and
ERPNext stock entries and reconciliations. Purchase requests, RFQs, supplier
quotations, exchange rates, cost centres, UOMs, payment terms, price books,
partner addresses, custom source fields, and serial/batch allocations are also
mapped. Opening trial balance and valued item/warehouse stock are applied
idempotently before historical transactions. Every apply runs inside the same
domain transaction as UI posting, with immutable staging, payload hashes, dependency checks,
crosswalks, secondary source references, and unsupported-object archival.

Production execution is intentionally still customer-gated: it needs real
read-only SAP endpoints/users, discovered company/localisation metadata, named
approval, source freeze confirmation, a verified backup reference, and source
financial control totals. These are operational inputs, not missing code.
Cutover also requires a prior error-free dry run with the same discovered
capability manifest, and the approver must differ from the operator.

The cutover control-total JSON has seven mandatory sections. Account values are
signed debit-minus-credit balances; the opening balance must net to zero and is
posted once at the two-year boundary. The other sections use the same shape as
the corresponding sections written by the target `snapshot` command:

```json
{
  "opening_trial_balance": {"1000": "100.00", "3000": "-100.00"},
  "opening_inventory": {"ITEM-001|MAIN": {"quantity": "5", "value": "40"}},
  "trial_balance": {},
  "ar_aging": {},
  "ap_aging": {},
  "inventory": {},
  "tax": {}
}
```

Operator commands:

```bash
.venv/bin/python scripts/migrate_postgres.py --check
.venv/bin/python -m scripts.seed_postgres --launch-date 2026-08-09
.venv/bin/python -m scripts.migrate_erp provision-company --help
.venv/bin/python -m scripts.migrate_erp register --help
.venv/bin/python -m scripts.migrate_erp run --help
.venv/bin/python -m scripts.migrate_erp snapshot --help
.venv/bin/python -m scripts.migrate_erp status
.venv/bin/python -m scripts.migrate_erp failback --help
```

For a newly provisioned target company, source registration also records the
approved target account-role codes (never credentials). The transaction phase
will not start until every required role resolves to an active migrated account:

```json
{
  "account_roles": {
    "receivable": "1100", "payable": "2000", "inventory": "1200",
    "cogs": "5000", "sales": "4000", "purchase": "6000",
    "sales_tax": "2100", "purchase_tax": "1300",
    "exchange_gain": "4900", "exchange_loss": "6900",
    "grni": "2050", "default_bank": "1000"
  }
}
```

## Independent engineering boundary

ERPNext contributes behavior requirements, not an implementation template. Its
useful contracts include partial fulfillment, returns, payment allocation,
multicurrency posting, inventory valuation, immutable ledgers, cancellation,
reposting, and reconciliation. FastERP will express those outcomes in its own
terms and architecture.

The implementation rules are:

1. Record each required outcome as a FastERP Given/When/Then contract.
2. Link the contract to a public behavioral reference or SAP scenario for
   provenance, without copying source implementation.
3. Use FastERP naming, typed relational aggregates, and Python services.
4. Do not generate the target schema from Frappe DocType JSON.
5. Do not copy ERPNext algorithms, comments, test fixtures, field collections,
   or framework-specific lifecycle hooks.
6. Validate FastERP through independently written tests and reconciliation
   invariants.

This is a clean-room-style independent reimplementation. It must not be
described as a strict legal clean room unless specification and implementation
are performed by appropriately separated teams.

## Launch behavior scope

### Required

- Draft, approval, posting, hold, close, cancellation by reversal, and amendment.
- Posted operational records and ledger entries are immutable.
- Partial delivery, receipt, invoicing, return, credit/debit, and payment by line.
- Derived fulfillment, billing, and payment status; status is not a manually
  maintained substitute for quantities or allocations.
- Configurable over-delivery, over-receipt, and over-billing tolerances.
- Customer and supplier advances with partial and multiple-document allocation.
- AR/AP outstanding balances and aging from a party ledger.
- Voucher-balanced general-ledger posting and controlled reversal/reposting.
- Company, transaction, account, and optional reporting-currency amounts.
- Realized exchange gains/losses and deterministic rounding adjustments.
- UOM conversion, price books, payment terms, tax lines, and landed costs.
- Immutable stock quantity/value ledger with item/warehouse balances.
- SAP valuation method preservation, stock counts, and a controlled backdated
  transaction policy.
- Serial, batch, and expiry traceability when present in SAP.
- Fiscal-period locks, audit events, approvals, and optimistic concurrency.
- Unique SAP crosswalks and idempotent migration replay.

### Deferred unless source discovery makes them necessary

- Manufacturing, BOMs, work orders, and subcontracting.
- Fixed assets and depreciation.
- Payroll and HR.
- Maintenance and quality-management workflows.
- POS, loyalty points, commissions, and sales teams.
- Localisations outside the three target-company jurisdictions.
- Fully configurable Frappe-style workflows or a general DocType engine.

Unsupported source objects are preserved as immutable, searchable JSONB
snapshots with their SAP identity, checksum, and migration-run provenance.

## Independent PostgreSQL model

Do not introduce a generic ERPNext-style document table. Each business aggregate
has a typed header and typed lines, using a shared column convention:

- `BIGINT GENERATED ... AS IDENTITY` primary key;
- company and internal document number;
- lifecycle state and transaction/posting dates;
- revision, version, and optional amended/reversed record references;
- created, updated, approved, posted, and cancelled audit fields;
- source references through `external_references`, not source keys as PKs.

The existing `0001_fast_erp_baseline.sql` has already been applied. All changes
are append-only:

| Migration | Purpose |
|---|---|
| `0002_document_controls.sql` | Fiscal periods, locks, sequences, roles, approvals, audit events, lifecycle fields and demo-data backfill |
| `0003_commercial_masters.sql` | Partner roles, UOM conversions, price books, payment terms, bank accounts, tax rules and item/company settings |
| `0004_logistics_documents.sql` | Deliveries, goods receipts, line fulfillment links, sales/purchase returns and stock-count documents |
| `0005_receivables_payables.sql` | AP invoices, credit/debit notes, customer/supplier payments, advances, party ledger and allocations |
| `0006_inventory_ledger.sql` | Inventory events, immutable ledger, balances, valuation state/cost layers, serials, batches and landed costs |
| `0007_accounting_kernel.sql` | Posting batches, enhanced journals/GL, currency triples, voucher links, reversals and repost records |
| `0008_migration_extensions.sql` | Source capabilities, raw snapshots, dependency plans, apply manifests and expanded reconciliation results |
| `0009_preorder_and_allocation_details.sql` | Open quotations/RFQs, accounting defaults and currency-complete customer/supplier allocations |
| `0010_accounting_defaults_and_currency_identity.sql` | Stable currency crosswalk identity and receipt, advance, adjustment, FX and rounding accounts |
| `0011_return_quantity_controls.sql` | Delivery, receipt and invoice line counters that prevent excessive returns and credits/debits |
| `0012_purchase_return_breakdown.sql` | Separate accepted/rejected return counters for correct rejected-warehouse handling |
| `0013_migration_master_idempotency.sql` | Stable natural keys for replay-safe partner addresses and price-book entries |

Applied migrations are never edited or reapplied. Each migration includes a
deterministic backfill and a verification query for pre-existing demo rows.

## Source object mapping

| Order | SAP Business One object | FastERP capability/target |
|---:|---|---|
| 1 | Company/session/fiscal metadata | Companies, fiscal periods, migration sources |
| 2 | Currencies and exchange rates | Currency and dated-rate masters |
| 3 | Chart of accounts | Company chart of accounts |
| 4 | Cost centres/distribution rules | Business units and accounting dimensions |
| 5 | Projects | Projects and posting dimensions |
| 6 | Warehouses | Plants, warehouses, and company ownership |
| 7 | Tax/VAT groups | Tax rules, codes, rates, and account mappings |
| 8 | Business partners and addresses | Partners with customer/supplier roles and addresses |
| 9 | Items, UOMs, price lists | Items, UOM conversions, company settings, and price books |
| 10 | Sales quotations | Open operational quotes; closed snapshot archive |
| 11 | Sales orders | Sales orders and lines |
| 12 | Delivery notes and returns | Deliveries, sales returns, inventory postings |
| 13 | AR invoices and credit notes | Sales invoices, credit notes, party ledger and GL |
| 14 | Incoming payments | Customer payments, advances and allocations |
| 15 | Purchase requests/RFQs/quotes | Open operational documents; closed snapshot archive |
| 16 | Purchase orders | Purchase orders and lines |
| 17 | Goods receipts and returns | Goods receipts, purchase returns and inventory postings |
| 18 | AP invoices and debit notes | Purchase invoices, debit notes, party ledger and GL |
| 19 | Outgoing payments | Supplier payments, advances and allocations |
| 20 | Inventory transactions/counts | Inventory events, counts, valuation and stock ledger |
| 21 | Journal entries | Journals, GL, currency amounts and voucher links |
| 22 | Serial/batch data | Serial, batch, expiry and traceability records |
| 23 | Attachments and UDFs | Attachments, approved mapped fields, raw snapshot fallback |

The connector discovers `$metadata` and verifies the actual entity sets, fields,
extensions, and feature-pack differences for each company before extraction.

## Connector architecture

```text
migration/
  connectors/
    base.py                    # connector protocol and capability declarations
    sap_business_one.py        # selected OData production connector
    erpnext_rest.py            # operational REST connector; no DocType/schema copy
    sap_s4_odata.py            # non-operational future stub
    sap_ecc.py                 # non-operational future stub
    csv_bundle.py              # recovery and offline fallback
    mock_sap.py                # deterministic synthetic connector
  connectors/base.py          # source-neutral records and connector protocol
  mapping.py
  staging.py
  validation.py
  apply.py                     # calls domain services, never writes ledgers directly
  transactions.py              # typed operational transaction handlers
  orchestrator.py              # discovery through gated T+7 cutover
  reconcile.py
```

The protocol exposes `test_connection`, `discover`, `count`, `extract`,
`normalize`, and `close`. Extraction returns records plus an opaque continuation
cursor. Connectors write only immutable staging data. Applying staged records
uses the same domain services and posting rules as the application UI/API.

The Business One connector supports:

- HTTPS base URL ending in `/b1s/v2`;
- `Login` and `Logout`, with the session cookie held in memory only;
- one configured `CompanyDB` per migration source;
- `$select`, `$filter`, `$expand`, and server-driven pagination;
- bounded retry for throttling and transient server failures;
- canonical payload hashes and resumable checkpoints;
- metadata/capability discovery and health checks;
- redacted logs containing no passwords, cookies, personal payloads, or secrets.

## PostgreSQL application transition

1. PostgreSQL connection pooling and the repository protocol are implemented.
2. Every PostgreSQL connection sets `search_path=fast_erp,public`; migration
   and administrative SQL schema-qualified.
3. The application facade translates remaining legacy read queries; PostgreSQL
   schema introspection powers the public API.
4. Route mutations call typed domain services for sales, purchasing, inventory,
   accounting, quotes, and RFQs.
5. A deterministic PostgreSQL-native three-company fixture replaces SQLite
   backfill for the live demo.
6. Repository, connector, posting-kernel, replay, and reconciliation tests run
   against disposable PostgreSQL schemas.
7. PostgreSQL is authoritative whenever `DB_URL` is set; local startup and API
   smoke tests have parity with the fallback UI.

Credentials stay in local/Coolify environment variables and never appear in
source, staging payloads, migration metadata, or logs.

## Relative T+7 delivery and cutover plan

This schedule is suitable for the agreed synthetic scale and connector/demo
release. A production customer cutover remains gated by source access and
successful reconciliation, even if a nominal milestone has been reached.

| Milestone | Activity and exit condition |
|---|---|
| `T0` | Freeze behavior contracts, generate the three-company synthetic manifest, capture source capability assumptions |
| `T+1` | Apply document-control/commercial migrations; PostgreSQL repositories and migration runner pass contract tests |
| `T+2` | Accounting and inventory posting kernels pass balanced-voucher, reversal, quantity, and value invariants |
| `T+3` | Order-to-cash supports partial delivery/invoice/payment, advances, returns, and credit notes |
| `T+4` | Procure-to-pay supports partial receipt/AP/payment, advances, returns, and debit notes |
| `T+5` | Connector stubs and synthetic fixture complete; all three companies and nine currency pairs extract and replay idempotently |
| `T+6` | Full rehearsal, reconciliation pack, backup/restore rehearsal, warning approval, and go/no-go review |
| `T+7` | Freeze writes, final delta extract, backup, dependency-ordered apply, reconcile, approve, switch FastERP live |
| `T+8 onward` | SAP remains read-only; monitor FastERP and retain manifests, snapshots, reports, and rollback evidence |

If a hard acceptance gate fails, cutover moves to the next approved window; the
software must not waive a financial or inventory invariant to preserve `T+7`.

## Synthetic behavior matrix

The fixture must distribute its 1,000 rows per company across, at minimum:

- draft, open, partially fulfilled, completed, held, cancelled, and amended docs;
- sales orders, deliveries, AR invoices, incoming payments, and returns;
- purchase orders, receipts, AP invoices, outgoing payments, and returns;
- multi-line and split fulfillment/allocation scenarios;
- all nine document/base currency pairs from USD, GBP, and EUR;
- advances, rounding, exchange gains/losses, and payment terms;
- moving-average, FIFO, and standard-cost examples until discovery narrows scope;
- normal, backdated, reversal, stock-count, negative-stock rejection, and repost
  cases;
- serial, batch, and expiry examples;
- duplicate page replay, changed payload hash, transient failure, resume, and
  unsupported-object archival.

The generator uses a fixed seed and writes a manifest containing expected counts,
document totals, AR/AP balances, inventory quantities/values, and trial balances.

## Migration workflow

1. **Discover** — inspect each company database, metadata, fiscal years,
   localisations, currencies, valuation methods, serial/batch use, codes, UDFs,
   counts, and dependencies.
2. **Map** — approve company, account, tax, warehouse, UOM, partner, status, and
   currency crosswalks.
3. **Extract** — load all required masters, two fiscal years, and all older open
   items into immutable JSONB staging with hashes and checkpoints.
4. **Validate** — check required values, duplicates, references, totals,
   currencies, inventory quantities/values, allocations, and balanced journals.
5. **Dry run** — apply into a disposable target and produce the reconciliation
   pack; repeat until hard errors are zero.
6. **Approve** — record operator, approver, snapshot, mapping version, cutover
   window, accepted warnings, backup, and rollback point.
7. **Cut over** — freeze SAP writes, take a final delta, back up PostgreSQL, and
   apply dependency-ordered batches.
8. **Reconcile** — compare counts, values, AR/AP, stock, tax, and trial balance by
   company and currency.
9. **Sign off** — retain SAP read-only and preserve the manifest and evidence.

## Date, identity, and replay rules

- Let `T0` be the recorded project-launch timestamp and `C = T0 + 7 calendar
  days` be the planned cutover timestamp.
- For each company, extraction starts at the first day of the fiscal year
  immediately preceding the fiscal year containing `C` and ends at the final
  cutover snapshot.
- Every open document, advance, balance, and required master older than that
  boundary is also included.
- Dates are interpreted in the source company's timezone and stored with the
  original local date plus an unambiguous timestamp where applicable.
- `DocEntry` is the stable SAP document key and `DocNum` is retained for display;
  neither becomes a FastERP primary key.
- `(source_id, source_object, source_key)` is unique and makes replay idempotent.
- A changed canonical payload hash invalidates approval and requires another dry
  run before cutover.
- Posted source documents migrate as immutable posted records. Open documents
  become operational only after their source state and balances reconcile.

## Acceptance gates

- All three company databases pass authentication and capability discovery.
- The fixed synthetic suite passes at the per-company volumes above.
- Every selected source object has an approved mapping and count baseline.
- No unresolved validation errors remain; warnings have named approval.
- Replay creates no duplicates and resumes correctly after an interrupted page.
- Customer, supplier, order, delivery/receipt, invoice, payment, return, item,
  stock-ledger, and journal counts reconcile by company.
- AR/AP aging reconciles to the open document and allocation level.
- Inventory quantity and value reconcile by company, item, warehouse, and, when
  applicable, serial/batch.
- Every voucher is balanced in base and transaction/account currency within the
  approved rounding tolerance.
- Trial balance and tax totals agree with SAP on the approved opening basis.
- Every imported record exposes its SAP secondary reference and migration run.
- Pre-cutover backup restoration and rollback are rehearsed before go-live.

## Discovery inputs required for a real SAP run

The product decisions are closed. These customer-specific facts are collected by
discovery rather than treated as design questions:

- SAP `CompanyDB` identifiers, legal names, countries, tax registrations, base
  currencies, fiscal calendars, and timezones;
- SAP patch/feature-package level and HANA versus SQL Server;
- Service Layer URLs, network route, TLS policy, and read-only users;
- plant and warehouse codes and company ownership;
- valuation method by company/item and negative-stock policy;
- serial, batch, expiry, UDF, attachment, and open-quotation/RFQ usage;
- attachment size limits and destination object storage;
- financial rounding tolerances, business approvers, and retention period.
