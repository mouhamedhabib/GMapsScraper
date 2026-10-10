# Phase 4B.6A — Cutover Blocker Resolution and Minimal Safe Activation Plan

Date: 2026-10-10  
Status: **ANALYSIS COMPLETE / CONDITIONAL GO TO REQUEST SCOPED APPROVAL**  
Execution remains **NO-GO** until the operator decisions below are recorded.

## Consumer sign-off

| Consumer | Requirement / status |
| --- | --- |
| Storage, resolver, service, lifecycle, leases | Exact schema v6; verified |
| Maps authoritative, qualification, exports | Exact v6; verified offline. Canonical path also needs exact `authoritative` mode and explicit production opt-in |
| Historical import | Dry-run copies/migrates; `--apply` writes v6. Stop during maintenance |
| Replay diagnostics / shadow validation | Read production and migrate temporary copies; compatible, but stop during maintenance |
| Shadow observer | Requires v6; protected v3 is incompatible and optional |
| Legacy Maps/lead identity reader | CSV-backed; verified independent of SQLite |
| Search authoritative | Shared v6 core, but canonical production activation is not enabled or in roadmap; keep stopped |
| `job_search/*` | Separate `data/job_search.db`; not a registry consumer |

No in-repository production consumer requires v2. Direct registry access is
confined to `company_registry/*`; default paths exist in storage, resolver,
service, import, shadow, validation, and diagnostics. Phase 4B.5 verified the
Maps/v6 and legacy paths. Unknown external consumers remain **unverified**.

External sign-off checklist: inspect only approved service/cron definitions and
workspaces for the canonical path and aliases; list scripts, notebooks,
dashboards, desktop tools and direct SQLite readers; record owner, command,
schedule, read/write mode, schema assumptions and shutdown method; inspect
`lsof`/`fuser`; test each retained consumer on the accepted disposable v6 copy;
then mark it adapted, retired, or explicitly approved. The operator must attest
that the inventory is complete; repository inspection cannot supply that fact.

## Shadow decision

Authoritative Maps opens a shadow observer only in `shadow` mode and rejects
combining passive shadow with authoritative mode. Therefore daily authoritative
operation does not require the protected schema-v3 shadow.

Recommendation: leave `data/company_registry_shadow.db` untouched, logically
archived/inactive, and disable shadow flags for the approved workflow. Do not
create a new v6 shadow for cutover. A new v6 snapshot may be separately approved
later if passive comparison again has an operational purpose.

## Minimal two-stage runbook

1. Record external-consumer attestation, inactive-shadow decision, validation
   authority, backup retention, and post-write risk choice. Acquire one
   cooperative cutover lock; require one approved application version.
2. Stop Maps, Search, import `--apply`, shadow tools, diagnostics, schedulers,
   notebooks and all declared external readers/writers. Disable restarts.
3. Require no `lsof`/`fuser` owners. A zero-timeout `BEGIN IMMEDIATE` probe must
   succeed and roll back. Any owner or lock failure is NO-GO.
4. After readers exit, if WAL is active, require
   `PRAGMA wal_checkpoint(TRUNCATE)` busy code `0`; close SQLite normally and
   verify sidecars. Never delete WAL/SHM files. Recheck no owners.
5. Create a final SQLite online v2 backup; fsync file/parent and verify hash,
   schema 2, integrity, foreign keys, counts, historical digests, and a tested
   restore path.
6. Copy the backup to same-filesystem staging, explicitly migrate v2→v6, and
   require schema 6/11 tables, integrity `ok`, zero FK violations, exact
   protected digests, and deterministic backfills.
7. While staging is disposable, repeat repository consumer checks. Keep Search,
   shadow, imports, schedulers and browser work disabled.
8. Obtain explicit **Stage A** approval, atomically replace the canonical file,
   fsync, and enter the write-free validation window. This window begins at
   replacement and ends only with a separate signed `ENABLE V6 WRITES` decision;
   elapsed time alone never ends it.
9. During that window run only schema/integrity/FK/digest checks, production-path
   validation, legacy CSV loading, and read-only resolver preview. Do **not**
   construct a fresh `AuthoritativeMapsSession` or launch authoritative CLI:
   session construction immediately inserts a run.
10. If any gate fails before a v6 write, stop/close all handles and atomically
    restore the verified v2 backup; repeat all v2 checks. Otherwise obtain
    separate **Stage B** approval before the first v6 write.

## Recovery scope and operator decisions

Pre-write rollback is byte-exact and rehearsed. A write-free Stage A cutover is
therefore supportable with retained backups. It does not solve post-write
recovery: Phase 4B.5 proved blind v2 restoration loses new v6 data.

Comprehensive reconciliation may be deferred for a small, single-writer local
activation only if the operator explicitly accepts that the first v6 write is
the no-downgrade point, accepts possible loss since the latest v6 backup, runs
one Maps run at a time, creates/verifies a v6 baseline backup before Stage B and
another after every successful run, and uses durable resume/forward recovery
before considering restore. If zero-loss recovery or return to v2 after writes
is required, comprehensive delta reconciliation is a prerequisite and Stage B
is NO-GO.

Required decisions: external inventory attestation; shadow inactive; Stage A
cutover approval; validation checklist owner; v2/v6 backup retention; whether
post-write residual risk is accepted; and separate Stage B enable-writes
approval. Outreach remains outside this approval.

Recommendation: **CONDITIONAL GO to request Stage A production cutover approval
for migration plus write-free validation only. NO-GO for Stage B writes until
the operator records all decisions and either accepts the bounded residual risk
or requires comprehensive reconciliation first.**
