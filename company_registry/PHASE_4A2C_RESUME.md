# Phase 4A.2C — Safe Resume and Writer Leases

Status: **PASS / COMPLETE**  
Scope: explicit local schema-v6 Google Maps resume only. No automatic resume,
live scrape, production/shadow/canary migration, Search change, export recovery,
new finalization protocol, or outreach change is included.

## Explicit resume contract

`--resume-run-id` is separate from `--registry-run-id` and is valid only in an
authoritative Maps mode. A resume session initially opens the registry without
mutation. Before workers—and therefore before browser creation—it hashes the
requested normalized query list, mode, NEW limit, resolver/qualification
versions, and canonical export root, then compares that hash and limit with the
stored run configuration.

Only `INTERRUPTED`, or `RUNNING` with no active lease, may resume. `SUCCESS`,
`PARTIAL`, `FAILED`, and `FINALIZING` fail closed. Missing configuration,
schema mismatch, changed query/mode/limit/export root, and active ownership also
fail closed. `INTERRUPTED` is the one resumable lifecycle exception; other
terminal states remain terminal. No run is selected or resumed automatically.

## Writer lease and fencing

Fresh Maps runs atomically store a unique writer owner, expiration, and
heartbeat. Explicit resume uses `BEGIN IMMEDIATE` to validate recovery state
and acquire or take over an absent/expired lease. Concurrent attempts serialize,
so exactly one owner succeeds. A background heartbeat renews the lease while
workers and existing finalization run. Graceful `SUCCESS`, `PARTIAL`, `FAILED`,
or `INTERRUPTED` handling releases it.

Maps decision commits and lifecycle transitions carry the owner token and
verify both ownership and expiration. Thus, after expired-lease takeover, the
old process is fenced from durable writes even if it later wakes after WSL
suspend. Lease time uses UTC wall clock; an unsafe clock rollback fails closed
by retaining apparent active ownership longer rather than guessing.

## Restart reconciliation

Acquisition records a read-only checkpoint containing total decisions, the
latest durable decision ID/time, and the SQLite-derived NEW budget snapshot.
The process-local throttle is initialized from that durable committed count.

Resume intentionally schedules the complete supplied query list from the
beginning. Browser position, result pagination, scrolling state, and card index
are not recoverable. Re-scanning avoids silently skipping unprocessed results;
stable observation/decision keys turn exact already-committed results into
same-run replay without duplicate entities or NEW budget use. Changed payloads
remain distinct observations under the durable cap.

## Phase boundary

The pre-existing completion path remains unchanged for a run that finishes in
the resumed process. This phase does not recover a run already in `FINALIZING`,
reconstruct missing exports, or repair/complete publication; those are Phase
4A.2D concerns.

## Validation

Focused disposable-database tests covered committed-decision interruption,
remaining budget, replay, active/concurrent writers, expired takeover,
configuration mismatch, terminal/missing-configuration rejection, heartbeat,
graceful lease release, CLI opt-in, lifecycle/budget compatibility, and SQLite
integrity:

```text
.venv/bin/python -m unittest -q \
  tests.test_company_registry_resume \
  tests.test_company_registry_durable_budget \
  tests.test_company_registry_v6_lifecycle \
  tests.test_company_registry_authoritative_foundation \
  tests.test_maps_authoritative_integration \
  tests.test_maps_cli_validation \
  tests.test_maps_run_observability
Ran 59 tests — OK
```

All writes used temporary databases/directories and fake workers. No browser,
live scrape, production/shadow/existing-canary migration, Search operation,
historical mutation, or outreach operation occurred.
