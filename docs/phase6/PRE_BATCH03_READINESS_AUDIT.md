# Pre-Batch 03 Readiness Audit

Date: 2026-11-01  
Scope: read-only production-readiness review of Phase 6D durable per-query
budgeting and Phase 6E per-query observability

## Final recommendation

**NO-GO for live Batch 03.**

The budgeting and new-run observability paths pass their offline tests, and the
v6 data migrates cleanly through v7 to v8 on disposable copies. However, a
blocking backward-compatibility defect exists in Phase 6E reporting after the
active v6 registry is migrated to v8: historical runs have no v8 query ledger,
and their recoverable per-query decision counts are emitted as zero. Batch 02,
for example, reports zero per-query NEW/KNOWN/REVIEW totals after migration even
though its global authoritative NEW total remains 300.

The active registry also remains schema v6. This is an expected operational
precondition rather than data corruption, but `local_release.py` requires
schema v8 and therefore cannot start Batch 03 against the active registry until
an approved verified backup and explicit migration are performed. Migration
should wait until the reporting defect is corrected and regression-tested.

No live scraping or email sending occurred during this audit.

## Check results

| # | Check | Result | Evidence |
|---:|---|---|---|
| 1 | Schema v6 → v7 → v8 migration compatibility on a disposable active-registry copy | **PASS** | Both transitions validated with the repository schema validator. `PRAGMA integrity_check` was `ok`, foreign-key violations were 0, and logical fingerprints of every v6 table were unchanged at v7 and v8. The supported migration CLI also completed on a second disposable copy. |
| 2 | Preserve the existing 405 unique companies | **PASS** | `COUNT(DISTINCT company_id)` for non-review `NEW/CREATE_COMPANY` decisions was 405 before migration, after v7, and after v8. The registry contains 4,243 total companies including legacy imports; 405 is the authoritative NEW population across the three Maps runs. |
| 3 | Preserve historical runs and assessments | **PASS** | Four runs and 3,558 assessments were preserved. Assessment distribution remained 1,123 `maps-dual-purpose-v1` and 2,435 `maps-dual-purpose-v3-business-relevance`. Source-table row counts and SHA-256 logical fingerprints were unchanged. |
| 4 | Global and per-query limits work together | **PASS** | Phase 6D tests cover independent query caps, the global cap, concurrent commits, and database invariants. The focused 71-test suite passed. |
| 5 | Query budgets survive interruption and resume | **PASS** | Durable ledger and configuration-mismatch tests passed, including resume and crash rollback. Counts are transactionally persisted and not reset by resume. |
| 6 | Observability counters avoid retry double-counting | **PASS** for schema-v8 runs | Unique result keys and decision links remained idempotent in retry/resume tests. This does not clear the separate historical post-migration defect described below. |
| 7 | Continue after individual query exhaustion | **PASS** | Offline worker integration and query-budget tests demonstrate that the next query runs after a query reaches its cap; global exhaustion still stops the run. |
| 8 | `local_release.py` syntax matches documentation | **PASS** | `run` and `resume` accept `--query-file`, `--max-new`, `--max-new-per-query`, and `--run-id`. The constructed Maps commands forward the cap and use the correct fresh/resume run-ID option. |
| 9 | Observability CLI can generate without modifying the registry | **PASS** | Registry SHA-256 was unchanged and repeated output was deterministic (`reused: true`). The separate correctness defect for a migrated historical run is recorded as the blocking issue in check 12. |
| 10 | Existing CSV exports and manifests remain unchanged | **PASS** | All 35 CSV/manifest artifacts retained aggregate fingerprint `2f35122b8751e0413920cb5c7266e0d7595a9deaf96ba3b332c4f87efab258c0`. |
| 11 | Default qualification policy | **PASS** | `QualificationPolicy().version` is `maps-dual-purpose-v3-business-relevance`. |
| 12 | Blocking issues before live scraping | **FAIL** | The historical v8 reporting defect is blocking. The still-v6 active registry is also an unmet operational prerequisite for Phase 6D/6E activation. |

## Database integrity results

### Active protected registry (read-only)

- Path: `data/local_registry/company_registry.db`
- Schema: 6
- File SHA-256 before and after audit:
  `a379add919c4facfae5f1065a4757be2af9f8f2c89c2ee57a7f6ba511c16fb74`
- `PRAGMA integrity_check`: `ok`
- `PRAGMA foreign_key_check`: 0 rows
- Companies: 4,243
- Authoritative unique NEW companies: 405
- Historical runs: 4
- Discovery observations/decisions: 2,452 / 2,452
- Qualification assessments: 3,558
- Run-company links: 5,198

Historical Maps decision totals were intact:

| Run | NEW | KNOWN | UPDATED | REVIEW |
|---|---:|---:|---:|---:|
| `maps-20261010-first-local-01` | 5 | 302 | 2 | 0 |
| `maps-20261011-global-01` | 100 | 712 | 2 | 3 |
| `maps-20261031-batch02-300` | 300 | 1,002 | 10 | 14 |

### Disposable migration

The corrected stepwise harness created
`/tmp/pre-batch03-v6-to-v8-eb9s2j_c.db` through the SQLite backup API, then
applied the repository's v7 and v8 scripts separately.

| State | Integrity | FK violations | Authoritative NEW | Runs | Assessments |
|---|---|---:|---:|---:|---:|
| v6 source copy | `ok` | 0 | 405 | 4 | 3,558 |
| after v7 | `ok` | 0 | 405 | 4 | 3,558 |
| after v8 | `ok` | 0 | 405 | 4 | 3,558 |

All eleven pre-existing v6 tables had identical row counts and logical SHA-256
fingerprints at both migrated states. The v7 query ledger and the v8 query
decision/result tables correctly started empty because migrations do not
fabricate historical telemetry.

The supported migration CLI was independently exercised on
`/tmp/pre-batch03-official-migrate-20261101.db`; it produced schema v8 with
`integrity_check=ok`, zero foreign-key violations, 405 authoritative NEW
companies, four runs, and 3,558 assessments. It emitted a non-fatal Python
`runpy` warning because `company_registry.storage` was already imported by the
package before module execution. The migration nevertheless validated and
exited 0; this warning is an operator-noise limitation, not the blocker.

## Blocking Phase 6E finding

After the official disposable copy was migrated to v8, the Batch 02 report was
generated with its original query file. The report correctly stated:

- registry schema 8;
- query identity `caller_supplied_not_persisted`;
- 30 query rows;
- global committed NEW = 300;
- global remaining = 0.

It incorrectly stated:

- per-query NEW total = 0;
- per-query KNOWN total = 0;
- per-query REVIEW total = 0;
- first-query NEW/KNOWN/REVIEW = 0/0/0.

The same run reports 300 NEW, 1,002 KNOWN, and 14 REVIEW when read directly
from its immutable decisions. The cause is deterministic: the report falls
back to grouping historical decisions by `source_query` when no query ledger
exists, but later selects index-based counts solely because the database schema
is v8. A migrated historical run has neither query-decision links nor durable
query rows, so the recovered text-based counts are discarded. Some durability
labels are also misleading for this fallback case.

This does not modify or lose registry data. It does make Phase 6E output
unreliable immediately after the required migration of the active registry.
The issue is absent from the current automated regression suite.

## CLI verification

The documented local wrapper syntax is accepted:

```bash
.venv/bin/python local_release.py run \
  --query-file queries.txt \
  --max-new 300 \
  --max-new-per-query 20 \
  --run-id maps-example

.venv/bin/python local_release.py resume \
  --query-file queries.txt \
  --max-new 300 \
  --max-new-per-query 20 \
  --run-id maps-example
```

Fresh execution forwards `--registry-run-id maps-example`; resume forwards
`--resume-run-id maps-example`. Both forward `--max-new-per-query 20`, while
the global limit is forwarded through the supported `--limit` alias.

The current active v6 registry will be rejected by this wrapper because its
help and preflight require schema v8. Runtime opening does not silently migrate.

## Tests performed

Focused command:

```bash
.venv/bin/python -m unittest -q \
  tests.test_company_registry_migration \
  tests.test_company_registry_schema \
  tests.test_company_registry_query_budget \
  tests.test_company_registry_query_observability \
  tests.test_local_release \
  tests.test_maps_cli_validation
```

Result: **71 tests passed in 8.257 seconds**.

Complete offline command:

```bash
.venv/bin/python -m unittest discover -q
```

Result: **860 tests passed in 249.266 seconds**. A second execution with stdout
suppressed for unambiguous timing exited 0 after 249.928 wall-clock seconds.

The suite uses temporary databases and simulated drivers. No live provider or
browser session was invoked.

## Validation commands executed

The material audit commands were:

```bash
sha256sum data/local_registry/company_registry.db

.venv/bin/python -c 'import sqlite3, json; p="data/local_registry/company_registry.db"; c=sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True); c.row_factory=sqlite3.Row; print(c.execute("PRAGMA user_version").fetchone()[0]); print(c.execute("PRAGMA integrity_check").fetchone()[0]); print(len(c.execute("PRAGMA foreign_key_check").fetchall())); c.close()'

.venv/bin/python -c 'import sqlite3; src=sqlite3.connect("file:data/local_registry/company_registry.db?mode=ro&immutable=1",uri=True); dst=sqlite3.connect("/tmp/pre-batch03-official-migrate-20261101.db"); src.backup(dst); dst.close(); src.close()'

.venv/bin/python -m company_registry.storage migrate \
  --database /tmp/pre-batch03-official-migrate-20261101.db

.venv/bin/python -m company_registry.query_observability \
  --database /tmp/pre-batch03-official-migrate-20261101.db \
  --run-id maps-20261031-batch02-300 \
  --query-file queries_batch02.txt \
  --output-dir /tmp/pre-batch03-observability

.venv/bin/python local_release.py run --help

.venv/bin/python -c 'from company_registry.qualification import QualificationPolicy; print(QualificationPolicy().version)'

.venv/bin/python -m unittest -q \
  tests.test_company_registry_migration \
  tests.test_company_registry_schema \
  tests.test_company_registry_query_budget \
  tests.test_company_registry_query_observability \
  tests.test_local_release \
  tests.test_maps_cli_validation

.venv/bin/python -m unittest discover -q
```

The stepwise migration command was an inline Python harness that:

1. opened the active registry with `mode=ro&immutable=1`;
2. copied it with `sqlite3.Connection.backup()` to a `tempfile` under `/tmp`;
3. captured sorted JSON row fingerprints for every v6 table;
4. applied `MIGRATION_7`, set `user_version=7`, committed, and called
   `_validate_schema(..., schema_version=7)`;
5. applied `MIGRATION_8`, set `user_version=8`, committed, and called
   `_validate_schema(...)`; and
6. compared all source-table fingerprints and integrity checks.

Two preliminary versions of this inline harness exited before performing a
migration because of Python/shell quoting errors. They created only empty or
unmodified temporary files under `/tmp`; no protected artifact was opened for
writing.

## Export and registry safety verification

Before and after the audit:

- active-registry SHA-256:
  `a379add919c4facfae5f1065a4757be2af9f8f2c89c2ee57a7f6ba511c16fb74`;
- local export artifact count: 35;
- aggregate path/hash/size fingerprint:
  `2f35122b8751e0413920cb5c7266e0d7595a9deaf96ba3b332c4f87efab258c0`.

The observability command left the disposable migrated registry hash unchanged
at `b71f54c285965006dd95db2b5ebc98c7ab942cb2e78a0fe213020ad035245efc`.
Repeated report generation reused identical output:

- JSON SHA-256:
  `7dab9304e35a8b4dcfc1582119c21d2286830d2f8d60e944be43b319c2b4d928`;
- text SHA-256:
  `4957eaa77062d24ef43cee9fed358e858be43a97845ca7c44512b94565ae169d`.

Only this audit document was added inside the project. All database mutations
and report outputs used disposable paths under `/tmp`.

## Required disposition before GO

1. Correct historical decision-count selection for schema-v8 databases whose
   run has no durable query ledger, including accurate durability labels.
2. Add a regression test that migrates a historical v6 run to v8, supplies its
   query list, and verifies recovered per-query NEW/KNOWN/REVIEW counts plus
   unchanged registry bytes during report generation.
3. Re-run the focused and complete offline suites.
4. Create and verify an approved active-registry backup.
5. Explicitly migrate the active copy to schema v8 and repeat integrity,
   405-company, run, assessment, export-hash, and historical-report checks.

Do not begin live Batch 03 until all five items pass.
