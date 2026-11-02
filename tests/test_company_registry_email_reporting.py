"""Phase 6B regression coverage for non-destructive email reporting."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.discovery_adapter import AuthoritativeDiscoveryAdapter
from company_registry.email_reporting import build_email_coverage_report
from company_registry.exports import (
    QUALIFIED_EXPORT_FIELDS,
    export_new_companies,
    export_qualified_leads,
)
from company_registry.models import IdentityObservation
from company_registry.qualification import (
    assess_run,
    classify_email_availability,
)
from company_registry.storage import initialize_registry, open_registry


STAMP = "2026-11-01T12:00:00+00:00"


class EmailAvailabilityTests(TestCase):
    def test_empty_and_null_are_missing(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                result = classify_email_availability(value)
                self.assertEqual(result.status, "MISSING")
                self.assertFalse(result.available)
                self.assertIsNone(result.decision)

    def test_unavailable_placeholders_are_not_available(self):
        for value in ("Not Available", " N/A ", "none", "-", "UNKNOWN"):
            with self.subTest(value=value):
                result = classify_email_availability(value)
                self.assertEqual(result.status, "PLACEHOLDER")
                self.assertFalse(result.available)
                self.assertIsNone(result.decision)

    def test_plausible_accept_and_review_keep_policy_distinction(self):
        accepted = classify_email_availability("Contact@Company.Test")
        review = classify_email_availability("firstname@company.test")
        self.assertEqual(
            (accepted.status, accepted.available, accepted.decision),
            ("AVAILABLE", True, "ACCEPT"),
        )
        self.assertEqual(
            (review.status, review.available, review.decision),
            ("REVIEW", True, "REVIEW"),
        )

    def test_malformed_email_is_rejected_and_unavailable(self):
        result = classify_email_availability("hello@")
        self.assertEqual(
            (result.status, result.available, result.decision, result.reason_code),
            ("REJECTED", False, "REJECT", "malformed_address"),
        )


class EmailCoverageReportTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "registry.db"
        self.exports = self.root / "immutable-exports"
        self.reports = self.root / "reports" / "email-coverage"
        initialize_registry(self.database)
        connection = open_registry(self.database)
        connection.execute(
            """INSERT INTO discovery_runs
               (run_id, run_type, status, started_at, created_at)
               VALUES ('phase-6b', 'SCRAPE', 'RUNNING', ?, ?)""",
            (STAMP, STAMP),
        )
        connection.commit()
        connection.close()
        self.adapter = AuthoritativeDiscoveryAdapter(self.database)

    def _commit(self, key: str, email: object) -> None:
        row = {
            "title": f"Company {key}",
            "category": "Software company",
            "about_desc": "Software development",
            "webpage": f"https://{key}.test",
            "phone_number": "+216 71 123 456",
            "site_email": email,
            "address": f"{key} Street, Tunis, Tunisia",
            "country": "Tunisia",
            "city": "Tunis",
        }
        self.adapter.commit(
            "phase-6b",
            IdentityObservation(
                source_system="GOOGLE_MAPS",
                source_record_key=key,
                observed_at=STAMP,
                name=row["title"],
                place_id=f"place_id:{key}",
                website_url=row["webpage"],
                phone=row["phone_number"],
                address=row["address"],
                raw_payload=row,
            ),
        )

    def test_report_is_idempotent_and_preserves_existing_exports(self):
        for key, email in (
            ("missing", None),
            ("placeholder", "Not Available"),
            ("accepted", "contact@accepted.test"),
            ("review", "person@review.test"),
            ("rejected", "hello@"),
        ):
            self._commit(key, email)
        assess_run(self.database, "phase-6b", assessed_at=STAMP)

        discovery = export_new_companies(
            self.database, "phase-6b", self.exports,
            verified_maps_publication=True,
        )
        employment = export_qualified_leads(
            self.database, "phase-6b", self.exports, purpose="employment",
        )
        protected = {
            path: path.read_bytes()
            for path in (
                discovery["csv"], discovery["manifest"],
                employment["csv"], employment["manifest"],
            )
        }

        with self.assertRaisesRegex(ValueError, "separate"):
            build_email_coverage_report(
                self.database, "phase-6b", self.exports,
            )

        first = build_email_coverage_report(
            self.database, "phase-6b", self.reports,
        )
        first_bytes = first["report"].read_bytes()
        first_mtime = first["report"].stat().st_mtime_ns
        repeated = build_email_coverage_report(
            self.database, "phase-6b", self.reports,
        )

        self.assertFalse(first["reused"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(first["sha256"], repeated["sha256"])
        self.assertEqual(first_bytes, repeated["report"].read_bytes())
        self.assertEqual(first_mtime, repeated["report"].stat().st_mtime_ns)
        self.assertEqual(
            repeated["populations"]["discovery_new"],
            {
                "records": 5,
                "available": 2,
                "unavailable": 3,
                "assessed_records": 5,
                "unassessed_records": 0,
                "source_status_counts": {
                    "MISSING": 1,
                    "PLACEHOLDER": 1,
                    "AVAILABLE": 1,
                    "REVIEW": 1,
                    "REJECTED": 1,
                },
                "decision_counts": {"ACCEPT": 1, "REVIEW": 1, "REJECT": 1},
                "records_with_decision": {
                    "ACCEPT": 1, "REVIEW": 1, "REJECT": 1,
                },
            },
        )
        self.assertEqual(
            repeated["populations"]["employment_eligible"]["records"], 5,
        )
        self.assertEqual(
            repeated["populations"]["mission_eligible"]["records"], 5,
        )
        self.assertTrue(all(
            path.read_bytes() == content
            for path, content in protected.items()
        ))
        self.assertEqual(tuple(QUALIFIED_EXPORT_FIELDS), (
            "qualification_assessment_id", "qualification_policy_version",
            "purpose", "company_id", "registry_decision_id",
            "registry_run_id", "branch_id", "source_system",
            "source_record_key", "relevance", "eligibility", "reasons_json",
            "evidence_hash", "assessed_at", "name", "category",
            "description", "website", "phone", "email", "address",
            "country", "city",
        ))
        self.assertEqual(first["report"].parent, self.reports)
        self.assertNotEqual(first["report"].parent, self.exports)
