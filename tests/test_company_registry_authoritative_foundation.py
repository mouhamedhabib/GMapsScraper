"""Offline Phase 3B authoritative-foundation regression coverage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from csv import DictReader
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.discovery_adapter import (
    DEFAULT_DISCOVERY_MODE,
    AuthoritativeDiscoveryAdapter,
    DiscoveryMode,
    RegistryUnavailableError,
)
from company_registry.exports import export_new_companies, verify_export_manifest
from company_registry.models import Classification, IdentityObservation, ResolutionAction
from company_registry.schema import MIGRATION_1, MIGRATION_2, MIGRATION_3, SCHEMA_VERSION
from company_registry.service import RegistryService
from company_registry.storage import (
    initialize_registry,
    migrate_registry,
    open_registry,
)


STAMP = "2026-10-08T12:00:00+00:00"


class AuthoritativeFoundationTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "registry.db"
        initialize_registry(self.database)
        self.add_run("run-a")
        self.add_run("run-b")
        self.adapter = AuthoritativeDiscoveryAdapter(self.database)

    def add_run(self, run_id):
        connection = open_registry(self.database)
        connection.execute(
            """INSERT INTO discovery_runs
               (run_id, run_type, status, started_at, created_at)
               VALUES (?, 'SCRAPE', 'RUNNING', ?, ?)""",
            (run_id, STAMP, STAMP),
        )
        connection.commit()
        connection.close()

    def observation(self, key="same", **changes):
        values = {
            "source_system": "GOOGLE_MAPS",
            "source_record_key": key,
            "observed_at": STAMP,
            "name": "Acme Consulting",
            "place_id": "ChIJ-Acme-Tunis",
            "website_url": "https://acme.example.tn/contact",
            "phone": "+216 71 123 456",
            "address": "1 Avenue de Tunis, Tunis",
            "raw_payload": {"title": "Acme Consulting", "source": "fixture"},
        }
        values.update(changes)
        return IdentityObservation(**values)

    def scalar(self, sql, parameters=()):
        connection = open_registry(self.database)
        try:
            return connection.execute(sql, parameters).fetchone()[0]
        finally:
            connection.close()

    def test_feature_mode_contract_defaults_to_legacy(self):
        self.assertEqual(DEFAULT_DISCOVERY_MODE, DiscoveryMode.LEGACY)
        self.assertTrue(DiscoveryMode.AUTHORITATIVE_CANARY.is_authoritative)
        self.assertTrue(DiscoveryMode.AUTHORITATIVE.is_authoritative)
        self.assertFalse(DiscoveryMode.SHADOW.is_authoritative)

    def test_preview_never_persists_or_authorizes_export(self):
        decision = self.adapter.preview(self.observation())
        self.assertEqual(decision.resolution.classification, Classification.NEW)
        self.assertFalse(decision.committed)
        self.assertFalse(decision.authorizes_new_company_export)
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM discovery_run_decisions"), 0)

    def test_same_run_is_idempotent_and_later_run_re_evaluates_new_as_known(self):
        observation = self.observation()
        first = self.adapter.commit("run-a", observation)
        repeated = self.adapter.commit("run-a", observation)
        later = self.adapter.commit("run-b", observation)
        self.assertEqual(first.resolution.classification, Classification.NEW)
        self.assertEqual(repeated, first)
        self.assertEqual(later.resolution.classification, Classification.KNOWN)
        self.assertEqual(later.resolution.action, ResolutionAction.MATCH_ONLY)
        self.assertEqual(first.resolution.company_id, later.resolution.company_id)
        self.assertEqual(first.resolution.branch_id, later.resolution.branch_id)
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM branches"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM discovery_observations"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM discovery_run_decisions"), 2)

    def test_multiple_branches_keep_one_company_and_stable_ids(self):
        first = self.adapter.commit("run-a", self.observation("tunis"))
        second = self.adapter.commit("run-a", self.observation(
            "sousse", place_id="ChIJ-Acme-Sousse", phone="+216 73 999 999",
            address="2 Avenue Habib Bourguiba, Sousse",
            raw_payload={"title": "Acme Consulting", "branch": "Sousse"},
        ))
        self.assertEqual(second.resolution.classification, Classification.UPDATED)
        self.assertEqual(second.resolution.action, ResolutionAction.CREATE_BRANCH)
        self.assertEqual(second.resolution.company_id, first.resolution.company_id)
        self.assertNotEqual(second.resolution.branch_id, first.resolution.branch_id)
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM branches"), 2)

    def test_concurrent_identical_commits_create_one_decision(self):
        observation = self.observation("concurrent")
        with ThreadPoolExecutor(max_workers=6) as workers:
            decisions = list(workers.map(
                lambda _: AuthoritativeDiscoveryAdapter(self.database).commit(
                    "run-a", observation,
                ),
                range(12),
            ))
        self.assertTrue(all(item == decisions[0] for item in decisions))
        self.assertEqual(self.scalar("SELECT count(*) FROM companies"), 1)
        self.assertEqual(self.scalar("SELECT count(*) FROM discovery_run_decisions"), 1)

    def test_transaction_failure_rolls_back_entity_observation_and_decision(self):
        def fail(checkpoint):
            if checkpoint == "after_entity_changes":
                raise RuntimeError("injected failure")

        with self.assertRaisesRegex(RuntimeError, "injected failure"):
            RegistryService(self.database, failure_injector=fail).resolve(
                "run-a", self.observation("rollback"),
            )
        for table in (
            "companies", "branches", "discovery_observations",
            "discovery_run_decisions",
        ):
            self.assertEqual(self.scalar(f"SELECT count(*) FROM {table}"), 0)

    def test_legacy_origin_and_unknown_first_seen_are_preserved(self):
        connection = open_registry(self.database)
        connection.execute(
            """INSERT INTO companies VALUES
               ('legacy-company', 'Legacy Co', 'LEGACY_UNKNOWN', NULL, NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO branches VALUES
               ('legacy-branch', 'legacy-company', 'Legacy Co', 'place_id:legacy',
                NULL, NULL, NULL, 'LEGACY_UNKNOWN', NULL, NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO branch_identities VALUES
               ('legacy-place', 'legacy-branch', 'GOOGLE_MAPS_PLACE_ID',
                'place_id:legacy', 'place_id:legacy', NULL, NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.commit()
        connection.close()
        decision = self.adapter.commit("run-a", self.observation(
            "legacy", name="Legacy Co", place_id="legacy", website_url="",
            phone="", address="", raw_payload={"name": "Legacy Co"},
        ))
        self.assertNotEqual(decision.resolution.classification, Classification.NEW)
        connection = open_registry(self.database)
        company = connection.execute(
            "SELECT discovery_status, first_seen_at FROM companies WHERE company_id='legacy-company'"
        ).fetchone()
        branch = connection.execute(
            "SELECT discovery_status, first_seen_at FROM branches WHERE branch_id='legacy-branch'"
        ).fetchone()
        connection.close()
        self.assertEqual(tuple(company), ("LEGACY_UNKNOWN", None))
        self.assertEqual(tuple(branch), ("LEGACY_UNKNOWN", None))

    def test_export_filters_review_and_branches_and_recovers_after_failure(self):
        created = self.adapter.commit("run-a", self.observation("new"))
        self.adapter.commit("run-a", self.observation(
            "branch", place_id="ChIJ-Acme-Sfax", phone="+216 74 222 222",
            address="3 Route de Sfax", raw_payload={"title": "Acme Consulting"},
        ))
        ambiguous = self.adapter.commit("run-a", self.observation(
            "ambiguous", name="Different Company", place_id="ChIJ-Different",
            phone="+216 70 000 000", address="Other Street",
            raw_payload={"title": "Different Company"},
        ))
        quarantined = self.adapter.commit("run-a", self.observation(
            "quarantine", name="", place_id="", website_url="", phone="",
            address="", raw_payload={"title": ""},
        ))
        self.assertEqual(ambiguous.resolution.classification, Classification.AMBIGUOUS)
        self.assertEqual(quarantined.resolution.classification, Classification.QUARANTINED)
        output = self.root / "exports"

        def fail(checkpoint):
            if checkpoint == "before_manifest_publish":
                raise OSError("injected publication failure")

        before = self.scalar("SELECT count(*) FROM discovery_run_decisions")
        with self.assertRaisesRegex(OSError, "injected publication failure"):
            export_new_companies(self.database, "run-a", output, failure_injector=fail)
        self.assertEqual(self.scalar("SELECT count(*) FROM discovery_run_decisions"), before)

        result = export_new_companies(self.database, "run-a", output)
        repeated = export_new_companies(self.database, "run-a", output)
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["sha256"], repeated["sha256"])
        with result["csv"].open("r", newline="", encoding="utf-8-sig") as handle:
            rows = list(DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["registry_decision_id"], created.decision_id)
        self.assertEqual(rows[0]["classification"], "NEW")
        manifest = json.loads(result["manifest"].read_text(encoding="utf-8"))
        self.assertEqual(manifest["row_count"], 1)
        self.assertEqual(manifest["decision_ids"], [created.decision_id])
        verified = verify_export_manifest(result["manifest"])
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["sha256"], result["sha256"])


class RegistryConnectionSafetyTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_runtime_open_never_creates_a_missing_database(self):
        missing = self.root / "missing" / "registry.db"
        with self.assertRaises(FileNotFoundError):
            open_registry(missing)
        self.assertFalse(missing.exists())
        with self.assertRaises(RegistryUnavailableError):
            AuthoritativeDiscoveryAdapter(missing).preview(IdentityObservation(
                "TEST", "one", STAMP, name="Acme", website_url="https://acme.tn",
            ))
        self.assertFalse(missing.exists())

    def test_runtime_open_refuses_v3_without_migrating_it(self):
        path = self.root / "v3.db"
        connection = sqlite3.connect(path)
        connection.executescript(MIGRATION_1)
        connection.executescript(MIGRATION_2)
        connection.executescript(MIGRATION_3)
        connection.execute("PRAGMA user_version=3")
        connection.commit()
        connection.close()
        before = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "runtime requires version"):
            open_registry(path)
        self.assertEqual(path.read_bytes(), before)
        migrate_registry(path)
        connection = open_registry(path)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        connection.close()

    def test_runtime_open_rejects_newer_unsupported_schema(self):
        path = self.root / "future.db"
        initialize_registry(path)
        connection = sqlite3.connect(path)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        connection.close()
        with self.assertRaisesRegex(RuntimeError, "Unsupported company registry schema"):
            open_registry(path)

    def test_v4_migration_backfills_original_run_decision(self):
        path = self.root / "observed-v3.db"
        connection = sqlite3.connect(path)
        connection.executescript(MIGRATION_1)
        connection.executescript(MIGRATION_2)
        connection.executescript(MIGRATION_3)
        connection.execute("PRAGMA user_version=3")
        connection.execute(
            """INSERT INTO discovery_runs
               (run_id, run_type, status, started_at, created_at)
               VALUES ('old-run', 'SCRAPE', 'RUNNING', ?, ?)""",
            (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO companies VALUES
               ('company-1', 'Acme', 'OBSERVED', ?, ?, ?, ?)""",
            (STAMP, STAMP, STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO discovery_observations
               (observation_id, run_id, source_system, source_record_key,
                payload_hash, raw_payload_json, normalized_evidence_json,
                classification, resolution_action, company_id, branch_id,
                candidate_company_ids_json, candidate_branch_ids_json,
                matched_evidence_json, conflicts_json, requires_review,
                resolution_reason, resolver_version, observed_at, created_at)
               VALUES ('observation-1', 'old-run', 'TEST', 'row-1', ?, '{}', '{}',
                       'NEW', 'CREATE_COMPANY', 'company-1', NULL,
                       '[]', '[]', '[]', '[]', 0, 'original new company',
                       '2A.1', ?, ?)""",
            ("a" * 64, STAMP, STAMP),
        )
        connection.commit()
        connection.close()
        migrate_registry(path)
        connection = open_registry(path)
        decision = connection.execute(
            """SELECT run_id, observation_id, classification, resolution_action,
                      company_id FROM discovery_run_decisions"""
        ).fetchone()
        connection.close()
        self.assertEqual(tuple(decision), (
            "old-run", "observation-1", "NEW", "CREATE_COMPANY", "company-1",
        ))
