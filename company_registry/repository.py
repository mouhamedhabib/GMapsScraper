"""SQLite persistence primitives for deterministic identity resolution."""

from __future__ import annotations

from collections import defaultdict
import json
import sqlite3
from uuid import UUID, uuid5

from company_registry.models import Classification, Resolution, ResolutionAction
from company_registry.normalization import canonical_json


ENTITY_NAMESPACE = UUID("3ac8b3f8-1124-52c4-9087-418739f4e7c2")


def stable_id(kind: str, seed: str) -> str:
    return str(uuid5(ENTITY_NAMESPACE, f"{kind}|{seed}"))


class RegistryRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def observation(self, source_system: str, source_key: str,
                    payload_hash: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT * FROM discovery_observations
               WHERE source_system=? AND source_record_key=? AND payload_hash=?""",
            (source_system, source_key, payload_hash),
        ).fetchone()

    def prior_run_decision(self, run_id: str, source_system: str,
                           source_key: str, payload_hash: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT decision.* FROM discovery_run_decisions decision
               JOIN discovery_observations observation
                 ON observation.observation_id=decision.observation_id
               WHERE decision.run_id=? AND observation.source_system=?
                 AND observation.source_record_key=? AND observation.payload_hash=?""",
            (run_id, source_system, source_key, payload_hash),
        ).fetchone()

    @staticmethod
    def resolution_from_row(row: sqlite3.Row) -> Resolution:
        return Resolution(
            classification=Classification(row["classification"]),
            action=ResolutionAction(row["resolution_action"]),
            company_id=row["company_id"], branch_id=row["branch_id"],
            candidate_company_ids=tuple(json.loads(row["candidate_company_ids_json"])),
            candidate_branch_ids=tuple(json.loads(row["candidate_branch_ids_json"])),
            matched_evidence=tuple(json.loads(row["matched_evidence_json"])),
            conflicting_evidence=tuple(json.loads(row["conflicts_json"])),
            requires_review=bool(row["requires_review"]),
            reason=row["resolution_reason"],
        )

    def company_candidates(self, identity_type: str, value: str) -> set[str]:
        if not value:
            return set()
        return {
            row[0] for row in self.connection.execute(
                """SELECT company_id FROM company_identities
                   WHERE identity_type=? AND normalized_value=?""",
                (identity_type, value),
            )
        }

    def branch_candidates(self, identity_type: str, value: str) -> set[str]:
        if not value:
            return set()
        return {
            row[0] for row in self.connection.execute(
                """SELECT branch_id FROM branch_identities
                   WHERE identity_type=? AND normalized_value=?""",
                (identity_type, value),
            )
        }

    def branch_company(self, branch_id: str) -> str:
        row = self.connection.execute(
            "SELECT company_id FROM branches WHERE branch_id=?", (branch_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Unknown branch: {branch_id}")
        return row[0]

    def company(self, company_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM companies WHERE company_id=?", (company_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Unknown company: {company_id}")
        return row

    def branch(self, branch_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM branches WHERE branch_id=?", (branch_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Unknown branch: {branch_id}")
        return row

    def company_identity_values(self, company_id: str) -> dict[str, set[str]]:
        values: dict[str, set[str]] = defaultdict(set)
        for row in self.connection.execute(
            """SELECT identity_type, normalized_value FROM company_identities
               WHERE company_id=?""", (company_id,),
        ):
            values[row["identity_type"]].add(row["normalized_value"])
        return values

    def branch_identity_values(self, branch_id: str) -> dict[str, set[str]]:
        values: dict[str, set[str]] = defaultdict(set)
        for row in self.connection.execute(
            """SELECT identity_type, normalized_value FROM branch_identities
               WHERE branch_id=?""", (branch_id,),
        ):
            values[row["identity_type"]].add(row["normalized_value"])
        return values

    def unresolved_history(self):
        return self.connection.execute(
            """SELECT resolution_status, source_content_hash, raw_record_json, reason
               FROM historical_source_records
               WHERE resolution_status IN ('AMBIGUOUS', 'QUARANTINED')"""
        )

    def create_company(self, company_id: str, name: str, observed_at: str,
                       now: str) -> None:
        self.connection.execute(
            """INSERT INTO companies
               (company_id, canonical_name, discovery_status, first_seen_at,
                last_seen_at, created_at, updated_at)
               VALUES (?, ?, 'OBSERVED', ?, ?, ?, ?)""",
            (company_id, name or None, observed_at, observed_at, now, now),
        )

    def create_branch(self, branch_id: str, company_id: str, name: str,
                      place_id: str, website: str, phone: str, address: str,
                      observed_at: str, now: str) -> None:
        self.connection.execute(
            """INSERT INTO branches
               (branch_id, company_id, display_name, google_maps_place_id,
                website_url, phone, address, discovery_status, first_seen_at,
                last_seen_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'OBSERVED', ?, ?, ?, ?)""",
            (branch_id, company_id, name or None, place_id or None,
             website or None, phone or None, address or None,
             observed_at, observed_at, now, now),
        )

    def add_company_identity(self, company_id: str, identity_type: str,
                             raw: str, normalized: str, observed_at: str,
                             now: str) -> bool:
        if not normalized:
            return False
        identity_id = stable_id("company-identity", f"{company_id}|{identity_type}|{normalized}")
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO company_identities
               (identity_id, company_id, identity_type, identity_value,
                normalized_value, first_seen_at, last_seen_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (identity_id, company_id, identity_type, raw, normalized,
             observed_at, observed_at, now, now),
        )
        return cursor.rowcount == 1

    def add_branch_identity(self, branch_id: str, identity_type: str,
                            raw: str, normalized: str, observed_at: str,
                            now: str) -> bool:
        if not normalized:
            return False
        identity_id = stable_id("branch-identity", f"{branch_id}|{identity_type}|{normalized}")
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO branch_identities
               (branch_identity_id, branch_id, identity_type, identity_value,
                normalized_value, first_seen_at, last_seen_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (identity_id, branch_id, identity_type, raw, normalized,
             observed_at, observed_at, now, now),
        )
        return cursor.rowcount == 1

    def touch_company(self, company_id: str, observed_at: str, now: str) -> None:
        self.connection.execute(
            """UPDATE companies SET
                 last_seen_at=CASE WHEN last_seen_at IS NULL OR last_seen_at < ?
                                   THEN ? ELSE last_seen_at END,
                 updated_at=? WHERE company_id=?""",
            (observed_at, observed_at, now, company_id),
        )

    def touch_branch(self, branch_id: str, observed_at: str, now: str) -> None:
        self.connection.execute(
            """UPDATE branches SET
                 last_seen_at=CASE WHEN last_seen_at IS NULL OR last_seen_at < ?
                                   THEN ? ELSE last_seen_at END,
                 updated_at=? WHERE branch_id=?""",
            (observed_at, observed_at, now, branch_id),
        )

    def persist_observation(self, observation_id: str, run_id: str, normalized,
                            resolution: Resolution, normalized_json: str,
                            resolver_version: str, now: str) -> None:
        self.connection.execute(
            """INSERT INTO discovery_observations
               (observation_id, run_id, source_system, source_record_key,
                payload_hash, raw_payload_json, normalized_evidence_json,
                classification, resolution_action, company_id, branch_id,
                candidate_company_ids_json, candidate_branch_ids_json,
                matched_evidence_json, conflicts_json, requires_review,
                resolution_reason, resolver_version, observed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                observation_id, run_id, normalized.source_system,
                normalized.source_record_key, normalized.payload_hash,
                normalized.raw_payload_json, normalized_json,
                resolution.classification.value, resolution.action.value,
                resolution.company_id, resolution.branch_id,
                canonical_json(resolution.candidate_company_ids),
                canonical_json(resolution.candidate_branch_ids),
                canonical_json(resolution.matched_evidence),
                canonical_json(resolution.conflicting_evidence),
                int(resolution.requires_review), resolution.reason,
                resolver_version, normalized.observed_at, now,
            ),
        )

    def persist_run_decision(self, decision_id: str, run_id: str,
                             observation_id: str, resolution: Resolution,
                             resolver_version: str, observed_at: str,
                             now: str) -> None:
        self.connection.execute(
            """INSERT INTO discovery_run_decisions
               (decision_id, run_id, observation_id, classification,
                resolution_action, company_id, branch_id,
                candidate_company_ids_json, candidate_branch_ids_json,
                matched_evidence_json, conflicts_json, requires_review,
                resolution_reason, resolver_version, observed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                decision_id, run_id, observation_id,
                resolution.classification.value, resolution.action.value,
                resolution.company_id, resolution.branch_id,
                canonical_json(resolution.candidate_company_ids),
                canonical_json(resolution.candidate_branch_ids),
                canonical_json(resolution.matched_evidence),
                canonical_json(resolution.conflicting_evidence),
                int(resolution.requires_review), resolution.reason,
                resolver_version, observed_at, now,
            ),
        )

    def record_run_company(self, run_id: str, resolution: Resolution,
                           observed_at: str, now: str) -> None:
        if resolution.company_id is None or resolution.classification in {
            Classification.AMBIGUOUS, Classification.QUARANTINED,
        }:
            return
        status = resolution.classification.value
        priority = {"LEGACY_UNKNOWN": 0, "KNOWN": 1, "UPDATED": 2, "NEW": 3}
        existing = self.connection.execute(
            """SELECT discovery_status FROM run_companies
               WHERE run_id=? AND company_id=?""",
            (run_id, resolution.company_id),
        ).fetchone()
        if existing is None:
            self.connection.execute(
                """INSERT INTO run_companies
                   (run_id, company_id, discovery_status, observed_at, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (run_id, resolution.company_id, status, observed_at, now),
            )
        elif priority[status] > priority[existing[0]]:
            self.connection.execute(
                """UPDATE run_companies SET discovery_status=?, observed_at=?
                   WHERE run_id=? AND company_id=?""",
                (status, observed_at, run_id, resolution.company_id),
            )
