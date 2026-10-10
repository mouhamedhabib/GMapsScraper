"""Migration validation against a temporary production-registry copy."""

from pathlib import Path
from csv import DictWriter
from hashlib import sha256
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.schema import SCHEMA_VERSION
from company_registry.storage import connect_registry, migrate_registry
from utils.known_companies import KnownCompanies


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DATABASE = PROJECT_ROOT / "data" / "company_registry.db"


class ProductionCopyMigrationTests(TestCase):
    @staticmethod
    def _digest(path, table, columns, order_by):
        connection = sqlite3.connect(path)
        try:
            digest = sha256()
            query = f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order_by}"
            for row in connection.execute(query):
                digest.update(repr(row).encode("utf-8"))
                digest.update(b"\n")
            return digest.hexdigest()
        finally:
            connection.close()

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

            migrate_registry(copy)
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

    def test_failed_migration_copy_is_restored_from_verified_v2_backup(self):
        with TemporaryDirectory() as directory:
            backup = Path(directory) / "verified-v2.db"
            failed = Path(directory) / "failed.db"
            shutil.copy2(PRODUCTION_DATABASE, backup)
            shutil.copy2(backup, failed)
            expected_hash = sha256(backup.read_bytes()).hexdigest()
            expected_provenance = self._digest(
                backup,
                "historical_source_records",
                ("source_record_id", "run_id", "source_path", "source_kind",
                 "source_row_number", "source_content_hash", "resolution_status",
                 "company_id", "branch_id", "observed_at", "reason",
                 "raw_record_json", "created_at"),
                "source_record_id",
            )

            connection = sqlite3.connect(failed)
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "DELETE FROM companies WHERE company_id="
                "(SELECT company_id FROM companies ORDER BY company_id LIMIT 1)"
            )
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(RuntimeError, "foreign-key violations"):
                migrate_registry(failed)
            connection = sqlite3.connect(failed)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            connection.close()

            replacement = Path(directory) / "restored.tmp"
            shutil.copy2(backup, replacement)
            replacement.replace(failed)
            self.assertEqual(sha256(failed.read_bytes()).hexdigest(), expected_hash)
            connection = sqlite3.connect(failed)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            connection.close()
            self.assertEqual(
                self._digest(
                    failed,
                    "historical_source_records",
                    ("source_record_id", "run_id", "source_path", "source_kind",
                     "source_row_number", "source_content_hash", "resolution_status",
                     "company_id", "branch_id", "observed_at", "reason",
                     "raw_record_json", "created_at"),
                    "source_record_id",
                ),
                expected_provenance,
            )

    def test_legacy_csv_identity_reader_is_independent_of_registry_schema(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "registry.db"
            shutil.copy2(PRODUCTION_DATABASE, database)
            csv_path = root / "google_maps_data.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = DictWriter(handle, fieldnames=("title", "map_link", "phone_number"))
                writer.writeheader()
                writer.writerow({
                    "title": "Legacy Co",
                    "map_link": "https://google.com/maps/data=!4m1!1sChIJ-Legacy",
                    "phone_number": "+216 71 000 000",
                })
            before = KnownCompanies.from_directory(root, require_nonempty=True)
            migrate_registry(database)
            after = KnownCompanies.from_directory(root, require_nonempty=True)
            candidate = {"map_link": "https://google.com/maps/data=!4m1!1sChIJ-Legacy"}
            self.assertEqual(before.duplicate_kind(candidate), "known")
            self.assertEqual(after.duplicate_kind(candidate), "known")
