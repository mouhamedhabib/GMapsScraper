# Phase 4B.2 — Disposable Migration Rehearsal and CLI Interruption Fix

Status: **PASS / COMPLETE**. Decision: **GO** to request approval for the next
offline production-readiness phase; **NO-GO** for production migration or
activation.

## Disposable evidence

Root: `data/phase_4b2_migration_rehearsal/20261010T140116Z/`

| Purpose | Relative path |
| --- | --- |
| Verified v2 backup | `database/production_v2_backup.db` |
| Preserved v6 result | `database/migration_verified_v6.db` |
| Offline functionality copy | `database/functionality_smoke_v6.db` |
| Failure/restore copy | `database/failure_restore.db` |
| Smoke exports | `exports/` |

Production/shadow hashes remained
`ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22` /
`ee5551ef5f6a82fa5e80bce02cf16b7635b10ad325e0fcf65a1c5ac4a71a43b7`.
SQLite online backup hash was
`bdb35f9912fdbfaebd28525a85dcd8a9d7051ac4819170edf26c8e7af75386e8`.

## Migration and rollback

Backup validation: schema 2, integrity `ok`, zero foreign-key violations.
Supported migration produced schema 6, 11 tables, integrity `ok`, zero
violations, and 18,696 backfilled branch identities. Counts stayed companies
3,838; branches 5,297; company identities 9,049; provenance 18,623; run
summaries 3,838. Canonical digests for all five sets matched before/after.

The functionality copy committed one offline NEW decision, one assessment, and
finished `SUCCESS`; discovery/employment/mission exports contained 1/1/0 rows.

A second v2 copy was deliberately given an orphaned branch. Migration failed
foreign-key validation after reaching intermediate schema 5 (nine violations).
It was atomically replaced from the backup. Restored hash matched byte-for-byte;
schema returned to 2, integrity was `ok`, violations zero, counts and provenance
digest exact. Rollback is therefore rehearsed, not assumed.

## Compatibility

| Reader | Result |
| --- | --- |
| Current registry/discovery/qualification/export | PASS on v6 |
| Legacy CSV `KnownCompanies` | PASS; independent of SQLite version |
| Protected schema-v3 shadow | BLOCKED from v6 API; unchanged |
| External/uninventoried readers | BLOCKED pending inventory |

## CLI fix and tests

`GMapsScraper.export_maps_csv()` now returns no export only for an explicitly
`INTERRUPTED` authoritative result; missing metadata otherwise fails closed.
`main()` reports interruption without paths or artifacts. SIGINT/SIGTERM share
the verified clean-stop path; success reporting is unchanged. Durable
INTERRUPTED state, lease release, and resume coverage passed.

Focused offline run: **57/57 tests passed** across migration/failure restore,
historical preservation, legacy reading, v6 lifecycle, qualification/export,
resume/lease, CLI signals, and success reporting. No browser, network, Search,
outreach, protected migration, or historical mutation occurred.
