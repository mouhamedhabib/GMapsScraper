# Phase 3D.1 Maps-only live validation and quality audit

## Recommendation

**FAIL for Maps-only production readiness; PASS for the isolated authoritative
Maps pipeline.** Identity resolution, budget enforcement, SQLite persistence,
exports, and protected-state isolation passed. Lead qualification is not yet a
publication gate: the live export contained one target, one possible target,
and one clear non-target. Production mode was not activated.

Google Search was intentionally excluded and remains unchanged. It is outside
the current roadmap unless separately approved.

## Preflight and offline validation

- Artifact root: `data/phase_3d1_maps_canary/20261009T120000Z/`.
- Production was copied read-only with SQLite's backup API; only the disposable
  copy was explicitly migrated from schema v2 to schema v4.
- Before browser launch: `integrity_check=ok`, no foreign-key violations,
  3,838 companies, 5,297 branches, and 18,623 historical source records.
- Focused Maps/registry suite: **89/89 passed**. The dedicated ten-case matrix
  separates registry classification, business relevance, and a test-only
  conservative outreach recommendation.
- Offline cases passed: software company with/without a website, unrelated
  travel and marketing agencies, new branch, multi-query duplicate, shared
  domain, missing phone/site, conflicting evidence, and historical company.

The read-only audit found no Maps-specific identity or durability defect. CLI
mode/path validation is fail-closed; runtime opening cannot create or migrate;
normalization and company/branch matching preserve shared-domain ambiguity;
NEW capacity is provisionally reserved and committed only after the SQLite
transaction; exports are rebuilt from committed NEW/CREATE_COMPANY decisions
and committed by their manifest; authoritative failures propagate; run/query,
budget, classification, and browser metrics are exposed.

## Live command and result

```bash
.venv/bin/python maps.py \
  --discovery-mode authoritative-canary \
  --registry-database data/phase_3d1_maps_canary/20261009T120000Z/registry-v4.db \
  --registry-run-id phase3d1-maps-20261009T120000Z \
  --authoritative-export-dir data/phase_3d1_maps_canary/20261009T120000Z/maps/exports \
  --query-file data/phase_3d1_maps_canary/20261009T120000Z/maps/queries.txt \
  --threads 1 --low-resource --limit 3 \
  --output-folder data/phase_3d1_maps_canary/20261009T120000Z/maps/runtime \
  --export-dir data/phase_3d1_maps_canary/20261009T120000Z/maps/runtime \
  --known-companies-dir data/phase_3d1_maps_canary/20261009T120000Z/maps/runtime \
  --disable-verbose
```

- Three explicit development queries were scheduled and completed. The first
  query, `software development company Nabeul Tunisia`, reached the global
  limit; the remaining two queries performed no detail enrichment.
- 128 Place URLs found; 5 result cards inspected; 2 KNOWN/MATCH_ONLY and
  3 NEW/CREATE_COMPANY; 3 branches created; no UPDATED, AMBIGUOUS,
  QUARANTINED, reviews, or resolver errors.
- Budget: limit 3, committed 3, reserved 0, remaining 0. Termination reason:
  `GLOBAL_LIMIT_REACHED`.
- Resources: one Chrome instance, zero recreations, five detail pages, 25/25
  temporary tabs opened/closed, 91.77 MB final Python RSS, three readiness
  retries, and zero verification prompts.
- Run status: `SUCCESS`. Export manifest verified three rows and ordered
  decision IDs; CSV SHA-256:
  `f0ba792f24b011a0d7b96b1ca951b986d842abfb10e0e1da9358d159c057fbc5`.

| Business | Category | Company ID | Branch ID | Quality |
| --- | --- | --- | --- | --- |
| Smart Soft | Software company | `2656aae9-1214-5a39-b061-58378cebbd41` | `1625137a-b7d5-5996-abbc-913bfbd81d2b` | TARGET; usable website |
| WeBuild Solutions Agency | Website designer | `9423ed2a-77d5-535f-b1c6-5ce868543ca1` | `fb2edd11-8ee4-57bb-a1da-63f6f309c33a` | POSSIBLE; manual qualification |
| Your Best Software | Computer software store | `1c8b3b92-144d-525a-a8d1-4678b97f4bfa` | `0278d3f5-9c46-5b90-b3d4-7b3c2548e03a` | NOISE; not a development company |

## Quality analysis and conservative policy

The previous Tozeur query was not merely too broad. Maps returned TOZEUR
EXPERIENCE as a sightseeing tour agency despite explicit software wording.
LIGHT AGENCY had a Maps `Software company` category but no website or phone;
DevAppLand had both the category and a usable website. The new, more explicit
query still returned a software store. Query wording helps retrieval but does
not qualify a business.

Keep the stages independent:

1. Registry classification answers entity identity only and retains all
   observations without changing NEW/KNOWN/UPDATED/AMBIGUOUS/QUARANTINED.
2. Business relevance uses Maps category as primary evidence: a narrow software
   development allowlist is TARGET; adjacent web/IT services are POSSIBLE;
   explicit travel, tour, marketing, retail/store, and similar categories are
   NOISE. Query text alone is never positive evidence.
3. Automatic outreach eligibility should require committed non-review
   NEW/CREATE_COMPANY, TARGET relevance, a usable company website, and
   non-conflicting geography/evidence. POSSIBLE, NOISE, missing-site, branch,
   ambiguous, and quarantined records remain stored and reviewable, not deleted.

## Integrity and remaining blockers

- Final disposable state: 3,841 companies, 5,300 branches, 18,623 historical
  source records, no duplicate Place identities or duplicate run decisions,
  `integrity_check=ok`, and zero foreign-key violations.
- Historical provenance SHA-256 matched production exactly:
  `03c37863884e44b6a95cc81cb44958b5d0087d2888fe893397594ff99e758fe8`.
  All LEGACY_UNKNOWN company/branch first-seen values remained null.
- Production registry stayed
  `ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22`;
  shadow stayed
  `ee5551ef5f6a82fa5e80bce02cf16b7635b10ad325e0fcf65a1c5ac4a71a43b7`.
- All 60 protected CSV/XLSX files matched aggregate SHA-256
  `cded21a0ad9917722416f48777f3cb85c65338ba7a72caf9b4993155069b54a9`.
  `leads_master` and all outreach files were unchanged; no outreach command ran.

Remaining Maps-only blockers are the lack of an enforced relevance/eligibility
boundary before downstream publication, incomplete geography auditing outside
the currently recognized locations, and the cost of full detail enrichment for
KNOWN/review records. Stop here pending explicit approval; do not migrate
production or begin Phase 3E.
