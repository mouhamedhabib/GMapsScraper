# Pre-Batch 03 Active Registry Migration Runbook

Date prepared: 2026-11-01  
Target: `data/local_registry/company_registry.db`  
Migration: schema v6 → v7 → v8  
Status: **PREPARATION ONLY — NOT EXECUTED ON THE ACTIVE REGISTRY**

## Decision and scope

This runbook is ready for a separately approved maintenance window. It is a
**conditional GO** for backup, restore rehearsal, explicit migration, and
write-free verification. It is not authorization to scrape or to begin Batch
03.

The migration is **not automatic**. Runtime opening and authoritative run
startup require the exact current schema and refuse an existing v6 database.
The operator must run the explicit migration command in this document.

Only the independent active local registry is in scope:

```text
/home/wsl/GitHub/GMapsScraper/data/local_registry/company_registry.db
```

The following protected registries and all existing exports/manifests are out
of scope and must remain byte-identical:

```text
data/company_registry.db
data/company_registry_shadow.db
data/local_registry/exports/**
```

## Read-only preparation findings

Observed on 2026-11-01:

| Check | Result |
|---|---|
| Active schema | 6 |
| `PRAGMA integrity_check` | `ok` |
| Foreign-key violations | 0 |
| Journal mode | `delete` |
| `RUNNING` or `FINALIZING` runs | 0 |
| Rows with a lease owner or expiry | 0 |
| File holders reported by `lsof` | none; container pseudo-filesystem warnings make this advisory |
| SQLite WAL/SHM/journal sidecars | none |
| Registry SHA-256 | `a379add919c4facfae5f1065a4757be2af9f8f2c89c2ee57a7f6ba511c16fb74` |

Historical baseline:

| Data | Count |
|---|---:|
| Companies | 4,243 |
| Branches | 5,702 |
| Company identities | 9,713 |
| Branch identities | 20,234 |
| Historical source records | 18,623 |
| Discovery observations | 2,452 |
| Discovery decisions | 2,452 |
| Run-company rows | 5,198 |
| Qualification assessments | 3,558 |
| Authoritative unique NEW companies | 405 |
| Historical runs | 4 |

Assessment baseline:

- `maps-dual-purpose-v1`: 1,123;
- `maps-dual-purpose-v3-business-relevance`: 2,435.

Run baseline:

- legacy import: `PARTIAL`;
- `maps-20261010-first-local-01`: `SUCCESS`, 5 NEW;
- `maps-20261011-global-01`: `SUCCESS`, 100 NEW;
- `maps-20261031-batch02-300`: `SUCCESS`, 300 NEW.

## Supported tools and an important v6 limitation

Official migration CLI:

```bash
.venv/bin/python -m company_registry.storage migrate --database <database>
```

This explicitly applies pending migrations and validates the current schema.
It migrates in place and commits each schema version separately. It does not
create a backup or orchestrate rollback.

Official local backup CLI:

```bash
.venv/bin/python local_release.py backup \
  --registry-path <database> \
  --backup-directory <directory>
```

The backup CLI currently verifies against `SCHEMA_VERSION == 8`. It therefore
works after migration, but **cannot create the required pre-migration v6
backup**: a v6 invocation fails verification with `requires schema 8` and
removes the rejected copy. Do not use it for the pre-v8 backup.

For the pre-migration v6 snapshot, the repository-supported procedure is
SQLite's online backup API (`sqlite3.Connection.backup()`), followed by explicit
schema-6, integrity, foreign-key, hash, logical-fingerprint, permission, and
restore-rehearsal checks. Exact commands are below.

There is no dedicated restore CLI. Restoration uses the documented SQLite
backup/restore procedure, same-filesystem staging, validation, fsync, and
atomic replacement.

The storage CLI may emit a Python `runpy` warning because the package imports
`company_registry.storage` before module execution. Treat it as non-fatal only
when the command exits 0 and every postflight assertion below passes.

## Preconditions

Do not start unless all are true:

1. A maintenance window is approved and no Maps/Search/import/reassessment,
   report writer, notebook, scheduler, or other SQLite consumer is running.
2. The exact project revision containing Phase 6D, Phase 6E, and Phase 6E.1 is
   selected; no alternate/old process can restart.
3. The active path resolves exactly to the independent local registry above.
4. There are no `RUNNING`/`FINALIZING` runs, persisted leases, open file
   holders, or SQLite sidecars.
5. Sufficient free space exists for at least three approximately 80 MB database
   copies plus SQLite temporary space.
6. The pre-v8 backup, its manifest, and a restore rehearsal all pass before the
   active migration command is run.
7. No Batch 03 write occurs until postflight is accepted. Rollback in this
   runbook is pre-write rollback; after a new v8 decision, blind restoration
   would lose data.

Any failed assertion is **STOP / NO-GO**.

## 1. Establish the operator environment

Run in one WSL Bash session. Keep these exported variables for all later
commands:

```bash
cd /home/wsl/GitHub/GMapsScraper
set -euo pipefail
umask 077

export MIGRATION_REGISTRY="/home/wsl/GitHub/GMapsScraper/data/local_registry/company_registry.db"
export MIGRATION_BACKUP_DIR="/home/wsl/GitHub/GMapsScraper/data/local_registry/backups"
export MIGRATION_ID="$(date -u +%Y%m%dT%H%M%S%6NZ)"
export MIGRATION_BACKUP="$MIGRATION_BACKUP_DIR/registry_pre-v8-migration_$MIGRATION_ID.db"
export MIGRATION_MANIFEST="${MIGRATION_BACKUP%.db}.verification.json"
export MIGRATION_RESTORE_TEST="$MIGRATION_BACKUP_DIR/restore-rehearsal_$MIGRATION_ID.db"

mkdir -p -- "$MIGRATION_BACKUP_DIR"
chmod 700 -- "$MIGRATION_BACKUP_DIR"

printf '%s\n' \
  "Registry: $MIGRATION_REGISTRY" \
  "Backup: $MIGRATION_BACKUP" \
  "Manifest: $MIGRATION_MANIFEST" \
  "Restore rehearsal: $MIGRATION_RESTORE_TEST"
```

Expected: four absolute paths under `data/local_registry`; the backup,
manifest, and rehearsal filenames must not already exist.

## 2. Quiescence and read-only preflight

First check operating-system holders. Any printed PID is a hard stop:

```bash
MIGRATION_HOLDERS="$(lsof -t -- "$MIGRATION_REGISTRY" 2>/dev/null || true)"
if [[ -n "$MIGRATION_HOLDERS" ]]; then
  printf 'STOP: registry is open by PID(s): %s\n' "$MIGRATION_HOLDERS" >&2
  exit 1
fi

for MIGRATION_SUFFIX in -wal -shm -journal; do
  if [[ -e "${MIGRATION_REGISTRY}${MIGRATION_SUFFIX}" ]]; then
    printf 'STOP: unexpected SQLite sidecar: %s\n' \
      "${MIGRATION_REGISTRY}${MIGRATION_SUFFIX}" >&2
    exit 1
  fi
done
```

Then run the authoritative database preflight with URI read-only access:

```bash
.venv/bin/python - <<'PY'
import json
import os
import sqlite3
from pathlib import Path

database = Path(os.environ["MIGRATION_REGISTRY"]).resolve()
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
try:
    schema = connection.execute("PRAGMA user_version").fetchone()[0]
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    active_runs = connection.execute(
        "SELECT run_id, status FROM discovery_runs "
        "WHERE status IN ('RUNNING', 'FINALIZING') ORDER BY run_id"
    ).fetchall()
    leases = connection.execute(
        "SELECT run_id, lease_owner, lease_expires_at FROM discovery_runs "
        "WHERE lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL "
        "ORDER BY run_id"
    ).fetchall()
    assert schema == 6, schema
    assert integrity == ["ok"], integrity
    assert not foreign_keys, foreign_keys
    assert not active_runs, [dict(row) for row in active_runs]
    assert not leases, [dict(row) for row in leases]
    print(json.dumps({
        "schema_version": schema,
        "integrity_check": integrity[0],
        "foreign_key_violations": 0,
        "active_runs": 0,
        "active_leases": 0,
    }, indent=2, sort_keys=True))
finally:
    connection.close()
PY
```

Expected: schema 6, integrity `ok`, and all three counts zero.

## 3. Create and verify the timestamped v6 backup

This is the approved pre-v8 substitute for the currently v8-only backup CLI.
It writes only new files under `data/local_registry/backups` and never alters
the source database:

```bash
.venv/bin/python - <<'PY'
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

source_path = Path(os.environ["MIGRATION_REGISTRY"]).resolve()
backup_path = Path(os.environ["MIGRATION_BACKUP"]).resolve()
manifest_path = Path(os.environ["MIGRATION_MANIFEST"]).resolve()
if backup_path.exists() or manifest_path.exists():
    raise SystemExit("STOP: backup or manifest already exists")

def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'

def logical_fingerprints(connection):
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    result = {}
    for table in tables:
        columns = [row[1] for row in connection.execute(
            f"PRAGMA table_info({quote(table)})"
        )]
        selected = ", ".join(quote(column) for column in columns)
        rows = [json.dumps(list(row), separators=(",", ":"), ensure_ascii=False)
                for row in connection.execute(
                    f"SELECT {selected} FROM {quote(table)}"
                )]
        rows.sort()
        payload = "\n".join(rows) + ("\n" if rows else "")
        result[table] = {
            "columns": columns,
            "rows": len(rows),
            "sha256": sha256(payload.encode("utf-8")).hexdigest(),
        }
    return result

source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
target = sqlite3.connect(backup_path)
try:
    source.backup(target)
finally:
    target.close()
    source.close()
os.chmod(backup_path, 0o600)

backup = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
try:
    schema = backup.execute("PRAGMA user_version").fetchone()[0]
    integrity = [row[0] for row in backup.execute("PRAGMA integrity_check")]
    foreign_keys = backup.execute("PRAGMA foreign_key_check").fetchall()
    logical = logical_fingerprints(backup)
    active_runs = backup.execute(
        "SELECT COUNT(*) FROM discovery_runs "
        "WHERE status IN ('RUNNING', 'FINALIZING')"
    ).fetchone()[0]
    leases = backup.execute(
        "SELECT COUNT(*) FROM discovery_runs "
        "WHERE lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL"
    ).fetchone()[0]
    authoritative_new = backup.execute(
        "SELECT COUNT(DISTINCT company_id) FROM discovery_run_decisions "
        "WHERE classification='NEW' AND resolution_action='CREATE_COMPANY' "
        "AND requires_review=0"
    ).fetchone()[0]
    policies = dict(backup.execute(
        "SELECT policy_version, COUNT(*) FROM qualification_assessments "
        "GROUP BY policy_version ORDER BY policy_version"
    ))
finally:
    backup.close()

assert schema == 6, schema
assert integrity == ["ok"], integrity
assert not foreign_keys, foreign_keys
assert active_runs == 0, active_runs
assert leases == 0, leases
assert authoritative_new == 405, authoritative_new
assert policies == {
    "maps-dual-purpose-v1": 1123,
    "maps-dual-purpose-v3-business-relevance": 2435,
}, policies

with backup_path.open("rb") as handle:
    os.fsync(handle.fileno())
record = {
    "created_at": datetime.now(timezone.utc).isoformat(),
    "source": str(source_path),
    "backup": str(backup_path),
    "schema_version": schema,
    "integrity_check": "ok",
    "foreign_key_violations": 0,
    "active_runs": active_runs,
    "active_leases": leases,
    "authoritative_new_unique": authoritative_new,
    "assessment_counts": policies,
    "backup_sha256": sha256(backup_path.read_bytes()).hexdigest(),
    "size_bytes": backup_path.stat().st_size,
    "logical_fingerprints": logical,
}
with manifest_path.open("x", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(manifest_path, 0o600)
directory_fd = os.open(backup_path.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
print(json.dumps({
    "backup": str(backup_path),
    "manifest": str(manifest_path),
    "sha256": record["backup_sha256"],
    "schema_version": schema,
    "integrity_check": "ok",
    "foreign_key_violations": 0,
}, indent=2, sort_keys=True))
PY
```

Expected: a unique mode-0600 schema-v6 backup and manifest, integrity `ok`,
zero FK violations, 405 authoritative NEW companies, and the two exact policy
counts. Record the printed SHA-256 in the maintenance ticket.

## 4. Rehearse restoration before migration

This restores the verified backup to a new file, validates every historical
table fingerprint, and leaves the active registry untouched:

```bash
.venv/bin/python - <<'PY'
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

backup_path = Path(os.environ["MIGRATION_BACKUP"]).resolve()
manifest_path = Path(os.environ["MIGRATION_MANIFEST"]).resolve()
restore_path = Path(os.environ["MIGRATION_RESTORE_TEST"]).resolve()
if restore_path.exists():
    raise SystemExit("STOP: restore rehearsal target already exists")
record = json.loads(manifest_path.read_text(encoding="utf-8"))
assert sha256(backup_path.read_bytes()).hexdigest() == record["backup_sha256"]

source = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
target = sqlite3.connect(restore_path)
try:
    source.backup(target)
finally:
    target.close()
    source.close()
os.chmod(restore_path, 0o600)

def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'

restored = sqlite3.connect(f"file:{restore_path}?mode=ro", uri=True)
try:
    assert restored.execute("PRAGMA user_version").fetchone()[0] == 6
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not restored.execute("PRAGMA foreign_key_check").fetchall()
    for table, expected in record["logical_fingerprints"].items():
        selected = ", ".join(quote(column) for column in expected["columns"])
        rows = [json.dumps(list(row), separators=(",", ":"), ensure_ascii=False)
                for row in restored.execute(
                    f"SELECT {selected} FROM {quote(table)}"
                )]
        rows.sort()
        payload = "\n".join(rows) + ("\n" if rows else "")
        assert len(rows) == expected["rows"], table
        assert sha256(payload.encode("utf-8")).hexdigest() == expected["sha256"], table
finally:
    restored.close()
with restore_path.open("rb") as handle:
    os.fsync(handle.fileno())
print(f"Restore rehearsal PASS: {restore_path}")
PY
```

Expected: `Restore rehearsal PASS`. Retain this rehearsal file until the
migration and write-free validation are accepted.

## 5. Recheck quiescence immediately before migration

Repeat sections 2 and 3's manifest-hash check. At minimum:

```bash
test -z "$(lsof -t -- "$MIGRATION_REGISTRY" 2>/dev/null || true)"

.venv/bin/python - <<'PY'
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

database = Path(os.environ["MIGRATION_REGISTRY"]).resolve()
backup = Path(os.environ["MIGRATION_BACKUP"]).resolve()
manifest = json.loads(Path(os.environ["MIGRATION_MANIFEST"]).read_text())
assert sha256(backup.read_bytes()).hexdigest() == manifest["backup_sha256"]
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
try:
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    assert connection.execute(
        "SELECT COUNT(*) FROM discovery_runs "
        "WHERE status IN ('RUNNING', 'FINALIZING')"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM discovery_runs "
        "WHERE lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL"
    ).fetchone()[0] == 0
finally:
    connection.close()
print("Immediate pre-migration gate PASS")
PY
```

Expected: `Immediate pre-migration gate PASS` and command exit 0.

## 6. Perform the supported migration

This is the only command in the main procedure that modifies the active
registry. Run it only with explicit migration approval:

```bash
.venv/bin/python -m company_registry.storage migrate \
  --database "$MIGRATION_REGISTRY"
```

Expected exit code: 0. Expected final lines:

```text
Company registry migrated: .../data/local_registry/company_registry.db
Schema version: 8
```

Do not start Batch 03. Proceed directly to write-free postflight. If the
command exits nonzero, treat the database as potentially schema 7 and execute
rollback; do not rerun blindly.

## 7. Verify schema v8 and historical preservation

This command compares every pre-existing v6 column against the backup manifest,
as well as exact counts, policy versions, run states, and the new empty tables:

```bash
.venv/bin/python - <<'PY'
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

database = Path(os.environ["MIGRATION_REGISTRY"]).resolve()
record = json.loads(Path(os.environ["MIGRATION_MANIFEST"]).read_text())

def quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'

connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
try:
    schema = connection.execute("PRAGMA user_version").fetchone()[0]
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert schema == 8, schema
    assert integrity == ["ok"], integrity
    assert not foreign_keys, foreign_keys

    for table, expected in record["logical_fingerprints"].items():
        selected = ", ".join(quote(column) for column in expected["columns"])
        rows = [json.dumps(list(row), separators=(",", ":"), ensure_ascii=False)
                for row in connection.execute(
                    f"SELECT {selected} FROM {quote(table)}"
                )]
        rows.sort()
        payload = "\n".join(rows) + ("\n" if rows else "")
        assert len(rows) == expected["rows"], table
        assert sha256(payload.encode("utf-8")).hexdigest() == expected["sha256"], table

    assert connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 4243
    assert connection.execute("SELECT COUNT(*) FROM discovery_runs").fetchone()[0] == 4
    assert connection.execute("SELECT COUNT(*) FROM qualification_assessments").fetchone()[0] == 3558
    authoritative_new = connection.execute(
        "SELECT COUNT(DISTINCT company_id) FROM discovery_run_decisions "
        "WHERE classification='NEW' AND resolution_action='CREATE_COMPANY' "
        "AND requires_review=0"
    ).fetchone()[0]
    assert authoritative_new == 405, authoritative_new
    policies = dict(connection.execute(
        "SELECT policy_version, COUNT(*) FROM qualification_assessments "
        "GROUP BY policy_version ORDER BY policy_version"
    ))
    assert policies == {
        "maps-dual-purpose-v1": 1123,
        "maps-dual-purpose-v3-business-relevance": 2435,
    }, policies
    runs = [dict(row) for row in connection.execute(
        "SELECT run_id, status, lease_owner, lease_expires_at "
        "FROM discovery_runs ORDER BY created_at"
    )]
    assert [row["status"] for row in runs] == [
        "PARTIAL", "SUCCESS", "SUCCESS", "SUCCESS"
    ], runs
    assert all(row["lease_owner"] is None and row["lease_expires_at"] is None
               for row in runs), runs
    assert connection.execute(
        "SELECT COUNT(*) FROM discovery_run_queries"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM discovery_run_query_decisions"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM discovery_run_query_results"
    ).fetchone()[0] == 0
finally:
    connection.close()

print(json.dumps({
    "schema_version": schema,
    "integrity_check": integrity[0],
    "foreign_key_violations": 0,
    "historical_v6_fingerprints_preserved": True,
    "authoritative_new_unique": authoritative_new,
    "assessment_counts": policies,
    "runs": runs,
    "historical_query_tables_empty": True,
}, indent=2, sort_keys=True))
PY
```

Expected: every assertion passes; schema 8; integrity `ok`; zero FK violations;
405 authoritative NEW; assessment counts 1,123 and 2,435; no leases; historical
query tables empty rather than fabricated.

## 8. Verify Phase 6E.1 historical reporting

This command is read-only against the migrated registry and writes only to a
new `/tmp` report directory:

```bash
export MIGRATION_REPORT_DIR="/tmp/gmaps-prebatch03-$MIGRATION_ID"

.venv/bin/python -m company_registry.query_observability \
  --database "$MIGRATION_REGISTRY" \
  --run-id maps-20261031-batch02-300 \
  --query-file /home/wsl/GitHub/GMapsScraper/queries_batch02.txt \
  --output-dir "$MIGRATION_REPORT_DIR"

.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

path = (Path(os.environ["MIGRATION_REPORT_DIR"])
        / "query-observability-v1"
        / "maps-20261031-batch02-300"
        / "query_observability.json")
report = json.loads(path.read_text(encoding="utf-8"))
assert report["global_authoritative_totals"]["new_companies_committed"] == 300
assert report["global_authoritative_totals"]["known_records"] == 1002
assert report["global_authoritative_totals"]["review_only_records"] == 14
assert report["global_authoritative_totals"]["updated_records"] == 10
assert all(query["new_companies_committed"] is None for query in report["queries"])
assert all(query["known_records"] is None for query in report["queries"])
assert all(query["review_only_records"] is None for query in report["queries"])
print("Phase 6E.1 historical report PASS")
PY
```

Expected: global 300/1,002/14/10 and null—not zero—historical per-query
counters.

## 9. Verify protected artifacts remain unchanged

The protected registry hashes must remain exactly:

```bash
cat <<'EOF' | sha256sum --check --strict
ab28c6836aba8c2c004fb3e88e2c367f0f3913d61a462911534fb9bd90251c22  data/company_registry.db
ee5551ef5f6a82fa5e80bce02cf16b7635b10ad325e0fcf65a1c5ac4a71a43b7  data/company_registry_shadow.db
EOF
```

Expected: both `OK`.

Verify all existing local CSVs and manifests collectively:

```bash
.venv/bin/python - <<'PY'
from hashlib import sha256
from pathlib import Path

root = Path("data/local_registry/exports")
files = sorted(path for path in root.rglob("*") if path.is_file()
               and (path.suffix == ".csv"
                    or path.name.endswith("manifest.json")))
entries = [(str(path), sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
           for path in files]
aggregate = sha256("".join(
    f"{name}\0{digest}\0{size}\n" for name, digest, size in entries
).encode("utf-8")).hexdigest()
assert len(entries) == 35, len(entries)
assert aggregate == "2f35122b8751e0413920cb5c7266e0d7595a9deaf96ba3b332c4f87efab258c0", aggregate
print("Protected CSV/manifest aggregate PASS:", aggregate)
PY
```

Expected: artifact count 35 and the recorded aggregate hash.

## 10. Create the first official v8 backup

Only after all postflight gates pass, exercise the official backup CLI:

```bash
.venv/bin/python local_release.py backup \
  --registry-path "$MIGRATION_REGISTRY" \
  --backup-directory "$MIGRATION_BACKUP_DIR"
```

Expected: paths for a new verified backup and verification manifest plus its
SHA-256. Retain both the pre-v8 schema-v6 backup and this schema-v8 backup.

Still do not start Batch 03. Approval of this postflight is a separate gate.

## Rollback if migration verification fails

Rollback is allowed only while the registry remains write-free after migration.
If any Batch 03 or other v8 write has occurred, **STOP**: restoring the v6
backup would discard it. Use forward recovery or an explicitly approved
reconciliation plan instead.

### A. Stop and recheck

Stop every consumer again, verify `lsof` prints no PID, and ensure no SQLite
sidecars exist. Preserve command output and the failed postflight diagnostics.

### B. Prepare a same-filesystem restore candidate

```bash
export MIGRATION_RESTORE_TMP="/home/wsl/GitHub/GMapsScraper/data/local_registry/.company_registry.restore-$MIGRATION_ID.db"

.venv/bin/python - <<'PY'
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3

backup_path = Path(os.environ["MIGRATION_BACKUP"]).resolve()
manifest_path = Path(os.environ["MIGRATION_MANIFEST"]).resolve()
restore_path = Path(os.environ["MIGRATION_RESTORE_TMP"]).resolve()
if restore_path.exists():
    raise SystemExit("STOP: restore staging path already exists")
record = json.loads(manifest_path.read_text(encoding="utf-8"))
assert sha256(backup_path.read_bytes()).hexdigest() == record["backup_sha256"]

source = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
target = sqlite3.connect(restore_path)
try:
    source.backup(target)
finally:
    target.close()
    source.close()
os.chmod(restore_path, 0o600)

connection = sqlite3.connect(f"file:{restore_path}?mode=ro", uri=True)
try:
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    def quote(identifier):
        return '"' + identifier.replace('"', '""') + '"'
    for table, expected in record["logical_fingerprints"].items():
        selected = ", ".join(quote(column) for column in expected["columns"])
        rows = [json.dumps(list(row), separators=(",", ":"), ensure_ascii=False)
                for row in connection.execute(
                    f"SELECT {selected} FROM {quote(table)}"
                )]
        rows.sort()
        payload = "\n".join(rows) + ("\n" if rows else "")
        assert len(rows) == expected["rows"], table
        assert sha256(payload.encode("utf-8")).hexdigest() == expected["sha256"], table
    assert connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 4243
    assert connection.execute(
        "SELECT COUNT(*) FROM qualification_assessments"
    ).fetchone()[0] == 3558
    assert connection.execute(
        "SELECT COUNT(DISTINCT company_id) FROM discovery_run_decisions "
        "WHERE classification='NEW' AND resolution_action='CREATE_COMPANY' "
        "AND requires_review=0"
    ).fetchone()[0] == 405
finally:
    connection.close()
with restore_path.open("rb") as handle:
    os.fsync(handle.fileno())
print(f"Validated restore staging file: {restore_path}")
PY
```

Expected: a validated schema-v6 candidate in the same directory as the active
registry. Do not activate it until an operator confirms that no post-migration
write occurred.

### C. Atomically activate the validated restore

This is destructive to the failed migrated active file and requires explicit
rollback approval:

```bash
.venv/bin/python - <<'PY'
import os
from pathlib import Path

active = Path(os.environ["MIGRATION_REGISTRY"]).resolve()
staged = Path(os.environ["MIGRATION_RESTORE_TMP"]).resolve()
if not staged.is_file():
    raise SystemExit("STOP: validated restore staging file is missing")
for suffix in ("-wal", "-shm", "-journal"):
    if Path(str(active) + suffix).exists():
        raise SystemExit(f"STOP: active SQLite sidecar exists: {active}{suffix}")
os.replace(staged, active)
directory_fd = os.open(active.parent, os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
print(f"Atomic rollback installed: {active}")
PY
```

### D. Verify rollback

Repeat section 2's read-only preflight and section 4's logical-fingerprint
verification against the manifest. Expected schema is 6, integrity is `ok`, FK
violations are zero, runs/leases remain inactive, 405 authoritative NEW and
3,558 assessments remain, and every historical fingerprint matches. Recheck
the two protected registry hashes and the 35-artifact export aggregate.

After rollback, Batch 03 remains **NO-GO** until the migration issue is
diagnosed and a new maintenance window is approved.

## Final operator checklist

### Before migration

- [ ] Approved maintenance window and named operator.
- [ ] Exact Phase 6D/6E/6E.1 revision selected.
- [ ] No running consumer or automatic restart path.
- [ ] `lsof` reports no registry holder.
- [ ] No WAL/SHM/journal sidecar.
- [ ] Schema 6, integrity `ok`, zero FK violations.
- [ ] No `RUNNING`/`FINALIZING` run.
- [ ] No lease owner or lease expiry.
- [ ] Unique online v6 backup created with mode 0600.
- [ ] Backup manifest/hash/logical fingerprints verified.
- [ ] Restore rehearsal passed.
- [ ] Rollback variables and commands reviewed.
- [ ] Separate approval to execute the migration received.

### After migration, before any write

- [ ] Migration command exited 0.
- [ ] Schema exactly 8.
- [ ] Integrity `ok`; zero FK violations.
- [ ] All v6 logical fingerprints preserved.
- [ ] 4,243 companies; 405 authoritative unique NEW.
- [ ] Four historical runs and their statuses preserved.
- [ ] 3,558 assessments with exact 1,123/2,435 policy counts.
- [ ] No writer lease and no active run.
- [ ] New historical query tables empty.
- [ ] Phase 6E.1 Batch 02 global totals correct and per-query counters null.
- [ ] Protected production/shadow hashes unchanged.
- [ ] All 35 CSV/manifest artifacts unchanged.
- [ ] Official post-migration v8 backup created and verified.
- [ ] Pre-v8 and post-v8 backups retained.
- [ ] Postflight reviewed and separately accepted before Batch 03.

### Rollback gate

- [ ] Confirm no post-migration write occurred.
- [ ] Revalidate the pre-v8 backup and manifest.
- [ ] Prepare and validate same-filesystem restore staging.
- [ ] Obtain explicit rollback approval.
- [ ] Atomically replace, fsync, and re-run all schema/integrity/count/hash checks.

Stop after preparation. This document does not authorize or perform the active
migration.
