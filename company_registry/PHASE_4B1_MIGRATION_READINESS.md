# Phase 4B.1 — Local Migration Readiness and CLI Interruption Audit

Status: **AUDIT COMPLETE**. **GO** for a disposable rehearsal; **NO-GO** for
production change/activation.

## Baseline and compatibility

Read-only checks reconfirmed production schema v2, integrity `ok`, zero foreign-
key violations, and SHA-256 `ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22`.
Shadow remains clean schema v3; a fresh backup is required.

| Consumer | Contract | Result |
| --- | --- | --- |
| Explicit migrator | Known v1–v6 | v2→v6 supported; each version commits separately |
| Registry API/import/resolver/lifecycle | Exact v6 | Rejects v2/v3; never auto-migrates |
| Authoritative Maps/Search | Exact v6 | Migrated copy works; Maps still rejects production/shadow paths |
| Shadow snapshot | Versioned source | Migrates its copy; existing v3 shadow cannot serve v6 API |
| Legacy identity reader | CSV | SQLite-independent; old/sparse CSV tolerated |
| Qualification/exports | v6 | Preserves `company_id`; publishes committed, non-review NEW only |
| Lead/outreach | CSV | Still drops `company_id`; not send-safe |
| External/older readers | Unknown | Inventory and acceptance remain required |

Migrations backfill deterministic branch identities and run decisions, add
qualification, then rebuild only `discovery_runs`; new configuration/lease
fields are NULL. Existing IDs, `LEGACY_UNKNOWN`, null first-seen values, source
links, and provenance are preserved by tests and copy evidence.
Risks: the whole chain is not atomic (failure may leave v3/v4/v5); the CLI has
no backup, `integrity_check`, cutover, or reader gate. Recovery must replace
from an immutable backup, never downgrade in place. Production activation and
outreach suppression remain blockers.

## CLI KeyError

The exact missing key is `authoritative_export`. SIGINT yields a valid
`INTERRUPTED` authoritative result, releases the lease, and intentionally skips
finalization/export. `main()` then calls `export_maps_csv()`, which directly
indexes the absent key. SIGTERM and any graceful authoritative `INTERRUPTED`
path are affected; the programmatic API is not. Other terminal paths either
raise or finalize PARTIAL exports.

Smallest safe fix: return no export only for explicit `INTERRUPTED`; make
`main()` report “interrupted; no export finalized”; retain fail-closed missing-
key behavior otherwise. Tests: SIGINT/SIGTERM, durable state/lease release, no
traceback or partial export, unchanged success printing, and non-interrupted
missing-key failure.

## Next-phase acceptance

1. Quiesce writers; verify hashes, v2, free space, integrity/FKs, counts/digests.
2. Create/fsync a new SQLite online backup; validate it exactly against source.
3. Migrate only its disposable copy; require v6, 11 tables, integrity/FKs, and
   idempotency.
4. Pass focused migration, API/resolver, legacy-reader, qualification/export,
   and inventoried external-reader tests.
5. Match canonical digests for companies, branches, identities, provenance, and
   run summaries; verify deterministic backfills.
6. Restore the v2 backup to another disposable path and revalidate readers;
   document atomic cutover/stop conditions.
7. Request explicit approval before touching `data/company_registry.db`.

Validation: 27/27 focused offline tests passed; no full suite or protected-data
write/migration occurred.
