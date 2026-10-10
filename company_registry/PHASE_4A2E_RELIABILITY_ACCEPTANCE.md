# Phase 4A.2E — Offline Fault & Integration Testing

Status: **PASS / COMPLETE**  
Decision: **GO for one separately approved, tightly limited reliability canary**  
Scope: offline Google Maps schema-v6 reliability acceptance only. No browser,
network, live scrape, migration, Search operation, outreach, or Phase 4B work.

## Acceptance matrix

| Area | Evidence | Result |
| --- | --- | --- |
| Lifecycle | RUNNING, INTERRUPTED, FINALIZING, SUCCESS, PARTIAL, and FAILED; invalid/terminal transitions and terminal writes | PASS |
| Durable budget | Reopen snapshot, final-slot worker race, replay, rollback, zero/exhausted capacity, resumed remaining capacity | PASS |
| Resume and leases | Explicit resume, active rejection, concurrent acquisition, expired takeover, owner fencing, configuration mismatch, full-query replay | PASS |
| Export recovery | Failures before CSV, after CSV/before manifest, after manifests/before SUCCESS, corrupt CSV/manifest, retry during FINALIZING, stale lease | PASS |
| Integrated restart | NEW plus KNOWN, interruption, reopened SQLite, resume/replay, remaining budget, interrupted export, recovery, three verified exports, SUCCESS | PASS |
| SQLite | Integrity check `ok`; foreign-key check empty after recovery | PASS |
| Protected state | Pre/post SHA-256 inventory of 56 production/shadow/historical/lead/outreach files | PASS |

The integrated scenario finished with three unique NEW companies plus one
pre-existing KNOWN company. The resumed run contained exactly four unique
decisions (three NEW, one KNOWN), consumed exactly three budget slots, exported
three unique discovery company IDs, and introduced no replay duplicates.

## Failure-injection outcomes

- Before CSV replacement: run remained `FINALIZING`; no false SUCCESS.
- After CSV replacement and before manifest replacement: the manifest commit
  marker remained absent/stale; explicit recovery replaced the artifact.
- After all manifests verified and before SUCCESS: retry preserved valid files
  and committed SUCCESS only after fresh SQLite comparison.
- Missing/corrupt CSV or manifest: only invalid artifacts were rebuilt.
- Recovery interruption: lease was released, state remained `FINALIZING`, and a
  later explicit retry completed safely.
- Concurrent/active finalizers: one writer won; competitors failed closed.

No confirmed implementation defect was found, so no runtime fix was made.

## Execution evidence

```text
.venv/bin/python -m unittest -q \
  tests.test_company_registry_reliability_acceptance \
  tests.test_company_registry_export_recovery \
  tests.test_company_registry_v6_lifecycle \
  tests.test_company_registry_durable_budget \
  tests.test_company_registry_resume \
  tests.test_company_registry_qualification \
  tests.test_company_registry_authoritative_foundation \
  tests.test_maps_authoritative_integration \
  tests.test_maps_cli_validation \
  tests.test_maps_run_observability
Ran 79 tests — OK
Chrome instances created: 0
```

Protected hashes remained unchanged:

- production: `ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22`;
- shadow: `ee5551ef5f6a82fa5e80bce02cf16b7635b10ad325e0fcf65a1c5ac4a71a43b7`;
- all 56 inventoried protected files: zero changes.

## Remaining risks and canary gate

The tests simulate failures with deterministic exceptions; they do not prove
power-loss durability or an OS hard-kill at every machine instruction. WSL
`/mnt/*`, network, and host-backed filesystem semantics still require an
environment-specific rehearsal. Lease expiry remains UTC wall-clock based, and
browser position remains intentionally unrecoverable. Real Chrome, Maps UI,
anti-bot behavior, WSL suspend, and network interruption were not exercised.

The offline evidence supports one separately approved canary only if it uses a
new run ID, disposable schema-v6 database, isolated export directory and HOME,
a small durable NEW limit, explicit operator supervision, and no production,
shadow, historical, Search, or outreach path. This is not approval for
production migration, unattended operation, outreach consumption, or Phase 4B.
