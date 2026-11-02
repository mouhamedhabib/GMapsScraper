# Phase 6E.1 Historical Observability Correctness Fix

Date: 2026-11-01  
Scope: reporting-only correction for historical runs after schema-v8 migration

## Outcome and recommendation

Phase 6E.1 is **PASS**. The software blocker identified by the Pre-Batch 03
audit is resolved.

The recommendation is **GO for the approved backup, disposable rehearsal, and
explicit active-registry migration preflight**. Live Batch 03 remains an
operational **NO-GO until** the still-schema-v6 active registry is backed up,
migrated explicitly to v8, and its post-migration integrity checks pass. This
phase deliberately did not perform that protected-data operation.

## Root cause

Phase 6E selected per-query decision counts based on the database schema
version. After a v6 registry was migrated to v8, the new v8 tables existed but
historical runs had no rows in their query ledger or decision-link tables.
The report correctly fell back to caller-supplied query text, then incorrectly
selected an empty index-based count map and converted missing evidence to zero.

Schema version proves table availability; it does not prove that a particular
run was observed with Phase 6E instrumentation.

## Corrected semantics

Availability is now determined from durable evidence rather than schema version
alone:

- A caller-supplied query list restores display order only. It does not prove
  counter attribution.
- Per-query NEW/KNOWN/REVIEW/UPDATED values require a durable ledger and exact
  attribution evidence. Complete v8 decision links prove classification
  counts. An exact NEW ledger whose sum matches the global NEW total can also
  prove NEW attribution.
- Result, duplicate, rejection, status, and attempt values require complete v8
  instrumentation and an actually recorded query attempt. A measured empty
  query is reported as numeric zero; a query never measured is null.
- Incomplete historical values are JSON `null` and text
  `N/A (historical data unavailable)`.
- A configured but not-yet-attempted current v8 query uses `not_measured` and
  `N/A (not measured)`, distinct from historical unavailability.
- Global authoritative totals remain a separate durable projection and are
  never distributed across queries.
- Observation payloads, company records, and global totals are not used to
  reconstruct missing per-query attribution.

JSON now includes top-level and per-query `counter_availability` metadata plus
`historical_data_limitations`. The top-level
`global_authoritative_totals` object preserves exact NEW, KNOWN, REVIEW, and
UPDATED run totals independently from per-query availability.

## Batch 02 disposable-copy validation

The active v6 registry was copied through SQLite's backup API to
`/tmp/phase6e1-active-copy-v51k5l5w.db` and only that copy was explicitly
migrated to v8. Reporting the historical Batch 02 run with its original query
file produced:

| Field | Result |
|---|---:|
| Global NEW | 300 |
| Global KNOWN | 1,002 |
| Global REVIEW | 14 |
| Global UPDATED | 10 |
| Per-query NEW/KNOWN/REVIEW/UPDATED | `null` |
| Per-query found/inspected/duplicate/rejected/status | `null` |
| Query identity | `caller_supplied_not_persisted` |

The human report displays `N/A (historical data unavailable)` for those
fields. The disposable registry SHA-256 was unchanged before and after report
generation:

`b71f54c285965006dd95db2b5ebc98c7ab942cb2e78a0fe213020ad035245efc`

Report hashes were:

- JSON: `5686432e074f8b3471765aedf929b43c0ec1d03fef9be8b21c5a2a56bd5d1b57`;
- text: `86e3da0183aae94e0417e3f586fc89d766cce7cd0b9fcce35a6326250da0ff8d`.

## Files changed

- `company_registry/query_observability.py`
  - evidence-based per-query availability;
  - null historical counters and measured v8 zeros;
  - global authoritative totals;
  - availability/limitation metadata;
  - explicit historical and not-measured text rendering.
- `tests/test_company_registry_query_observability.py`
  - migrated-v6 regression with a preserved global NEW decision;
  - v8 measured-zero behavior;
  - interrupted/resumed v8 behavior;
  - historical and new runs in one v8 registry;
  - deterministic/read-only output and sentinel export/manifest preservation.
- `docs/phase6/PHASE_6E1_HISTORICAL_OBSERVABILITY_FIX.md`
  - this implementation and validation record.
- `context/progress-tracker.md` and `context/code-map.md`
  - current behavior and navigation notes.

No schema migration, budget enforcement, qualification policy, scraper
scheduling, export schema, or manifest behavior changed.

## Tests

Focused compatibility command:

```bash
.venv/bin/python -m unittest -q \
  tests.test_company_registry_query_observability \
  tests.test_company_registry_query_budget \
  tests.test_company_registry_migration \
  tests.test_company_registry_schema \
  tests.test_company_registry_unified_export \
  tests.test_company_registry_email_reporting \
  tests.test_local_release \
  tests.test_maps_cli_validation
```

Result: **84 tests passed in 11.084 seconds**.

Complete offline command:

```bash
.venv/bin/python -m unittest discover -q
```

Final result: **863 tests passed in 238.173 seconds** (238.747 seconds wall
time). An earlier complete run on the initial fix also passed 863/863; the
reported final result followed the last conservative mixed-evidence adjustment.
A final metadata-consistency check then passed all 8 observability tests in
3.833 seconds.

Compilation and `git diff --check` also passed.

## Safety verification

- No live scraping, network provider, browser, or email sender was invoked.
- The active registry was opened read-only and was not migrated or reassessed.
- Active-registry SHA-256 remained
  `a379add919c4facfae5f1065a4757be2af9f8f2c89c2ee57a7f6ba511c16fb74`.
- All 35 local CSV/manifest artifacts retained aggregate fingerprint
  `2f35122b8751e0413920cb5c7266e0d7595a9deaf96ba3b332c4f87efab258c0`.
- Migration tests and report validation used temporary/disposable databases.
- Report output was written only under `/tmp` or test temporary directories.

## Remaining limitations

- Missing historical telemetry cannot be recovered; the fix makes that absence
  explicit instead of estimating it.
- Caller-supplied query order is not authoritative attribution.
- A legacy ledger with no decisions and no attempts can be intrinsically
  indistinguishable from a configured but not-yet-attempted query. It remains
  null rather than being presented as a measured zero.
- The active registry still requires the separately approved backup and
  explicit migration procedure before Phase 6D/6E can be used in Batch 03.

Phase 6E.1 stops here. No live Batch 03 action or later phase was started.
