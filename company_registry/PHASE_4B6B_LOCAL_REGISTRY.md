# Phase 4B.6B — Independent Local Registry Preparation

Date: 2026-10-10  
Status: **PASS / GO for Phase 4C Local Release**

## Permanent artifacts

| Purpose | Path |
| --- | --- |
| Daily schema-v6 registry | `data/local_registry/company_registry.db` |
| Verified initial schema-v2 backup | `data/local_registry/backups/source_v2_20261010T144100Z.db` |
| Machine-readable verification | `data/local_registry/validation.json` |

The target directory was absent before preparation. The backup was created with
SQLite's online backup API from the protected read-only source; only the closed
independent copy was migrated through the supported v2→v6 chain. No canary or
temporary database was reused. The files are on WSL `ext4`, not `/mnt`.

## Verification

The registry is schema 6 with 11 tables, integrity `ok`, zero foreign-key
violations, journal mode `delete`, and SHA-256
`70e6dd7e667708e1040b9bdd340c61c44e122b2c17df11d73fee014d1c212612`.
It closed/reopened successfully with no journal/WAL/SHM residue.

| Historical set | Count | Digest preserved |
| --- | ---: | --- |
| Companies | 3,838 | Yes |
| Branches | 5,297 | Yes |
| Company identities | 9,049 | Yes |
| Historical provenance | 18,623 | Yes |
| Run-company summaries | 3,838 | Yes |

Migration deterministically created 18,696 branch identities. Discovery
observations, decisions, reviews, and qualification assessments are all zero;
there are no new companies. Directory permissions are `0700`; database,
backup, and validation files are `0600`. The protected production/shadow hashes
remain unchanged.

Offline Maps path validation accepted the independent database without
`--allow-production-registry`. A read-only preview matched an existing place as
`KNOWN/MATCH_ONLY` without committing anything; qualification and the 16,731-
identity legacy CSV index loaded successfully. Shadow mode is unnecessary.
Focused regression tests passed **4/4**.

## Phase 4C handoff

Use a new run ID for each daily run:

```text
--discovery-mode authoritative
--registry-database data/local_registry/company_registry.db
--registry-run-id <unique-run-id>
--authoritative-export-dir data/local_registry/exports
```

Do not pass `--allow-production-registry` or a shadow flag. Explicit resume uses
`--resume-run-id <existing-run-id>` instead of `--registry-run-id`.

Successful Maps finalization produces three CSVs plus manifests:

- `new_companies_<run-id>.csv`
- `qualified_employment_leads_<run-id>.csv`
- `qualified_mission_leads_<run-id>.csv`

## Local backup and restore

Run one Maps writer at a time. Close it cleanly and create a uniquely named
backup before the first release run and after every successful run. Use the
SQLite online backup API (Python `source.backup(destination)` or SQLite CLI
`.backup`), then close/reopen the backup and require schema 6, integrity `ok`,
zero FK violations, a SHA-256 record, and mode `0600`. This API captures a
consistent committed snapshot even if the database uses WAL; never copy only
the live main file or delete WAL/SHM files.

To restore: stop all readers/writers, validate the selected backup, restore it
to a same-filesystem temporary file with SQLite backup/restore, revalidate and
fsync it, then atomically replace the independent registry and fsync the parent
directory. Reopen and repeat schema/integrity/FK checks before Maps starts. The
retained initial source backup is schema v2, so a restore from it must be
migrated and validated to v6 in the temporary file before installation; never
point the runtime directly at that v2 backup.

Limitations: this local registry diverges from the protected v2 source after its
first write; external consumers remain unverified unless explicitly pointed at
this path; only one Maps writer/run should operate at once; general post-backup
reconciliation is not implemented; Search, outreach, and production activation
remain out of scope. Eligibility exports are not send authorization.

Recommendation: **GO for Phase 4C Local Release using this path, subject to a
fresh verified schema-v6 backup and explicit approval before the first live
Maps run.**
