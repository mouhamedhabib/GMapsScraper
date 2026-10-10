# Phase 4B.5 — Local Cutover Safety Rehearsal

Date: 2026-10-10  
Result: **REHEARSAL PASS / PRODUCTION CUTOVER NO-GO**

Accepted evidence root:
`data/phase_4b5_cutover_safety/20261010T142501Z_r3/`.
`reports/rehearsal.json` contains paths, hashes, counts, digests, stable IDs,
and gate results. Two earlier disposable roots are incomplete and excluded:
the first exposed an incorrect comparison column; `_r2` correctly showed that
whole-table equality is invalid after an intentional new write. The accepted
check requires every baseline row to remain byte-for-value unchanged while
allowing audited additions.

## Consumer inventory

| Consumer | v6 result |
| --- | --- |
| Storage, resolver, service, lifecycle, leases | Compatible; exact-v6 APIs |
| Historical import and replay diagnostics | Compatible; explicit/copy migration, operator-sensitive |
| Qualification and exports | Compatible |
| Maps authoritative | PASS offline through explicit validated opt-in |
| Shadow snapshot/validation | v6 copies compatible; protected v3 rejected |
| Legacy Maps/lead identity reader | PASS; CSV-backed, 16,731 identities |
| Search authoritative | Code uses v6 APIs; not exercised in this Maps-only phase |
| `job_search/*` | Separate `data/job_search.db`; not a registry consumer |

External applications remain unverified. Operator checklist: inventory approved
services, cron/systemd tasks, scripts, notebooks, dashboards and desktop tools;
search their configured paths; record owner/version/read-write mode/schedule;
inspect `lsof`/`fuser`; test each against the disposable v6 copy; then retire,
adapt, or obtain sign-off. Do not scan unrelated private directories.

## Freeze, WAL, cutover, and rollback results

- SQLite online backup:
  `database/verified_production_v2_backup.db`, SHA-256
  `bdb35f9912fdbfaebd28525a85dcd8a9d7051ac4819170edf26c8e7af75386e8`.
  Schema 2, integrity `ok`, zero FK violations, and all five baseline
  counts/digests matched the read-only source.
- A held `BEGIN IMMEDIATE` caused the zero-timeout freeze probe to reject with
  `database is locked`. A second cutover lock owner was also rejected; after
  shutdown, `fuser` found zero owners.
- WAL was created by SQLite. `wal_checkpoint(TRUNCATE)` returned busy
  `(1,1,0)` while a reader pinned it, then clean `(0,0,0)` after closure.
  SQLite removed sidecars after the clean checkpoint/journal transition; none
  were manually deleted.
- Migration reached schema 6/11 tables with integrity `ok`, zero FK violations,
  and exact protected counts/digests. Same-filesystem replacement and parent
  fsync were rehearsed.
- Canonical-path validation was simulated by patching only the path constant to
  the disposable v6 target: default denial and explicit exact-authoritative
  acceptance both passed. No production activation occurred.
- Pre-write rollback restored the v2 backup byte-for-byte and preserved all
  digests.
- One offline Maps NEW decision/export was committed without Chrome. Blind v2
  restoration then demonstrably lost company
  `548f43a4-85f0-55d1-9ff3-7f0b1839058b`.
- Forward recovery from v2→v6 plus audited observation replay reproduced the
  company, branch, observation and decision IDs, logical identity digest, and
  every historical baseline row. This proves the single-record mechanism, not
  a general lossless rollback.

Focused tests: **6/6 passed** (migration/restore, legacy CSV, activation gate,
CLI opt-in, baseline separation). No browser, network, Search, outreach, or
protected write occurred. Production/shadow hashes remain `ab28c683…251c22` /
`ee5551ef…a43b7`. A final read-only sweep found integrity `ok` and zero foreign-
key violations on all seven accepted disposable databases.

## Acceptance gates

| Gate | Decision |
| --- | --- |
| Repository consumers | GO |
| External consumers | **NO-GO: inventory/sign-off absent** |
| Write freeze/exclusive operator | Rehearsal GO; production procedure unapproved |
| WAL handling | GO when checkpoint is clean; any busy result is NO-GO |
| Backup/migration/integrity/digests | GO |
| Activation validation/offline Maps/legacy CSV | GO |
| Pre-write rollback | GO |
| Post-write recovery | **NO-GO: only one deterministic NEW replay verified** |

Production recommendation: **NO-GO** until external consumers are signed off,
old binaries are disabled, the freeze/WAL procedure is approved, the v3 shadow
decision is complete, and a comprehensive post-write delta capture/replay or
forward-recovery procedure covers updates, branches, reviews, qualification,
and exports.
