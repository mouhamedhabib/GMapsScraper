# Phase 6C Unified Lead Export

Date: 2026-11-01  
Scope: deterministic, read-only consolidation of discovery and qualification
evidence; no scraping, reassessment, or source-artifact mutation

## Outcome

Phase 6C adds a versioned unified CSV/manifest export for one or more completed
`SUCCESS` runs. The registry is opened with SQLite URI `mode=ro` plus
`PRAGMA query_only=ON`. The exporter does not create assessments, change policy,
or modify SQLite, existing CSVs, or existing manifests.

The inclusion cohort is the union of committed, non-review
`NEW/CREATE_COMPANY` decisions in the selected runs. Deduplication uses only
authoritative `company_id`; names are never identity keys.

## Deterministic cross-run handling

- Input run IDs are de-duplicated and ordered by `(started_at, run_id)`.
- Every row records all selected runs in which that company has a resolved,
  non-review appearance through `source_run_ids`.
- Discovery/contact/qualification columns come from the latest ordered selected
  `NEW/CREATE_COMPANY` record with the requested policy assessment. Later
  KNOWN/UPDATED appearances add provenance but do not replace immutable NEW
  evidence.
- If an assessment for the requested policy is absent, all qualification fields
  and both `*_eligible` flags are blank. Missing does not mean EXCLUDED.
- `employment_eligible` and `mission_eligible` are `TRUE` only for exact stored
  `ELIGIBLE`, `FALSE` for stored REVIEW/EXCLUDED, and blank when unassessed.
- Rows are ordered lexicographically by `company_id`.

## CSV schema

The `company-registry-unified-leads-v1` CSV has 25 columns:

| # | Column | Meaning |
| ---: | --- | --- |
| 1 | `company_id` | Authoritative deduplication key |
| 2 | `source_run_ids` | Canonical JSON list of selected runs where company appeared |
| 3 | `first_source_run_id` | First source run in canonical order |
| 4 | `latest_source_run_id` | Last source run in canonical order |
| 5 | `selected_observation_run_id` | NEW record supplying discovery/contact fields |
| 6 | `qualification_run_id` | NEW record supplying the selected assessment, or blank |
| 7 | `qualification_assessment_id` | Stored assessment ID, or blank |
| 8 | `qualification_policy_version` | Stored assessment policy, or blank |
| 9 | `employment_eligible` | `TRUE`, `FALSE`, or blank |
| 10 | `mission_eligible` | `TRUE`, `FALSE`, or blank |
| 11 | `employment_eligibility` | Stored ELIGIBLE/REVIEW/EXCLUDED value, or blank |
| 12 | `mission_eligibility` | Stored ELIGIBLE/REVIEW/EXCLUDED value, or blank |
| 13 | `employment_relevance` | Stored relevance, or blank |
| 14 | `mission_relevance` | Stored relevance, or blank |
| 15 | `company_name` | Assessment evidence name, with raw NEW fallback |
| 16 | `country` | Assessment evidence country, with raw fallback |
| 17 | `city` | Assessment evidence city, with raw fallback |
| 18 | `website` | Assessment evidence website, with raw fallback |
| 19 | `phone` | Assessment evidence phone, with raw fallback |
| 20 | `email` | Selected ACCEPT/REVIEW/REJECT candidate; blank if unavailable |
| 21 | `email_classification` | ACCEPT, REVIEW, REJECT, or UNAVAILABLE |
| 22 | `email_status` | Phase 6B AVAILABLE/REVIEW/REJECTED/PLACEHOLDER/MISSING status |
| 23 | `email_reason_code` | Stored or Phase 6B deterministic reason |
| 24 | `google_maps_link` | Maps link from the selected NEW payload |
| 25 | `source_query` | Query provenance from the selected NEW payload |

ACCEPT is a deterministic syntax/role policy result. It is not MX, SMTP,
deliverability, ownership, consent, or legal-contact verification.

## Output and manifest safety

The destination layout is:

```text
<output-dir>/unified-leads-v1/<dataset-token>/
  unified_leads.csv
  unified_leads.manifest.json
```

Single-run tokens use the sanitized run ID. Combined datasets use a stable hash
of canonical run IDs and policy version. CSV and manifest bytes are staged,
flushed, fsynced, and atomically replaced; the manifest is published last. An
identical committed export is reused without rewriting. A differing committed
export or colliding CSV path fails closed.

The manifest records format/export schema version, registry schema version,
source run IDs and statuses, requested qualification policy, CSV filename and
SHA-256, row count, ordered company IDs, eligibility/overlap counts, and missing
qualification count. `verify_unified_export()` validates all of those contracts.

## CLI

Single completed run:

```bash
python3 -m company_registry.unified_export \
  --database data/local_registry/company_registry.db \
  --run-id maps-20261031-batch02-300 \
  --output-dir /tmp/gmaps-unified
```

Multiple completed runs:

```bash
python3 -m company_registry.unified_export \
  --database data/local_registry/company_registry.db \
  --run-id maps-20261010-first-local-01 \
  --run-id maps-20261011-global-01 \
  --run-id maps-20261031-batch02-300 \
  --output-dir /tmp/gmaps-unified
```

An existing historical policy can be selected with
`--policy-version <version>`. This only projects stored assessments; it never
creates a reassessment.

## Historical validation

Validation output was written only below `/tmp/gmaps-phase6c-validation-2`.

| Dataset | Companies | Employment TRUE | Mission TRUE | Overlap | Either purpose |
| --- | ---: | ---: | ---: | ---: | ---: |
| Batch 02 | 300 | 57 | 54 | 54 | 57 |
| All three SUCCESS runs | 405 | 119 | 114 | 114 | 119 |

Batch 02 email classifications are 72 ACCEPT, 50 REVIEW, 1 REJECT, and 177
UNAVAILABLE, matching Phase 6A/6B. The combined export has 119 ACCEPT, 69
REVIEW, 3 REJECT, and 214 UNAVAILABLE. Thirty-one combined rows trace to two
selected runs; 374 trace to one.

Batch 02 CSV SHA-256:

`266f478f9c69ab8e53ea87eed2992a5fbefc45ced62d1be1e30c4a33ef02ab60`

Combined CSV SHA-256:

`bf3356d2c54dbdaecd0e471713ea642e2250c30e6981f9d3023bf5cbc7f88989`

Reversing the three CLI run arguments reused the same combined destination and
bytes.

## Files modified

- `company_registry/unified_export.py` — unified projection, verifier, atomic
  publisher, manifest, and CLI.
- `company_registry/storage.py` — reusable fully validated read-only registry
  opener.
- `company_registry/qualification.py` — centralized multi-field Phase 6B email
  classification helper.
- `company_registry/email_reporting.py` — uses the shared read-only opener and
  multi-field classifier.
- `tests/test_company_registry_unified_export.py` — Phase 6C regressions.
- `docs/phase6/PHASE_6C_UNIFIED_EXPORT.md` — this implementation record.
- `context/progress-tracker.md`, `context/code-map.md`, and
  `context/architecture.md` — status and boundary updates.

## Tests and limitations

Focused command:

```text
python3 -m unittest -q \
  tests.test_company_registry_unified_export \
  tests.test_company_registry_email_reporting \
  tests.test_company_registry_qualification \
  tests.test_company_registry_export_recovery \
  tests.test_company_registry_authoritative_foundation \
  tests.test_company_registry_schema
```

Result: 73/73 passed.

Full offline command:

```text
.venv/bin/python -m unittest discover -q
```

Result: 845/845 passed in 264.494 seconds. The environment lacks pytest, so the
repository's unittest-style suite was run directly with the project venv. The
system Python also lacks Selenium and Beautiful Soup, while the project venv
contains them.

Tests cover authoritative company-ID cross-run deduplication, ordered source
traceability, overlapping eligibility, REVIEW versus ELIGIBLE, missing
qualification, placeholder/REVIEW email handling, deterministic input-order
normalization, atomic failure recovery, committed-output reuse, manifest/hash
integrity, output collisions, CLI behavior, unchanged existing exports, and a
read-only/chmod-protected SQLite source.

Limitations:

- Only terminal SUCCESS runs are accepted. PARTIAL or nonterminal runs fail
  closed.
- Only the selected policy version is projected. Missing assessments stay
  blank rather than being synthesized.
- The export retains one selected email candidate per company, while full
  candidate audit history remains in assessment evidence JSON.
- Historical extraction still cannot recover additional addresses discarded
  before registry persistence.

## Safety verification

Before and after historical validation, the source database remained byte- and
mtime-identical:

`a379add919c4facfae5f1065a4757be2af9f8f2c89c2ee57a7f6ba511c16fb74`

The protected Batch 02 CSV hashes also remained unchanged:

- discovery: `ec512011b3d4ad583bc531d59be819857a932cd4b81012d1a8fb3f81c1ae7bdf`;
- employment: `51c5abb4497d0d87853c39b530841b9a6d0a469015a6a17f48b39a478ee554d9`;
- mission: `41f0ba346660ffabe08863833b9af28e798248bc501d05145d50f607ea8b580d`.

No live scraping, browser launch, network call, email sending, assessment write,
policy change, SQLite write, existing CSV rewrite, or manifest rewrite occurred.

Phase 6C stops here. Phase 6D requires separate approval.
