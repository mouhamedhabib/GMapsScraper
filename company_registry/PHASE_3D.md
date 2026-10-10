# Phase 3D isolated live canary

## Recommendation

**FAIL / incomplete for Phase 3D as a whole.** The controlled live Maps canary
passed. Google Search presented a verification page before any organic result
could be inspected, so cross-source matching could not be verified live. No
production transition or Phase 3E work is authorized by this result.

## Isolation and preflight

- Artifact root: `data/phase_3d_canary/20261009T091209Z/` (Git-ignored by
  `/data/`).
- Disposable registry: `data/phase_3d_canary/20261009T091209Z/registry-v4.db`.
- Maps exports: `data/phase_3d_canary/20261009T091209Z/maps/exports/`.
- Search exports: `data/phase_3d_canary/20261009T091209Z/search/exports/`.
- Production `data/company_registry.db` was opened read-only and copied with
  the SQLite backup API. Only the disposable copy was explicitly migrated.
- Production was schema v2; existing shadow was schema v3; the disposable copy
  verified as schema v4 with `integrity_check=ok` and no foreign-key violations.
- Pre/post production SHA-256:
  `ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22`.
- Pre/post shadow SHA-256:
  `ee5551ef5f6a82fa5e80bce02cf16b7635b10ad325e0fcf65a1c5ac4a71a43b7`.
- All 60 discovered CSV/XLSX paths had identical pre/post hash manifests.
  This includes `leads_master.csv`, outreach files, and legacy production
  exports. No email sender exists in this repository and no outreach builder or
  queue command was invoked.
- Focused offline safety tests: 108 passed with `unittest`. The initial
  `pytest` invocation could not run because pytest is not installed in the
  virtual environment; this was a runner/tooling issue, not a test failure.

The copied history initially contained 3,838 companies, 5,297 branches, and
18,623 source records. All 3,838 historical companies retained
`LEGACY_UNKNOWN`; all historical company and branch `first_seen_at` values
remained null. After the canary, only `last_seen_at`/`updated_at` changed for the
five KNOWN historical matches. Historical origin, first-seen, creation,
company/branch linkage, raw provenance, and content hashes were unchanged.

## Commands

The disposable registry was created with Python's `sqlite3.Connection.backup`
from a read-only URI, then migrated explicitly:

```bash
.venv/bin/python -m company_registry.storage migrate \
  --database data/phase_3d_canary/20261009T091209Z/registry-v4.db
```

The successful Maps NEW-path run used:

```bash
.venv/bin/python maps.py \
  --discovery-mode authoritative-canary \
  --registry-database data/phase_3d_canary/20261009T091209Z/registry-v4.db \
  --registry-run-id phase3d-maps-20261009T091209Z-r3 \
  --authoritative-export-dir data/phase_3d_canary/20261009T091209Z/maps/exports \
  --query-file data/phase_3d_canary/20261009T091209Z/maps/queries.txt \
  --threads 1 --low-resource --limit 3 \
  --output-folder data/phase_3d_canary/20261009T091209Z/maps/runtime \
  --export-dir data/phase_3d_canary/20261009T091209Z/maps/runtime \
  --known-companies-dir data/phase_3d_canary/20261009T091209Z/maps/runtime \
  --disable-verbose
```

Two preceding Maps attempts used the same flags and paths with the original
Tunis query file. Their exact run-ID substitutions were
`--registry-run-id phase3d-maps-20261009T091209Z` (sandbox-blocked browser
launch) and `--registry-run-id phase3d-maps-20261009T091209Z-r2` (controlled
KNOWN diagnostic). Neither run ID was reused.

The Search run used the same registry and a separate run ID/budget:

```bash
.venv/bin/python -m utils.google_search_discovery \
  --discovery-mode authoritative-canary \
  --registry-database data/phase_3d_canary/20261009T091209Z/registry-v4.db \
  --registry-run-id phase3d-search-20261009T091209Z \
  --authoritative-export-dir data/phase_3d_canary/20261009T091209Z/search/exports \
  --query-file data/phase_3d_canary/20261009T091209Z/search/queries.txt \
  --output data/phase_3d_canary/20261009T091209Z/search/runtime/discoveries.csv \
  --limit 3 --delay 0 --timeout 15 --verbose
```

## Maps outcomes

The first Maps launch (`phase3d-maps-20261009T091209Z`) was contained by the
managed filesystem and could not create the undetected-chromedriver cache. It
committed no observations and finalized `PARTIAL` with a verified zero-row
export. A fresh run ID was used rather than resuming it.

The Tunis diagnostic (`phase3d-maps-20261009T091209Z-r2`) was intentionally
stopped after six inspected cards to avoid enriching 100 known results. Five
decisions were `KNOWN/MATCH_ONLY`; one card hit a browser `MaxRetryError` before
resolution. Budget remained reserved 0, committed 0, remaining 3. The run was
`PARTIAL`; its zero-row export manifest verifies. Reported resources were one
Chrome instance, six detail pages, 28/26 temporary tabs opened/closed, and
99.78 MB Python RSS at completion.

The Tozeur run (`phase3d-maps-20261009T091209Z-r3`) completed successfully:

- Query: `software companies Tozeur Tunisia`.
- Queries scheduled/completed: 1/1; results inspected: 3.
- Decisions: 3 NEW, 0 KNOWN, 0 UPDATED, 0 AMBIGUOUS, 0 QUARANTINED.
- Branches created: 3.
- Budget: reserved 0 at completion, committed 3, remaining 0; no release path
  was needed by the three successful NEW decisions.
- Resolver/registry errors: none.
- Export: 3 rows, SHA-256
  `236106811ded37f01361e4b6e76c074297108c10aef971b720108c453c806268`;
  manifest hash, row count, and ordered decision IDs verified.
- Resources: one Chrome instance, three detail pages, 7/7 temporary tabs, and
  83.10 MB Python RSS at completion.

Created companies were DevAppLand, TOZEUR EXPERIENCE, and LIGHT AGENCY. Exact
company IDs and Place-ID branch identities were unique. DevAppLand supplied
`devappland.com`; the other two had no usable website. The latter two names are
weakly aligned with the software query and remain a lead-quality/identity risk,
although their distinct Maps Place IDs make the persisted branches unambiguous.

## Search outcome and cross-source verification

The Search query was `DevAppLand Tozeur official website`, selected to test the
Maps-created DevAppLand company ID using name plus trustworthy domain. Google
verification appeared before results were inspectable. Headless mode stopped
safely; no CAPTCHA bypass or unattended manual flow was attempted.

- Results inspected and decisions: 0.
- NEW/KNOWN/UPDATED/AMBIGUOUS/QUARANTINED: all 0.
- Budget committed: 0 of the independent Search limit of 3.
- Export: zero rows; manifest verified with SHA-256
  `6bfdf48d1327555ae0fc744a9b24b7d05c11caf8bf21e3597a3950b8b94749c2`.
- Cross-source company-ID preservation: **not verified live**. The three Maps
  IDs remained unchanged, but Search supplied no observation to match.
- Observability limitation: the Search run is stored as `SUCCESS` even though
  verification prevented query completion. Treat that status as insufficient
  evidence for release readiness.

## Final integrity and remaining risks

The final disposable registry had 3,841 companies, 5,300 branches, eight
observations/decisions, no reviews, no duplicate canary decision keys, no
duplicate Maps Place IDs, `integrity_check=ok`, and no foreign-key violations.
No ambiguous identities were automatically merged or corrected.

Remaining risks are the unexecuted live cross-source match, Search verification
handling/status observability, Maps' expensive full enrichment of KNOWN cards,
and query relevance for weakly aligned results. Retry Search only with a new
run ID in an approved interactive environment; do not migrate production or
begin Phase 3E without separate approval.
