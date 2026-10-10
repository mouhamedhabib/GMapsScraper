# Phase 4B.3 — Database Consumer Inventory and Cutover Readiness

Status: **AUDIT COMPLETE / NO-GO for cutover**. GO only for remediation.

## Repository consumer inventory

| Consumer | Access/path dependency | Verdict |
| --- | --- | --- |
| Core storage/resolver/service/lifecycle/lease | Default production path; exact-v6 table access | Compatible |
| Historical import | Default production; dry-run copies, `--apply` writes | Compatible; operator-sensitive |
| Qualification/exports | Caller-selected v6 DB | Compatible |
| Maps authoritative session | Explicit DB; guard rejects canonical production/shadow paths | **Requires adaptation** |
| Shadow snapshot/observer | Hardcoded defaults; snapshot migrates copy | Snapshot compatible; v3 observer **blocked** |
| shadow validation | Hardcoded production path; migrates copy, then requires schema `== 4` | **Requires adaptation to v6** |
| Replay diagnostics | Hardcoded production; copies then migrates | Compatible |
| Legacy Maps/lead tools | `CSV_FILES`; no registry access | Compatible/unaffected |
| `job_search/*` | Separate `data/job_search.db` | Not a registry consumer |

No in-repository runtime requires schema v2. Direct registry SQL is confined to
`company_registry/*`; APIs require v6. Production remains healthy v2. Hashes
match accepted baselines; journal mode is `delete` with no sidecars.

## External boundary

The repository cannot verify cron jobs, scripts, notebooks, dashboards, desktop
tools, services, aliases, or copied databases elsewhere. Operator: search
approved service/cron definitions and workspaces for the canonical path; inspect
open-file owners; record owner, command, mode, version assumption and schedule;
test each on a disposable v6 copy; retire, adapt, or sign off every entry. Do
not scan unrelated private directories.

## Cutover/rollback runbook

1. Stop Maps, import, shadow, diagnostics, jobs, and external consumers. Verify
   no handles; enforce one application version.
2. If WAL is enabled, checkpoint/truncate under sole ownership, close, and
   verify sidecars. Use SQLite online backup, never a live file copy.
3. Validate/fsync backup: hash, schema 2, integrity/FKs, counts and canonical
   historical digests; rehearse restore before proceeding.
4. Migrate a same-filesystem staging copy explicitly. Require v6/11 tables,
   integrity/FKs, exact protected digests and deterministic backfills.
5. Run every consumer smoke test offline; keep writes disabled.
6. Retain v2; fsync staging/parent; atomically replace on the same filesystem.
   Start only approved v6 binaries; prevent old/alternate-path writers.
7. Recheck schema/digests/startup. Enable writes only after all gates pass.
   Before writes, rollback by stopping consumers and atomically restoring the
   verified v2 backup. After any v6 write, simple rollback is **NO-GO** without
   an approved reconciliation plan.

## Acceptance gates and blockers

GO requires: complete external inventory; both in-repository adaptations and a
v6 shadow decision; verified/restorable backup; clean migration/digests;
offline application startup; approved Maps production-path topology with
lease/export smoke; legacy CSV PASS; exclusive-writer proof; pre-write rollback
PASS. Any failure is NO-GO. No tests were run and no database was modified.
