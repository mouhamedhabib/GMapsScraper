# Phase 4A.3A — Live Resume Canary Preflight

Status: **PASS / PREFLIGHT COMPLETE**  
Decision: **GO to request explicit approval for one controlled canary; do not
launch automatically.**

## Prepared isolation

Root: `/home/wsl/GitHub/GMapsScraper/data/phase_4a3a_resume_preflight/20261010T085658Z`

| Purpose | Path |
| --- | --- |
| Verified immutable v2 backup | `database/production_v2_backup.db` |
| Disposable migrated registry | `database/resume_canary_v6.db` |
| Process-scoped HOME | `browser_home/` (`0700`, writable) |
| One-query input | `queries/maps_query.txt` |
| Scraper scratch / unused legacy export | `runtime/scratch/`, `runtime/legacy_exports/` |
| Authoritative exports | `exports/` |
| Logs and evidence | `logs/`, `evidence/` |

Run ID: `phase4a3a-resume-20261010T085658Z` (absent from production, shadow,
backup, and migrated copy). Query: `software development companies in Tunisia`.
Durable NEW limit: `3`; workers: `1`; low-resource/headless mode.

## Backup and migration verification

SQLite online backup read protected production in read-only mode. The verified
v2 backup was then copied to the disposable working database; only that working
copy received the explicit v2-to-v6 migration.

- v6 `integrity_check`: `ok`; foreign-key violations: `0`; tables: `11`.
- Preserved counts: companies `3,838`; branches `5,297`; identities `9,049`;
  historical source records `18,623`; run-company summaries `3,838`.
- Provenance SHA-256 before/after migration:
  `03c37863884e44b6a95cc81cb44958b5d0087d2888fe893397594ff99e758fe8`.
- Every listed protected-table row digest matched the production source, v2
  backup, and migrated v6 copy. Detailed JSON is under `evidence/`.
- Backup SHA-256: `bdb35f9912fdbfaebd28525a85dcd8a9d7051ac4819170edf26c8e7af75386e8`.
- Disposable v6 SHA-256: `70e6dd7e667708e1040b9bdd340c61c44e122b2c17df11d73fee014d1c212612`.
- Expected stored configuration hash after fresh configuration:
  `7c8a7dba20a223a019bb63471137da99795f779beab8bfb6a378170a14076311`.

The root is on WSL `ext2/ext3`, not a `/mnt/*` host mount. Pre/post manifests
for 56 protected registry/historical/lead/outreach files match exactly. The
production and shadow hashes remain `ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22`
and `ee5551ef5f6a82fa5e80bce02cf16b7635b10ad325e0fcf65a1c5ac4a71a43b7`.

## Proposed commands (not executed)

Both invocations must run from `/home/wsl/GitHub/GMapsScraper` and retain the
same query content, mode, limit, database, and canonical export root.
For the interruption watcher, the approved operator should append
`> data/phase_4a3a_resume_preflight/20261010T085658Z/logs/fresh.log 2>&1 &`,
immediately save `CANARY_PID=$!`, and later `wait "$CANARY_PID"`.

Fresh:

```bash
HOME=/home/wsl/GitHub/GMapsScraper/data/phase_4a3a_resume_preflight/20261010T085658Z/browser_home \
  .venv/bin/python maps.py \
  --query-file /home/wsl/GitHub/GMapsScraper/data/phase_4a3a_resume_preflight/20261010T085658Z/queries/maps_query.txt \
  --threads 1 --limit 3 --low-resource --browser-wait 15 --scroll-minutes 1 \
  --output-format CSV \
  --output-folder /home/wsl/GitHub/GMapsScraper/data/phase_4a3a_resume_preflight/20261010T085658Z/runtime/scratch \
  --export-dir /home/wsl/GitHub/GMapsScraper/data/phase_4a3a_resume_preflight/20261010T085658Z/runtime/legacy_exports \
  --known-companies-dir /home/wsl/GitHub/GMapsScraper/data/phase_4a3a_resume_preflight/20261010T085658Z/runtime/known_companies_empty \
  --discovery-mode authoritative-canary \
  --registry-database /home/wsl/GitHub/GMapsScraper/data/phase_4a3a_resume_preflight/20261010T085658Z/database/resume_canary_v6.db \
  --registry-run-id phase4a3a-resume-20261010T085658Z \
  --authoritative-export-dir /home/wsl/GitHub/GMapsScraper/data/phase_4a3a_resume_preflight/20261010T085658Z/exports
```

Resume uses the identical command with only:

```text
--registry-run-id phase4a3a-resume-20261010T085658Z
```

replaced by:

```text
--resume-run-id phase4a3a-resume-20261010T085658Z
```

Both complete argument sets were parsed offline; each resolved exactly one
query, authoritative-canary mode, limit 3, one worker, the same database and
export root, and mutually exclusive fresh/resume IDs.

## Controlled interruption and lease rules

Run fresh in the foreground while a second terminal polls only the disposable
database every 0.2 seconds. At the first committed run decision—and while the
committed NEW count is below 3—send one `SIGINT` to the Python PID. With one
worker, the handler requests stop, workers close Chrome in `finally`, the run is
recorded `INTERRUPTED`, and its lease is released. Verify process exit, status
`INTERRUPTED`, at least one decision, NEW count `< 3`, and NULL lease fields
before running the resume command. Resume then re-scans the same complete query
and must reuse committed observations without extra budget consumption.

For the approved run, capture the exact Python PID from the launching shell
(`CANARY_PID=$!`) and copy that numeric value into a second terminal. The
watcher is read-only except for signaling that exact PID:

```bash
DB=/home/wsl/GitHub/GMapsScraper/data/phase_4a3a_resume_preflight/20261010T085658Z/database/resume_canary_v6.db
RUN_ID=phase4a3a-resume-20261010T085658Z
CANARY_PID=<exact-python-pid-from-launching-shell>
while kill -0 "$CANARY_PID" 2>/dev/null; do
  read -r DECISIONS NEW_COUNT <<EOF
$(sqlite3 -readonly -separator ' ' "$DB" "SELECT count(*), coalesce(sum(classification='NEW' AND resolution_action='CREATE_COMPANY' AND requires_review=0),0) FROM discovery_run_decisions WHERE run_id='$RUN_ID';")
EOF
  if [ "$DECISIONS" -ge 1 ]; then
    kill -INT "$CANARY_PID"
    [ "$NEW_COUNT" -lt 3 ] || echo "INCONCLUSIVE: budget exhausted before signal"
    break
  fi
  sleep 0.2
done
```

Do not send repeated signals or edit lifecycle/lease rows manually. Default
heartbeat is 40 seconds and lease duration is 120 seconds. If the process dies
without graceful cleanup, require that its PID is absent, keep the state
`RUNNING`, and wait until the persisted `lease_expires_at` plus a 5-second
safety margin (up to about 125 seconds after the last heartbeat) before resume.
An earlier attempt must be rejected as an active writer; an expired lease may
be explicitly taken over.

The interruption-before-exhaustion timing is not mathematically guaranteed:
three NEW decisions could commit before the watcher reacts. If that occurs,
the run cannot prove remaining-budget resume and must be reported inconclusive;
do not reuse a SUCCESS run. The safe weaker alternative is a new separately
approved disposable copy/run ID interrupted immediately after `RUNNING` is
observed, which proves real lifecycle/lease resume but not committed-decision
survival. A deterministic post-decision pause would require a separately
approved test hook and is outside this no-code-change preflight.

## Post-resume acceptance checks

- Same run ID and configuration hash; terminal `SUCCESS`; lease fields NULL.
- Durable NEW count exactly 3, remaining 0, and no budget overshoot.
- Decision count equals distinct `(run_id, observation_id)` count; no duplicate
  companies or globally duplicated non-null Maps Place IDs.
- Replayed pre-interruption decisions do not create companies or consume budget.
- Discovery, employment, and mission manifests pass `verify_run_exports()`;
  hashes, row counts, ordered provenance IDs, and company IDs match SQLite.
- `integrity_check=ok`, empty `foreign_key_check`, preserved historical counts
  and provenance digest, and unchanged protected-file hash manifest.

No browser, network request, live scrape, Search operation, or outreach command
was executed during this preflight.
