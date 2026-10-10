# Phase 3C.1 offline Maps authoritative integration

Maps has four mutually exclusive discovery modes: `legacy` (default),
`shadow`, `authoritative-canary`, and `authoritative`. The old
`--company-registry-shadow` flag remains a compatible alias for `shadow`.

Authoritative modes require an explicitly selected existing schema-v4 database.
They refuse `data/company_registry.db`, `data/company_registry_shadow.db`,
outdated/missing registries, and legacy production export directories. They do
not create or migrate databases.

Example for offline/mock validation only:

```bash
.venv/bin/python maps.py \
  --discovery-mode authoritative-canary \
  --registry-database /tmp/gmaps-registry-v4.db \
  --authoritative-export-dir /tmp/gmaps-authoritative-exports \
  --registry-run-id offline-validation-001 \
  --limit 5
```

The run ID is durable, but Phase 3C.1 does not support resuming it. Reusing an
existing run ID fails before browser work begins.

For a possible NEW company, capacity is reserved before final resolution. The
resolver re-evaluates and commits in one SQLite transaction. Capacity is
committed only when that transaction creates a `NEW/CREATE_COMPANY` decision;
KNOWN, UPDATED, CREATE_BRANCH, review-only decisions, retries, and failed
transactions release or consume no slot.

SQLite is the only authoritative persistence point. At run completion,
`new_companies_<run>.csv` is reconstructed from committed decisions and
published with a manifest commit marker. New branches and review-only records
are never in that export. Publication failure does not remove SQLite decisions;
rerun `export_new_companies()` for the same run to recover.
