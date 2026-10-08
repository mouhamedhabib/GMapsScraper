"""Regression tests for the Phase 1.6 historical backfill."""

import csv
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.historical_import import SourceFile, import_history
from company_registry.storage import connect_registry


FIELDS = ("name", "website", "map_place_id", "phone", "location", "added_at")


class HistoricalImportTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "data" / "company_registry.db"

    def source(self, rows, name="history.csv"):
        path = self.root / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return [SourceFile(path, name, "LEAD_EXPORT")]

    def apply(self, rows, **kwargs):
        return import_history(
            self.root, self.database, dry_run=False,
            sources=self.source(rows), **kwargs,
        )

    def counts(self):
        connection = connect_registry(self.database)
        self.addCleanup(connection.close)
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "companies", "branches", "company_identities",
                "historical_source_records",
            )
        }

    def registry_snapshot(self):
        connection = connect_registry(self.database)
        self.addCleanup(connection.close)
        return {
            "company_ids": tuple(row[0] for row in connection.execute(
                "SELECT company_id FROM companies ORDER BY company_id"
            )),
            "branch_ids": tuple(row[0] for row in connection.execute(
                "SELECT branch_id FROM branches ORDER BY branch_id"
            )),
            "resolutions": tuple(tuple(row) for row in connection.execute(
                """SELECT source_path, source_row_number, source_content_hash,
                          resolution_status, reason, company_id, branch_id
                     FROM historical_source_records
                    ORDER BY source_path, source_row_number, source_content_hash"""
            )),
            "counts": {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "companies", "branches", "company_identities",
                    "historical_source_records",
                )
            },
            "new": connection.execute(
                "SELECT count(*) FROM run_companies WHERE discovery_status='NEW'"
            ).fetchone()[0],
        }

    def test_repeated_imports_do_not_duplicate_entities_or_provenance(self):
        rows = [{"name": "Acme", "website": "https://acme.test", "map_place_id": "ChIJ-one"}]
        first = self.apply(rows)
        second = self.apply(rows)
        self.assertEqual(first.proposed_companies, 1)
        self.assertEqual(second.proposed_companies, 0)
        self.assertEqual(self.counts(), {
            "companies": 1, "branches": 1, "company_identities": 2,
            "historical_source_records": 1,
        })
        self.assertIsNotNone(second.backup_path)
        self.assertTrue(Path(second.backup_path).exists())

    def test_duplicate_place_ids_are_exact_branch_matches(self):
        report = self.apply([
            {"name": "Acme", "website": "acme.test", "map_place_id": "ChIJ-one"},
            {"name": "Acme renamed", "website": "new.test", "map_place_id": "chij-one"},
        ])
        self.assertEqual(report.exact_place_id_matches, 1)
        self.assertEqual(self.counts()["branches"], 1)

    def test_distinct_place_ids_become_multiple_branches_of_one_company(self):
        self.apply([
            {"name": "Acme", "website": "acme.test", "map_place_id": "ChIJ-one"},
            {"name": "Acme", "website": "acme.test", "map_place_id": "ChIJ-two"},
        ])
        self.assertEqual(self.counts()["companies"], 1)
        self.assertEqual(self.counts()["branches"], 2)

    def test_shared_domain_alone_does_not_merge_unrelated_companies(self):
        self.apply([
            {"name": "Alpha", "website": "shared.test", "map_place_id": "ChIJ-alpha"},
            {"name": "Beta", "website": "shared.test", "map_place_id": "ChIJ-beta"},
        ])
        self.assertEqual(self.counts()["companies"], 2)

    def test_three_persistent_imports_preserve_resolutions_and_ids(self):
        rows = [
            {"name": "Acme", "website": "acme.test", "map_place_id": "ChIJ-one",
             "phone": "+1 111 1111", "location": "North"},
            {"name": "Acme", "website": "acme.test", "map_place_id": "ChIJ-two",
             "phone": "+1 222 2222", "location": "South"},
            {"name": "Acme", "phone": "+1 111 1111", "location": "South"},
            {"name": "", "website": "unknown.test"},
            {"name": "Beta", "website": "acme.test", "map_place_id": "ChIJ-beta"},
        ]
        reports = []
        snapshots = []
        for _ in range(3):
            reports.append(self.apply(rows))
            snapshots.append(self.registry_snapshot())

        self.assertEqual([report.ambiguous_records for report in reports], [1, 1, 1])
        self.assertEqual([report.quarantined_records for report in reports], [1, 1, 1])
        self.assertEqual(
            [(report.proposed_companies, report.proposed_branches) for report in reports],
            [(2, 3), (0, 0), (0, 0)],
        )
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[1], snapshots[2])
        self.assertEqual(snapshots[0]["new"], 0)

    def test_changed_place_id_is_a_new_branch_but_keeps_company_identity(self):
        original = [{
            "name": "Acme", "website": "acme.test", "map_place_id": "ChIJ-old",
            "added_at": "2026-09-01T12:00:00+01:00",
        }]
        changed = [{
            "name": "Acme", "website": "acme.test", "map_place_id": "ChIJ-new",
            "added_at": "2026-09-02T12:00:00+01:00",
        }]
        self.apply(original)
        original_snapshot = self.registry_snapshot()
        second = self.apply(changed)
        changed_snapshot = self.registry_snapshot()
        third = self.apply(changed)

        self.assertEqual(second.proposed_companies, 0)
        self.assertEqual(second.proposed_branches, 1)
        self.assertEqual(third.proposed_branches, 0)
        self.assertEqual(original_snapshot["company_ids"], changed_snapshot["company_ids"])
        self.assertTrue(set(original_snapshot["branch_ids"]).issubset(changed_snapshot["branch_ids"]))
        self.assertEqual(len(changed_snapshot["branch_ids"]), 2)
        self.assertEqual(len(changed_snapshot["resolutions"]), 2)

    def test_missing_names_are_quarantined_and_not_discarded(self):
        report = self.apply([
            {"name": "", "website": "unnamed.test", "map_place_id": "ChIJ-unnamed"},
        ])
        self.assertEqual(report.quarantined_records, 1)
        connection = connect_registry(self.database)
        self.addCleanup(connection.close)
        row = connection.execute("SELECT resolution_status, reason FROM historical_source_records").fetchone()
        self.assertEqual(tuple(row), ("QUARANTINED", "missing company name"))

    def test_missing_historical_timestamp_leaves_seen_dates_unknown(self):
        self.apply([{"name": "Acme", "website": "acme.test"}])
        connection = connect_registry(self.database)
        self.addCleanup(connection.close)
        company = connection.execute("SELECT first_seen_at, last_seen_at FROM companies").fetchone()
        branch = connection.execute("SELECT first_seen_at, last_seen_at FROM branches").fetchone()
        self.assertEqual(tuple(company), (None, None))
        self.assertEqual(tuple(branch), (None, None))

    def test_conflicting_supporting_identities_are_ambiguous(self):
        report = self.apply([
            {"name": "Acme", "website": "acme.test", "map_place_id": "ChIJ-one",
             "phone": "+1 111 1111", "location": "North"},
            {"name": "Acme", "website": "acme.test", "map_place_id": "ChIJ-two",
             "phone": "+1 222 2222", "location": "South"},
            {"name": "Acme", "phone": "+1 111 1111", "location": "South"},
        ])
        self.assertEqual(report.ambiguous_records, 1)
        connection = connect_registry(self.database)
        self.addCleanup(connection.close)
        status = connection.execute(
            "SELECT resolution_status FROM historical_source_records WHERE source_row_number=4"
        ).fetchone()[0]
        self.assertEqual(status, "AMBIGUOUS")

    def test_import_rolls_back_completely_on_failure(self):
        rows = [
            {"name": "Acme", "website": "acme.test"},
            {"name": "Beta", "website": "beta.test"},
        ]
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.apply(rows, fail_after_records=1)
        self.assertEqual(self.counts(), {
            "companies": 0, "branches": 0, "company_identities": 0,
            "historical_source_records": 0,
        })

    def test_legacy_timestamp_is_preserved_across_restarts_without_first_seen(self):
        rows = [{
            "name": "Acme", "website": "acme.test",
            "added_at": "2026-09-01T12:00:00+01:00",
        }]
        for _ in range(3):
            self.apply(rows)
        connection = connect_registry(self.database)
        self.addCleanup(connection.close)
        company = connection.execute(
            "SELECT discovery_status, first_seen_at, last_seen_at FROM companies"
        ).fetchone()
        branch = connection.execute(
            "SELECT discovery_status, first_seen_at, last_seen_at FROM branches"
        ).fetchone()
        expected = ("LEGACY_UNKNOWN", None, "2026-09-01T12:00:00+01:00")
        self.assertEqual(tuple(company), expected)
        self.assertEqual(tuple(branch), expected)

    def test_all_historical_statuses_remain_legacy_unknown_and_never_new(self):
        self.apply([{
            "name": "Acme", "website": "acme.test", "map_place_id": "ChIJ-one",
            "added_at": "2026-09-01T12:00:00+01:00",
        }])
        connection = connect_registry(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(
            connection.execute("SELECT discovery_status FROM companies").fetchone()[0],
            "LEGACY_UNKNOWN",
        )
        self.assertEqual(
            connection.execute("SELECT discovery_status FROM branches").fetchone()[0],
            "LEGACY_UNKNOWN",
        )
        self.assertEqual(
            connection.execute("SELECT discovery_status FROM run_companies").fetchone()[0],
            "LEGACY_UNKNOWN",
        )
        self.assertEqual(
            connection.execute("SELECT count(*) FROM run_companies WHERE discovery_status='NEW'").fetchone()[0],
            0,
        )

    def test_dry_run_never_creates_or_changes_production_database(self):
        source = self.source([{"name": "Acme", "website": "acme.test"}])
        report = import_history(self.root, self.database, dry_run=True, sources=source)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.proposed_companies, 1)
        self.assertFalse(self.database.exists())
