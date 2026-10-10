# Phase 3D.3B Maps Qualification Canary

Status: **FAIL**. Exactly one authorized Maps process was executed for run
`phase3d3b-maps-20261009T173743Z`; it was not retried.

## Execution result

Preflight passed: the disposable registry existed at schema v5, integrity was
`ok`, foreign-key violations were zero, the run ID was unused, isolated paths
were Git-ignored and collision-free, production/shadow hashes matched their
baselines, and all 65 protected CSV/XLSX hashes matched.

The browser did not start. Driver initialization raised `OSError: [Errno 30]
Read-only file system` for
`/home/wsl/.local/share/undetected_chromedriver/undetected_chromedriver`.
The process exited 0, but application telemetry recorded termination `ERROR`,
one scheduled query, zero completed queries, one blocked query, zero Chrome
instances, and zero inspected cards. The durable registry run is `PARTIAL`.

Discovery counts were NEW 0, KNOWN 0, UPDATED 0, AMBIGUOUS 0, QUARANTINED 0;
companies created 0; branches created 0. Budget was committed 0, reserved 0,
remaining 3. No resolver call or resolver error occurred. There were zero
duplicate run decisions and zero duplicate Place IDs.

## Qualification and exports

No company was discovered, so there are no employment or mission assessments,
website usability findings, or location/identity conflicts to report.

All manifests verified independently:

- `new_companies_phase3d3b-maps-20261009T173743Z.csv`: 0 rows, SHA-256
  `6bfdf48d1327555ae0fc744a9b24b7d05c11caf8bf21e3597a3950b8b94749c2`,
  company IDs `[]`.
- `qualified_employment_leads_phase3d3b-maps-20261009T173743Z.csv`: 0 rows,
  SHA-256 `1cf6088448911ab0578a45c7711b9880c5b9f5f058c4e7114b43282f85d2114b`,
  company IDs `[]`.
- `qualified_mission_leads_phase3d3b-maps-20261009T173743Z.csv`: 0 rows,
  the same SHA-256 as employment, company IDs `[]`.

The eligible-only invariant holds vacuously: neither qualified export contains
REVIEW or EXCLUDED rows.

## Final integrity

SQLite integrity remained `ok` with zero foreign-key violations. Historical
company, branch, and provenance comparison found zero differences. Production
and shadow hashes remained
`ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22` and
`ee5551ef5f6a82fa5e80bce02cf16b7635b10ad325e0fcf65a1c5ac4a71a43b7`.
All 65 protected CSV/XLSX files remained unchanged, including `leads_master`
and all outreach files. Full log:
`data/phase_3d3_canary/20261009T173743Z/runtime/phase3d3b-maps-20261009T173743Z.log`.
