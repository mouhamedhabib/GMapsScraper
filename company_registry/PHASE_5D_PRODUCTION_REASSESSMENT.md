# Phase 5D Controlled v3 Production Reassessment

## Verdict

**PASS.** The active local schema-v6 registry was reassessed under
`maps-dual-purpose-v3-business-relevance` using the established immutable,
policy-versioned assessment lifecycle. Historical assessments and exports were
preserved, all new manifests verify against SQLite, and the current default for
future authoritative local Maps completions is v3. No scraping, browser,
network, email, Batch 02, production-v2, or shadow-v3 write occurred.

## 1. Target and isolation boundary

- Targeted registry:
  `/home/wsl/GitHub/GMapsScraper/data/local_registry/company_registry.db`
- Schema: 6
- Protected and unchanged:
  `/home/wsl/GitHub/GMapsScraper/data/company_registry.db` and
  `/home/wsl/GitHub/GMapsScraper/data/company_registry_shadow.db`
- Reassessed runs:
  `maps-20261010-first-local-01` and `maps-20261011-global-01`
- Both runs were terminal `SUCCESS`; no registry lease or file-owning scraper
  process was present before the backup or reassessment.

The lifecycle used `assess_run()`, whose `BEGIN IMMEDIATE` transaction and
stable `(decision, policy)` assessment identity preserve prior versions and
make retries idempotent. Three unresolved, review-required global decisions
had no company identity and were skipped under the existing lifecycle rule.

## 2. Preflight and backup

The pre-write registry SHA-256 was
`68662831e6448c8b027589b7bbfb85ab70032e473df12f418a87126c61c44a35`.
Preflight returned SQLite integrity `ok`, zero foreign-key violations, schema
6, no active leases, and only 1,123 v1 qualification assessments.

The established SQLite online-backup API created:

- Database:
  `data/local_registry/backups/registry_phase5d-pre-v3-reassessment_20261031T122839841334Z.db`
- Verification record:
  `data/local_registry/backups/registry_phase5d-pre-v3-reassessment_20261031T122839841334Z.verification.json`
- SHA-256:
  `2589dd8a8778fc3271b0eb2e10451ff8b75b00a25804a5b334ef911b99e7c476`
- Size: 68,304,896 bytes
- Reopened verification: schema 6, integrity `ok`, foreign-key violations 0

The full preflight inventory is at
`data/phase_5d_production_reassessment/20261031T122832Z/evidence/preflight.json`.

## 3. Commands executed

Key commands and lifecycle calls were:

```text
fuser data/local_registry/company_registry.db
PRAGMA user_version; PRAGMA integrity_check; PRAGMA foreign_key_check;
local_release.create_verified_backup(..., label="phase5d-pre-v3-reassessment")
assess_run(database, run_id, policy=DEFAULT_QUALIFICATION_POLICY)
export_qualified_leads(..., purpose="employment"|"mission", policy_version=v3)
export_review_queue(..., purpose="employment"|"mission", policy_version=v3)
verify_run_export(...); verify_review_export(...); verify_export_manifest(...)
PYTHONPATH=/home/wsl/GitHub/scrapingDimaV/.venv/lib/python3.12/site-packages \
  .venv/bin/python -m pytest -q
```

The project `.venv` did not itself contain pytest. The successful offline test
command supplied pytest read-only from an existing local environment while
retaining the project interpreter and project dependencies. Nothing was
installed or downloaded.

## 4. Assessments before and after

| Policy | Before | After | Preserved/added |
| --- | ---: | ---: | ---: |
| `maps-dual-purpose-v1` | 1,123 | 1,123 | unchanged |
| `maps-dual-purpose-v3-business-relevance` | 0 | 1,123 | +1,123 |
| Total | 1,123 | 2,246 | +1,123 |

Initial v3 inserts were 309 for the first-local run and 814 for the global run;
the global run skipped its three unresolved decisions. An immediate retry
inserted 0, reused 309 and 814 respectively, and again skipped only those same
three unresolved decisions. No duplicate `(decision, policy)` assessments
exist.

For the 105 committed, non-review `NEW/CREATE_COMPANY` companies:

| Purpose | Expected ELIGIBLE | Actual ELIGIBLE | Expected REVIEW | Actual REVIEW | Expected EXCLUDED | Actual EXCLUDED |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Employment | 62 | 62 | 16 | 16 | 27 | 27 |
| Mission | 60 | 60 | 40 | 40 | 5 | 5 |

There was no discrepancy from the Phase 5B/5C verification baseline.

## 5. Versioned exports

All outputs are under the new, policy- and execution-versioned root:

`data/local_registry/exports/policies/maps-dual-purpose-v3-business-relevance/20261031T122832Z/`

| Run | Employment qualified | Mission qualified | Employment review | Mission review |
| --- | ---: | ---: | ---: | ---: |
| `maps-20261010-first-local-01` | 4 | 4 | 0 | 1 |
| `maps-20261011-global-01` | 58 | 56 | 16 | 39 |
| **Total** | **62** | **60** | **16** | **40** |

Each run directory contains four CSV files and four corresponding manifests.
All eight manifests passed file-hash, row-count, ordered assessment/company ID,
policy, run, purpose, eligibility, uniqueness, and exact SQLite-projection
verification. Cross-purpose membership was verified independently; no
cross-purpose deduplication was applied.

The export root was required not to exist before publication. A future retry
must use another unique execution directory; this preserves the existing
manifest-last export implementation without overwriting this promotion or any
historical output.

## 6. Preservation and postflight verification

- SQLite integrity: `ok`
- Foreign-key violations: 0
- Active/unresolved writer leases: 0
- Non-qualification table digests changed: 0
- Company, branch, identity, observation, decision, and run rows: unchanged
- Historical v1 assessment digest: unchanged
- Historical export inventory: all 12 pre-existing files retained identical
  SHA-256, size, and modification time; all historical manifests still verify
- Protected production-v2 and shadow-v3 hashes, sizes, and mtimes: unchanged
- New export company duplicates: 0 in every CSV
- Active registry after promotion:
  SHA-256 `117c930c437dd07da405b90fc85970c078d75afdc663e3e22340dad483f7f7a0`,
  size 69,922,816 bytes
- The regression suite did not change the postflight registry or export hashes

Evidence is retained at:

- `data/phase_5d_production_reassessment/20261031T122832Z/evidence/backup.json`
- `data/phase_5d_production_reassessment/20261031T122832Z/evidence/reassessment_and_exports.json`
- `data/phase_5d_production_reassessment/20261031T122832Z/evidence/postflight.json`
- `data/phase_5d_production_reassessment/20261031T122832Z/evidence/regression_and_final_verification.json`

## 7. Regression results

The complete offline suite passed:

- Tests: 835 passed, 0 failed, 0 skipped
- Subtests: 345 passed
- Duration: 289.02 seconds

The earlier direct `.venv/bin/python -m pytest -q` attempt stopped before test
collection because pytest was absent from that environment. It was a harness
availability issue, not a product test failure, and made no registry/export
change.

## 8. Future-run policy and remaining risks

`DEFAULT_QUALIFICATION_POLICY.version`, the default qualified/review export
policy arguments, and `AuthoritativeMapsSession.complete()` all resolve to
`maps-dual-purpose-v3-business-relevance`. Future local authoritative Maps runs
therefore assess and label exports with v3 automatically; no new run was
started during this phase.

The remaining operational requirement is to allocate a new policy-versioned
execution directory for each manual replay/export publication, because the
low-level atomic exporter intentionally replaces a same-named target when the
caller selects an existing directory. This Phase 5D directory is retained and
must not be reused. REVIEW remains human-triage metadata and does not establish
a job opening, available contract, mailbox deliverability, or outreach
authorization.

The controlled v3 production reassessment is complete and future local runs
are ready to use v3. Batch 02, outreach, review-result promotion, and any
further phase remain unapproved.
