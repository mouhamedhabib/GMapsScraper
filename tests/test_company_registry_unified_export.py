"""Phase 6C regression coverage for the unified lead export."""

from __future__ import annotations

from contextlib import redirect_stdout
from csv import DictReader
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.discovery_adapter import AuthoritativeDiscoveryAdapter
from company_registry.exports import export_new_companies
from company_registry.models import IdentityObservation
from company_registry.qualification import assess_run
from company_registry.storage import (
    initialize_registry,
    open_registry,
    open_registry_readonly,
)
from company_registry.unified_export import (
    UNIFIED_EXPORT_FIELDS,
    export_unified_leads,
    main,
    verify_unified_export,
)


RUN_TIMES = {
    "run-a": "2026-10-01T10:00:00+00:00",
    "run-b": "2026-10-02T10:00:00+00:00",
    "run-c": "2026-10-03T10:00:00+00:00",
}


class UnifiedExportTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "registry.db"
        self.output = self.root / "unified-output"
        initialize_registry(self.database)
        for run_id, started_at in RUN_TIMES.items():
            self._add_run(run_id, started_at)
        self.adapter = AuthoritativeDiscoveryAdapter(self.database)
        self._build_fixture()

    def _add_run(self, run_id: str, started_at: str) -> None:
        connection = open_registry(self.database)
        connection.execute(
            """INSERT INTO discovery_runs
               (run_id, run_type, status, started_at, created_at)
               VALUES (?, 'SCRAPE', 'RUNNING', ?, ?)""",
            (run_id, started_at, started_at),
        )
        connection.commit()
        connection.close()

    def _observation(
        self,
        key: str,
        *,
        email: object = "contact@company.test",
        phone: str = "+216 71 123 456",
    ) -> IdentityObservation:
        row = {
            "title": f"Company {key}",
            "category": "Software company",
            "about_desc": "Software development and systems integration",
            "webpage": f"https://{key}.test",
            "phone_number": phone,
            "site_email": email,
            "address": f"{key} Street, Tunis, Tunisia",
            "country": "Tunisia",
            "city": "Tunis",
            "map_link": f"https://maps.google.test/{key}",
            "source_query": f"software company {key}",
        }
        return IdentityObservation(
            source_system="GOOGLE_MAPS",
            source_record_key=key,
            observed_at=RUN_TIMES["run-a"],
            name=row["title"],
            place_id=f"place_id:{key}",
            website_url=row["webpage"],
            phone=phone,
            address=row["address"],
            raw_payload=row,
        )

    def _finish(self, run_id: str) -> None:
        connection = open_registry(self.database)
        connection.execute(
            """UPDATE discovery_runs
                  SET status='SUCCESS', finished_at=?
                WHERE run_id=?""",
            (RUN_TIMES[run_id], run_id),
        )
        connection.commit()
        connection.close()

    def _build_fixture(self) -> None:
        shared = self._observation("shared")
        self.adapter.commit("run-a", shared)
        self.adapter.commit(
            "run-a",
            self._observation(
                "review-email", email="person@review-email.test", phone="",
            ),
        )
        self.adapter.commit(
            "run-a", self._observation("placeholder", email="Not Available"),
        )
        assess_run(self.database, "run-a", assessed_at=RUN_TIMES["run-a"])
        self._finish("run-a")

        self.adapter.commit("run-b", shared)
        self.adapter.commit("run-b", self._observation("second"))
        assess_run(self.database, "run-b", assessed_at=RUN_TIMES["run-b"])
        self._finish("run-b")

        self.adapter.commit(
            "run-c", self._observation("missing-qualification", email=None),
        )
        self._finish("run-c")

    @staticmethod
    def _rows(result: dict) -> list[dict]:
        with result["csv"].open("r", newline="", encoding="utf-8-sig") as handle:
            reader = DictReader(handle)
            if tuple(reader.fieldnames or ()) != UNIFIED_EXPORT_FIELDS:
                raise AssertionError("unexpected unified export fields")
            return list(reader)

    def test_cross_run_dedup_eligibility_and_email_semantics(self):
        result = export_unified_leads(
            self.database, ["run-c", "run-a", "run-b"], self.output,
        )
        rows = self._rows(result)
        self.assertEqual(len(rows), 5)
        self.assertEqual(
            [row["company_id"] for row in rows],
            sorted(row["company_id"] for row in rows),
        )

        by_name = {row["company_name"]: row for row in rows}
        shared = by_name["Company shared"]
        self.assertEqual(
            json.loads(shared["source_run_ids"]), ["run-a", "run-b"],
        )
        self.assertEqual(shared["latest_source_run_id"], "run-b")
        self.assertEqual(shared["selected_observation_run_id"], "run-a")
        self.assertEqual(shared["qualification_run_id"], "run-a")
        self.assertEqual(shared["employment_eligible"], "TRUE")
        self.assertEqual(shared["mission_eligible"], "TRUE")

        review = by_name["Company review-email"]
        self.assertEqual(review["email"], "person@review-email.test")
        self.assertEqual(review["email_classification"], "REVIEW")
        self.assertEqual(review["email_status"], "REVIEW")
        self.assertEqual(review["employment_eligible"], "TRUE")
        self.assertEqual(review["mission_eligible"], "FALSE")
        self.assertEqual(review["mission_eligibility"], "REVIEW")

        placeholder = by_name["Company placeholder"]
        self.assertEqual(placeholder["email"], "")
        self.assertEqual(placeholder["email_classification"], "UNAVAILABLE")
        self.assertEqual(placeholder["email_status"], "PLACEHOLDER")

        missing = by_name["Company missing-qualification"]
        self.assertEqual(missing["qualification_assessment_id"], "")
        self.assertEqual(missing["qualification_policy_version"], "")
        self.assertEqual(missing["employment_eligible"], "")
        self.assertEqual(missing["mission_eligible"], "")
        self.assertEqual(missing["employment_eligibility"], "")
        self.assertEqual(missing["mission_eligibility"], "")

        self.assertEqual(result["employment_eligible_count"], 4)
        self.assertEqual(result["mission_eligible_count"], 3)
        self.assertEqual(result["employment_mission_overlap_count"], 3)
        self.assertEqual(result["qualified_company_count"], 4)
        self.assertEqual(result["missing_qualification_count"], 1)

    def test_single_run_and_reversed_multi_run_inputs_are_deterministic(self):
        single = export_unified_leads(
            self.database, ["run-a"], self.output,
        )
        self.assertEqual(single["rows"], 3)
        first = export_unified_leads(
            self.database, ["run-a", "run-b"], self.output,
        )
        csv_bytes = first["csv"].read_bytes()
        manifest_bytes = first["manifest"].read_bytes()
        repeated = export_unified_leads(
            self.database, ["run-b", "run-a", "run-b"], self.output,
        )
        self.assertTrue(repeated["reused"])
        self.assertEqual(first["sha256"], repeated["sha256"])
        self.assertEqual(csv_bytes, repeated["csv"].read_bytes())
        self.assertEqual(manifest_bytes, repeated["manifest"].read_bytes())

    def test_atomic_recovery_manifest_integrity_and_path_collision(self):
        atomic_root = self.root / "atomic"

        def fail(point: str) -> None:
            if point == "before_manifest_publish":
                raise OSError("injected publication failure")

        with self.assertRaisesRegex(OSError, "injected"):
            export_unified_leads(
                self.database, ["run-a", "run-b"], atomic_root,
                failure_injector=fail,
            )
        destination = next((atomic_root / "unified-leads-v1").iterdir())
        self.assertTrue((destination / "unified_leads.csv").is_file())
        self.assertFalse((destination / "unified_leads.manifest.json").exists())

        recovered = export_unified_leads(
            self.database, ["run-a", "run-b"], atomic_root,
        )
        self.assertTrue(verify_unified_export(recovered["manifest"])["valid"])
        original = recovered["csv"].read_bytes()
        recovered["csv"].write_bytes(original + b"corrupt")
        with self.assertRaisesRegex(ValueError, "hash"):
            verify_unified_export(recovered["manifest"])
        with self.assertRaisesRegex(ValueError, "hash"):
            export_unified_leads(
                self.database, ["run-a", "run-b"], atomic_root,
            )

        with self.assertRaisesRegex(ValueError, "directory"):
            export_unified_leads(
                self.database, ["run-a"], self.database,
            )

    def test_existing_exports_and_database_remain_unchanged(self):
        existing = self.root / "existing-exports"
        legacy = export_new_companies(
            self.database, "run-a", existing,
            verified_maps_publication=True,
        )
        protected = {
            legacy["csv"]: legacy["csv"].read_bytes(),
            legacy["manifest"]: legacy["manifest"].read_bytes(),
        }
        database_bytes = self.database.read_bytes()
        database_mtime = self.database.stat().st_mtime_ns
        os.chmod(self.database, 0o444)
        self.addCleanup(os.chmod, self.database, 0o644)

        result = export_unified_leads(
            self.database, ["run-a", "run-b"], existing,
        )
        self.assertTrue(result["csv"].is_file())
        self.assertTrue(all(
            path.read_bytes() == content for path, content in protected.items()
        ))
        self.assertEqual(self.database.read_bytes(), database_bytes)
        self.assertEqual(self.database.stat().st_mtime_ns, database_mtime)

        connection = open_registry_readonly(self.database)
        try:
            self.assertEqual(
                connection.execute("PRAGMA query_only").fetchone()[0], 1,
            )
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute(
                    "UPDATE discovery_runs SET status='FAILED' WHERE run_id='run-a'"
                )
        finally:
            connection.close()

    def test_cli_and_non_success_run_rejection(self):
        connection = open_registry(self.database)
        connection.execute(
            """INSERT INTO discovery_runs
               (run_id, run_type, status, started_at, created_at)
               VALUES ('running', 'SCRAPE', 'RUNNING', ?, ?)""",
            (RUN_TIMES["run-c"], RUN_TIMES["run-c"]),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(ValueError, "SUCCESS"):
            export_unified_leads(
                self.database, ["running"], self.output,
            )

        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = main([
                "--database", str(self.database),
                "--run-id", "run-a",
                "--run-id", "run-b",
                "--output-dir", str(self.output),
            ])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["rows"], 4)
        self.assertEqual(payload["source_run_ids"], ["run-a", "run-b"])
