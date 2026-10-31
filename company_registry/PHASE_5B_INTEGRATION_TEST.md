# Phase 5B End-to-End Integration Test

## Verdict

**PASS.** Policy `maps-dual-purpose-v3-business-relevance` reproduced the
Phase 5B simulation exactly through the persisted assessment and existing CSV
export implementations. The policy is ready for a separately approved,
controlled production reassessment with the normal backup, isolation,
versioning, and NEW-only export controls. This test did not perform or
authorize that reassessment, outreach, Phase 5C, or Batch 02.

## 1. Environment and isolation

- Test timestamp: `2026-10-31T11:38:42Z`.
- Repository: `/home/wsl/GitHub/GMapsScraper`.
- Source registry:
  `/home/wsl/GitHub/GMapsScraper/data/local_registry/company_registry.db`.
- Isolated workspace:
  `/home/wsl/GitHub/GMapsScraper/data/phase_5b_integration_test/20261031T113842Z`.
- Writable test registry:
  `/home/wsl/GitHub/GMapsScraper/data/phase_5b_integration_test/20261031T113842Z/database/company_registry-v6-test-copy.db`.

The source was opened with SQLite URI `mode=ro` and copied with
`sqlite3.Connection.backup()`. No plain file copy, migration, WAL deletion, or
write-capable source connection was used. All reassessment and export writes
targeted the isolated workspace. The protected production and shadow
registries were not opened as writable targets. Historical exports were not
used as output paths.

Before mutation, source and copy both reported schema/user version 6,
`integrity_check=ok`, and zero foreign-key violations. All 11 user-table row
counts and table-level SHA-256 logical fingerprints matched. The physical file
hashes were expectedly different because the SQLite backup API reconstructs
the destination database:

| Database | Initial SHA-256 |
| --- | --- |
| Source | `68662831e6448c8b027589b7bbfb85ab70032e473df12f418a87126c61c44a35` |
| Backup copy | `2589dd8a8778fc3271b0eb2e10451ff8b75b00a25804a5b334ef911b99e7c476` |

## 2. Project contract and commands

The review covered `codex.md`, the absence of an applicable `AGENTS.md`, the
progress tracker and code map, the Phase 5B design, `qualification.py`,
`exports.py`, authoritative finalization/recovery, focused tests, and local
lifecycle documentation.

Key commands and API calls executed were:

```text
find .. -name AGENTS.md -print
rg ... codex.md context company_registry tests LOCAL_USAGE.md
sqlite3.connect("file:<source>?mode=ro", uri=True).backup(<isolated copy>)
assess_run(<isolated copy>, "maps-20261010-first-local-01", policy=v3)
assess_run(<isolated copy>, "maps-20261011-global-01", policy=v3)
export_qualified_leads(..., purpose="employment", policy_version=v3)
export_qualified_leads(..., purpose="mission", policy_version=v3)
verify_export_manifest(...)
verify_run_export(..., policy_version=v3)
.venv/bin/python -m pytest -q --junitxml=...
.venv/bin/python -m unittest discover -s tests -q
```

The `pytest` attempt did not run tests because `pytest` is not installed in the
project virtual environment. The established standard-library `unittest`
runner was then used for the complete offline suite.

Both requested source runs were confirmed `SUCCESS` with no lease owner before
reassessment. Policy-version uniqueness supplied the supported reassessment
mechanism: historical v1 rows remained immutable while v3 rows were inserted
under new deterministic assessment IDs. A second v3 assessment call reused all
existing v3 rows (`309` and `814`) and inserted zero, proving idempotency.

## 3. Persisted assessment results

The lifecycle inserted 1,123 v3 assessments: 309 for the first-local run and
814 for the global run. The global run's three `AMBIGUOUS`, review-required,
unresolved decisions had no company ID, were reported as skipped, received no
assessment, and appeared in no export.

The expected/simulated comparison cohort is the 105 committed, non-review
`NEW/CREATE_COMPANY` decisions. Its actual persisted results were:

| Purpose | Status | Expected | Actual | Difference |
| --- | --- | ---: | ---: | ---: |
| Employment | ELIGIBLE | 62 | 62 | 0 |
| Employment | REVIEW | 16 | 16 | 0 |
| Employment | EXCLUDED | 27 | 27 | 0 |
| Mission | ELIGIBLE | 60 | 60 | 0 |
| Mission | REVIEW | 40 | 40 | 0 |
| Mission | EXCLUDED | 5 | 5 | 0 |

Per-run eligibility was:

| Run | Employment E/R/X | Mission E/R/X |
| --- | --- | --- |
| `maps-20261010-first-local-01` | 4 / 0 / 1 | 4 / 1 / 0 |
| `maps-20261011-global-01` | 58 / 16 / 26 | 56 / 39 / 5 |
| **Total** | **62 / 16 / 27** | **60 / 40 / 5** |

Relevance also matched the simulation: employment TARGET/POSSIBLE/NOISE was
70/8/27 and mission TARGET/POSSIBLE/NOISE was 70/30/5.

For completeness, the assessment lifecycle evaluates all resolved Maps
decisions, not only NEW decisions. Across all 1,123 v3 assessment rows,
employment eligibility was 702/212/209 and mission eligibility was
664/441/18 (ELIGIBLE/REVIEW/EXCLUDED). The export contract correctly prevented
the 640 non-NEW employment-eligible assessment rows and 604 non-NEW
mission-eligible assessment rows from re-entering qualified exports.

## 4. CSV and manifest results

Version isolation is provided by the policy-named output directories; original
run IDs remain unchanged in filenames, rows, and manifests.

| Run | Employment rows | Mission rows |
| --- | ---: | ---: |
| `maps-20261010-first-local-01` | 4 | 4 |
| `maps-20261011-global-01` | 58 | 56 |
| **Total** | **62** | **60** |

All four manifests passed format, CSV SHA-256, row-count, ordered assessment-ID,
ordered company-ID, run-ID, purpose, and v3 policy-version verification. Each
CSV also matched a fresh SQLite projection through `verify_run_export()`.

| Artifact | SHA-256 |
| --- | --- |
| First-local employment CSV | `fc75cca4e25aff25eeff10d053295dd35266da62abd3a06fff1030be94412259` |
| First-local mission CSV | `1066dd1314b551efbfd6857c4d12f74ade6f281858dbcf74e7741d74fdca2700` |
| Global employment CSV | `9642b0e808957494fb37579d1f716b9b8d4cab0d606b9bde4215746544ce198e` |
| Global mission CSV | `00d99109eaf8b9c6b680d3f66f6eea7d041ff12736aae1e5e0940aeb765f0f1f` |

All exported rows have the requested run provenance, a Google Maps source
record key, the matching committed decision/company ID, TARGET relevance, and
ELIGIBLE status. Every exported decision is non-review
`NEW/CREATE_COMPANY`. There are no duplicate companies within any export or
across the two runs for one purpose.

Purpose-specific deduplication remained independent: 60 companies are present
in both exports, two are employment-only, and none is mission-only. The shared
60 were not incorrectly removed from either purpose.

## 5. Contact, ambiguity, and opportunity-claim audit

- The 68 Phase 5A `site_email` candidates reproduced exactly: 47 ACCEPT,
  19 REVIEW, and 2 REJECT.
- Every stored candidate revalidated to the same normalized value, decision,
  and reason code with `validate_contact_email()`.
- Every selected evidence email is the first deterministic ACCEPT candidate;
  REVIEW and REJECT candidates never supplied contact eligibility.
- All 60 mission CSV rows have a phone or accepted email. Two employment rows
  lack direct contact, which is allowed by the employment contract and explains
  why they are not mission-eligible.
- The three ambiguous global decisions remained unresolved, unassessed, and
  unexported.
- Zero NEW-cohort assessments contain explicit hiring evidence and zero contain
  explicit mission evidence. All 105 record
  `current_hiring_not_established` and `active_mission_not_established`.
  Therefore the outputs make no claim of confirmed openings, active freelance
  contracts, demand, or mailbox deliverability.

## 6. Regression and integrity results

| Check | Result |
| --- | --- |
| Complete offline tests | 833 passed, 0 failed, 0 skipped in 283.728 s |
| Qualification/export errors | None |
| Copy `integrity_check` after reassessment | `ok` |
| Copy foreign-key violations | 0 |
| Manifest/projection verification | 4/4 valid |
| Duplicate assessment `(decision_id, policy_version)` groups | 0 |
| Source database unchanged | Yes; size, mtime, and SHA-256 unchanged |
| Original historical exports unchanged | Yes; all 12 size/mtime/SHA-256 records unchanged |
| Historical v1 assessments unchanged | Yes; 1,123-row logical digest unchanged |
| Unexpected isolated-copy table mutations | None; only `qualification_assessments` changed |

The final copy remains schema v6 and contains 1,123 historical v1 plus 1,123
v3 assessments. Its final SHA-256 is
`18ffe81f28d3acd5ab2a37b289fa61d34989b1d8f67648911e6a4d923f348459`.
The source remains
`68662831e6448c8b027589b7bbfb85ab70032e473df12f418a87126c61c44a35`.

## 7. Failed checks and limitations

No product, qualification, export, SQLite, manifest, regression, or historical
integrity check failed. The only command-level issue was unavailable `pytest`;
it executed no tests and was superseded by the project's established
`unittest` command.

The qualified-export contract is intentionally ELIGIBLE-only and NEW-only.
It cannot export REVIEW/EXCLUDED or historically KNOWN/UPDATED companies, even
when those rows have v3 assessments. This is expected behavior, not a bypassed
limitation. No discovery CSV was regenerated because the requested test output
was limited to the two qualified purposes and historical discovery exports had
to remain untouched.

## 8. Retained artifacts

- Workspace root:
  `data/phase_5b_integration_test/20261031T113842Z/`
- Database copy: `database/company_registry-v6-test-copy.db`
- Backup/source evidence: `evidence/source-before.json`,
  `evidence/backup-verification.json`, and
  `evidence/logical-fingerprints.json`
- Lifecycle results: `evidence/lifecycle-results.json`
- Detailed audit: `evidence/data-integrity-audit.json`
- Post-mutation table audit:
  `evidence/post-mutation-table-fingerprints.json`
- Final source/test verification: `evidence/final-verification.json`
- Qualified CSVs/manifests:
  `exports/<original-run-id>/maps-dual-purpose-v3-business-relevance/`

All test artifacts are retained for review. No artifact was promoted, copied
into the historical export directory, or deleted.
