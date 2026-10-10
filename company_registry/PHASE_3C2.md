# Phase 3C.2 offline Google Search authoritative integration

Google Search supports the same mutually exclusive modes as Maps: `legacy`
(default), `shadow`, `authoritative-canary`, and `authoritative`. The compatible
`--company-registry-shadow` alias remains passive and keeps CSV decisions
authoritative.

## Verified Search boundaries

- CLI and scheduling: `utils/google_search_discovery.py`; `main()` delegates to
  `run_search_discovery()`, which validates mode and registry state before driver
  creation, then `discover_companies()` schedules query pages serially.
- Parsing and identity: `extract_organic_results()` supplies title/URL records;
  `search_observation()` creates a `GOOGLE_SEARCH` observation with a
  source-specific domain/fingerprint key. Query-derived geography remains
  provenance and is not treated as a branch address.
- Deduplication and persistence: legacy/incremental mode retains
  `KnownCompanies` plus atomic CSV checkpoints. Authoritative modes bypass both
  for identity decisions and commit through the shared resolver/service into an
  explicitly selected schema-v4 SQLite registry.
- Enrichment: Search company discovery has no enrichment stage; it discovers
  likely official sites only.
- Export and recovery: authoritative output is reconstructed from committed
  decisions by `export_new_companies()`. It includes source system/key and raw
  Search fields. Only non-review `NEW/CREATE_COMPANY` decisions publish.
- Errors: passive-shadow failures do not affect legacy decisions. Registry
  preview/commit errors propagate; interruption fails the durable run; export
  failure leaves committed decisions recoverable from SQLite.

## Offline invocation

```bash
.venv/bin/python -m utils.google_search_discovery \
  --discovery-mode authoritative-canary \
  --registry-database /tmp/gmaps-registry-v4.db \
  --authoritative-export-dir /tmp/gmaps-search-authoritative \
  --registry-run-id offline-search-001 \
  --query-file queries.txt \
  --limit 5
```

The database must already exist at schema v4. Production, existing shadow,
missing, and unsupported-schema databases are rejected before browser work.
Authoritative Search never writes the legacy `--output` CSV.

## Identity, budget, and coordination boundary

Maps and Search use the same normalization, repository, resolver, transactional
service, and registry. Their observation IDs deliberately remain source-specific;
cross-source company matching relies on shared normalized evidence, not equal
observation fingerprints. Name plus trustworthy domain can establish a company
match; a domain alone cannot. A Maps Place ID can add a branch to a company first
seen through Search.

One Search process owns one durable run and one in-memory acceptance budget.
Only a newly committed `NEW/CREATE_COMPANY` consumes capacity. Separate Maps and
Search processes have separate budgets even when they share a registry. A shared
cross-process run/budget is not supported: attempting to reuse the same durable
run ID is rejected. Callers needing one global limit across both sources must not
launch separate processes; durable cross-process accounting is deferred.

## Validation

- Focused Search/Maps/registry/shadow selection: 111/111 passed.
- Full repository suite: 724/724 passed on 2026-10-09.
- Production schema-v2 SHA-256 remained
  `ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22`.
- Existing shadow schema-v3 SHA-256 remained
  `ee5551ef5f6a82fa5e80bce02cf16b7635b10ad325e0fcf65a1c5ac4a71a43b7`.
- All 60 discovered CSV/XLSX files matched their before/after hashes.
