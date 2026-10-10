"""Focused schema-v6 and Maps lifecycle foundation tests."""

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from company_registry.maps_authoritative import AuthoritativeMapsSession, maps_observation
from company_registry.run_lifecycle import RunStateTransitionError, transition_run
from company_registry.schema import (
    MIGRATION_1,
    MIGRATION_2,
    MIGRATION_3,
    MIGRATION_4,
    MIGRATION_5,
)
from company_registry.service import RegistryService
from company_registry.storage import initialize_registry, migrate_registry, open_registry
from utils.run_acceptance_budget import RunAcceptanceBudget


STAMP = "2026-10-10T10:00:00+00:00"


class SchemaV6LifecycleTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _v5_database(self) -> Path:
        database = self.root / "v5.db"
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        for version, script in enumerate(
            (MIGRATION_1, MIGRATION_2, MIGRATION_3, MIGRATION_4, MIGRATION_5),
            start=1,
        ):
            connection.executescript(script)
            connection.execute(f"PRAGMA user_version={version}")
        connection.execute(
            """INSERT INTO discovery_runs
               (run_id, run_type, status, started_at, created_at)
               VALUES ('legacy-run', 'LEGACY_IMPORT', 'SUCCESS', ?, ?)""",
            (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO companies
               (company_id, canonical_name, discovery_status, first_seen_at,
                last_seen_at, created_at, updated_at)
               VALUES ('legacy-company', 'Legacy', 'LEGACY_UNKNOWN', NULL,
                       NULL, ?, ?)""",
            (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO branches
               (branch_id, company_id, display_name, discovery_status,
                created_at, updated_at)
               VALUES ('legacy-branch', 'legacy-company', 'Legacy',
                       'LEGACY_UNKNOWN', ?, ?)""",
            (STAMP, STAMP),
        )
        connection.execute(
            """INSERT INTO historical_source_records
               (source_record_id, run_id, source_path, source_kind,
                source_row_number, source_content_hash, resolution_status,
                company_id, branch_id, raw_record_json, created_at)
               VALUES ('source-1', 'legacy-run', 'history.csv', 'GOOGLE_MAPS',
                       2, ?, 'IMPORTED', 'legacy-company', 'legacy-branch',
                       '{}', ?)""",
            ("a" * 64, STAMP),
        )
        connection.commit()
        connection.close()
        return database

    def _observation(self):
        return maps_observation(
            {
                "title": "Lifecycle Co",
                "map_link": "https://google.com/maps/data=!4m1!1sChIJ-Life",
                "webpage": "https://lifecycle.test",
                "phone_number": "+216 71 000 001",
                "address": "1 Test Street, Tunis",
            },
            query="software Tunis",
            observed_at=STAMP,
        )

    def test_v5_migration_is_explicit_idempotent_and_preserves_history(self):
        database = self._v5_database()
        with self.assertRaisesRegex(RuntimeError, "explicit registry migration"):
            open_registry(database)
        raw = sqlite3.connect(database)
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 5)
        raw.close()

        migrate_registry(database)
        migrate_registry(database)
        connection = open_registry(database)
        run = connection.execute(
            """SELECT status, new_company_limit, run_config_hash, updated_at
                 FROM discovery_runs WHERE run_id='legacy-run'"""
        ).fetchone()
        company = connection.execute(
            """SELECT discovery_status, first_seen_at FROM companies
                WHERE company_id='legacy-company'"""
        ).fetchone()
        provenance = connection.execute(
            """SELECT run_id, company_id, branch_id, raw_record_json
                 FROM historical_source_records WHERE source_record_id='source-1'"""
        ).fetchone()
        self.assertEqual(tuple(run[:3]), ("SUCCESS", None, None))
        self.assertTrue(run["updated_at"])
        self.assertEqual(tuple(company), ("LEGACY_UNKNOWN", None))
        self.assertEqual(
            tuple(provenance),
            ("legacy-run", "legacy-company", "legacy-branch", "{}"),
        )
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        connection.close()

    def test_v6_migration_rolls_back_on_foreign_key_violation(self):
        database = self._v5_database()
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM companies WHERE company_id='legacy-company'")
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(RuntimeError, "foreign-key violations"):
            migrate_registry(database)
        connection = sqlite3.connect(database)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(discovery_runs)")
        }
        self.assertNotIn("run_config_hash", columns)
        self.assertEqual(connection.execute(
            "SELECT status FROM discovery_runs WHERE run_id='legacy-run'"
        ).fetchone()[0], "SUCCESS")
        connection.close()

    def test_invalid_transitions_and_terminal_writes_are_rejected(self):
        database = self.root / "registry.db"
        initialize_registry(database)
        session = AuthoritativeMapsSession(database, self.root / "exports", run_id="run")
        session.fail()
        with self.assertRaises(RunStateTransitionError):
            transition_run(database, "run", "RUNNING")
        with self.assertRaisesRegex(RuntimeError, "does not accept decisions"):
            RegistryService(database).resolve("run", self._observation())
        connection = open_registry(database)
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM discovery_run_decisions"
        ).fetchone()[0], 0)
        connection.close()

    def test_maps_completion_persists_config_and_succeeds_after_verification(self):
        database = self.root / "registry.db"
        initialize_registry(database)
        session = AuthoritativeMapsSession(
            database,
            self.root / "exports",
            run_id="maps-run",
            new_company_limit=2,
        )
        session.configure_queries([" software Tunis "])
        session.resolve(self._observation(), RunAcceptanceBudget(2))
        result = session.complete()
        self.assertEqual(result["rows"], 1)
        connection = open_registry(database)
        row = connection.execute(
            """SELECT status, new_company_limit, length(run_config_hash),
                      finished_at
                 FROM discovery_runs WHERE run_id='maps-run'"""
        ).fetchone()
        self.assertEqual(tuple(row[:3]), ("SUCCESS", 2, 64))
        self.assertTrue(row["finished_at"])
        connection.close()

    def test_failed_finalization_stays_finalizing_and_rejects_writes(self):
        database = self.root / "registry.db"
        initialize_registry(database)
        session = AuthoritativeMapsSession(database, self.root / "exports", run_id="run")
        session.resolve(self._observation(), RunAcceptanceBudget(1))
        with patch(
            "company_registry.maps_authoritative.verify_export_manifest",
            side_effect=ValueError("bad manifest"),
        ):
            with self.assertRaisesRegex(ValueError, "bad manifest"):
                session.complete()
        connection = open_registry(database)
        self.assertEqual(connection.execute(
            "SELECT status FROM discovery_runs WHERE run_id='run'"
        ).fetchone()[0], "FINALIZING")
        connection.close()
        with self.assertRaisesRegex(RuntimeError, "FINALIZING"):
            RegistryService(database).resolve("run", self._observation())
