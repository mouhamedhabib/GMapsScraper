"""Migration validation against a temporary production-registry copy."""

from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.schema import SCHEMA_VERSION
from company_registry.storage import connect_registry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "company_registry.db"


class ProductionCopyMigrationTests(TestCase):
    def test_v2_copy_migrates_without_changing_entities_or_provenance(self):
        self.assertTrue(PRODUCTION_DATABASE.is_file())
        with TemporaryDirectory() as directory:
            copy = Path(directory) / "company_registry.db"
            shutil.copy2(PRODUCTION_DATABASE, copy)
            raw = sqlite3.connect(copy)
            before_counts = {
                table: raw.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "companies", "branches", "company_identities",
                    "historical_source_records",
                )
            }
            company_ids = tuple(row[0] for row in raw.execute(
                "SELECT company_id FROM companies ORDER BY company_id"
            ))
            branch_ids = tuple(row[0] for row in raw.execute(
                "SELECT branch_id FROM branches ORDER BY branch_id"
            ))
            origins = tuple(raw.execute(
                """SELECT
                       sum(discovery_status='LEGACY_UNKNOWN'),
                       sum(first_seen_at IS NULL)
                     FROM companies"""
            ).fetchone())
            raw.close()

            connection = connect_registry(copy)
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                SCHEMA_VERSION,
            )
            after_counts = {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in before_counts
            }
            self.assertEqual(after_counts, before_counts)
            self.assertEqual(company_ids, tuple(row[0] for row in connection.execute(
                "SELECT company_id FROM companies ORDER BY company_id"
            )))
            self.assertEqual(branch_ids, tuple(row[0] for row in connection.execute(
                "SELECT branch_id FROM branches ORDER BY branch_id"
            )))
            self.assertEqual(origins, tuple(connection.execute(
                """SELECT
                       sum(discovery_status='LEGACY_UNKNOWN'),
                       sum(first_seen_at IS NULL)
                     FROM companies"""
            ).fetchone()))
            identity_count = connection.execute(
                "SELECT count(*) FROM branch_identities"
            ).fetchone()[0]
            self.assertGreaterEqual(identity_count, before_counts["branches"])
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok",
            )
            self.assertEqual(connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall(), [])
            connection.close()

            repeated = connect_registry(copy)
            self.assertEqual(repeated.execute(
                "SELECT count(*) FROM branch_identities"
            ).fetchone()[0], identity_count)
            repeated.close()
