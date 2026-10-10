# Phase 4A.2D — Export Recovery and Verified Finalization

Status: **PASS / COMPLETE**  
Scope: local schema-v6 Google Maps authoritative finalization only. No live
scrape, production/shadow migration, Search change, outreach operation, or
Phase 4A.2E work is included.

## Verified publication contract

Maps completion persists `FINALIZING` before qualification or file publication.
Discovery, employment, and mission CSVs are deterministic projections of
committed SQLite decisions/assessments. Each manifest records the CSV hash, row
count, ordered provenance IDs, and ordered company IDs. `verify_run_exports()`
checks those values against fresh SQLite projections, not merely against the
CSV itself. Duplicate company IDs, stale but internally consistent files,
wrong run/purpose/policy metadata, and missing or corrupt artifacts fail.

Only after all three projections verify may the owned lease transition
`FINALIZING -> SUCCESS`. An exception leaves `FINALIZING`, where decision
writes remain forbidden.

## Explicit recovery and fencing

`recover_finalizing_run()` is an explicit export-only operation. It accepts the
original query list, mode, NEW limit, database, and canonical export root;
recomputes the stored configuration hash; and acquires a FINALIZING-only writer
lease under `BEGIN IMMEDIATE`. Active owners reject competitors. An expired
owner may be taken over only after configuration validation.

Recovery idempotently reconstructs qualification from committed data, preserves
each artifact that already matches SQLite, replaces only invalid publications,
verifies the complete set, and writes `SUCCESS` last. It never invokes browser
or discovery code and never creates a discovery decision. SUCCESS cannot be
recovered again; `verify_finalized_run()` is the separate read-only verification
entry point.

## Filesystem protocol

CSV and manifest data are staged in the destination directory, flushed, and
file-`fsync`ed. CSV replacement occurs first; the manifest is the commit marker
and is replaced last. The containing directory is `fsync`ed after each rename
where the platform supports directory descriptors. Temp debris is ignored and
later retries rebuild from SQLite.

Atomic rename is guaranteed only within one filesystem. WSL Linux filesystems
normally implement rename and directory `fsync`; `/mnt/*` DrvFS, network,
removable, or host-backed filesystems may weaken crash/power-loss guarantees or
reject directory `fsync`. Unsupported directory `fsync` is tolerated, so these
mounts still have atomic replacement but require an environment-specific
durability rehearsal before operational use.

## Boundaries

Recovery relies on `FINALIZING` as the durable assertion that discovery work
finished; it does not restore browser position or resume scraping. Lease expiry
uses wall-clock timestamps and therefore retains the documented suspend/clock
risk. This phase does not publish to downstream outreach, repair terminal run
history, or add production rollout tooling.

## Validation

Focused validation used disposable schema-v6 databases and export directories:

```text
.venv/bin/python -m unittest -q \
  tests.test_company_registry_export_recovery \
  tests.test_company_registry_v6_lifecycle \
  tests.test_company_registry_durable_budget \
  tests.test_company_registry_resume \
  tests.test_company_registry_qualification \
  tests.test_company_registry_authoritative_foundation \
  tests.test_maps_authoritative_integration
Ran 56 tests — OK
```

No browser was created and no production, shadow, canary, historical, Search,
or outreach state was migrated or modified.
