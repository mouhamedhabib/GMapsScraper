# Phase 3D.2 — Dual-Purpose Maps Qualification

## Decision

Phase 3D.2 is implemented and verified offline. It adds a separate qualification
boundary after company identity resolution. It does not change the meaning of
registry classifications/actions, delete companies, infer hiring or confirmed
software need, run Google Search, scrape live data, or activate production.

The initial policy version is `maps-dual-purpose-v1`.

## Architecture and persistence

`company_registry/qualification.py` evaluates the Maps evidence stored on a
committed decision. `assess_run()` persists one immutable assessment for each
`(decision_id, policy_version)` pair. Repeating the same assessment reuses it;
changing the policy version creates a new assessment without overwriting prior
results.

Schema v5 adds only `qualification_assessments`. Each row records:

- stable assessment, decision, and company IDs;
- policy version, evidence hash, and canonical evidence snapshot;
- separate employment and mission relevance and eligibility;
- separate machine-readable reason lists;
- assessment and creation timestamps.

Migration is explicit through the existing `migrate_registry()` lifecycle.
`open_registry()` still refuses every non-current schema and never creates or
migrates a database. Tests migrate only temporary databases/copies. Production
schema v2 and the existing shadow schema v3 were not migrated.

## Policy

Both purposes use only supplied Maps name, category, description, website,
contact, address/country/city, and committed resolution conflict evidence.
Query text is not positive qualification evidence.

Employment:

- `TARGET` requires a named business plus strong software/IT evidence in the
  category or description. A technical-looking name alone is only `POSSIBLE`.
- `ELIGIBLE` additionally requires a usable non-shared website, address
  evidence, and no identity/location conflict.
- Adjacent businesses such as software retailers, support/repair shops, or a
  name-only technical match remain `POSSIBLE/REVIEW`.
- Nontechnical businesses are `NOISE/EXCLUDED`; insufficient evidence is
  `UNKNOWN/REVIEW`.
- This is relevance to employment prospecting, not evidence of current hiring.

Mission:

- `TARGET` requires explicit software-service, integration, automation, or
  partnership/overflow fit attached to the business evidence.
- `ELIGIBLE` additionally requires a usable website, direct phone/email,
  address evidence, and no identity/location conflict.
- Hotels, travel agencies, e-commerce merchants, and other commercial sectors
  may be `POSSIBLE/REVIEW`, but their industry or a missing website never proves
  a need and never makes them automatically eligible.
- A marketing agency becomes `TARGET` only when its description explicitly
  supplies development/service evidence; marketing category alone is review.
- Insufficient evidence is `UNKNOWN/REVIEW`.

The two assessments are independent. One company can be eligible for both,
review for one and excluded for the other, or excluded from both.

## Exports

The existing `new_companies_<run>.csv` and its
`company-registry-new-companies-v1` manifest are unchanged. It continues to
answer only which committed, non-review `NEW/CREATE_COMPANY` identities were
discovered.

Maps authoritative completion now also assesses committed resolved Maps
decisions and publishes:

- `qualified_employment_leads_<run>.csv`
- `qualified_mission_leads_<run>.csv`

Each is a qualified subset of committed, non-review `NEW/CREATE_COMPANY`
decisions, so known companies and branch-only updates cannot re-enter outreach.
Each has an independent `company-registry-qualified-leads-v1` manifest with
purpose, policy version, SHA-256, row count, ordered assessment IDs, and ordered
company IDs. Exports include only `ELIGIBLE`, deduplicate by company, sort
deterministically, retain decision/source provenance and reasons, and can be
rebuilt safely after a partial publication. `REVIEW` and `EXCLUDED` never enter
these files.

## Offline validation

The Phase 3D.2 matrix covers software development, IT consulting, software
retail, travel, hotels, e-commerce, marketing plus development, missing sites,
identity conflicts, insufficient evidence, known companies, multiple branches,
cross-query/company deduplication through decision/branch behavior, dual-purpose
qualification, policy-version reassessment, export retry/recovery, historical
preservation through migration regressions, and purpose independence.

- Focused qualification/registry/Maps/audit suite: 65/65 passed.
- First full suite: 745/747 passed; the only failures were two test assertions
  hard-coded to schema v4 after the intentional schema-v5 change.
- Final full-suite result and protected-state hashes are recorded in
`context/progress-tracker.md` after the final rerun.

## Limitations and next unit

The v1 rules are deliberately conservative and deterministic; they do not
validate whether a site is owned by the company beyond the registry's evidence,
confirm hiring, confirm budget or need, score lead value, or enforce the 60/40
outreach planning preference. The latter remains downstream planning only.

No live canary was run. The next possible unit is an explicitly approved,
isolated Maps canary on a disposable schema-v5 database. Production/shadow
migration and Phase 3E remain unapproved.
