# Phase 5C — Deterministic Review Queue

## Status and scope

Phase 5C is **IMPLEMENTED / ISOLATED INTEGRATION PASS**. It adds optional,
purpose-specific CSV review queues for v3 `REVIEW` assessments. It does not
change qualification, relevance, eligibility, email-validation, identity, or
authoritative run-lifecycle rules.

Review queues use the same committed, non-review `NEW/CREATE_COMPANY` boundary
as qualified exports. Historical `KNOWN`, `UPDATED`, ambiguous, quarantined,
and unresolved decisions cannot enter the queue. A company may independently
appear in both purpose files.

## Files changed

- `company_registry/exports.py` — review projection, atomic publication,
  manifest support, and exact SQLite verification.
- `tests/test_company_registry_qualification.py` — REVIEW filtering,
  priorities, deduplication, purpose independence, email, missing-field,
  ordering, manifest, and qualified-export compatibility coverage.
- `company_registry/PHASE_5C_REVIEW_QUEUE.md` — implementation and isolated
  integration record.
- `codex.md`, `context/progress-tracker.md`, `context/code-map.md`, and
  `context/architecture.md` — current phase, status, symbols, flow, and
  invariants.

## Export API and lifecycle boundary

`company_registry.exports.export_review_queue()` publishes one run- and
purpose-specific CSV plus a manifest-last commit marker. The output format is
`company-registry-review-queue-v1`. `verify_review_export()` verifies the
manifest and then compares ordered assessment and company IDs with a fresh
SQLite projection.

Review files are additive, optional artifacts. They are intentionally not part
of the three required authoritative SUCCESS artifacts (`discovery`,
`employment`, and `mission`) and do not alter `verify_run_exports()` or
`recover_run_exports()`. This preserves all historical SUCCESS and recovery
contracts.

## CSV schema

The columns, in deterministic order, are:

1. `qualification_assessment_id`
2. `qualification_policy_version`
3. `purpose`
4. `company_id`
5. `registry_decision_id`
6. `source_run_id`
7. `branch_id`
8. `source_system`
9. `source_record_key`
10. `review_priority`
11. `relevance`
12. `eligibility`
13. `classification_reason_codes`
14. `missing_evidence`
15. `evidence_hash`
16. `assessed_at`
17. `company_name`
18. `country`
19. `address`
20. `website`
21. `accepted_contact_email`
22. `phone`
23. `google_maps_url`
24. `business_category`

`classification_reason_codes` is the purpose-specific immutable v3 reason
array. `missing_evidence` is a deterministic JSON array derived from the stored
evidence snapshot. Placeholder text such as `Not Available` is emitted as an
empty field. `accepted_contact_email` can contain only the accepted email
already selected by the Phase 5A validation contract.

The manifest records schema version, run, purpose, policy, CSV filename and
SHA-256, row count, ordered assessment IDs, ordered company IDs, and
HIGH/MEDIUM/LOW counts.

## Priority and missing-evidence rules

Priority uses only persisted relevance:

| Relevance | Priority | Meaning |
| --- | --- | --- |
| `TARGET` | `HIGH` | Relevant company has an evidence/readiness gap that could change eligibility. |
| `POSSIBLE` | `MEDIUM` | Potentially relevant company needs additional relevance verification. |
| `UNKNOWN` | `LOW` | Weak or substantially incomplete evidence. |

`NOISE` is `EXCLUDED` under v3 and cannot enter a REVIEW queue. Opportunity
evidence does not alter priority and the queue does not claim hiring, an active
mission, demand, or mailbox deliverability.

Missing-evidence codes are emitted in a fixed order when applicable:

- `target_relevance_not_established`
- `usable_website_not_available`
- `direct_contact_not_available` (mission only)
- `location_evidence_not_available`
- `identity_conflict_requires_resolution`
- `location_conflict_requires_resolution`
- `additional_verification_required` when REVIEW has no more specific gap

Rows sort by HIGH/MEDIUM/LOW, normalized company name, company ID, and
assessment ID. Company ID is deduplicated only within the selected purpose.

## Isolated integration result

Workspace:
`data/phase_5c_review_queue/20261031T115242Z/`

The local schema-v6 registry was opened read-only and copied with SQLite's
online backup API. The copy passed `integrity_check`, had zero foreign-key
violations, and matched all source table counts before reassessment. Policy v3
was then persisted only in the copy and review exports were generated in new,
policy-versioned directories.

| Run | Employment REVIEW | Mission REVIEW |
| --- | ---: | ---: |
| `maps-20261010-first-local-01` | 0 | 1 |
| `maps-20261011-global-01` | 16 | 39 |
| **Total** | **16** | **40** |

| Purpose | HIGH | MEDIUM | LOW |
| --- | ---: | ---: | ---: |
| Employment | 8 | 8 | 0 |
| Mission | 10 | 30 | 0 |

All 16 employment companies also appear in the mission queue; 24 additional
companies are mission-only. This overlap is retained independently rather than
cross-purpose deduplicated. Each purpose has unique company IDs within and
across the two run-scoped files for this cohort.

All four manifests and SQLite projections verified. Re-exporting produced the
same hashes and order:

| Run/purpose | CSV SHA-256 |
| --- | --- |
| First-local employment (header only) | `2f95fbf96bd28098c7e73aec9821100f6a1128afd648a6e01698bc676de603dc` |
| First-local mission | `7798ca989bfaf9a3febbe3bf1adb69fe85222a22150b0c87c268e51319c87434` |
| Global employment | `e73bacbfe1f88c08c3b342c9fd25e1a2bde0352373f1e6fd206aeea9ee8566b1` |
| Global mission | `48d04e13061109200ac2e097b419ec7ab9780b25c267995e2718a254a12887f8` |

## Example rows

Representative fields are shown below; the CSVs retain full IDs, provenance,
reason arrays, addresses, and Google Maps URLs.

| Purpose | Company | Priority | Category | Accepted email | Missing evidence | Source run |
| --- | --- | --- | --- | --- | --- | --- |
| Employment | APPSFACTOR | HIGH | Software company | empty | `usable_website_not_available` | `maps-20261011-global-01` |
| Employment | Agence Lookiimobile | MEDIUM | Computer support and services | empty | `target_relevance_not_established` | `maps-20261011-global-01` |
| Mission | Microsoft Dynamics Nav Tunisie - Business Management | MEDIUM | Business management consultant | `info@businessmanagement.tn` | `target_relevance_not_established` | `maps-20261010-first-local-01` |
| Mission | APPSFACTOR | HIGH | Software company | empty | `usable_website_not_available` | `maps-20261011-global-01` |

## Validation

- Correct REVIEW-only and NEW-only filtering: pass.
- No duplicate company within a CSV: pass.
- Cross-purpose overlap retained: 16 companies, pass.
- Company, assessment, decision, branch, source, and run provenance: pass.
- Phase 5A email revalidation and selected accepted address: pass.
- Empty/placeholder fields and JSON arrays: pass.
- Deterministic ordering and repeat hashes: pass.
- Manifest and exact SQLite projection verification: 4/4 pass.
- Existing historical discovery/qualified manifests: 6/6 remain valid.
- Focused qualification/export/recovery tests: 51/51 pass.
- Complete offline suite: 835/835 pass in 255.772 seconds.
- Final copied SQLite integrity: `ok`; foreign-key violations: 0.
- Source database and all 12 historical run artifacts: unchanged by
  size, mtime, and SHA-256.

Detailed evidence is retained under the workspace `evidence/` directory,
especially `review-queue-audit.json`, `lifecycle-results.json`, and
`final-verification.json`.

## Remaining risks and recommendation

- Priority is deterministic triage, not proof of an opening, contract, need,
  budget, mailbox delivery, or current decision-maker.
- Maps categories, addresses, websites, phones, and URLs may be stale.
- No real cohort row is LOW; LOW behavior is covered by isolated tests but has
  no Phase 5C live-data example.
- Review queues are optional and must be explicitly regenerated and verified
  after a controlled reassessment; they are not automatic SUCCESS artifacts.
- Human decisions are not persisted by this phase. No dashboard, review-state
  schema, external service, or promotion mechanism was added.

Recommendation: **ready for a separately approved controlled production
reassessment**, provided it uses a verified backup, policy-v3 versioning,
isolated versioned output paths, all manifest/projection checks, and human
review before any downstream action. This result does not itself authorize
production reassessment, outreach, Phase 5D, or Batch 02.
