# Phase 4A.3B — Controlled Live Resume Canary

Status: **PASS / COMPLETE**  
Decision: the one approved Google Maps canary proved durable interruption and
same-run resume. Stop here; this is not production or Phase 4B approval.

## Execution

- Isolation root: `data/phase_4a3a_resume_preflight/20261010T085658Z`
- Registry: `database/resume_canary_v6.db`
- Run ID: `phase4a3a-resume-20261010T085658Z`
- Query: `software development companies in Tunisia`; one worker; NEW limit 3.
- Preflight was repeated before launch: schema v6, `integrity_check=ok`, zero
  foreign-key violations, run ID absent in all four checked registries, exact
  prepared database hashes, writable ext2/ext3 isolation, empty run outputs,
  no Maps/Chrome process, and unchanged protected hashes.

Exactly one fresh process was launched with the Phase 4A.3A command. Its exact
PID was `64508`. The read-only 0.2-second watcher first observed one committed
decision with NEW=0 and sent exactly one `SIGINT`. Graceful shutdown finished
with durable status `INTERRUPTED`; two distinct KNOWN/MATCH_ONLY decisions had
committed, NEW remained 0/3, the configuration hash was
`7c8a7dba20a223a019bb63471137da99795f779beab8bfb6a378170a14076311`,
and all lease fields were NULL.

After durable cleanup the CLI attempted to print a missing
`authoritative_export` statistic and exited 1 with `KeyError`. This did not
alter the verified interrupted lifecycle or lease state. No retry, second
signal, manual row edit, or lease bypass occurred.

Exactly one resume used the documented identical arguments with only
`--registry-run-id` replaced by `--resume-run-id`. It exited 0 and finalized the
same run as `SUCCESS`; its lease fields are NULL. The two pre-interruption
decision IDs remain present with their original KNOWN decisions.

## Final evidence

- Durable budget: configured 3, committed 3, remaining 0, exhausted true.
- Decisions: 71 total and 71 distinct observations: 67 KNOWN, 3 NEW, 1
  UPDATED; zero review decisions and zero `(run_id, observation_id)` duplicates.
- Identity: company count moved from 3,838 to 3,841; the three NEW decisions
  reference three distinct companies; global duplicate non-null Maps Place IDs
  are zero.
- Qualification: 71 distinct decisions assessed; 35 employment-eligible and
  zero mission-eligible overall. Publication correctly contains only eligible
  NEW companies.
- Read-only `verify_finalized_run()` passed all SQLite projections:
  - discovery: 3 rows, SHA-256
    `49d33561a27e543098d4baa3207e51bd93d8cd4c5273121eda4aa33901a2b3b4`;
  - employment: 1 row, SHA-256
    `8cce329a38466782af4adb38f4930da2b8531d5c09de5f05c567f4e2f7ff79b4`;
  - mission: 0 rows, SHA-256
    `1cf6088448911ab0578a45c7711b9880c5b9f5f058c4e7114b43282f85d2114b`.
- Final SQLite integrity is `ok`; foreign-key violations are zero.
- Bidirectional historical provenance differences are zero. Legacy company and
  branch immutable-field differences and prior `run_companies` differences
  against the verified v2 backup are also zero.
- Production and shadow SHA-256 remain
  `ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22`
  and `ee5551ef5f6a82fa5e80bce02cf16b7635b10ad325e0fcf65a1c5ac4a71a43b7`.
  Every valid entry in the prepared protected-hash inventory passed. No worker,
  Chrome, or ChromeDriver process remained.

Verbose runtime evidence is in `logs/fresh.log` and `logs/resume.log`; exports
and their manifests are in the isolated `exports/` directory. No application
code, production/shadow registry, historical dataset, lead/outreach file,
Google Search flow, cloud resource, or outreach operation was changed.
