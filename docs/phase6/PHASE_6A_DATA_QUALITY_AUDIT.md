# Phase 6A Production Data Quality Audit

Run: `maps-20261031-batch02-300`  
Audit date: 2026-11-01  
Scope: read-only investigation of email integrity, geographic distribution, and qualification export semantics

## 1. Executive summary

The run and its three published exports are internally consistent. The discovery manifest contains 300 unique NEW companies; the employment and mission manifests contain 57 and 54 unique companies; their overlap is 54 and their union is 57. All CSV SHA-256 values match their manifests.

The apparent 300/300 discovery email coverage is not real contact coverage. Exactly 177 of the 300 `site_email` cells contain the non-empty sentinel `Not Available`; only 123 contain an `@` candidate. Qualification intentionally maps only policy-`ACCEPT` candidates into the normalized `email` field. Among all 300 NEW companies, 72 candidates are ACCEPT, 50 are REVIEW, one is REJECT, and 177 have no candidate. Among the 57 employment-eligible companies specifically, 28 are ACCEPT, 13 are REVIEW, and 16 have no candidate. Therefore 28/57 is the expected output of the current contact policy, not a CSV column-mapping failure.

No discovery email evidence is erased between registry persistence and qualification. The raw `site_email` remains in `discovery_observations.raw_payload_json`; qualification stores every parsed candidate and its decision/reason in `qualification_assessments.evidence_json.email_candidates`. However, REVIEW candidates are deliberately not copied to the qualified CSV's `email` column, so 13 employment-qualified companies have potentially legitimate but non-contact-ready evidence visible only in the database. There is also a separate, earlier loss risk: the website extractor keeps only the first extracted address before discovery. The number of additional addresses discarded in this run cannot be reconstructed from persisted artifacts.

The geographic skew is primarily caused by the global 300-NEW acceptance budget combined with query order. The durable registry has 1,326 Batch 02 decisions: 300 NEW, 1,002 KNOWN, 10 UPDATED, and 14 AMBIGUOUS review cases. Every durable decision came from query-file entries 1–11. Queries 1–10 supplied all 283 Tunisian NEW companies; query 11 supplied all 17 French NEW companies and exhausted the budget. Queries 12–30 could complete at the query-state level but the result loop exits at its pre-inspection budget guard. URL collection can occur before that guard, but exact per-query “results found” telemetry was not persisted.

The qualified exports are ELIGIBLE-only. Every employment row is `TARGET` + `ELIGIBLE` under `maps-dual-purpose-v3-business-relevance`, and the same is true for every mission row. `reasons_json` is valid JSON on every row and is purpose-specific. The policy explicitly does **not** claim that a current hiring or mission opportunity exists.

## 2. Audit method and immutable evidence

SQLite was opened with `sqlite3 -readonly`. CSVs, manifests, source, tests, and logs were read only. No scraper, resume, assessment, export, migration, or policy command was run.

Primary evidence:

- `data/local_registry/company_registry.db`
- `data/local_registry/exports/new_companies_maps-20261031-batch02-300.csv`
- `data/local_registry/exports/new_companies_maps-20261031-batch02-300.manifest.json`
- `data/local_registry/exports/qualified_employment_leads_maps-20261031-batch02-300.csv`
- `data/local_registry/exports/qualified_employment_leads_maps-20261031-batch02-300.manifest.json`
- `data/local_registry/exports/qualified_mission_leads_maps-20261031-batch02-300.csv`
- `data/local_registry/exports/qualified_mission_leads_maps-20261031-batch02-300.manifest.json`
- `data/local_registry/logs/writer_lease.jsonl*`
- `queries_batch02.txt`

The database run row is `SUCCESS`, started `2026-10-31T12:56:29.006011+00:00`, finished `2026-11-01T11:46:27.533493+00:00`, and has `new_company_limit=300`.

### Export integrity

| Artifact | Rows | Unique companies | Manifest/actual SHA-256 |
|---|---:|---:|---|
| Discovery | 300 | 300 | `ec512011b3d4ad583bc531d59be819857a932cd4b81012d1a8fb3f81c1ae7bdf` |
| Employment | 57 | 57 | `51c5abb4497d0d87853c39b530841b9a6d0a469015a6a17f48b39a478ee554d9` |
| Mission | 54 | 54 | `41f0ba346660ffabe08863833b9af28e798248bc501d05145d50f607ea8b580d` |

### Durable run outcomes

| Registry outcome | Count | Meaning in this audit |
|---|---:|---|
| NEW | 300 | New company, consumes the global budget |
| KNOWN | 1,002 | Matched known registry identity; may include a company first accepted earlier in this run through a different observation |
| UPDATED | 10 | Existing entity evidence/branch update outcome |
| AMBIGUOUS | 14 | `requires_review=1`; no company assessment is created |
| QUARANTINED | 0 | No quarantined decisions |
| Total decisions | 1,326 | Exact durable decision count |
| Qualification assessments | 1,312 | All resolved decisions; excludes 14 unresolved AMBIGUOUS decisions |

`DUPLICATE` and `REJECT` are not registry classifications. An exact prior run observation returns `decision_created=False` and is reported transiently as `same_run`; scraper noise and per-result exceptions are also transient. Those counters were printed at runtime but were not saved in a Batch 02 run report. Consequently, exact DUPLICATE, noise/rejected, results-found, and inspected totals cannot be recovered per query. The durable decision count is an exact lower bound on inspections that reached registry resolution, not an exact replacement for “results inspected.”

## 3. Root causes and confidence

| Finding | Confidence | Evidence |
|---|---|---|
| Discovery “300/300 email coverage” counts an unavailable sentinel as populated | High | 300 non-empty `site_email`; 177 equal `Not Available`; 123 contain `@` |
| Qualified `email` is a policy-selected contact field, not a rename of `site_email` | High | Only ACCEPT candidates populate evidence `email`; export reads that evidence field |
| 28/57 employment coverage is expected under the active policy | High | 28 ACCEPT + 13 REVIEW + 16 no candidate = 57 |
| Qualified CSV export did not drop ACCEPT addresses | High | CSV count, evidence count, and policy rows agree at 28 |
| REVIEW candidates remain in assessment evidence but are omitted from contact-ready `email` | High | 13/57 employment and 12/54 mission rows have REVIEW evidence with blank exported `email` |
| The 300-global-NEW budget and ordered early markets caused the Tunisia/France-only accepted set | High | NEW reaches 283 after query 10 and 300 during query 11; no later-query decisions exist |
| Google Maps result supply influenced the differing counts within queries 1–11 | Medium | Per-query durable volumes vary, but exact found/collected counts were not persisted |
| A per-query scrape limit caused the skew | Low / not supported | Authoritative inspection ceiling is `max(100, result_range * 10)` = 3,000; the largest durable per-query total is 223 across both passes |
| Unsupported country normalization caused this run's zero later-country acceptance | Low / not causal here | Later markets were never durably inspected after budget exhaustion; nevertheless several input countries are absent from the alias table and would normalize blank if reached without an explicit supported address match |
| Additional extracted addresses may be lost before discovery because only the first is retained | High for code behavior; unquantifiable for this run | `find_patterns()` replaces each list with `social_data[key][0]`; only the scalar is persisted |

## 4. Email data-flow and mapping analysis

### 4.1 Google Maps and website extraction

The Maps scraper obtains the Maps website URL and phone before enrichment (`utils/google_maps_scraper.py:1084-1095`). It then calls `PatternScrapper.find_patterns()` and merges its returned fields into the Maps row (`utils/google_maps_scraper.py:1164-1205`).

The website extractor:

1. scans rendered HTML and `mailto:` links for syntactically shaped candidates (`utils/web_site_scraper.py:325-399`);
2. rejects obvious placeholder domains/local parts and asset-looking suffixes (`utils/web_site_scraper.py:401-424`);
3. case-insensitively deduplicates candidates (`utils/web_site_scraper.py:426-469`);
4. returns the configured unavailable marker when no candidate exists, and otherwise keeps only the first candidate (`utils/web_site_scraper.py:480-523`).

The resulting scalar is stored as `site_email`. Therefore a non-empty `site_email` means either “first syntactically plausible extracted candidate” or the literal unavailable sentinel. It does not mean verified mailbox, deliverable address, approved outreach destination, or even an email candidate.

### 4.2 Normalization and registry persistence

The Maps row, including `site_email`, `source_query`, `country`, and `city`, is copied into the observation raw payload (`company_registry/maps_authoritative.py:233-267`). Normalization canonicalizes and hashes the full raw payload rather than projecting away `site_email` (`company_registry/resolver.py:33-57`). The registry writes it to `discovery_observations.raw_payload_json` (`company_registry/repository.py:203-228`), and the NEW discovery export reconstructs its extra columns directly from that JSON (`company_registry/exports.py:55-99`).

There is no registry column mapping from `site_email` to a separate company email column. The raw observation is the durable source of this evidence.

### 4.3 Qualification classification

Qualification checks source fields in this precedence order: `email`, `emails`, `email_address`, `site_email` (`company_registry/qualification.py:23-29`, `company_registry/qualification.py:293-322`). For this run, the Maps payload supplies `site_email`.

Candidate decisions are deterministic (`company_registry/qualification.py:200-263`):

- **ACCEPT**: syntactically valid and local part is one of the configured business roles (`admin`, `careers`, `commercial`, `contact`, `hello`, `hr`, `info`, `jobs`, `office`, `recruiting`, `recruitment`, `sales`, `support`). Reason: `business_role_address`.
- **REVIEW**: syntactically valid but the local part is not on the role allowlist. Reason: `uncertain_local_part`.
- **REJECT**: empty/unavailable, too long, malformed, placeholder, asset-like, automated sender, or telemetry address. The unavailable sentinel produces no candidate because `clean_text()` reduces it to empty before candidate assessment.

Only the first ACCEPT candidate becomes `evidence.email`; every candidate and decision remains in `evidence.email_candidates` (`company_registry/qualification.py:293-347`). No MX lookup, SMTP verification, deliverability check, consent check, role ownership check, or public-provider restriction is performed. The existing test explicitly accepts a role address at a public mail provider (`tests/test_company_registry_qualification.py:326-329`). “ACCEPT” therefore means policy-approved syntax/role, not contact-ready in the stronger operational or legal sense.

Employment eligibility does not require either email or phone; TARGET relevance plus a usable website, location, and no conflict is sufficient (`company_registry/qualification.py:415-430`). Mission requires direct contact, but a phone satisfies that gate even when normalized email is blank (`company_registry/qualification.py:474-492`). This is why a company can correctly appear in either qualified export without an exported email.

### 4.4 Exact Batch 02 email counts

| Population | Rows | Non-empty source field | Unavailable/no candidate | Any candidate | ACCEPT | REVIEW | REJECT |
|---|---:|---:|---:|---:|---:|---:|---:|
| All NEW discovery companies | 300 | 300 | 177 | 123 | 72 | 50 | 1 |
| Employment ELIGIBLE | 57 | 57 | 16 | 41 | 28 | 13 | 0 |
| Mission ELIGIBLE | 54 | 54 | 14 | 40 | 28 | 12 | 0 |

The 72 ACCEPT addresses among all NEW companies use these policy-recognized local parts: `contact` 49, `hello` 11, `info` 6, `commercial` 3, and `admin`, `sales`, `support` one each. There were no placeholder or malformed qualification outcomes among the 300 NEW rows, consistent with the extractor's earlier syntax/placeholder filter. The single REJECT was telemetry-shaped.

Representative anonymized structures:

- `contact@<company-domain>` — ACCEPT / `business_role_address`.
- `<non-role-person-or-department>@<company-domain>` — REVIEW / `uncertain_local_part`; potentially legitimate but not auto-approved.
- `<telemetry-local>@<vendor-or-site-domain>` — REJECT / `telemetry_address`.
- `Not Available` — non-empty discovery CSV cell, but normalized to no candidate.

### 4.5 Export mapping and information retention

Qualified row construction loads `qualification_assessments.evidence_json`, then maps its normalized `email` key directly to the CSV `email` column (`company_registry/exports.py:243-269`). It does not read raw `site_email` at export time. This is an intentional policy boundary, not a typo or missing field alias.

Legitimate information handling is therefore:

- ACCEPT evidence: retained in raw observation, assessment audit, normalized evidence `email`, and qualified CSV `email` when the company is eligible.
- REVIEW evidence: retained in raw observation and `email_candidates`, but intentionally absent from qualified CSV `email`. This is a visibility gap for human review, not database loss.
- REJECT evidence: retained in raw observation and candidate audit, not promoted.
- Additional addresses found on the same website: potentially discarded before the Maps row is built because only index zero is retained. Historical Batch 02 artifacts cannot quantify this pre-persistence loss.

## 5. Geographic distribution and per-query analysis

### 5.1 Execution order and resume history

`queries_batch02.txt` orders ten Tunisia searches first, five France searches next, and other countries afterward. Worker coordination partitions query indices round-robin (`utils/threading_controller.py:305-338`). Durable decision timestamps show sequential query order with no overlapping decision intervals:

- First pass on 2026-10-31: queries 1 through 11.
- Resume/pass on 2026-11-01: queries 1 through 11 again.
- The operator baseline states that all 30 queries completed. Query states were not persisted to the production database or a Batch 02 run report, so completion of 12–30 is accepted from that baseline, while their zero durable decision count is independently confirmed from SQLite.

Per-pass durable outcomes for queries 1–11 were:

| Query | 2026-10-31 decisions / NEW | 2026-11-01 decisions / NEW |
|---:|---:|---:|
| 1 | 112 / 3 | 111 / 3 |
| 2 | 91 / 8 | 105 / 14 |
| 3 | 111 / 103 | 51 / 2 |
| 4 | 91 / 20 | 51 / 0 |
| 5 | 72 / 28 | 27 / 5 |
| 6 | 20 / 3 | 20 / 1 |
| 7 | 111 / 16 | 111 / 6 |
| 8 | 71 / 64 | 45 / 6 |
| 9 | 11 / 0 | 26 / 0 |
| 10 | 11 / 1 | 11 / 0 |
| 11 | 32 / 12 | 35 / 5 |

This explains the high KNOWN count: the same early queries were run again on resume, and their result sets also overlap one another.

### 5.2 Per-query durable outcomes

`Review` below means AMBIGUOUS plus QUARANTINED (the latter is zero everywhere). `Durable` is the exact count that reached a registry decision. Exact Maps “found,” scraper “inspected,” same-run DUPLICATE, and transient rejected/noise/error counts are **not available** (`n/a`) because no Batch 02 query-summary artifact persisted them.

| # | Query | Durable | NEW | KNOWN | UPDATED | Review | NEW country | Duplicate / rejected |
|---:|---|---:|---:|---:|---:|---:|---|---|
| 1 | software development company Tunis Tunisia | 223 | 6 | 215 | 0 | 2 | Tunisia 6 | n/a |
| 2 | web application development company Sousse Tunisia | 196 | 22 | 171 | 3 | 0 | Tunisia 22 | n/a |
| 3 | IT consulting company Nabeul Tunisia | 162 | 105 | 55 | 0 | 2 | Tunisia 105 | n/a |
| 4 | software engineering company Sfax Tunisia | 142 | 20 | 121 | 1 | 0 | Tunisia 20 | n/a |
| 5 | web agency Hammamet Tunisia | 99 | 33 | 62 | 1 | 3 | Tunisia 33 | n/a |
| 6 | ERP integrator Tunis Tunisia | 40 | 4 | 36 | 0 | 0 | Tunisia 4 | n/a |
| 7 | mobile app agency Tunis Tunisia | 222 | 22 | 197 | 2 | 1 | Tunisia 22 | n/a |
| 8 | IT services company Bizerte Tunisia | 116 | 70 | 41 | 0 | 5 | Tunisia 70 | n/a |
| 9 | custom software development company Tunisia | 37 | 0 | 37 | 0 | 0 | — | n/a |
| 10 | cloud consulting company Tunisia | 22 | 1 | 21 | 0 | 0 | Tunisia 1 | n/a |
| 11 | software development agency Lyon France | 67 | 17 | 46 | 3 | 1 | France 17 | n/a |
| 12 | web development company Lille France | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 13 | IT consulting company Toulouse France | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 14 | software agency Nantes France | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 15 | software outsourcing company Bordeaux France | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 16 | web application development company Rotterdam Netherlands | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 17 | software development company Utrecht Netherlands | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 18 | IT consultancy Eindhoven Netherlands | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 19 | web development agency Antwerp Belgium | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 20 | software engineering company Ghent Belgium | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 21 | software development company Porto Portugal | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 22 | IT consulting company Madrid Spain | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 23 | web development company Barcelona Spain | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 24 | software development company Krakow Poland | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 25 | IT outsourcing company Wroclaw Poland | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 26 | software engineering company Prague Czech Republic | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 27 | software development agency Bucharest Romania | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 28 | web application development company Munich Germany | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 29 | IT consulting company Hamburg Germany | 0 | 0 | 0 | 0 | 0 | — | n/a |
| 30 | software development company Ottawa Canada | 0 | 0 | 0 | 0 | 0 | — | n/a |
| **Total** |  | **1,326** | **300** | **1,002** | **10** | **14** | **Tunisia 283; France 17** | **n/a** |

### 5.3 Country extraction and normalization

Maps geography prefers an address-derived country/city, then fills compatible gaps from the query (`utils/geography.py:142-207`). The address is explicitly stronger than the prospecting query (`utils/google_maps_scraper.py:1148-1155`).

The supported country alias table is limited to United Kingdom, Switzerland, Belgium, Tunisia, Ireland, Malta, France, and Canada (`utils/geography.py:15-24`). It does **not** include Netherlands, Portugal, Spain, Poland, Czech Republic, Romania, or Germany. Query extraction for those countries returns blank (`utils/geography.py:101-120`), and an address ending in one of those unsupported country names also cannot normalize it (`utils/geography.py:142-184`). This is a confirmed coverage defect, but not the cause of this run's accepted distribution because those queries had no durable inspection after the budget was full.

All 1,326 persisted decisions have a non-blank normalized country: Tunisia for decisions from queries 1–10, France for query 11. Among NEW decisions the split is exactly Tunisia 283 / France 17.

### 5.4 Budget and Google Maps behavior

The durable budget counts NEW + CREATE_COMPANY + no-review decisions and derives remaining capacity from SQLite (`company_registry/service.py:50-76`, `company_registry/service.py:159-165`). Authoritative resolution reserves only possible NEW outcomes and releases the reservation for known/review outcomes (`company_registry/maps_authoritative.py:713-767`).

The scraper collects URLs, then checks `committed_exhausted()` before inspecting each result (`utils/google_maps_scraper.py:1318-1326`). Once query 11 accepted the 300th NEW company, subsequent queries could still load/search/collect and report COMPLETED, but their result loops could not inspect cards or create decisions. A global-limit termination is intentionally published as a successful run (`utils/threading_controller.py:269-276`).

Google Maps behavior affected which and how many candidates were available to the early queries; that is inherent to the source. It cannot be quantified exactly here because `search_results_found`, `place_urls_extracted`, per-query `inspected`, same-run duplicates, and result errors were not preserved in a Batch 02 artifact. The database confirms only the outcomes that reached resolution. The query-level inspection ceiling was 3,000 in this configuration (`utils/google_maps_scraper.py:233-237`), far above observed durable totals, so there is no evidence that this ceiling caused the country skew. A scroll/feed limit or Maps ranking may have bounded each early query's source pool, but that remains a hypothesis.

## 6. Qualification export semantics

The active default policy is declared as `maps-dual-purpose-v3-business-relevance` (`company_registry/qualification.py:87-172`). Batch 02 has 1,312 assessments and every one uses that exact policy version; no v1/v2 assessment was added to this run. Historical records elsewhere in the registry were not altered.

The concepts are related but distinct:

- `relevance` describes business fit: `TARGET`, `POSSIBLE`, `NOISE`, or `UNKNOWN`.
- `eligibility` is the publication gate: `ELIGIBLE`, `REVIEW`, or `EXCLUDED`.
- `qualification_policy_version` identifies the immutable rule set used to create the assessment.
- `reasons_json` is the purpose-specific explanation for relevance and eligibility; it does not contain the email candidate audit, which is in assessment `evidence_json`.

Under v3, TARGET is necessary but not sufficient. Employment also requires a usable official website, location evidence, and no identity/location conflict (`company_registry/qualification.py:372-430`). Mission additionally requires direct contact (phone or ACCEPT email), a usable website, location, and no conflict (`company_registry/qualification.py:432-492`).

### NEW-company assessment distribution

| Purpose | Relevance | Eligibility | Count |
|---|---|---|---:|
| Employment | TARGET | ELIGIBLE | 57 |
| Employment | TARGET | REVIEW | 18 |
| Employment | POSSIBLE | REVIEW | 26 |
| Employment | NOISE | EXCLUDED | 199 |
| Mission | TARGET | ELIGIBLE | 54 |
| Mission | TARGET | REVIEW | 21 |
| Mission | POSSIBLE | REVIEW | 224 |
| Mission | NOISE | EXCLUDED | 1 |

The export SQL requires NEW + CREATE_COMPANY + no registry review + purpose eligibility ELIGIBLE, and then deduplicates by company (`company_registry/exports.py:210-270`). Manifest verification rejects any non-ELIGIBLE qualified row and any duplicate company (`company_registry/exports.py:560-590`). Direct inspection confirms:

- Employment: 57/57 `TARGET`, 57/57 `ELIGIBLE`, 57/57 policy v3.
- Mission: 54/54 `TARGET`, 54/54 `ELIGIBLE`, 54/54 policy v3.
- Employment reasons: all 57 include `target_technical_business_category`, `strong_relevance_website_and_location`, and `current_hiring_not_established`.
- Mission reasons: all 54 include `target_technical_service_category`, `explicit_fit_with_contact_and_evidence_quality`, and `active_mission_not_established`.
- Every `reasons_json` parses as a JSON list.
- Employment/mission overlap: 54; union: 57.

Thus both files contain exclusively ELIGIBLE companies. They are business-relevance lead lists, not evidence that the companies are currently hiring or currently procuring a mission.

## 7. Recommended minimal fixes for Phase 6B (not implemented)

1. **P0 — Correct email coverage semantics.** Treat configured unavailable values as missing in coverage metrics. Prefer blank/null for absent `site_email`, or add an explicit `site_email_status`; do not count non-empty cells alone.
2. **P0 — Preserve all extracted candidates.** Stop collapsing website email lists to index zero before registry persistence. Persist a bounded candidate list while retaining a separate selected scalar for compatibility.
3. **P0 — Keep the contact-ready boundary.** Do not promote REVIEW candidates into `email`. Add safe `email_decision`/`email_reason` columns, or a separate human-review export, so the 13 employment-qualified REVIEW candidates are discoverable without mislabeling them as approved.
4. **P0 — Persist per-query audit telemetry.** Store query index, execution attempt/resume, state, found/collected/inspected counts, NEW/KNOWN/UPDATED/AMBIGUOUS/QUARANTINED/same-run/noise/error counts, country outcomes, and the budget-at-start/end snapshot in an immutable run report.
5. **P1 — Make geographic allocation explicit.** If balanced country coverage is required, use per-market quotas, staged budgets, or round-robin market scheduling. A global first-accepted budget over an ordered file necessarily favors early markets.
6. **P1 — Expand geography aliases.** Add every country present in the production query file, including Netherlands, Portugal, Spain, Poland, Czech Republic, Romania, and Germany, with address/query tests and canonical names.
7. **P2 — Clarify resume semantics.** Persist completed query checkpoints or explicitly document that resume restarts the query file. Repeating queries 1–11 created avoidable overlap and consumed time before later markets.
8. **P2 — Strengthen contact readiness separately from syntax policy.** If operationally needed, add domain plausibility/deliverability and consent/workflow checks as a versioned policy; do not silently reinterpret historical v3 ACCEPT records.

## 8. Regression tests needed

1. Discovery coverage excludes `Not Available`, `N/A`, blank, and other configured sentinels.
2. ACCEPT, REVIEW, and REJECT candidates round-trip from raw observation to assessment evidence without mutation; only ACCEPT populates normalized/exported `email`.
3. An ELIGIBLE company with REVIEW-only email exports a blank `email` plus an auditable status/review path after the chosen fix.
4. Multiple website addresses survive extraction and persistence in deterministic order; selection of the contact-ready scalar remains stable.
5. Placeholder, automated, telemetry, malformed, role, public-provider, and non-role cases remain versioned and deterministic. Existing coverage starts at `tests/test_company_registry_qualification.py:249-335`.
6. Qualified exports contain only the requested policy version, purpose, NEW/non-review company decisions, and `ELIGIBLE` rows; manifests match row IDs, company IDs, counts, and hashes.
7. A mixed employment/mission fixture verifies TARGET-versus-eligibility distinctions and purpose-specific `reasons_json`.
8. Every country in `queries_batch02.txt` normalizes from both query and representative Maps-address forms. Existing geography coverage is limited (`tests/test_geography_pipeline.py:30-98`).
9. A global-budget test proves later queries receive zero inspections after exhaustion and records that fact per query, while distinguishing URLs found from cards inspected.
10. A market-balanced budget/scheduler test verifies the intended per-country allocation and remains restart-safe.
11. Resume tests verify whether completed queries are skipped or intentionally replayed and that query-attempt telemetry makes the behavior explicit.
12. Historical v1/v2/v3 assessments and manifests remain selectable and unchanged when a future policy version is introduced.

## 9. Safety confirmation

This Phase 6A audit did not modify source code, SQLite databases, production CSVs, manifests, policies, or historical v1/v2/v3 records. It did not run or resume scraping, create qualification assessments, or publish/overwrite exports. The only created deliverable is `docs/phase6/PHASE_6A_DATA_QUALITY_AUDIT.md`. Phase 6B was not implemented.
