# Phase 4B.4 — Registry Compatibility Fixes

Date: 2026-10-10  
Result: **PASS for implementation; NO-GO for production cutover**

## Implemented compatibility boundary

Maps still rejects the canonical production registry by default. A future
operator must select exact `authoritative` mode, explicitly pass
`--allow-production-registry`, and name the canonical path. Before any session
or run write, validation requires an existing complete schema-v6 database,
`PRAGMA integrity_check = ok`, and zero foreign-key violations. The opt-in is
rejected for canary/disposable paths, never migrates a database, and does not
bypass configuration, resume, lease, lifecycle, or export validation.

The current protected schema-v2 production registry therefore remains rejected.
Tests exercised the canonical-path branch only by patching its identity to
temporary v2/v6 files; no production activation occurred.

## Shadow compatibility

| Registry | Result |
| --- | --- |
| Protected schema-v3 shadow | Rejected before report-directory or database writes, with explicit replacement/no-auto-migration guidance |
| Disposable schema-v6 shadow | Supported by `ShadowObserver` |
| Future migrated schema-v6 production | Maps-compatible only through the explicit validated opt-in |

Offline shadow validation now compares against `SCHEMA_VERSION` (6), removing
the stale v4 pass condition. Runtime opening still never upgrades an old shadow.

## Regression evidence

The project virtual environment passed **58/58 focused offline tests** covering
production default denial, v2 rejection without mutation, disposable v6 opt-in,
CLI intent validation, v3 shadow rejection, v6 shadow operation, legacy
Maps/CSV behavior, lifecycle, leases, and resume. An earlier system-Python
invocation was invalid because Selenium was absent: 14 tests ran and four
modules failed import; it is not acceptance evidence.

Protected SHA-256 values remained:

- production: `ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22`
- shadow: `ee5551ef5f6a82fa5e80bce02cf16b7635b10ad325e0fcf65a1c5ac4a71a43b7`

## Remaining cutover blockers

- External scripts, notebooks, dashboards, and other consumers remain
  uninventoried and must be verified by the operator.
- Restoring the v2 backup is safe only before any new v6 production write.
  Afterward, rollback requires an approved reconciliation/recovery procedure.
- Cutover requires a write freeze, WAL checkpoint/sidecar handling, exclusive
  prevention of old-schema writers, verified backup, atomic replacement where
  supported, and smoke/rollback gates from Phase 4B.3.
- The protected v3 shadow needs a separately approved replacement snapshot.
- Production migration and activation still require explicit approval.

Recommendation: **NO-GO** for cutover; **GO** only for a separately approved,
write-frozen cutover preflight after every blocker above is closed.
