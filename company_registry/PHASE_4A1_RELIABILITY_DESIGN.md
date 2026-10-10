# Phase 4A.1 — Local Reliability Design

Status: **DESIGN COMPLETE; NO IMPLEMENTATION AUTHORIZED**  
Scope: local WSL Ubuntu, Google Maps authoritative schema-v5 path only. This
review made no application, schema, database, browser, Search, or outreach
change.

## 1. Current lifecycle and transaction boundaries

```text
maps.py: run_maps_discovery()/GMapsScraper.scrape_maps_data()
  -> FastSearchAlgo.__init__()
     -> AuthoritativeMapsSession.__init__()
        -> validate_authoritative_paths()
        -> _create_run(): BEGIN IMMEDIATE; INSERT RUNNING; COMMIT
  -> fast_search_algorithm()
     -> worker GoogleMaps.start_scrapper(); lazy browser creation
     -> _scrape_result_and_store()
        -> maps_observation()
        -> AuthoritativeMapsSession.resolve()
           -> adapter.preview()                         [read only]
           -> RunAcceptanceBudget.try_reserve()         [memory only]
           -> adapter.commit()
              -> RegistryService.resolve_with_metadata()
                 BEGIN IMMEDIATE
                 reuse same-run decision, or resolve and write company/branch,
                 identities, observation, run decision, and run_companies
                 COMMIT (all-or-nothing)
           -> reservation.commit()/release()            [after SQLite commit]
     -> worker finally: GoogleMaps.quit_driver()
     -> all workers join
     -> AuthoritativeMapsSession.complete()
        -> _set_status(SUCCESS or PARTIAL); COMMIT
        -> export_new_companies()                       [filesystem]
        -> assess_run(): BEGIN IMMEDIATE; assessments; COMMIT
        -> export_qualified_leads(employment)           [filesystem]
        -> export_qualified_leads(mission)              [filesystem]
```

Run creation is therefore before lazy browser startup. `_set_status()` is a
separate autocommit transaction and runs before publication. A handled fatal
worker exception calls `fail(FAILED|INTERRUPTED)` and does not publish. Ordinary
query/browser failures are converted to terminal query states; orchestration
then calls `complete(status="PARTIAL")` and publishes.

Current transition guards are incomplete: `_set_status()` updates only a
`RUNNING` row but does not check its affected-row count, while
`RegistryService._ensure_run()` checks only that the run exists—not that it is
`RUNNING`. Consequently a direct adapter call, or a retained session object,
can append a decision after terminal status and make an already published
manifest stale. Normal CLI flow does not intentionally do this, but the storage
interface does not enforce the lifecycle invariant.

Each export reads committed rows on a separate connection, writes and `fsync`s
temporary CSV/JSON files, atomically replaces the CSV, then replaces the
manifest as its commit marker. It does not `fsync` the directory and completion
does not call `verify_export_manifest()`. Browser cleanup is best-effort:
workers call `quit_driver()` in `finally`, and `driver.quit()` errors are
ignored. No Python cleanup is possible after `SIGKILL`, WSL loss, or host loss.

## 2. Confirmed failure windows

| Crash point | Durable SQLite state | What the next launch does today |
| --- | --- | --- |
| Before browser startup | If session construction has not run: nothing. After construction: a committed `RUNNING` row; a handled driver failure normally ends `PARTIAL` with zero-row exports. | A new ID starts a fresh zero-count in-memory budget. Reusing the ID is rejected. A hard-crash `RUNNING` row is not reconciled. |
| After run creation, before scraping | `RUNNING`, no decisions. | Same behavior: no resume; old run stays orphaned. |
| After an observation commit | Entity/identity changes, observation, decision, and run summary all survive together. | Same ID is rejected. A fresh run re-evaluates the evidence, normally as `KNOWN`; the old run remains incomplete. |
| After a NEW commit, before budget bookkeeping | The complete `NEW/CREATE_COMPANY` decision survives; the reservation/committed counter does not. | A fresh process starts at zero. This can exceed the operator's intended cap across retries. |
| During CSV export | Decisions survive. A temp file, new CSV with no manifest, or new CSV with an old mismatching manifest may remain. A caught error changes the run to `PARTIAL`; hard death may preserve its earlier status. | CLI recovery is unavailable. Direct export calls can deterministically rebuild, but startup does not do so. |
| After marking `SUCCESS`, before export verification | `SUCCESS` and `finished_at` survive even if any discovery/qualification export or manifest is absent, stale, or corrupt. | Status is trusted neither reconciled nor downgraded; same ID is rejected. This is the false-success defect. |
| Process termination / WSL shutdown | Completed SQLite transactions survive; SQLite rolls back an interrupted transaction on recovery. Catchable signals normally stop workers and publish `PARTIAL`; abrupt loss leaves the last committed status. Filesystem replaces lack directory durability. | No integrity/recovery preflight, lease check, export reconciliation, or owned-Chrome cleanup occurs. Browser `finally` runs only for catchable control flow. |

SQLite protects transaction atomicity, but the application does not explicitly
set/verify journal or synchronous policy and does not run post-shutdown
`integrity_check`; these are operational assumptions, not recovery proof.
Independently of crashes, the missing write-state guard permits a post-terminal
decision to invalidate an otherwise valid export without changing `SUCCESS`.

## 3. Proposed recovery state machine

```text
fresh (ID must not exist) -> RUNNING -> FINALIZING -> SUCCESS
                                |            |
                                |            +-- crash/error: remain FINALIZING
                                +-- signal: INTERRUPTED --explicit resume--> RUNNING
                                +-- fatal/non-retryable: FAILED
                     stale lease + explicit resume --> RUNNING
FINALIZING --verified incomplete publication--> PARTIAL
PARTIAL/FAILED: terminal; export-only reconstruction allowed, scraping resume denied
```

`SUCCESS` is written last, in one transaction, only after workers have joined,
qualification is committed, all three required manifests (discovery,
employment, mission) have been rebuilt and verified, and the intended query set
completed. `FINALIZING` forbids new decisions. A publication failure stays
`FINALIZING`; recovery may only rebuild/verify exports and finish the transition.
`PARTIAL` truthfully means a terminal run with incomplete query coverage but
verified outputs for its committed subset. `FAILED` never silently resumes.

Fresh run and resume must be separate CLI/API operations. Fresh creation fails
if the ID exists. Resume requires an existing eligible state, expired/no writer
lease, exact stored configuration hash, the same database and export root, and
an explicit operator flag. Unknown schema/state/configuration, active lease,
legacy v5 orphan without a provable original limit, or a `SUCCESS` manifest
mismatch fails closed. Existing v5 runs may receive export-only reconciliation;
they must not be allowed to continue scraping.

Resume replays the complete stored query input. Stable observation and decision
keys plus the existing unique constraints make exact replay idempotent; later
payloads remain distinct observations. Existing conservative identity policy
still cannot promise that two genuinely identical businesses with insufficient
shared evidence will never be split.

## 4. Durable budget strategy

The in-memory reservation remains only a performance throttle. The authority is
the count of committed, non-review `NEW/CREATE_COMPANY` decisions for the run.
Inside the same `BEGIN IMMEDIATE` transaction that would create a company,
recompute that count and reject the write when it equals the stored run limit.
SQLite's single writer then prevents concurrent overshoot. On resume, derive the
committed count from decisions; never restore it from telemetry and never keep a
second mutable counter.

Schema v5 cannot durably prove the original limit or run configuration and has
no `FINALIZING` state or lease. A minimal schema-v6 migration is therefore
required before safe resume: rebuild/extend `discovery_runs` to add
`FINALIZING` to its status check and add `new_company_limit`,
`run_config_hash`, `lease_owner`, `lease_expires_at`, `heartbeat_at`, and
`updated_at`. `NULL` limit must explicitly mean unlimited. The configuration
hash should cover normalized query content, discovery mode, limit, resolver and
qualification policy versions, and canonical export root. No budget ledger or
counter table is needed. The migration is proposed only; it was not executed.

## 5. Minimal implementation plan

1. Add the reviewed v6 migration and strict open-time validation; leave v5
   runtime non-resumable.
2. Add explicit fresh/resume/recover-export entry points, lease acquisition and
   heartbeat, and exact configuration checks before browser creation.
3. Move the authoritative budget test into `RegistryService`'s write
   transaction; hydrate in-memory telemetry from the decision count.
4. Require `RUNNING` for decision writes and introduce atomic, guarded status
   transitions. Persist `FINALIZING` before any publication and `SUCCESS` last.
5. Make finalization idempotently assess, rebuild, directory-`fsync`, and verify
   all three manifests against the expected SQLite IDs before terminal status.
6. Keep worker cleanup in `finally`; make signal handling request stop, join,
   quit, and record `INTERRUPTED`. On startup report—not blindly kill—stale
   owned browser PIDs. Capture sanitized durable run diagnostics.

## 6. Acceptance test matrix

| Area | Focused acceptance |
| --- | --- |
| Transaction crashes | Inject before entity writes, after entity writes, before SQLite commit, and immediately after commit; assert rollback or the complete entity/observation/decision/run-summary set, never a subset. |
| Same-run replay | Kill after commit, explicitly resume, replay all queries; assert one observation/decision/entity and unchanged NEW count. Changed payload creates one new observation under the same cap. |
| Cross-run duplicate | Fresh run over prior evidence creates a new run decision but no duplicate company/branch and consumes no NEW budget. |
| Budget restart | Limit 3; crash after 2 committed NEW; resume reports 2 and permits exactly 1. Four workers/processes racing distinct NEW candidates never exceed 3. |
| Export recovery | Inject before/after CSV replace and before/after each manifest replace, then hard-kill subprocesses. Recovery rebuilds byte-identical outputs, removes/ignores temp debris, directory-`fsync`s, and verifies DB-derived ordered IDs. |
| Status machine | Reject illegal transitions and writes outside `RUNNING`; prove no `SUCCESS` until qualification plus three manifests verify. Missing/corrupt artifacts keep `FINALIZING`; verified incomplete coverage becomes `PARTIAL`. |
| Recovery gates | Reject implicit resume, config/limit/query/export-root mismatch, active lease, `SUCCESS`, `FAILED`, unknown status/schema, and legacy v5 scrape resume. Permit explicit stale-lease takeover and export-only reconstruction where safe. |
| Concurrency | Exercise same and different observations across workers and two processes; assert SQLite-enforced cap, unique decisions, bounded-lock failure behavior, and single finalizer lease. |
| Browser cleanup | Fake driver verifies exactly one quit on success, driver error, registry error, SIGINT, and SIGTERM. Subprocess hard-kill test documents that cleanup cannot run and that next launch reports stale owned processes. |

All tests must use temporary databases/directories and fake browsers; no live
Maps access or protected data.

## 7. Risks and open decisions

- Approve the exact v6 migration/rollback procedure; this design does not make
  production schema v2 or shadow schema v3 eligible for migration.
- Decide lease duration, heartbeat cadence, and the operator authorization for
  stale-lease takeover. Wall-clock rollback and WSL suspend must fail closed.
- Define the exact completeness contract for `SUCCESS` (all normalized input
  queries terminal-successful versus an explicitly accepted subset).
- Decide whether `PARTIAL` exports may be consumed downstream; default should be
  no automatic consumption even when manifests verify.
- Directory `fsync` semantics on the actual WSL filesystem and SQLite
  journal/synchronous settings need an isolated durability rehearsal.
- Recovery idempotency prevents retry-created duplicates; it does not eliminate
  the resolver's documented false-split risk or authorize outreach.
