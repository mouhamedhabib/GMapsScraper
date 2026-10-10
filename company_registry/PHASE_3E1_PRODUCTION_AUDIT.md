# Phase 3E.1 — Production Readiness Audit

Status: **COMPLETE — NO-GO**  
Scope: Google Maps authoritative discovery, qualification, export, and outreach
safety boundaries only. Google Search is excluded.

## Executive summary

The schema-v5 Maps canary demonstrates a sound isolated core: transactional
company/branch resolution, globally unique Place-ID identities, fail-closed
registry access, process-local budget reservations coupled to durable commits,
independent policy-versioned qualification, manifest-gated exports, and
conservative exclusion of review records all behaved as designed. Phase 3D.3D
inspected 51 cards, committed 48 KNOWN and 3 NEW decisions, exported 3 discovery
rows and 1 employment-eligible row, and finished with SQLite integrity `ok`, no
foreign-key violations, and no protected-state changes.

That evidence is sufficient for the isolated canary, not production rollout.
The designated production registry remains schema v2 and is explicitly rejected
by authoritative path validation. The migration command mutates its target
in-place without creating a backup or providing rollback/cutover orchestration.
Run finalization can persist `SUCCESS` before qualification and manifests exist,
and an uncatchable process death has no durable restart/budget reconciliation.
The verified isolated `HOME` workaround is also an external launch requirement,
not a validated CLI precondition.

The most serious boundary is outreach: qualified exports carry `company_id`, but
the existing outreach builder discards it and send history excludes only email
and domain. A previously contacted registry company can therefore re-enter under
a changed address/domain, so production eligibility must not be treated as send
authorization.

## Production readiness verdict

**NO-GO for production activation, production migration, or any automatic
outreach handoff.** The pipeline may remain available for explicitly approved,
disposable, isolated Maps canaries. There is no evidence of corruption in the
accepted Phase 3D.2/3D.3D artifacts; the verdict is driven by missing production
controls and confirmed recovery/outreach boundary gaps, not by a failed canary.

## Evidence basis

- `company_registry/PHASE_3D2_QUALIFICATION.md`: offline qualification and
  export evidence, including policy reassessment and conservative REVIEW rules.
- `company_registry/PHASE_3D3D_MAPS_CANARY.md`: isolated live Maps results,
  manifests, integrity checks, provenance comparison, and protected hashes.
- `company_registry/storage.py`: `migrate_schema()`, `migrate_registry()`,
  `open_registry()`, and `_validate_schema()`.
- `company_registry/maps_authoritative.py`:
  `validate_authoritative_paths()` and `AuthoritativeMapsSession`.
- `company_registry/service.py`: `RegistryService.resolve_with_metadata()`.
- `company_registry/resolver.py`: `ResolutionPolicy.evaluate()`,
  `_place_match()`, and `_historical_guard()`.
- `company_registry/qualification.py`: `QualificationPolicy`,
  `assess_maps_company()`, and `assess_run()`.
- `company_registry/exports.py`: `export_new_companies()`,
  `export_qualified_leads()`, and `verify_export_manifest()`.
- `utils/run_acceptance_budget.py`: `RunAcceptanceBudget`.
- `utils/threading_controller.py`: `FastSearchAlgo.fast_search_algorithm()`.
- `utils/build_outreach.py`: `OUTPUT_FIELDS`, `row_identity()`, and
  `build_outreach()`.
- `utils/build_outreach_queue.py`: `history_identity()`,
  `exclusion_indexes()`, and `build_queue()`.

No browser, live scrape, migration, production/shadow write, outreach command,
or test suite was run for this documentation-only audit. Existing Phase 3D.2
and Phase 3D.3D results were used; source inspection was targeted and read-only.

## Risk register

### 3E1-01 — P0 — Company-level prior-contact exclusion is not preserved

- **Status:** Confirmed control gap.
- **Evidence:** `company_registry/exports.py::QUALIFIED_EXPORT_FIELDS` includes
  `company_id`; `utils/build_outreach.py::OUTPUT_FIELDS` omits it and
  `row_identity()` uses domain, name, email, or row position;
  `utils/build_outreach_queue.py::history_identity()` and
  `exclusion_indexes()` block only normalized email/domain.
- **Failure scenario:** A previously contacted company is rediscovered with a
  different domain or email, or a separate branch reaches outreach preparation.
  The registry knows the company identity, but that identity is discarded before
  history comparison, so the company can enter a new send queue.
- **Impact:** Duplicate or unsafe outreach, including re-contact after an address
  change and inability to enforce a company-level suppression decision.
- **Minimal mitigation:** Preserve `company_id` through enrichment, ready/review,
  queue, send results, and history; reconcile historical campaign rows to company
  IDs with ambiguous mappings held for review; fail closed when registry-backed
  leads lack a usable suppression identity. Keep eligibility distinct from send
  authorization and require a final reviewed queue.
- **Blocks production:** **Yes**, for any outreach-connected rollout. Discovery
  output must remain isolated until this control exists.

### 3E1-02 — P1 — The authoritative runtime cannot target designated production

- **Status:** Confirmed blocker.
- **Evidence:** `company_registry/storage.py::open_registry()` requires exact
  schema v5; `company_registry/maps_authoritative.py::validate_authoritative_paths()`
  explicitly rejects `data/company_registry.db` and the shadow database.
- **Failure scenario:** Even after an approved v2-to-v5 migration, the production
  database path remains rejected. Operators must either stay on a disposable
  path or bypass/change a safety guard ad hoc.
- **Impact:** There is no supported production activation path, and an ad hoc
  workaround would undermine the canary's path-isolation guarantees.
- **Minimal mitigation:** Define the production topology and release gate first;
  then introduce an explicit, tested production activation configuration that
  retains exact-path, schema, export-root, and rollback checks. Do not weaken the
  current guard as an operational workaround.
- **Blocks production:** **Yes**.

### 3E1-03 — P1 — Production migration lacks backup, rollback, and cutover controls

- **Status:** Confirmed blocker.
- **Evidence:** `company_registry/storage.py::migrate_registry()` opens the
  selected database read/write and calls `migrate_schema()` in place.
  `migrate_schema()` commits each schema version separately. `_validate_schema()`
  checks expected columns and foreign keys, but does not run `integrity_check`.
  The CLI exposes `migrate` without creating or verifying a backup. The
  production-copy preservation test in
  `tests/test_company_registry_migration.py::test_v2_copy_migrates_without_changing_entities_or_provenance`
  validates the happy path only.
- **Failure scenario:** Migration 3 succeeds and commits, then a later migration,
  process, disk, or validation failure stops the v2-to-v5 sequence. Production is
  left at an intermediate version with no command-managed restore point or
  atomic cutover.
- **Impact:** Extended outage and operator-driven recovery; an incorrect manual
  restore could lose registry changes or historical provenance.
- **Minimal mitigation:** Use SQLite's backup API from a quiesced source, record
  hashes/counts/free-space, migrate a copy, run `integrity_check`,
  `foreign_key_check`, provenance/entity comparisons and reader acceptance,
  rehearse restore, then perform an approved atomic cutover with explicit stop
  conditions. Retain the immutable pre-migration backup.
- **Blocks production:** **Yes**.

### 3E1-04 — P1 — A hard crash can leave false `SUCCESS` without exports

- **Status:** Confirmed defect in crash ordering.
- **Evidence:** `company_registry/maps_authoritative.py::AuthoritativeMapsSession.complete()`
  calls `_set_status(status)` before discovery export, assessment, qualified
  exports, or manifest verification. Its exception handler can change the status
  to `PARTIAL`, but cannot run after `SIGKILL`, host loss, or abrupt interpreter
  termination.
- **Failure scenario:** The process dies after `_set_status("SUCCESS")` commits
  but before one or more manifests are published.
- **Impact:** Monitoring sees a successful run whose required qualification or
  publication artifacts are missing or stale. Downstream automation may accept
  an incomplete run.
- **Minimal mitigation:** Treat finalization as a durable state transition
  (`RUNNING`/`FINALIZING` to `SUCCESS`) and write `SUCCESS` only after all required
  manifests verify. Until implemented, a recovery procedure must distrust status
  alone and reconcile every run against verified manifests.
- **Blocks production:** **Yes**.

### 3E1-05 — P1 — Restart and acceptance-budget recovery are not durable

- **Status:** Confirmed design limitation.
- **Evidence:** `AuthoritativeMapsSession._create_run()` rejects every existing
  run ID; `RunAcceptanceBudget` stores reserved/committed counts only in memory;
  `FastSearchAlgo.fast_search_algorithm()` records failure only when Python can
  handle the termination. The progress tracker also states that resume accounting
  is deliberately not implemented.
- **Failure scenario:** A process is killed after committing two NEW companies
  from a limit-three run. The durable run remains `RUNNING`. Restarting requires
  a new run with a fresh limit of three; the two prior companies become KNOWN,
  allowing three additional NEW companies and five total across the operational
  attempt.
- **Impact:** Orphaned runs, ambiguous completion, and violation of an operator's
  intended rollout/campaign cap across retries, despite each individual process
  respecting its own budget.
- **Minimal mitigation:** Before retry, reconcile durable decisions and manifests,
  mark the orphaned run explicitly, and reduce the replacement run's capacity by
  prior committed NEW decisions. Phase 3E.2 should make run recovery and budget
  accounting durable or provide a mandatory equivalent operator workflow.
- **Blocks production:** **Yes**.

### 3E1-06 — P1 — Required ChromeDriver `HOME` isolation is not preflighted

- **Status:** Confirmed operational blocker.
- **Evidence:** `company_registry/PHASE_3D3C_CHROMEDRIVER_CACHE_REPAIR.md`
  establishes a writable process-scoped `HOME` as required after Phase 3D.3B
  failed. `maps.py::GMapsScraper.arg_parser()`/`check_args()` validate queries and
  numeric options but do not validate or establish the ChromeDriver cache home.
- **Failure scenario:** Production launches under the normal read-only home;
  undetected-chromedriver fails before Chrome starts, recreating Phase 3D.3B.
- **Impact:** Predictable launch failure and avoidable partial/orphaned run state.
- **Minimal mitigation:** Provide a controlled launcher/service configuration
  with a unique writable cache home, ownership/space checks, and a no-navigation
  preflight after dependency upgrades. Record the effective cache root without
  logging profile contents.
- **Blocks production:** **Yes** in the current managed environment.

### 3E1-07 — P1 — Compatibility with legacy registry readers is unverified

- **Status:** Unverified risk, not a confirmed incompatibility.
- **Evidence:** The repository's current registry API requires v5, while legacy
  lead/outreach flows primarily consume CSV. The migration regression verifies
  schema/data preservation, but no reader inventory or compatibility test proves
  that every external or older reader accepts schema v5/user-version 5.
- **Failure scenario:** A legacy operational reader hard-codes schema v2 or
  `PRAGMA user_version=2` and fails after cutover, or interprets new lifecycle
  state incorrectly.
- **Impact:** Post-migration outage or inconsistent views despite a structurally
  valid database.
- **Minimal mitigation:** Inventory every production reader and scheduled job;
  run read-only acceptance tests against a migrated copy; retire, pin, or update
  incompatible readers before cutover.
- **Blocks production:** **Yes until verified**.

### 3E1-08 — P2 — SQLite contention policy is fail-closed but availability-limited

- **Status:** Confirmed limitation; no corruption defect observed.
- **Evidence:** `company_registry/storage.py::_connect_existing()` configures a
  5,000 ms `busy_timeout`; `RegistryService.resolve_with_metadata()` uses
  `BEGIN IMMEDIATE` and rolls back on error. No application retry/backoff or
  single-writer production lease is present.
- **Failure scenario:** Another process, export/assessment writer, or maintenance
  task holds the write lock longer than five seconds. A discovery commit fails,
  the authoritative run stops, and recovery falls into the non-resumable path.
- **Impact:** Availability loss and operational churn; durable atomicity is still
  preserved.
- **Minimal mitigation:** Enforce one production writer/runner, schedule backup
  and maintenance outside runs, monitor lock failures, and define bounded retry
  behavior only where idempotency is proven.
- **Blocks production:** No by itself; it reinforces 3E1-05.

### 3E1-09 — P2 — Identity policy favors false splits over unsafe merges

- **Status:** Unverified production risk; the conservative behavior is intentional.
- **Evidence:** `ResolutionPolicy.evaluate()` requires exact combined evidence
  for company matching and deliberately ignores domain-only matches. A new Place
  ID plus changed name/domain can create a new company when no exact candidate is
  found. `_place_match()` trusts an exact Place ID unless multiple company or
  branch conflicts accumulate. Phase 3D.3D found no duplicate Place IDs or
  observed merge errors, but sampled only three NEW companies.
- **Failure scenario:** A company rebrands, changes domain/contact data, or a Maps
  listing changes ownership. The resolver may conservatively split one company,
  or retain an old company association for a reused Place ID when available
  conflict evidence is sparse.
- **Impact:** Duplicate company records and possible duplicate downstream
  outreach, or an incorrect company merge. Neither scenario was observed in the
  accepted canary.
- **Minimal mitigation:** Require review/reconciliation of production NEW rows
  against historical and prior-contact evidence during rollout; monitor split
  and Place-ID-conflict rates; never auto-merge on weak/domain-only evidence.
- **Blocks production:** No for isolated discovery with review; yes for automatic
  outreach when combined with 3E1-01.

### 3E1-10 — P2 — Qualification evidence is deterministic but not authoritative

- **Status:** Confirmed policy boundary with an unverified false-positive rate.
- **Evidence:** `assess_maps_company()` uses Maps name/category/description,
  website/contact/address presence, substring term matching, and recorded
  conflict flags. Missing website/contact/location and conflicts conservatively
  produce REVIEW where required; employment and mission logic is independent.
  `assess_run()` versions assessments by `(decision_id, policy_version)` and does
  not overwrite prior versions.
- **Failure scenario:** A stale, promotional, or negated Maps description matches
  a positive term and produces ELIGIBLE even though company ownership, current
  hiring, need, or contact consent is not established.
- **Impact:** Lead-quality false positives if ELIGIBLE is misused as send approval.
- **Minimal mitigation:** Keep policy version immutable, sample and approve
  production assessments, require website/contact ownership and suppression
  checks downstream, and display evidence/reasons to reviewers. Never infer
  hiring, need, or consent from ELIGIBLE.
- **Blocks production:** No for qualification/export; blocks unattended sending.

### 3E1-11 — P2 — Crash observability is not durably retained

- **Status:** Confirmed limitation.
- **Evidence:** `FastSearchAlgo.run_observability()` returns/prints detailed
  query, resource, termination, classification, and budget metrics, but the Maps
  orchestration has no durable structured production log sink. SQLite retains
  runs and decisions, not the complete browser/query telemetry returned by the
  process.
- **Failure scenario:** Host or process loss occurs before stdout is captured or
  the return value is consumed.
- **Impact:** Incomplete incident reconstruction and slower reconciliation of
  orphaned runs, budgets, Chrome failures, and query coverage.
- **Minimal mitigation:** Capture sanitized structured logs with run ID, query
  terminal states, budget snapshots, exception boundary, schema/policy versions,
  manifest paths, and launcher environment checks. Define retention and access
  controls; do not log secrets, cookies, or raw sensitive payloads.
- **Blocks production:** No by itself; required as part of the release runbook.

## Confirmed issues versus unverified risks

Confirmed issues/control gaps are 3E1-01 through 3E1-06, 3E1-08, the evidence
boundary portion of 3E1-10, and 3E1-11. They follow directly from implemented
symbols or the Phase 3D.3B/3D.3D evidence.

Unverified risks are explicitly limited to: legacy/external reader
incompatibility (3E1-07), identity split/Place-ID reassignment scenarios
(3E1-09), and the production false-positive rate of the deterministic
qualification policy (3E1-10). These are not labeled as observed bugs.

Positive controls verified by prior phases remain material: company and branch
identity are separate; Place IDs have a global unique index; shared domain alone
does not merge; ambiguous/quarantined records require review; entity/observation/
decision writes are one transaction; same-run retries are idempotent; later runs
re-evaluate; budget reservations release on controlled errors; qualification
purposes and policy versions are independent; REVIEW/EXCLUDED rows do not enter
qualified exports; manifests are published last and are verifiable; foreign keys
and a bounded busy timeout are enabled; production/shadow/historical state was
unchanged by the accepted canary. Git ignores context, runtime data, databases,
logs, browser profiles, sessions, cookies, and common credential files.

## Minimum requirements before rollout

1. Close 3E1-01 with registry-company suppression across every outreach and
   campaign-history stage; explicitly prohibit sending from qualification alone.
2. Approve a production topology and activation mechanism that does not bypass
   `validate_authoritative_paths()` informally.
3. Approve and rehearse a v2-to-v5 backup, copy-migration, validation, atomic
   cutover, rollback, and disaster-recovery procedure; verify all readers.
4. Make completion recoverable: no `SUCCESS` before all required manifests
   verify, and reconcile existing RUNNING/FINALIZING/PARTIAL runs on startup.
5. Define durable retry/budget accounting, or an enforced operator procedure
   that subtracts earlier committed NEW decisions before a replacement run.
6. Deploy through a validated isolated-`HOME` launcher and enforce a single
   registry writer with durable sanitized logging/alerting.
7. Run a small approved production-copy rehearsal and reviewed production canary
   only after the preceding controls pass; recheck integrity, foreign keys,
   historical provenance, suppression mappings, exports, and rollback readiness.

## Recommended Phase 3E.2 priorities

1. Production migration/cutover/rollback tooling and reader compatibility gate.
2. Run-finalization state machine plus orphan reconciliation and durable budget
   recovery.
3. End-to-end `company_id` propagation and historical outreach suppression.
4. Production launcher/preflight for isolated `HOME`, single-writer locking,
   structured logs, retention, and alerts.
5. Reviewed identity/qualification monitoring for rebrands, Place-ID conflicts,
   historical near-matches, and policy false positives.

These are priorities only; no Phase 3E.2 implementation is authorized by this
audit.

## Explicit recommendation

**NO-GO.** Do not migrate or activate the production registry, do not connect
qualified exports to sending, and do not treat the successful isolated canary as
production approval. Continue only with a separately approved Phase 3E.2 that
closes the P0/P1 controls above, followed by a rehearsed, reversible release
procedure and a new explicit rollout decision.
