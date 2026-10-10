"""Phase 1.5 tests for the company-registry SQLite schema only."""

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.schema import SCHEMA_VERSION
from company_registry.storage import connect_registry, initialize_registry


STAMP_1 = "2026-09-01T10:00:00+00:00"
STAMP_2 = "2026-09-02T10:00:00+00:00"


class CompanyRegistrySchemaTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "company_registry.db"
        initialize_registry(self.path)
        self.connection = connect_registry(self.path)
        self.addCleanup(self.connection.close)

    def insert_company(
        self, company_id="company-1", *, status="OBSERVED", first_seen=STAMP_1,
    ):
        self.connection.execute(
            """INSERT INTO companies
               (company_id, canonical_name, discovery_status, first_seen_at,
                last_seen_at, created_at, updated_at)
               VALUES (?, 'Acme', ?, ?, ?, ?, ?)""",
            (company_id, status, first_seen, first_seen, STAMP_1, STAMP_1),
        )

    def insert_run(self, run_id="run-1"):
        self.connection.execute(
            """INSERT INTO discovery_runs
               (run_id, run_type, status, started_at, created_at)
               VALUES (?, 'SCRAPE', 'SUCCESS', ?, ?)""",
            (run_id, STAMP_1, STAMP_1),
        )

    def test_creates_exact_versioned_table_set(self):
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertEqual(tables, {
            "companies", "branches", "company_identities",
            "discovery_runs", "run_companies", "historical_source_records",
            "branch_identities", "discovery_observations", "resolution_reviews",
            "discovery_run_decisions", "qualification_assessments",
        })
        self.assertEqual(
            self.connection.execute("PRAGMA user_version").fetchone()[0],
            SCHEMA_VERSION,
        )

    def test_every_registry_connection_enables_foreign_keys(self):
        self.assertEqual(
            self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1,
        )
        second = connect_registry(self.path)
        self.addCleanup(second.close)
        self.assertEqual(second.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_foreign_keys_reject_orphans_and_cascade_dependents(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO branches
                   (branch_id, company_id, created_at, updated_at)
                   VALUES ('branch-orphan', 'missing', ?, ?)""",
                (STAMP_1, STAMP_1),
            )

        self.insert_company()
        self.insert_run()
        self.connection.execute(
            """INSERT INTO branches
               (branch_id, company_id, google_maps_place_id, discovery_status,
                first_seen_at, last_seen_at, created_at, updated_at)
               VALUES ('branch-1', 'company-1', 'place-1', 'OBSERVED',
                       ?, ?, ?, ?)""",
            (STAMP_1, STAMP_1, STAMP_1, STAMP_1),
        )
        self.connection.execute(
            """INSERT INTO run_companies
               (run_id, company_id, discovery_status, observed_at, created_at)
               VALUES ('run-1', 'company-1', 'NEW', ?, ?)""",
            (STAMP_1, STAMP_1),
        )
        self.connection.execute(
            "DELETE FROM companies WHERE company_id='company-1'"
        )
        self.assertEqual(self.connection.execute("SELECT count(*) FROM branches").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM run_companies").fetchone()[0], 0)

    def test_place_ids_are_branch_level_and_unique(self):
        self.insert_company()
        values = (STAMP_1, STAMP_1, STAMP_1, STAMP_1)
        self.connection.execute(
            """INSERT INTO branches
               (branch_id, company_id, google_maps_place_id, discovery_status,
                first_seen_at, last_seen_at, created_at, updated_at)
               VALUES ('branch-1', 'company-1', 'place-1', 'OBSERVED', ?, ?, ?, ?)""",
            values,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO branches
                   (branch_id, company_id, google_maps_place_id, created_at, updated_at)
                   VALUES ('branch-2', 'company-1', 'place-1', ?, ?)""",
                (STAMP_1, STAMP_1),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO company_identities
                   (identity_id, company_id, identity_type, identity_value,
                    normalized_value, created_at, updated_at)
                   VALUES ('identity-place', 'company-1', 'GOOGLE_MAPS_PLACE_ID',
                           'place-2', 'place-2', ?, ?)""",
                (STAMP_1, STAMP_1),
            )

    def test_shared_domain_does_not_merge_or_violate_uniqueness(self):
        self.insert_company("company-1")
        self.insert_company("company-2")
        for number, company_id in enumerate(("company-1", "company-2"), 1):
            self.connection.execute(
                """INSERT INTO company_identities
                   (identity_id, company_id, identity_type, identity_value,
                    normalized_value, first_seen_at, last_seen_at,
                    created_at, updated_at)
                   VALUES (?, ?, 'WEBSITE_DOMAIN', 'https://sinopesoft.com',
                           'sinopesoft.com', ?, ?, ?, ?)""",
                (f"identity-{number}", company_id, STAMP_1, STAMP_1, STAMP_1, STAMP_1),
            )
        self.assertEqual(
            self.connection.execute(
                """SELECT count(*) FROM company_identities
                   WHERE identity_type='WEBSITE_DOMAIN'
                     AND normalized_value='sinopesoft.com'"""
            ).fetchone()[0],
            2,
        )

    def test_identity_is_unique_only_within_one_company_and_type(self):
        self.insert_company()
        statement = """INSERT INTO company_identities
            (identity_id, company_id, identity_type, identity_value,
             normalized_value, created_at, updated_at)
            VALUES (?, 'company-1', 'NAME', 'Acme', 'acme', ?, ?)"""
        self.connection.execute(statement, ("identity-1", STAMP_1, STAMP_1))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(statement, ("identity-2", STAMP_1, STAMP_1))

    def test_first_seen_is_immutable_while_last_seen_is_mutable(self):
        self.insert_company()
        self.connection.execute(
            """UPDATE companies SET last_seen_at=?, updated_at=?
               WHERE company_id='company-1'""",
            (STAMP_2, STAMP_2),
        )
        row = self.connection.execute(
            "SELECT first_seen_at, last_seen_at FROM companies"
        ).fetchone()
        self.assertEqual((row["first_seen_at"], row["last_seen_at"]), (STAMP_1, STAMP_2))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "first_seen_at is immutable"):
            self.connection.execute(
                "UPDATE companies SET first_seen_at=? WHERE company_id='company-1'",
                (STAMP_2,),
            )

    def test_branch_origin_and_branch_identity_first_seen_are_immutable(self):
        self.insert_company()
        self.connection.execute(
            """INSERT INTO branches
               (branch_id, company_id, discovery_status, first_seen_at,
                created_at, updated_at)
               VALUES ('branch-1', 'company-1', 'OBSERVED', ?, ?, ?)""",
            (STAMP_1, STAMP_1, STAMP_1),
        )
        self.connection.execute(
            """INSERT INTO branch_identities
               (branch_identity_id, branch_id, identity_type, identity_value,
                normalized_value, first_seen_at, created_at, updated_at)
               VALUES ('bi-1', 'branch-1', 'PHONE', '123456', '123456', ?, ?, ?)""",
            (STAMP_1, STAMP_1, STAMP_1),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "discovery_status is immutable"):
            self.connection.execute(
                "UPDATE branches SET discovery_status='LEGACY_UNKNOWN' WHERE branch_id='branch-1'"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "first_seen_at is immutable"):
            self.connection.execute(
                "UPDATE branch_identities SET first_seen_at=? WHERE branch_identity_id='bi-1'",
                (STAMP_2,),
            )

    def test_place_alias_is_unique_across_branches(self):
        self.insert_company()
        for branch in ("branch-1", "branch-2"):
            self.connection.execute(
                """INSERT INTO branches
                   (branch_id, company_id, discovery_status, first_seen_at,
                    created_at, updated_at) VALUES (?, 'company-1', 'OBSERVED', ?, ?, ?)""",
                (branch, STAMP_1, STAMP_1, STAMP_1),
            )
        statement = """INSERT INTO branch_identities
            (branch_identity_id, branch_id, identity_type, identity_value,
             normalized_value, created_at, updated_at)
            VALUES (?, ?, 'GOOGLE_MAPS_PLACE_ID', 'place_id:x', 'place_id:x', ?, ?)"""
        self.connection.execute(statement, ("bi-1", "branch-1", STAMP_1, STAMP_1))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(statement, ("bi-2", "branch-2", STAMP_1, STAMP_1))

    def test_legacy_unknown_company_can_never_be_marked_new(self):
        self.insert_company(
            status="LEGACY_UNKNOWN", first_seen=None,
        )
        self.insert_run()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be classified as NEW"):
            self.connection.execute(
                """INSERT INTO run_companies
                   (run_id, company_id, discovery_status, observed_at, created_at)
                   VALUES ('run-1', 'company-1', 'NEW', ?, ?)""",
                (STAMP_2, STAMP_2),
            )
        self.connection.execute(
            """INSERT INTO run_companies
               (run_id, company_id, discovery_status, observed_at, created_at)
               VALUES ('run-1', 'company-1', 'KNOWN', ?, ?)""",
            (STAMP_2, STAMP_2),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "discovery_status is immutable"):
            self.connection.execute(
                """UPDATE companies SET discovery_status='OBSERVED'
                   WHERE company_id='company-1'"""
            )

    def test_constraints_reject_invalid_statuses_and_blank_keys(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_company(company_id=" ")
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_company(company_id="company-invalid", status="NEW")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO discovery_runs
                   (run_id, run_type, status, started_at, created_at)
                   VALUES ('run-bad', 'SCRAPE', 'COMPLETE', ?, ?)""",
                (STAMP_1, STAMP_1),
            )

    def test_repeated_initialization_preserves_schema_and_data(self):
        self.insert_company()
        self.connection.commit()
        self.connection.close()
        self.connection = connect_registry(self.path)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM companies").fetchone()[0], 1)
        self.connection.close()
        self.connection = connect_registry(self.path)
        self.assertEqual(
            self.connection.execute("PRAGMA user_version").fetchone()[0],
            SCHEMA_VERSION,
        )

    def test_initialize_registry_handles_an_existing_empty_database(self):
        self.connection.close()
        other = Path(self.temporary.name) / "existing-empty.db"
        sqlite3.connect(other).close()
        self.assertEqual(initialize_registry(other), other)
        connection = connect_registry(other)
        self.addCleanup(connection.close)
        self.assertEqual(
            connection.execute("SELECT count(*) FROM companies").fetchone()[0], 0,
        )

    def test_versioned_database_with_wrong_columns_is_rejected(self):
        self.connection.close()
        raw = sqlite3.connect(self.path)
        raw.execute("DROP TABLE companies")
        raw.execute("CREATE TABLE companies (wrong_column TEXT)")
        raw.commit()
        raw.close()
        with self.assertRaisesRegex(RuntimeError, "unexpected columns"):
            connect_registry(self.path)

    def test_rejects_a_newer_unknown_schema_version(self):
        self.connection.close()
        raw = sqlite3.connect(self.path)
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        raw.close()
        with self.assertRaisesRegex(RuntimeError, "Unsupported company registry schema"):
            connect_registry(self.path)
