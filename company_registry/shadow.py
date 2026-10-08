"""Isolated passive-shadow lifecycle, observation, and reporting utilities."""

from __future__ import annotations

from argparse import ArgumentParser
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import Lock
from uuid import UUID, uuid4, uuid5

from company_registry.models import IdentityObservation
from company_registry.normalization import canonical_json, clean_text, normalize_domain
from company_registry.resolver import IdentityResolver, normalize_observation
from company_registry.storage import connect_registry
from utils.maps_identity import maps_identities_for


DEFAULT_PRODUCTION_DATABASE = Path("data/company_registry.db")
DEFAULT_SHADOW_DATABASE = Path("data/company_registry_shadow.db")
DEFAULT_REPORT_DIRECTORY = Path("data/reports/company_registry_shadow")
SHADOW_NAMESPACE = UUID("da86313c-2cec-59dc-80e7-8c38dc1d5aa7")


SHADOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_comparisons (
    comparison_id TEXT PRIMARY KEY,
    first_run_id TEXT NOT NULL REFERENCES discovery_runs(run_id) ON DELETE RESTRICT,
    source_system TEXT NOT NULL,
    source_record_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    old_classification TEXT NOT NULL CHECK (old_classification IN ('NEW', 'KNOWN')),
    old_duplicate_kind TEXT NOT NULL,
    resolver_classification TEXT,
    resolver_action TEXT,
    company_id TEXT,
    branch_id TEXT,
    candidate_company_ids_json TEXT NOT NULL,
    candidate_branch_ids_json TEXT NOT NULL,
    matched_evidence_json TEXT NOT NULL,
    conflicts_json TEXT NOT NULL,
    requires_review INTEGER NOT NULL CHECK (requires_review IN (0, 1)),
    agreement INTEGER NOT NULL CHECK (agreement IN (0, 1)),
    historical_overlap INTEGER NOT NULL CHECK (historical_overlap IN (0, 1)),
    resolution_reason TEXT NOT NULL,
    resolver_error TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_system, source_record_key, payload_hash)
);

CREATE TABLE IF NOT EXISTS shadow_run_observations (
    run_id TEXT NOT NULL REFERENCES discovery_runs(run_id) ON DELETE CASCADE,
    comparison_id TEXT NOT NULL
        REFERENCES shadow_comparisons(comparison_id) ON DELETE CASCADE,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (run_id, comparison_id)
);

CREATE INDEX IF NOT EXISTS idx_shadow_comparisons_risk
    ON shadow_comparisons(old_classification, resolver_classification, agreement);
CREATE INDEX IF NOT EXISTS idx_shadow_run_observations_comparison
    ON shadow_run_observations(comparison_id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(kind: str, seed: str) -> str:
    return str(uuid5(SHADOW_NAMESPACE, f"{kind}|{seed}"))


def old_classification(duplicate_kind: str) -> str:
    return "KNOWN" if duplicate_kind in {"known", "same_run"} else "NEW"


def classifications_agree(old: str, new: str | None) -> bool:
    if old == "KNOWN":
        return new in {"KNOWN", "UPDATED", "LEGACY_UNKNOWN"}
    return new == "NEW"


def stable_source_key(source_system: str, row: dict, query: str = "") -> str:
    identities = maps_identities_for(row)
    place = identities["place_id"] or identities["place_url"]
    if place:
        identity = f"place:{place}"
    else:
        domain = normalize_domain(
            row.get("webpage") or row.get("website") or row.get("source_url")
        )
        if domain:
            identity = f"domain:{domain}"
        else:
            fallback = canonical_json({
                "name": row.get("title") or row.get("company_name") or row.get("name"),
                "phone": row.get("phone_number") or row.get("phone"),
                "address": row.get("address") or row.get("location"),
            })
            identity = "fallback:" + sha256(fallback.encode()).hexdigest()
    query_key = sha256(clean_text(query).casefold().encode()).hexdigest()[:16]
    return f"{source_system.casefold()}:{identity}:query:{query_key}"


def create_consistent_shadow_snapshot(
    production: Path = DEFAULT_PRODUCTION_DATABASE,
    shadow: Path = DEFAULT_SHADOW_DATABASE,
    *,
    reset: bool = False,
) -> dict[str, object]:
    """Copy production with SQLite backup and migrate only the isolated copy."""
    production = Path(production)
    shadow = Path(shadow)
    if not production.is_file():
        raise FileNotFoundError(f"Production registry not found: {production}")
    shadow.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if shadow.exists():
        if not reset:
            raise FileExistsError(
                f"Shadow registry already exists: {shadow}; use --reset to replace it"
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = shadow.with_name(f"{shadow.stem}.backup-{stamp}{shadow.suffix}")
        shadow.replace(backup_path)
    temporary = shadow.with_name(f".{shadow.name}.{uuid4().hex}.tmp")
    source = sqlite3.connect(f"file:{production.resolve()}?mode=ro", uri=True)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
    except BaseException:
        target.close()
        source.close()
        if temporary.exists():
            temporary.unlink()
        if backup_path is not None and not shadow.exists():
            backup_path.replace(shadow)
        raise
    finally:
        try:
            target.close()
        finally:
            source.close()
    try:
        connection = connect_registry(temporary)
        try:
            connection.executescript(SHADOW_SCHEMA)
            connection.commit()
        finally:
            connection.close()
        result = inspect_shadow_database(temporary)
        temporary.replace(shadow)
        result["path"] = str(shadow)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        if backup_path is not None and not shadow.exists():
            backup_path.replace(shadow)
        raise
    result["backup_path"] = str(backup_path) if backup_path else None
    return result


def inspect_shadow_database(shadow: Path = DEFAULT_SHADOW_DATABASE) -> dict[str, object]:
    shadow = Path(shadow)
    connection = sqlite3.connect(f"file:{shadow.resolve()}?mode=ro", uri=True)
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {}
        for table in (
            "companies", "branches", "company_identities", "branch_identities",
            "historical_source_records", "discovery_observations",
            "shadow_comparisons", "shadow_run_observations",
        ):
            counts[table] = (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if table in tables else None
            )
        return {
            "path": str(shadow),
            "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
            "counts": counts,
        }
    finally:
        connection.close()


class ShadowObserver:
    """Best-effort resolver adapter that cannot raise into its caller."""

    def __init__(
        self,
        database: Path = DEFAULT_SHADOW_DATABASE,
        *,
        source_system: str,
        report_directory: Path = DEFAULT_REPORT_DIRECTORY,
        run_id: str | None = None,
    ) -> None:
        self.database = Path(database)
        if not self.database.is_file():
            raise FileNotFoundError(
                f"Shadow registry is not initialized: {self.database}"
            )
        self.source_system = source_system.upper()
        self.run_id = run_id or f"shadow-{self.source_system.casefold()}-{uuid4()}"
        self.report_directory = Path(report_directory)
        self.report_directory.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.report_directory / f"{self.run_id}.jsonl"
        self.summary_path = self.report_directory / f"{self.run_id}.summary.json"
        self._lock = Lock()
        self._closed = False
        connection = connect_registry(self.database)
        try:
            connection.executescript(SHADOW_SCHEMA)
            now = utc_now()
            connection.execute(
                """INSERT OR IGNORE INTO discovery_runs
                   (run_id, run_type, status, started_at, created_at)
                   VALUES (?, 'SCRAPE', 'RUNNING', ?, ?)""",
                (self.run_id, now, now),
            )
            connection.commit()
        finally:
            connection.close()

    def _persist_comparison(self, item, old: str, duplicate_kind: str,
                            result, error: str, now: str) -> tuple[dict[str, object], bool]:
        comparison_id = _stable_id(
            "comparison",
            f"{item.source_system}|{item.source_record_key}|{item.payload_hash}",
        )
        new = result.classification.value if result is not None else None
        action = result.action.value if result is not None else None
        candidates_company = result.candidate_company_ids if result is not None else ()
        candidates_branch = result.candidate_branch_ids if result is not None else ()
        matched = result.matched_evidence if result is not None else ()
        conflicts = result.conflicting_evidence if result is not None else ()
        reason = result.reason if result is not None else "shadow resolver error"
        historical_overlap = "historical" in reason.casefold()
        record = {
            "comparison_id": comparison_id,
            "run_id": self.run_id,
            "source_system": item.source_system,
            "source_record_key": item.source_record_key,
            "payload_hash": item.payload_hash,
            "old_classification": old,
            "old_duplicate_kind": duplicate_kind,
            "resolver_classification": new,
            "resolver_action": action,
            "company_id": result.company_id if result is not None else None,
            "branch_id": result.branch_id if result is not None else None,
            "candidate_company_ids": list(candidates_company),
            "candidate_branch_ids": list(candidates_branch),
            "matched_evidence": list(matched),
            "conflicts": list(conflicts),
            "requires_review": bool(result.requires_review) if result is not None else True,
            "agreement": classifications_agree(old, new),
            "historical_overlap": historical_overlap,
            "resolution_reason": reason,
            "resolver_error": error,
            "observed_at": item.observed_at,
            "created_at": now,
        }
        connection = connect_registry(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO shadow_comparisons
                   (comparison_id, first_run_id, source_system, source_record_key,
                    payload_hash, old_classification, old_duplicate_kind,
                    resolver_classification, resolver_action, company_id, branch_id,
                    candidate_company_ids_json, candidate_branch_ids_json,
                    matched_evidence_json, conflicts_json, requires_review,
                    agreement, historical_overlap, resolution_reason, resolver_error,
                    observed_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    comparison_id, self.run_id, item.source_system,
                    item.source_record_key, item.payload_hash, old, duplicate_kind,
                    new, action, record["company_id"], record["branch_id"],
                    canonical_json(candidates_company), canonical_json(candidates_branch),
                    canonical_json(matched), canonical_json(conflicts),
                    int(record["requires_review"]), int(record["agreement"]),
                    int(historical_overlap), reason, error, item.observed_at, now,
                ),
            )
            cursor = connection.execute(
                """INSERT OR IGNORE INTO shadow_run_observations
                   (run_id, comparison_id, submitted_at) VALUES (?, ?, ?)""",
                (self.run_id, comparison_id, now),
            )
            inserted_for_run = cursor.rowcount == 1
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return record, inserted_for_run

    def observe(
        self,
        row: dict,
        *,
        duplicate_kind: str = "",
        query: str = "",
        source_record_key: str = "",
        observed_at: str = "",
    ) -> dict[str, object] | None:
        """Resolve and log one observation; return None on every failure."""
        try:
            old = old_classification(duplicate_kind)
            source_key = source_record_key or stable_source_key(
                self.source_system, row, query,
            )
            observation = IdentityObservation(
                source_system=self.source_system,
                source_record_key=source_key,
                observed_at=clean_text(observed_at or row.get("added_at")) or utc_now(),
                name=row.get("title") or row.get("company_name") or row.get("name") or "",
                place_id=(
                    row.get("place_id") or row.get("map_place_id")
                    or row.get("map_link") or row.get("maps_identity") or ""
                ),
                website_url=row.get("webpage") or row.get("website") or row.get("source_url") or "",
                phone=row.get("phone_number") or row.get("phone") or "",
                address=row.get("address") or row.get("location") or "",
                raw_payload=dict(row),
            )
            item = normalize_observation(observation)
            result = None
            error = ""
            try:
                result = IdentityResolver(self.database).resolve(self.run_id, observation)
            except BaseException as exception:
                error = f"{type(exception).__name__}: {exception}"[:1000]
            now = utc_now()
            record, inserted = self._persist_comparison(
                item, old, duplicate_kind, result, error, now,
            )
            if inserted:
                with self._lock:
                    with self.jsonl_path.open("a", encoding="utf-8") as handle:
                        handle.write(canonical_json(record) + "\n")
            return record
        except BaseException:
            return None

    def summary(self) -> dict[str, object]:
        connection = connect_registry(self.database)
        try:
            rows = connection.execute(
                """SELECT c.* FROM shadow_comparisons c
                   JOIN shadow_run_observations r USING (comparison_id)
                   WHERE r.run_id=? ORDER BY c.created_at, c.comparison_id""",
                (self.run_id,),
            ).fetchall()
        finally:
            connection.close()
        cross = Counter(
            f"{row['old_classification']}->{row['resolver_classification'] or 'ERROR'}"
            for row in rows
        )
        actions = Counter(row["resolver_action"] or "ERROR" for row in rows)
        return {
            "run_id": self.run_id,
            "source_system": self.source_system,
            "shadow_database": str(self.database),
            "total_observations": len(rows),
            "agreements": sum(row["agreement"] for row in rows),
            "disagreements": sum(not row["agreement"] for row in rows),
            "classification_comparison": dict(sorted(cross.items())),
            "new_company": actions["CREATE_COMPANY"],
            "new_branch": actions["CREATE_BRANCH"],
            "historical_ambiguity_overlap": sum(row["historical_overlap"] for row in rows),
            "resolver_errors": sum(bool(row["resolver_error"]) for row in rows),
            "high_risk_old_known_to_new": sum(
                row["old_classification"] == "KNOWN"
                and row["resolver_classification"] == "NEW" for row in rows
            ),
            "jsonl_report": str(self.jsonl_path),
        }

    def close(self) -> dict[str, object]:
        if self._closed:
            return self.summary()
        summary = self.summary()
        connection = connect_registry(self.database)
        try:
            status = "PARTIAL" if summary["resolver_errors"] else "SUCCESS"
            connection.execute(
                "UPDATE discovery_runs SET status=?, finished_at=? WHERE run_id=?",
                (status, utc_now(), self.run_id),
            )
            connection.commit()
        finally:
            connection.close()
        self.summary_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        self._closed = True
        return summary


def open_shadow_observer(enabled: bool, *, source_system: str,
                         database: Path = DEFAULT_SHADOW_DATABASE,
                         report_directory: Path = DEFAULT_REPORT_DIRECTORY):
    if not enabled:
        return None
    try:
        return ShadowObserver(
            database, source_system=source_system,
            report_directory=report_directory,
        )
    except BaseException as exception:
        print(f"[shadow] disabled after initialization error: {type(exception).__name__}: {exception}")
        return None


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "refresh", "inspect"))
    parser.add_argument("--production", type=Path, default=DEFAULT_PRODUCTION_DATABASE)
    parser.add_argument("--shadow", type=Path, default=DEFAULT_SHADOW_DATABASE)
    parser.add_argument(
        "--reset", action="store_true",
        help="Explicitly archive and replace an existing shadow database",
    )
    arguments = parser.parse_args()
    if arguments.action == "inspect":
        result = inspect_shadow_database(arguments.shadow)
    else:
        if arguments.action == "refresh" and not arguments.reset:
            parser.error("refresh requires --reset")
        result = create_consistent_shadow_snapshot(
            arguments.production, arguments.shadow, reset=arguments.reset,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
