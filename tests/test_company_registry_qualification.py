"""Offline Phase 3D.2 dual-purpose qualification coverage."""

from csv import DictReader
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.discovery_adapter import AuthoritativeDiscoveryAdapter
from company_registry.exports import export_qualified_leads, verify_export_manifest
from company_registry.models import IdentityObservation
from company_registry.qualification import (
    QualificationPolicy,
    assess_maps_company,
    assess_run,
)
from company_registry.storage import initialize_registry, open_registry


STAMP = "2026-10-09T12:00:00+00:00"


def maps_row(**changes):
    row = {
        "title": "Acme Digital",
        "category": "Software development company",
        "about_desc": "Custom software development and systems integration",
        "webpage": "https://acme.tn",
        "phone_number": "+216 71 123 456",
        "address": "1 Avenue de Tunis, Tunis, Tunisia",
        "country": "Tunisia",
        "city": "Tunis",
    }
    row.update(changes)
    return row


class QualificationPolicyTests(TestCase):
    def assert_outcomes(self, row, employment, mission, **context):
        result = assess_maps_company(row, **context)
        self.assertEqual(
            (result.employment_relevance, result.employment_eligibility), employment,
        )
        self.assertEqual((result.mission_relevance, result.mission_eligibility), mission)
        self.assertTrue(result.employment_reasons)
        self.assertTrue(result.mission_reasons)

    def test_software_agency_can_independently_qualify_for_both_purposes(self):
        self.assert_outcomes(
            maps_row(), ("TARGET", "ELIGIBLE"), ("TARGET", "ELIGIBLE"),
        )

    def test_it_consulting_is_strong_employment_evidence(self):
        self.assert_outcomes(
            maps_row(category="IT consulting company", about_desc="IT consulting"),
            ("TARGET", "ELIGIBLE"), ("TARGET", "ELIGIBLE"),
        )

    def test_software_retailer_is_not_automatically_employment_eligible(self):
        self.assert_outcomes(
            maps_row(category="Software retailer", about_desc="Computer software store"),
            ("POSSIBLE", "REVIEW"), ("POSSIBLE", "REVIEW"),
        )

    def test_travel_agency_and_hotel_are_review_only_mission_candidates(self):
        for category in ("Travel agency", "Hotel"):
            with self.subTest(category=category):
                self.assert_outcomes(
                    maps_row(category=category, about_desc="Local services"),
                    ("NOISE", "EXCLUDED"), ("POSSIBLE", "REVIEW"),
                )

    def test_ecommerce_merchant_is_not_eligible_from_industry_alone(self):
        self.assert_outcomes(
            maps_row(category="E-commerce service", about_desc="Online store"),
            ("NOISE", "EXCLUDED"), ("POSSIBLE", "REVIEW"),
        )

    def test_marketing_agency_requires_explicit_development_evidence(self):
        possible = assess_maps_company(maps_row(
            category="Marketing agency", about_desc="Brand campaigns and media",
        ))
        target = assess_maps_company(maps_row(
            category="Marketing agency",
            about_desc="Brand campaigns plus web development and systems integration",
        ))
        self.assertEqual((possible.mission_relevance, possible.mission_eligibility),
                         ("POSSIBLE", "REVIEW"))
        self.assertEqual((target.mission_relevance, target.mission_eligibility),
                         ("TARGET", "ELIGIBLE"))

    def test_missing_website_and_conflicts_prevent_automatic_eligibility(self):
        missing = assess_maps_company(maps_row(webpage=""))
        conflict = assess_maps_company(maps_row(), identity_conflict=True)
        self.assertEqual(missing.employment_eligibility, "REVIEW")
        self.assertEqual(missing.mission_eligibility, "REVIEW")
        self.assertEqual(conflict.employment_eligibility, "REVIEW")
        self.assertEqual(conflict.mission_eligibility, "REVIEW")

    def test_insufficient_evidence_is_unknown_and_review(self):
        self.assert_outcomes(
            {}, ("UNKNOWN", "REVIEW"), ("UNKNOWN", "REVIEW"),
        )

    def test_employment_and_mission_are_independent(self):
        result = assess_maps_company(maps_row(
            category="Hotel", about_desc="Hotel and resort with online reservations",
        ))
        self.assertEqual(result.employment_eligibility, "EXCLUDED")
        self.assertEqual(result.mission_eligibility, "REVIEW")


class QualificationPersistenceAndExportTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "registry.db"
        initialize_registry(self.database)
        self.adapter = AuthoritativeDiscoveryAdapter(self.database)
        self._add_run("run-a")
        self._add_run("run-b")

    def _add_run(self, run_id):
        connection = open_registry(self.database)
        connection.execute(
            """INSERT INTO discovery_runs
               (run_id, run_type, status, started_at, created_at)
               VALUES (?, 'SCRAPE', 'RUNNING', ?, ?)""",
            (run_id, STAMP, STAMP),
        )
        connection.commit()
        connection.close()

    def _observation(self, key="acme", **row_changes):
        row = maps_row(**row_changes)
        return IdentityObservation(
            source_system="GOOGLE_MAPS",
            source_record_key=key,
            observed_at=STAMP,
            name=row.get("title", ""),
            place_id=f"place_id:{key}",
            website_url=row.get("webpage", ""),
            phone=row.get("phone_number", ""),
            address=row.get("address", ""),
            raw_payload=row,
        )

    def _count(self, table):
        connection = open_registry(self.database)
        count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        connection.close()
        return count

    def test_assessment_is_idempotent_and_policy_change_adds_reassessment(self):
        self.adapter.commit("run-a", self._observation())
        first = assess_run(self.database, "run-a", assessed_at=STAMP)
        repeated = assess_run(self.database, "run-a", assessed_at="later")
        changed = assess_run(
            self.database, "run-a",
            policy=QualificationPolicy(version="maps-dual-purpose-v2"),
            assessed_at="later",
        )
        self.assertEqual((first["inserted"], repeated["reused"], changed["inserted"]),
                         (1, 1, 1))
        self.assertEqual(self._count("qualification_assessments"), 2)

    def test_previously_known_company_is_assessed_without_identity_mutation(self):
        first = self.adapter.commit("run-a", self._observation())
        known = self.adapter.commit("run-b", self._observation())
        before = self._count("companies")
        assess_run(self.database, "run-b", assessed_at=STAMP)
        self.assertEqual(first.resolution.company_id, known.resolution.company_id)
        self.assertEqual(self._count("companies"), before)
        self.assertEqual(self._count("qualification_assessments"), 1)
        exported = export_qualified_leads(
            self.database, "run-b", self.root / "known-exports",
            purpose="employment",
        )
        self.assertEqual(exported["rows"], 0)

    def test_multiple_branches_export_one_company_per_purpose(self):
        first = self.adapter.commit("run-a", self._observation("tunis"))
        second = self.adapter.commit("run-a", self._observation(
            "sfax", address="2 Route de Sfax, Sfax, Tunisia",
            phone_number="+216 74 999 999",
        ))
        self.assertEqual(first.resolution.company_id, second.resolution.company_id)
        assess_run(self.database, "run-a", assessed_at=STAMP)
        result = export_qualified_leads(
            self.database, "run-a", self.root / "exports", purpose="employment",
        )
        self.assertEqual(result["rows"], 1)
        verified = verify_export_manifest(result["manifest"])
        self.assertTrue(verified["valid"])
        with result["csv"].open(newline="", encoding="utf-8-sig") as handle:
            rows = list(DictReader(handle))
        self.assertEqual(rows[0]["eligibility"], "ELIGIBLE")
        self.assertEqual(rows[0]["company_id"], first.resolution.company_id)

    def test_eligible_exports_are_purpose_specific_and_recoverable(self):
        self.adapter.commit("run-a", self._observation())
        self.adapter.commit("run-a", self._observation(
            "hotel", title="Carthage Hotel", category="Hotel",
            about_desc="Hotel and resort",
            webpage="https://carthage-hotel.tn",
        ))
        assess_run(self.database, "run-a", assessed_at=STAMP)
        output = self.root / "exports"

        def fail(checkpoint):
            if checkpoint == "before_manifest_publish":
                raise OSError("injected publication failure")

        with self.assertRaisesRegex(OSError, "injected"):
            export_qualified_leads(
                self.database, "run-a", output, purpose="mission",
                failure_injector=fail,
            )
        first = export_qualified_leads(
            self.database, "run-a", output, purpose="mission",
        )
        repeated = export_qualified_leads(
            self.database, "run-a", output, purpose="mission",
        )
        self.assertEqual(first["sha256"], repeated["sha256"])
        self.assertEqual(first["rows"], 1)
        self.assertTrue(verify_export_manifest(first["manifest"])["valid"])
