# Phase 6D Durable Per-Query Budgeting

Date: 2026-11-01  
Scope: optional authoritative Google Maps NEW-company limits per query; no live
scraping, historical mutation, qualification change, or export-format change

## Outcome

Phase 6D adds an optional `--max-new-per-query N` limit to authoritative Maps
runs. The existing global `--max-new`/`--limit` remains the maximum number of
authoritative NEW companies for the entire run. Omitting the new option retains
the previous configuration hash and runtime behavior.

Each query-file entry is identified by its stable zero-based position. This is
intentional: repeated query text on two lines represents two independently
budgeted query entries, while resume must supply the same ordered query list.

## CLI syntax

Fresh authoritative run:

```bash
.venv/bin/python maps.py \
  --query-file queries.txt \
  --max-new 300 \
  --max-new-per-query 20 \
  --discovery-mode authoritative \
  --registry-database /path/to/schema-v7-registry.db \
  --registry-run-id maps-example \
  --authoritative-export-dir /path/to/isolated-exports
```

Explicit resume uses the same limits and ordered query file:

```bash
.venv/bin/python maps.py \
  --query-file queries.txt \
  --max-new 300 \
  --max-new-per-query 20 \
  --discovery-mode authoritative \
  --registry-database /path/to/schema-v7-registry.db \
  --resume-run-id maps-example \
  --authoritative-export-dir /path/to/isolated-exports
```

The guarded local release wrapper also accepts the option:

```bash
.venv/bin/python local_release.py run \
  --query-file queries.txt \
  --max-new 300 \
  --max-new-per-query 20 \
  --run-id maps-example
```

`--max-new` is now an alias of the historical Maps `--limit` spelling. The
per-query option is rejected outside authoritative modes because legacy CSV
state cannot provide the required durable accounting.

## Implementation and database semantics

Schema v7 adds nullable `discovery_runs.max_new_per_query` and the
`discovery_run_queries` ledger. A configured run stores each normalized query,
its index, and its committed NEW count. Existing schema-v6 databases require an
explicit migration before authoritative writes; read-only Phase 6B/6C reporting
continues to accept validated schema-v6 sources.

For a candidate NEW company, `RegistryService.resolve_with_metadata()` performs
all of the following in one `BEGIN IMMEDIATE` transaction:

1. validates the RUNNING state and writer lease;
2. reuses an existing same-run decision without consuming capacity;
3. checks the global committed NEW count;
4. checks the indexed query's durable committed count;
5. persists entity, observation, decision, and run-company data; and
6. increments the query ledger only for a newly committed, non-review
   `NEW/CREATE_COMPANY` decision.

SQLite's immediate writer transaction serializes concurrent commits. A crash or
exception rolls back the company, decision, and query count together. KNOWN,
UPDATED, duplicate/replayed, AMBIGUOUS, QUARANTINED, and other review-only
outcomes do not increment the ledger. When one query reaches its cap, the
scraper stops accepting NEW results from that query and workers continue with
later query indexes. Global exhaustion retains its existing run-wide behavior.

## Resume behavior

The per-query limit participates in the durable run configuration hash only
when supplied. This preserves hashes for historical runs where the option was
omitted. A capped run persists the limit on `discovery_runs` and the complete
indexed query ledger before decisions are accepted.

Explicit resume must provide the same global limit, per-query limit, discovery
mode, export root, and ordered query list. A mismatch fails before a writer
lease is acquired. Resume reloads the global committed count and reads each
query count from SQLite; interruption, retry, process restart, and replay cannot
reset either budget.

## Files modified

- `company_registry/schema.py` — schema v7 migration and query ledger.
- `company_registry/storage.py` — v7 validation plus read-only schema-v6
  compatibility for Phase 6B/6C consumers.
- `company_registry/service.py` — transactional query snapshots, enforcement,
  and counter updates.
- `company_registry/discovery_adapter.py` and
  `company_registry/maps_authoritative.py` — query context, configuration hash,
  resume validation, and query-limit outcomes.
- `maps.py`, `local_release.py`, `utils/threading_controller.py`, and
  `utils/google_maps_scraper.py` — CLI forwarding and indexed worker handling.
- `tests/test_company_registry_query_budget.py` and existing schema/CLI/local
  release integration tests — Phase 6D regressions and v7 expectations.
- `context/progress-tracker.md`, `context/code-map.md`, and
  `context/architecture.md` — repository status and invariants.

Phase 6B email classification, qualification policy, Phase 6C unified export
schema, scraper query ordering, global scheduling, and export publication logic
were not changed.

## Tests performed

Focused Phase 6B/6C/6D compatibility command:

```text
.venv/bin/python -m unittest \
  tests.test_company_registry_query_budget \
  tests.test_maps_cli_validation \
  tests.test_local_release \
  tests.test_company_registry_unified_export \
  tests.test_company_registry_email_reporting
```

Result: 58/58 passed.

Complete offline command:

```text
.venv/bin/python -m unittest discover -q
```

Result: 855/855 passed in 253.681 seconds.

Coverage includes omitted-option compatibility, per-query and global
enforcement, concurrent workers, interruption/resume, transactional crash
rollback, duplicate/KNOWN/review/rejected decisions, query exhaustion followed
by later queries, configuration mismatch, ledger constraints, existing unified
export compatibility, and read-only historical reporting.

## Limitations

- Durable per-query limits are available only for SQLite-authoritative Maps
  runs. Legacy CSV mode has no equivalent durable transaction boundary.
- A query that is already full may still inspect KNOWN/review results until the
  next candidate NEW result reports query exhaustion; it cannot commit beyond
  the cap.
- Resume replays the ordered query list and relies on durable same-run decision
  idempotency; browser position is not persisted.
- Schema migration remains an explicit operator action. Runtime opening never
  silently migrates an existing registry.

## Safety verification

No live browser or scraping provider was invoked, no emails were sent, and no
historical database, registry, CSV, manifest, or protected shadow was migrated
or modified. All writable database tests used temporary directories.

After validation, the protected local registry retained its previously recorded
SHA-256:

`a379add919c4facfae5f1065a4757be2af9f8f2c89c2ee57a7f6ba511c16fb74`

Batch 02 export hashes also remained unchanged:

- discovery: `ec512011b3d4ad583bc531d59be819857a932cd4b81012d1a8fb3f81c1ae7bdf`;
- employment: `51c5abb4497d0d87853c39b530841b9a6d0a469015a6a17f48b39a478ee554d9`;
- mission: `41f0ba346660ffabe08863833b9af28e798248bc501d05145d50f607ea8b580d`.

Phase 6D stops here. Phase 6E has not been started.
