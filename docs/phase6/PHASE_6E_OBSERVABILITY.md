# Phase 6E Per-Query Observability

Date: 2026-11-01  
Scope: durable per-query telemetry for future authoritative Maps runs and
read-only reporting for current and historical runs

## Outcome

Phase 6E adds deterministic machine-readable JSON and human-readable text
reports without changing identity resolution, NEW-budget enforcement,
qualification, CSV schemas, manifests, or resume configuration.

New schema-v8 authoritative runs durably record:

- the ordered query index/text and deterministic query geography;
- query attempts and latest completion/interruption state;
- unique stable Maps result identities, stored only as SHA-256 keys;
- whether each unique result reached detail inspection;
- whether a result was ever observed as a same-run duplicate or rejection;
- the latest result outcome;
- the query index responsible for each newly created authoritative decision;
- global and optional per-query NEW capacity.

The report is a read-only SQLite projection. It writes only below a separate
`query-observability-v1` directory.

## Durable versus unavailable counters

For schema-v8 runs:

- NEW, KNOWN, UPDATED, and review counts come from unique authoritative run
  decisions linked transactionally to their initial query index.
- Results discovered and detail pages inspected count unique stable result
  keys, not attempts. Replaying a query or seeing a URL twice does not increase
  these counts.
- Duplicate and rejected counts are durable “ever observed” flags per unique
  result. A later retry cannot erase an earlier duplicate/rejection event.
- Query state and attempt count are durable. A terminal FAILED/INTERRUPTED run
  projects any query left RUNNING as INTERRUPTED.
- Per-query remaining capacity is derived from the stored limit and committed
  NEW ledger. Global remaining capacity is derived from authoritative NEW
  decisions.

Runtime display counters remain transient and are not copied into the report.
Stable result telemetry can be lower than a scraper's raw in-memory list length
when a list item has no stable result identity; the report describes exactly
what it counts.

Historical schema-v6/v7 runs did not persist found, inspected, duplicate,
rejected, attempt, or query-state telemetry. These values are emitted as JSON
`null` and text `n/a`, never zero. Decision classifications remain recoverable
from immutable observation payloads. An original ordered query file may be
supplied to restore report row ordering, and the report labels that identity
source `caller_supplied_not_persisted`.

## Storage and transactional behavior

Schema v8 extends `discovery_run_queries` with status, attempt, and latest
start/finish fields and adds:

- `discovery_run_query_decisions`, linking each newly created decision to its
  initial query inside the same `BEGIN IMMEDIATE` decision transaction;
- `discovery_run_query_results`, keyed by `(run_id, query_index, SHA-256 result
  key)` with idempotent inspection/outcome flags.

All telemetry writes validate the current RUNNING state, writer lease owner,
and unexpired lease inside an immediate transaction. Reporting opens SQLite
with `mode=ro` and `query_only=ON`.

Phase 6E persists query rows for all new authoritative runs, including runs
without `--max-new-per-query`. The option's behavior is unchanged: an absent
per-query limit remains unlimited. Pre-6E uncapped runs without a query ledger
remain resumable through the historical compatibility path.

## Report schema

Each query entry includes:

- `query_index`, `query_text`;
- `target_country`, `target_city`, `target_location`;
- `results_discovered`, `detail_pages_inspected`;
- `new_companies_committed`, `known_records`, `duplicate_records`;
- `review_only_records`, `rejected_records`, `updated_records`;
- `per_query_budget_limit`, `per_query_budget_remaining`, `budget_exhausted`;
- `status`, `attempt_count`;
- field-group durability labels.

The top level includes the registry schema, run status, query identity source,
global budget, deterministic totals, and explicit null semantics.

Output layout:

```text
<output-dir>/query-observability-v1/<run-id>/
  query_observability.json
  query_observability.txt
```

Both files are staged, flushed, fsynced, and atomically replaced. Identical
bytes are reused. These report files are outside all production export and
manifest destinations.

## CLI

Schema-v8 run with a durable query ledger:

```bash
.venv/bin/python -m company_registry.query_observability \
  --database /path/to/schema-v8-registry.db \
  --run-id maps-example \
  --output-dir /tmp/gmaps-query-reports
```

Historical run whose original query order was not persisted:

```bash
.venv/bin/python -m company_registry.query_observability \
  --database data/local_registry/company_registry.db \
  --run-id maps-20261031-batch02-300 \
  --query-file queries_batch02.txt \
  --output-dir /tmp/gmaps-query-reports
```

## Historical Batch 02 validation

Read-only validation wrote only below `/tmp/gmaps-phase6e-validation`.
The source is schema v6, so query identity is explicitly
`caller_supplied_not_persisted` and never-persisted counters/statuses are null.

Durable totals exactly reproduce Phase 6A:

| Counter | Value |
| --- | ---: |
| Query rows from supplied original order | 30 |
| Authoritative NEW | 300 |
| Authoritative KNOWN | 1,002 |
| Review-only | 14 |
| Global budget remaining | 0 |
| Found / inspected / duplicate / rejected / status | unavailable (`null`) |

Queries 1–11 contain the exact Phase 6A per-query decision counts; queries
12–30 contain zero durable decisions. JSON SHA-256 was
`128f7659cef5294c851de47deff0d65e6cf179a82215b124f714880de6e44223`
and text SHA-256 was
`d404cd078ac33fe5d8d25426ef312f6b7d7dbbf04be4fa4e2433d894acb7a7d2`.

## Files modified

- `company_registry/query_observability.py` — durable query/result telemetry,
  read-only report projection, atomic JSON/text publisher, and CLI.
- `company_registry/schema.py` — schema-v8 query state, decision-link, and
  unique-result tables.
- `company_registry/storage.py` — schema-v8 validation and read-only v6/v7
  compatibility.
- `company_registry/service.py` — transactional decision-to-query link.
- `company_registry/maps_authoritative.py` — lease-fenced telemetry facade and
  historical resume compatibility.
- `utils/threading_controller.py` — durable query attempt/state boundaries.
- `utils/google_maps_scraper.py` — stable result discovery, inspection,
  duplicate/outcome, and rejection events.
- `maps.py` and `local_release.py` — schema-v8 operator wording.
- `tests/test_company_registry_query_observability.py` and existing schema,
  budget, CLI, local-release, and authoritative integration tests.
- `context/progress-tracker.md`, `context/code-map.md`, and
  `context/architecture.md` — implementation status and invariants.

## Tests

Focused Phase 6E/schema/budget command:

```text
.venv/bin/python -m unittest -q \
  tests.test_company_registry_query_observability \
  tests.test_company_registry_query_budget \
  tests.test_company_registry_schema
```

Result: 28/28 passed.

Complete offline command:

```text
.venv/bin/python -m unittest discover -q
```

Result: 860/860 passed in 283.660 seconds.

Coverage includes multi-query runs, KNOWN and same-run duplicates, review and
rejection outcomes, interruption/resume, per-query and global exhaustion,
decision/report consistency, idempotent unique-result counters, deterministic
JSON/text reuse, unchanged SQLite during reporting, geography, and schema-v6
historical null semantics.

## Limitations

- Historical runs cannot recover counters or status that were never persisted.
- Result discovery requires a stable non-empty identity. Non-URL continuation
  markers and similar anonymous transient items are excluded rather than
  assigned fabricated identities.
- Target geography uses the existing deterministic query parser. Unsupported
  or ambiguous locations remain null; no geocoder or network lookup is used.
- Schema migration is explicit. Runtime opening never silently migrates an
  existing registry.
- Reports describe scraping and resolution evidence, not contact quality,
  qualification, deliverability, or outreach eligibility.

## Safety verification

No live scraping, browser launch, network request, email sending, historical
reassessment, or production registry write occurred. All telemetry write tests
used temporary registries. Historical validation used read-only SQLite and
wrote reports only under `/tmp`.

The protected registry SHA-256 remains:

`a379add919c4facfae5f1065a4757be2af9f8f2c89c2ee57a7f6ba511c16fb74`

Batch 02 export hashes remain unchanged:

- discovery: `ec512011b3d4ad583bc531d59be819857a932cd4b81012d1a8fb3f81c1ae7bdf`;
- employment: `51c5abb4497d0d87853c39b530841b9a6d0a469015a6a17f48b39a478ee554d9`;
- mission: `41f0ba346660ffabe08863833b9af28e798248bc501d05145d50f607ea8b580d`.

Phase 6E stops here. No later phase has been started.
