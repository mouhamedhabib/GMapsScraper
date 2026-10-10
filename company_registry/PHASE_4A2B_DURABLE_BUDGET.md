# Phase 4A.2B — SQLite-Enforced Durable NEW Budget

Status: **PASS / COMPLETE**  
Scope: local schema-v6 Google Maps authoritative runs only. No live scrape,
production/shadow/canary migration, Search change, resume/takeover, lease
operation, export recovery, or outreach change is included.

## Durable authority

`discovery_runs.new_company_limit` is the configured limit; `NULL` means
unlimited. The authoritative committed count is derived from
`discovery_run_decisions` rows for the run that are all of:

- `classification='NEW'`;
- `resolution_action='CREATE_COMPANY'`; and
- `requires_review=0`.

No mutable counter or budget ledger is stored. KNOWN and UPDATED companies,
new branches, review decisions, same-run replay, and rolled-back attempts do
not consume capacity.

## Transaction boundary

`RegistryService.resolve_with_metadata()` begins `BEGIN IMMEDIATE`, verifies
that the run is `RUNNING`, and checks same-run replay first. After final
resolution, a qualifying NEW decision causes the service to count committed
budget decisions and compare that count with the stored limit before applying
company, branch, identity, observation, decision, or run-summary writes.

SQLite permits one writer under `BEGIN IMMEDIATE`, so concurrent workers cannot
both claim the final slot. An exhausted attempt raises a specific budget
exception and rolls back. Maps translates only that exception to its existing
`limit` outcome; all other authoritative failures remain fail-closed. The
in-memory reservation remains a performance throttle and process telemetry,
not the capacity authority.

## Restart-safe snapshot

`get_run_budget_snapshot()` opens schema v6 and returns the configured limit,
committed NEW count, remaining capacity (`NULL` for unlimited), and exhaustion
state. It derives every value from current SQLite rows, so a newly opened
process observes committed capacity without restoring in-memory counters.
An internally inconsistent count above the configured limit fails closed.

This read API does not authorize resume. Existing run-ID reuse, lease
acquisition/takeover, and interrupted-run orchestration remain unavailable.

## Validation

Focused disposable-database tests covered final-slot races with independent
in-memory throttles, replay, non-NEW decisions, cross-run known resolution,
transaction rollback, terminal-state rejection, reopened snapshots, schema-v6
lifecycle compatibility, and existing Maps authoritative behavior:

```text
.venv/bin/python -m unittest -q \
  tests.test_company_registry_durable_budget \
  tests.test_company_registry_v6_lifecycle \
  tests.test_company_registry_authoritative_foundation \
  tests.test_maps_authoritative_integration
Ran 33 tests — OK
```

All writes used temporary databases/directories. No browser was created and no
production, shadow, existing canary, historical, Search, or outreach state was
read for migration or modified.
