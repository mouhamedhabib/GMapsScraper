# Local Google Maps release

This workflow runs Google Maps in authoritative mode against the independent
schema-v6 registry. It does not use the protected production or shadow
database. Run one Maps writer at a time.

## Environment setup

From the repository on the WSL Linux filesystem (not `/mnt/...`):

```bash
cd /home/wsl/GitHub/GMapsScraper
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Put one Google Maps query per line in a UTF-8 text file. Blank lines are
ignored. The safe defaults are:

- Registry: `data/local_registry/company_registry.db`
- Exports: `data/local_registry/exports`
- Browser HOME: `data/local_registry/browser_home`
- Mode: authoritative Google Maps
- Browser resource mode: one low-resource worker

The wrapper creates the browser HOME with mode `0700`. It never adds
`--allow-production-registry` and refuses the canonical production and shadow
paths even if supplied explicitly.

## Verify the pre-release backup

Create a SQLite online backup before the first release run:

```bash
python local_release.py backup
```

The command writes a new timestamped `.db` and `.verification.json` under
`data/local_registry/backups/`. It never overwrites a prior backup. A valid
backup must reopen as schema 6, return `ok` from `integrity_check`, have zero
foreign-key violations, and record its SHA-256. Any failure returns exit code 1.

Every `run` and `resume` also performs this verified backup before Maps starts.
If the run reaches verified `SUCCESS`, the wrapper verifies all exports against
SQLite and automatically creates another timestamped backup.

## Fresh run

Use a unique run ID. A date plus a short label is practical:

```bash
python local_release.py run \
  --query-file queries.txt \
  --max-new 25 \
  --run-id maps-20261010-tunis-01
```

To select other local WSL paths:

```bash
python local_release.py run \
  --query-file /home/wsl/GitHub/GMapsScraper/queries.txt \
  --max-new 25 \
  --run-id maps-20261010-tunis-01 \
  --registry-path /home/wsl/GitHub/GMapsScraper/data/local_registry/company_registry.db \
  --export-directory /home/wsl/GitHub/GMapsScraper/data/local_registry/exports
```

Do not reuse a fresh run ID. The wrapper checks that it is absent before launch.

## Resume an interrupted run

Use the same query file contents, NEW limit, run ID, registry, and export
directory as the fresh command. Resume re-scans the full query list and relies
on durable same-run decisions:

```bash
python local_release.py resume \
  --query-file queries.txt \
  --max-new 25 \
  --run-id maps-20261010-tunis-01
```

Resume is never automatic. An unknown run ID or a stored configuration mismatch
fails before browser work.

For a run under heartbeat investigation, explicitly enable bounded periodic
all-thread snapshots (60 seconds is an operational diagnostic choice, not a
lease change):

```bash
python local_release.py resume \
  --query-file queries.txt \
  --max-new 100 \
  --run-id maps-20261011-global-01 \
  --diagnostic-stack-interval-seconds 60
```

Structured heartbeat, SQLite-stage, process/scheduler, and website-stage timing
is always retained for authoritative runs at
`data/local_registry/logs/writer_lease.jsonl` (1 MiB plus three backups). The
stack opt-in writes one current plus three rotated private files at
`data/local_registry/logs/writer_stacks.log*`. Stack files contain Python file
paths and function names but no locals or page content. Inspect them without
starting Maps:

```bash
jq -c 'select(.run_id == "maps-20261011-global-01")' \
  data/local_registry/logs/writer_lease.jsonl
ls -lh data/local_registry/logs/writer_stacks.log*
sed -n '1,240p' data/local_registry/logs/writer_stacks.log
```

## Output files

After `Run status: SUCCESS`, inspect `data/local_registry/exports/`:

```bash
ls -lh data/local_registry/exports/*maps-20261010-tunis-01*
```

The run produces three CSV/manifest pairs:

- `new_companies_<run-id>.csv` and `.manifest.json`
- `qualified_employment_leads_<run-id>.csv` and `.manifest.json`
- `qualified_mission_leads_<run-id>.csv` and `.manifest.json`

The two qualified files contain only independently eligible leads. They are not
authorization to send outreach. The wrapper checks every manifest, CSV hash,
row count, ordered provenance ID, and company ID against a fresh SQLite
projection before reporting success.

## Manual post-run backup

Successful wrapper runs already create a verified post-run backup. To create an
additional one:

```bash
python local_release.py backup
```

Keep the `.db` together with its `.verification.json`. Never back up a live
SQLite registry with plain `cp`, and never delete a WAL/SHM file to make a copy.

## Common errors

- `protected production/shadow registry`: use the default independent local
  registry; do not pass `data/company_registry.db` or
  `data/company_registry_shadow.db`.
- `schema is ... requires schema 6`, integrity failure, or foreign-key
  violations: Maps was not launched. Preserve the error and use the last
  verified schema-v6 backup; do not migrate the protected databases.
- `Fresh run ID already exists`: choose a new ID, or use `resume` only for the
  intended interrupted run.
- `configuration does not match`: resume with exactly the original query list,
  NEW limit, and export directory.
- ChromeDriver permission/cache error: keep the default isolated browser HOME,
  ensure `data/local_registry/browser_home` is writable, and retry only with an
  explicit `resume` command.
- Exit code or status is not `SUCCESS`: inspect the printed failure and registry
  run status. No success is claimed and no post-run backup is created; the
  verified pre-run backup path is printed.
