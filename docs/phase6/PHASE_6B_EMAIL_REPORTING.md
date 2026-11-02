# Phase 6B Email Availability and Quality Reporting

Date: 2026-11-01  
Scope: minimal backward-compatible email coverage correction; no scraping or
historical-data mutation

## Outcome

Phase 6B adds a centralized email-availability classifier and an optional,
deterministic JSON report. It does not change the existing
`maps-dual-purpose-v3-business-relevance` ACCEPT/REVIEW/REJECT policy, stored
assessments, qualification eligibility, or any CSV/manifest schema.

Read-only validation against `maps-20261031-batch02-300`, with the report written
under `/tmp`, reproduced the Phase 6A evidence:

| Population | Records | Available | Unavailable | ACCEPT | REVIEW | REJECT |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Discovery NEW | 300 | 122 | 178 | 72 | 50 | 1 |
| Employment ELIGIBLE | 57 | 41 | 16 | 28 | 13 | 0 |
| Mission ELIGIBLE | 54 | 40 | 14 | 28 | 12 | 0 |

Here, available means at least one syntactically plausible ACCEPT or REVIEW
candidate. REVIEW remains visible for coverage but is not promoted to the
accepted `email` field. Unavailable means missing, configured-placeholder-only,
or REJECT-only evidence.

## Root cause addressed

Discovery exports preserve the scraper's compatibility sentinel
`Not Available`. It is non-empty, so generic populated-cell metrics incorrectly
reported 300/300 coverage. Email availability now uses explicit classifications
instead of truthiness:

- `MISSING`: null, empty, or whitespace-only source value;
- `PLACEHOLDER`: a configured sentinel such as `Not Available`, `N/A`, `none`,
  `null`, `unknown`, or `-`;
- `AVAILABLE`: syntactically plausible candidate with the existing ACCEPT
  decision;
- `REVIEW`: syntactically plausible candidate requiring the existing human
  review path;
- `REJECTED`: malformed, placeholder-address, asset, automated-sender,
  telemetry, or other existing REJECT outcome.

No existing qualification decision was reinterpreted or rewritten. Stored
`evidence_json.email_candidates` remains authoritative for report decision
counts.

## Reporting semantics

`company_registry.email_reporting.build_email_coverage_report()`:

1. opens an existing schema-v6 registry with SQLite `mode=ro` and
   `PRAGMA query_only=ON`;
2. selects company-deduplicated, non-review `NEW/CREATE_COMPANY` decisions;
3. reports the discovery population and the employment/mission `ELIGIBLE`
   subsets for the requested policy version;
4. counts available/unavailable records, source statuses, stored candidate
   decisions, and records containing each decision;
5. writes canonical JSON atomically to an explicit report directory, defaulting
   to `data/reports/email_coverage`;
6. refuses a destination containing existing registry CSV exports or manifests;
7. reuses an identical existing report without rewriting it.

Example:

```python
from company_registry.email_reporting import build_email_coverage_report

result = build_email_coverage_report(
    "data/local_registry/company_registry.db",
    "maps-20261031-batch02-300",
    "/tmp/email-coverage",
)
print(result["report"])
```

The report is not a production export, has no production manifest, and is not
part of Maps SUCCESS finalization or recovery.

## Files modified

- `company_registry/qualification.py` — adds `EmailAvailability` and the
  centralized `classify_email_availability()` function while retaining the
  existing candidate validator and policy.
- `company_registry/email_reporting.py` — adds read-only projection,
  aggregation, deterministic JSON publication, and identical-output reuse.
- `tests/test_company_registry_email_reporting.py` — adds Phase 6B unit and
  isolated registry/export regression tests.
- `docs/phase6/PHASE_6B_EMAIL_REPORTING.md` — this implementation record.
- `context/progress-tracker.md`, `context/code-map.md`, and
  `context/architecture.md` — update local project status and boundaries.

Existing production exports, manifests, SQLite records, schema, scraper search
behavior, query scheduling, NEW budget, and resume logic were not modified.

## Tests performed

Commands executed:

```text
python3 -m py_compile company_registry/qualification.py \
  company_registry/email_reporting.py \
  tests/test_company_registry_email_reporting.py

python3 -m unittest -v \
  tests.test_company_registry_email_reporting \
  tests.test_company_registry_qualification \
  tests.test_company_registry_export_recovery \
  tests.test_company_registry_authoritative_foundation
```

Result: 53 focused tests passed. Coverage includes null/empty values, configured
placeholders, ACCEPT-looking and REVIEW-looking addresses, malformed REJECT
values, unchanged qualified export fields/content, read-only reporting, separate
report paths, and repeat-report byte/mtime idempotence.

The repository and virtual-environment interpreters do not have `pytest`
installed, so the project's `unittest`-style suite was run directly. No
dependency was installed.

A full `python3 -m unittest discover -q` attempt ran 347 tests but did not
complete successfully: 45 import/collection errors were caused by missing
pre-existing `selenium` and `bs4` packages in the available interpreter. The
additional registry qualification, export recovery, and authoritative export
foundation modules were therefore run explicitly and are included in the 53
passing focused tests above.

A read-only Batch 02 projection was also run with output directed to
`/tmp/gmaps-phase6b-email-report-validation`; it produced the counts in the
Outcome table. No live scraping, network call, registry write, export rewrite,
manifest rewrite, or reassessment ran.

The three Batch 02 CSV hashes remained exactly equal to the Phase 6A baseline:

- discovery: `ec512011b3d4ad583bc531d59be819857a932cd4b81012d1a8fb3f81c1ae7bdf`;
- employment: `51c5abb4497d0d87853c39b530841b9a6d0a469015a6a17f48b39a478ee554d9`;
- mission: `41f0ba346660ffabe08863833b9af28e798248bc501d05145d50f607ea8b580d`.

## Remaining limitations

- Plausible/ACCEPT is a syntax-and-policy result, not MX, SMTP, deliverability,
  ownership, consent, or legal-contact verification.
- Decision counts are candidate counts from stored assessment evidence. A
  future record with multiple candidates can contribute more than one decision;
  `records_with_decision` is included to make that distinction explicit.
- Records without an assessment can receive an availability result from their
  raw source value, but do not receive fabricated historical decision counts;
  they are reported as unassessed.
- The known-placeholder list is deterministic and finite. New upstream sentinel
  spellings must be added explicitly.
- The extractor still persists only its selected scalar address; Phase 6B does
  not recover additional addresses discarded before registry persistence.
- The reporting entry point is a Python API, not a new scraper or CLI command.

Phase 6B stops here. Phase 6C requires separate approval.
