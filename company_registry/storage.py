"""Safe connection and schema initialization for the company registry."""

from argparse import ArgumentParser
import json
from pathlib import Path
import sqlite3
from uuid import UUID, uuid5

from company_registry.schema import MIGRATIONS, SCHEMA_VERSION


DEFAULT_DATABASE = Path("data/company_registry.db")

EXPECTED_TABLES = {
    "companies",
    "branches",
    "company_identities",
    "discovery_runs",
    "run_companies",
    "historical_source_records",
    "branch_identities",
    "discovery_observations",
    "resolution_reviews",
    "discovery_run_decisions",
    "qualification_assessments",
}

EXPECTED_COLUMNS = {
    "companies": {
        "company_id", "canonical_name", "discovery_status", "first_seen_at",
        "last_seen_at", "created_at", "updated_at",
    },
    "branches": {
        "branch_id", "company_id", "display_name", "google_maps_place_id",
        "website_url", "phone", "address", "discovery_status",
        "first_seen_at", "last_seen_at", "created_at", "updated_at",
    },
    "company_identities": {
        "identity_id", "company_id", "identity_type", "identity_value",
        "normalized_value", "first_seen_at", "last_seen_at", "created_at",
        "updated_at",
    },
    "discovery_runs": {
        "run_id", "run_type", "status", "started_at", "finished_at",
        "created_at", "new_company_limit", "run_config_hash", "lease_owner",
        "lease_expires_at", "heartbeat_at", "updated_at",
    },
    "run_companies": {
        "run_id", "company_id", "discovery_status", "observed_at", "created_at",
    },
    "historical_source_records": {
        "source_record_id", "run_id", "source_path", "source_kind",
        "source_row_number", "source_content_hash", "resolution_status",
        "company_id", "branch_id", "observed_at", "reason",
        "raw_record_json", "created_at",
    },
    "branch_identities": {
        "branch_identity_id", "branch_id", "identity_type", "identity_value",
        "normalized_value", "first_seen_at", "last_seen_at", "created_at",
        "updated_at",
    },
    "discovery_observations": {
        "observation_id", "run_id", "source_system", "source_record_key",
        "payload_hash", "raw_payload_json", "normalized_evidence_json",
        "classification", "resolution_action", "company_id", "branch_id",
        "candidate_company_ids_json", "candidate_branch_ids_json",
        "matched_evidence_json", "conflicts_json", "requires_review",
        "resolution_reason", "resolver_version", "observed_at", "created_at",
    },
    "resolution_reviews": {
        "review_id", "observation_id", "previous_classification",
        "decided_classification", "company_id", "branch_id", "reviewer",
        "decision_reason", "created_at",
    },
    "discovery_run_decisions": {
        "decision_id", "run_id", "observation_id", "classification",
        "resolution_action", "company_id", "branch_id",
        "candidate_company_ids_json", "candidate_branch_ids_json",
        "matched_evidence_json", "conflicts_json", "requires_review",
        "resolution_reason", "resolver_version", "observed_at", "created_at",
    },
    "qualification_assessments": {
        "assessment_id", "decision_id", "company_id", "policy_version",
        "evidence_hash", "evidence_json", "employment_relevance",
        "mission_relevance", "employment_eligibility", "mission_eligibility",
        "employment_reasons_json", "mission_reasons_json", "assessed_at",
        "created_at",
    },
}


IDENTITY_NAMESPACE = UUID("9d77a853-c493-52c7-9f61-b36d0a5ce2b0")


def _insert_branch_identity(connection, branch, identity_type, raw, normalized, observed_at=None):
    if not normalized:
        return
    identity_id = str(uuid5(
        IDENTITY_NAMESPACE,
        f"{branch['branch_id']}|{identity_type}|{normalized}",
    ))
    connection.execute(
        """INSERT OR IGNORE INTO branch_identities
           (branch_identity_id, branch_id, identity_type, identity_value,
            normalized_value, first_seen_at, last_seen_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            identity_id, branch["branch_id"], identity_type, raw, normalized,
            branch["first_seen_at"], observed_at or branch["last_seen_at"],
            branch["created_at"], branch["updated_at"],
        ),
    )


def _backfill_branch_identities(connection: sqlite3.Connection) -> None:
    """Populate v3 branch identities without changing existing entity rows."""
    from company_registry.normalization import (
        clean_text,
        normalize_address,
        normalize_google_place_id,
        normalize_name,
        normalize_phone,
    )

    branches = {
        row["branch_id"]: row
        for row in connection.execute("SELECT * FROM branches")
    }
    for branch in branches.values():
        for identity_type, raw, normalized in (
            ("GOOGLE_MAPS_PLACE_ID", branch["google_maps_place_id"],
             normalize_google_place_id(branch["google_maps_place_id"])),
            ("NAME", branch["display_name"], normalize_name(branch["display_name"])),
            ("PHONE", branch["phone"], normalize_phone(branch["phone"])),
            ("ADDRESS", branch["address"], normalize_address(branch["address"])),
        ):
            _insert_branch_identity(
                connection, branch, identity_type, clean_text(raw), normalized,
            )

    for source in connection.execute(
        """SELECT branch_id, observed_at, raw_record_json
             FROM historical_source_records
            WHERE resolution_status='IMPORTED' AND branch_id IS NOT NULL"""
    ):
        branch = branches.get(source["branch_id"])
        if branch is None:
            continue
        raw = json.loads(source["raw_record_json"])
        name = clean_text(raw.get("title") or raw.get("company_name") or raw.get("name"))
        phone = clean_text(raw.get("phone_number") or raw.get("phone"))
        address = clean_text(raw.get("address") or raw.get("location"))
        place = normalize_google_place_id(
            raw.get("place_id") or raw.get("map_place_id")
            or raw.get("map_link") or raw.get("maps_identity")
        )
        for identity_type, value, normalized in (
            ("GOOGLE_MAPS_PLACE_ID", place, place),
            ("NAME", name, normalize_name(name)),
            ("PHONE", phone, normalize_phone(phone)),
            ("ADDRESS", address, normalize_address(address)),
        ):
            _insert_branch_identity(
                connection, branch, identity_type, value, normalized,
                source["observed_at"],
            )


def _backfill_run_decisions(connection: sqlite3.Connection) -> None:
    """Preserve every v3 observation as its original run's first decision."""
    from company_registry.repository import stable_id

    for row in connection.execute("SELECT * FROM discovery_observations"):
        decision_id = stable_id(
            "decision", f"{row['run_id']}|{row['observation_id']}",
        )
        connection.execute(
            """INSERT OR IGNORE INTO discovery_run_decisions
               (decision_id, run_id, observation_id, classification,
                resolution_action, company_id, branch_id,
                candidate_company_ids_json, candidate_branch_ids_json,
                matched_evidence_json, conflicts_json, requires_review,
                resolution_reason, resolver_version, observed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id, row["run_id"], row["observation_id"],
                row["classification"], row["resolution_action"],
                row["company_id"], row["branch_id"],
                row["candidate_company_ids_json"],
                row["candidate_branch_ids_json"],
                row["matched_evidence_json"], row["conflicts_json"],
                row["requires_review"], row["resolution_reason"],
                row["resolver_version"], row["observed_at"], row["created_at"],
            ),
        )


def _enable_foreign_keys(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    if enabled != 1:
        raise RuntimeError("SQLite foreign-key enforcement could not be enabled")


def _validate_schema(connection: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing = EXPECTED_TABLES - tables
    if missing:
        raise RuntimeError(
            "Company registry schema is incomplete; missing tables: "
            + ", ".join(sorted(missing))
        )
    for table, expected in EXPECTED_COLUMNS.items():
        actual = {
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        if actual != expected:
            raise RuntimeError(
                f"Company registry table {table!r} has unexpected columns"
            )
    violation = connection.execute("PRAGMA foreign_key_check").fetchone()
    if violation is not None:
        raise RuntimeError("Company registry contains foreign-key violations")


def migrate_schema(connection: sqlite3.Connection) -> None:
    """Explicitly apply pending migrations on a caller-selected database."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {version} is newer than supported "
            f"version {SCHEMA_VERSION}"
        )

    user_tables = connection.execute(
        "SELECT count(*) FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    if version == 0 and user_tables:
        raise RuntimeError(
            "Unsupported unversioned company registry; migration requires a "
            "known schema version"
        )

    for migration_version, script in MIGRATIONS:
        if version >= migration_version:
            continue
        foreign_keys_disabled = migration_version == 6
        try:
            if foreign_keys_disabled:
                connection.execute("PRAGMA foreign_keys = OFF")
            connection.executescript("BEGIN IMMEDIATE;\n" + script)
            if migration_version == 3:
                _backfill_branch_identities(connection)
            elif migration_version == 4:
                _backfill_run_decisions(connection)
            if migration_version == 6:
                # Validate the rebuilt parent table and every foreign-key edge
                # before committing the schema/version change.
                _validate_schema(connection)
            connection.execute(f"PRAGMA user_version = {migration_version}")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if foreign_keys_disabled:
                connection.execute("PRAGMA foreign_keys = ON")
                if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    raise RuntimeError(
                        "SQLite foreign-key enforcement could not be restored"
                    )
        version = migration_version

    _validate_schema(connection)


def _connect_existing(path: str | Path) -> sqlite3.Connection:
    database_path = Path(path)
    if not database_path.is_file():
        raise FileNotFoundError(f"Company registry does not exist: {database_path}")
    connection = sqlite3.connect(
        f"file:{database_path.resolve()}?mode=rw", uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        _enable_foreign_keys(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def open_registry(path: str | Path = DEFAULT_DATABASE) -> sqlite3.Connection:
    """Open an existing current-version registry without creating or migrating."""
    connection = _connect_existing(path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported company registry schema version {version}; "
                f"runtime requires version {SCHEMA_VERSION}. Run the explicit "
                "registry migration command first."
            )
        _validate_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection


# Backward-compatible name with runtime-safe semantics.
connect_registry = open_registry


def initialize_registry(path: str | Path = DEFAULT_DATABASE) -> Path:
    """Explicitly create a new current-version registry, or validate an existing one."""
    database_path = Path(path)
    if database_path.exists():
        connection = _connect_existing(database_path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            if version == 0 and tables == 0:
                migrate_schema(connection)
            else:
                if version != SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Unsupported company registry schema version {version}; "
                        f"initialization will not migrate an existing database. "
                        "Run the explicit registry migration command first."
                    )
                _validate_schema(connection)
        finally:
            connection.close()
        return database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        _enable_foreign_keys(connection)
        migrate_schema(connection)
    except BaseException:
        connection.close()
        database_path.unlink(missing_ok=True)
        raise
    finally:
        if connection:
            connection.close()
    return database_path


def migrate_registry(path: str | Path = DEFAULT_DATABASE) -> Path:
    """Explicitly migrate an existing registry to the supported schema version."""
    database_path = Path(path)
    connection = _connect_existing(database_path)
    try:
        migrate_schema(connection)
    finally:
        connection.close()
    return database_path


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "migrate"), nargs="?", default="init")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    arguments = parser.parse_args()
    if arguments.action == "migrate":
        path = migrate_registry(arguments.database)
        print(f"Company registry migrated: {path}")
    else:
        path = initialize_registry(arguments.database)
        print(f"Company registry initialized: {path}")
    print(f"Schema version: {SCHEMA_VERSION}")


if __name__ == "__main__":
    main()
