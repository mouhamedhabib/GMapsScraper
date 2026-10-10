# Phase 4A.2A — Schema v6 and Run Lifecycle Foundation

Status: **PASS / COMPLETE**  
Scope: local Google Maps authoritative runs only. No live scrape, production or
canary migration, Search change, outreach change, resume path, lease protocol,
or durable budget enforcement is included.

## Schema v6 contract

The explicit v5-to-v6 migration rebuilds only `discovery_runs`; company,
branch, identity, observation, decision, qualification, and historical
provenance rows are not rewritten. It adds:

- `FINALIZING` to the lifecycle status constraint;
- `new_company_limit` (`NULL` explicitly means unlimited) and
  `run_config_hash` for future restart validation;
- `lease_owner`, `lease_expires_at`, and `heartbeat_at` for future exclusive
  writer ownership; and
- `updated_at` for durable lifecycle/configuration change time.

Existing v5 rows retain their status and timestamps, receive `updated_at` from
`finished_at` or `created_at`, and leave configuration/lease fields `NULL`
because those facts cannot be reconstructed safely. Migration is explicit,
transactional, repeatable, validates known schema versions, restores foreign
key enforcement, and runs integrity validation. Runtime open still rejects v5
and never migrates it.

## Lifecycle state machine

```text
RUNNING -> FINALIZING -> SUCCESS
    |          |
    |          +-----> PARTIAL
    +---------------> FAILED
    +---------------> INTERRUPTED
```

All transitions use a guarded `BEGIN IMMEDIATE` transaction. Unknown runs,
terminal-state transitions, skipped states, and concurrent state changes fail
closed. Registry decision transactions require `RUNNING`, so `FINALIZING` and
terminal runs are immutable at the decision boundary.

Maps completion enters `FINALIZING`, publishes discovery and both qualified
exports, verifies all three manifests, and only then records `SUCCESS` or
`PARTIAL`. A publication or verification failure remains `FINALIZING`; it is
not mislabeled successful. Current Maps orchestration also stores its configured
NEW limit and a hash covering normalized query content, discovery mode, limit,
resolver/qualification policy versions, and canonical export root.

## Deliberate limitations

Schema fields do not constitute recovery. This phase does not acquire or renew
leases, resume an existing run, take over a stale writer, enforce the NEW limit
inside SQLite, reconstruct exports, reconcile startup state, or provide a
production migration/rollback procedure. Existing v5 runs remain ineligible for
scrape resume because their original configuration and limit are unprovable.

## Validation

Focused tests used only temporary databases and fake/offline Maps paths:

```text
.venv/bin/python -m unittest -q \
  tests.test_company_registry_v6_lifecycle \
  tests.test_maps_authoritative_integration
Ran 15 tests — OK
```

The initial `pytest` invocation executed no tests because `pytest` is not
installed in the repository virtualenv; no dependency was installed. The
successful `unittest` run created no browser and did not migrate production,
shadow, or existing canary databases.
