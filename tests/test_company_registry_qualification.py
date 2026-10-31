"""Offline Phase 3D.2 dual-purpose qualification coverage."""

from csv import DictReader
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from company_registry.discovery_adapter import AuthoritativeDiscoveryAdapter
from company_registry.exports import (
    REVIEW_EXPORT_FIELDS,
    export_new_companies,
    export_qualified_leads,
    export_review_queue,
    verify_export_manifest,
    verify_review_export,
    verify_run_exports,
)
from company_registry.models import IdentityObservation
from company_registry.qualification import (
    QualificationPolicy,
    assess_maps_company,
    assess_run,
    validate_contact_email,
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

    def test_exact_technical_categories_are_relevant_for_both_purposes(self):
        for category in (
            "Software company", "Website designer", "Computer consultant",
            "Computer security service", "Computer networking service",
        ):
            with self.subTest(category=category):
                result = assess_maps_company(maps_row(
                    category=category, about_desc="Not Available",
                ))
                self.assertEqual(result.employment_relevance, "TARGET")
                self.assertEqual(result.mission_relevance, "TARGET")
                self.assertIn(
                    "current_hiring_not_established", result.employment_reasons,
                )
                self.assertIn(
                    "active_mission_not_established", result.mission_reasons,
                )

    def test_technical_descriptions_cover_web_cloud_erp_and_application_work(self):
        descriptions = (
            "Backend development and API development",
            "Frontend development and website development",
            "Cloud infrastructure and managed IT services",
            "ERP software and business software",
            "Application development and systems integration",
            "Cybersecurity services and IT consulting",
        )
        for description in descriptions:
            with self.subTest(description=description):
                self.assert_outcomes(
                    maps_row(
                        category="Business to business service",
                        about_desc=description,
                    ),
                    ("TARGET", "ELIGIBLE"),
                    ("TARGET", "ELIGIBLE"),
                )

    def test_web_and_digital_agencies_need_explicit_technical_services(self):
        for category in ("Web agency", "Digital agency"):
            with self.subTest(category=category):
                technical = assess_maps_company(maps_row(
                    category=category,
                    about_desc="Website development and application development",
                ))
                self.assertEqual(
                    (technical.employment_relevance, technical.mission_relevance),
                    ("TARGET", "TARGET"),
                )

        marketing_only = assess_maps_company(maps_row(
            category="Digital agency",
            about_desc="Social media marketing, branding, and paid campaigns",
        ))
        self.assertEqual(
            (marketing_only.employment_relevance, marketing_only.mission_relevance),
            ("POSSIBLE", "POSSIBLE"),
        )

    def test_unrelated_training_and_government_categories_are_noise(self):
        for category in (
            "Training center", "Software training institute", "University",
            "Government office", "Government organization", "Council",
        ):
            with self.subTest(category=category):
                self.assert_outcomes(
                    maps_row(
                        category=category,
                        about_desc="Courses and research in software development",
                    ),
                    ("NOISE", "EXCLUDED"),
                    ("NOISE", "EXCLUDED"),
                )

    def test_ambiguous_category_without_technical_description_is_not_target(self):
        result = assess_maps_company(maps_row(
            category="Corporate office", about_desc="Business services",
        ))
        self.assertEqual(result.employment_relevance, "NOISE")
        self.assertEqual(result.mission_relevance, "POSSIBLE")

        missing = assess_maps_company(maps_row(category="", about_desc=""))
        self.assertEqual(missing.employment_relevance, "UNKNOWN")
        self.assertEqual(missing.mission_relevance, "UNKNOWN")

    def test_contact_and_completeness_gate_eligibility_not_relevance(self):
        missing_website = assess_maps_company(maps_row(
            category="Website designer", about_desc="Not Available", webpage="",
        ))
        missing_contact = assess_maps_company(maps_row(
            category="Website designer", about_desc="Not Available",
            phone_number="", site_email="",
        ))
        self.assertEqual(
            (missing_website.employment_relevance, missing_website.mission_relevance),
            ("TARGET", "TARGET"),
        )
        self.assertEqual(
            (missing_website.employment_eligibility,
             missing_website.mission_eligibility),
            ("REVIEW", "REVIEW"),
        )
        self.assertEqual(missing_contact.employment_eligibility, "ELIGIBLE")
        self.assertEqual(missing_contact.mission_eligibility, "REVIEW")

    def test_opportunity_evidence_is_recorded_without_changing_business_relevance(self):
        result = assess_maps_company(maps_row(
            category="Software company",
            about_desc=(
                "We are hiring. We are also seeking technology partner for a "
                "request for proposal."
            ),
        ))
        self.assertEqual(result.employment_relevance, "TARGET")
        self.assertEqual(result.mission_relevance, "TARGET")
        self.assertIn(
            "explicit_hiring_opportunity_evidence", result.employment_reasons,
        )
        self.assertIn(
            "explicit_mission_opportunity_evidence", result.mission_reasons,
        )
        self.assertNotIn(
            "current_hiring_not_established", result.employment_reasons,
        )
        self.assertNotIn(
            "active_mission_not_established", result.mission_reasons,
        )

    def test_site_email_general_and_recruitment_roles_are_accepted(self):
        for email in (
            "contact@company.test", "info@company.test",
            "careers@company.test", "jobs@company.test",
        ):
            with self.subTest(email=email):
                result = assess_maps_company(maps_row(
                    phone_number="", site_email=email,
                ))
                self.assertEqual(result.evidence["email"], email)
                self.assertEqual(
                    result.evidence["email_candidates"][0]["decision"], "ACCEPT",
                )
                self.assertEqual(result.mission_eligibility, "ELIGIBLE")

    def test_automated_telemetry_placeholder_and_malformed_emails_are_rejected(self):
        cases = {
            "noreply@company.test": "automated_sender",
            "no-reply@company.test": "automated_sender",
            "sentry@company.test": "telemetry_address",
            "3764a8f1d8c03247@sentry.company.test": "telemetry_address",
            "example@example.com": "placeholder_address",
            "hello@": "malformed_address",
        }
        for email, reason in cases.items():
            with self.subTest(email=email):
                result = assess_maps_company(maps_row(
                    phone_number="", site_email=email,
                ))
                candidate = result.evidence["email_candidates"][0]
                self.assertEqual(result.evidence["email"], "")
                self.assertEqual(
                    (candidate["decision"], candidate["reason_code"]),
                    ("REJECT", reason),
                )
                self.assertEqual(result.mission_eligibility, "REVIEW")

    def test_personal_looking_email_requires_review_and_does_not_count(self):
        result = assess_maps_company(maps_row(
            phone_number="", site_email="firstname@company.test",
        ))
        candidate = result.evidence["email_candidates"][0]
        self.assertEqual(result.evidence["email"], "")
        self.assertEqual(
            (candidate["decision"], candidate["reason_code"]),
            ("REVIEW", "uncertain_local_part"),
        )
        self.assertEqual(result.mission_eligibility, "REVIEW")

    def test_first_accepted_candidate_wins_without_invalid_masking(self):
        result = assess_maps_company(maps_row(
            phone_number="",
            email="noreply@company.test",
            site_email=["firstname@company.test", "INFO@Company.Test"],
        ))
        self.assertEqual(result.evidence["email"], "info@company.test")
        self.assertEqual(
            [item["decision"] for item in result.evidence["email_candidates"]],
            ["REJECT", "REVIEW", "ACCEPT"],
        )
        self.assertEqual(
            [item["source_field"] for item in result.evidence["email_candidates"]],
            ["email", "site_email", "site_email"],
        )
        self.assertEqual(result.mission_eligibility, "ELIGIBLE")

    def test_semicolon_delimited_existing_representation_is_supported(self):
        result = assess_maps_company(maps_row(
            phone_number="",
            emails="noreply@company.test; jobs@company.test",
        ))
        self.assertEqual(result.evidence["email"], "jobs@company.test")
        self.assertEqual(
            [item["decision"] for item in result.evidence["email_candidates"]],
            ["REJECT", "ACCEPT"],
        )

    def test_public_provider_role_address_is_not_rejected(self):
        validation = validate_contact_email("info@gmail.com", "site_email")
        self.assertEqual(validation.decision, "ACCEPT")
        self.assertEqual(validation.reason_code, "business_role_address")

    def test_existing_email_fields_keep_precedence_and_missing_email_is_compatible(self):
        for field in ("email", "emails", "email_address"):
            with self.subTest(field=field):
                result = assess_maps_company(maps_row(
                    phone_number="", **{field: "contact@company.test"},
                ))
                self.assertEqual(result.evidence["email"], "contact@company.test")
                self.assertEqual(
                    result.evidence["email_candidates"][0]["source_field"], field,
                )

        missing = assess_maps_company(maps_row(phone_number="", site_email=""))
        self.assertEqual(missing.evidence["email"], "")
        self.assertEqual(missing.evidence["email_candidates"], [])
        self.assertEqual(missing.mission_eligibility, "REVIEW")

    def test_email_validation_is_bounded_for_large_malformed_input(self):
        validation = validate_contact_email("a" * 1_000_000, "site_email")
        self.assertEqual(
            (validation.decision, validation.reason_code),
            ("REJECT", "address_too_long"),
        )


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

    def test_historical_policy_exports_remain_read_only_verifiable(self):
        historical = QualificationPolicy(version="maps-dual-purpose-v1")
        self.adapter.commit("run-a", self._observation())
        assess_run(self.database, "run-a", policy=historical, assessed_at=STAMP)
        output = self.root / "historical-exports"
        export_new_companies(
            self.database, "run-a", output, verified_maps_publication=True,
        )
        for purpose in ("employment", "mission"):
            export_qualified_leads(
                self.database, "run-a", output, purpose=purpose,
                policy_version=historical.version,
            )

        verified = verify_run_exports(self.database, "run-a", output)
        self.assertTrue(all(result["valid"] for result in verified.values()))

    def test_review_queues_are_deterministic_auditable_and_purpose_specific(self):
        decisions = {}
        decisions["high"] = self.adapter.commit("run-a", self._observation(
            "zulu-target",
            title="Zulu Target",
            category="Software company",
            about_desc="Software development",
            webpage="",
            phone_number="",
            site_email="INFO@Zulu.Test",
            map_link="https://www.google.com/maps/place/zulu-target",
        ))
        decisions["medium"] = self.adapter.commit("run-a", self._observation(
            "alpha-possible",
            title="Alpha Possible",
            category="Software retailer",
            about_desc="Computer software store",
            webpage="https://alpha.test",
        ))
        decisions["low"] = self.adapter.commit("run-a", self._observation(
            "beta-unknown",
            title="Beta Unknown",
            category="",
            about_desc="",
            webpage="",
            phone_number="",
            address="",
            country="",
            city="",
        ))
        decisions["mission_only"] = self.adapter.commit(
            "run-a", self._observation(
                "contact-gap",
                title="Contact Gap",
                category="Software company",
                about_desc="Software development",
                webpage="https://contact-gap.test",
                phone_number="",
                site_email="firstname@contact-gap.test",
            ),
        )
        self.adapter.commit("run-a", self._observation(
            "eligible",
            title="Eligible Company",
            category="Software company",
            webpage="https://eligible.test",
        ))
        self.adapter.commit("run-a", self._observation(
            "excluded",
            title="Excluded Training",
            category="Training center",
            webpage="https://training.test",
        ))
        assess_run(self.database, "run-a", assessed_at=STAMP)
        output = self.root / "review-exports"

        exports = {
            purpose: export_review_queue(
                self.database, "run-a", output, purpose=purpose,
            )
            for purpose in ("employment", "mission")
        }
        repeated = export_review_queue(
            self.database, "run-a", output, purpose="mission",
        )
        self.assertEqual(repeated["sha256"], exports["mission"]["sha256"])

        rows = {}
        for purpose, result in exports.items():
            with result["csv"].open(newline="", encoding="utf-8-sig") as handle:
                reader = DictReader(handle)
                self.assertEqual(tuple(reader.fieldnames), REVIEW_EXPORT_FIELDS)
                rows[purpose] = list(reader)
            self.assertEqual(
                len(rows[purpose]),
                len({row["company_id"] for row in rows[purpose]}),
            )
            self.assertTrue(all(
                row["eligibility"] == "REVIEW"
                and row["source_run_id"] == "run-a"
                and row["source_system"] == "GOOGLE_MAPS"
                for row in rows[purpose]
            ))
            self.assertTrue(verify_export_manifest(result["manifest"])["valid"])
            self.assertTrue(verify_review_export(
                self.database, "run-a", output, purpose,
            )["valid"])

        self.assertEqual(
            [row["company_name"] for row in rows["employment"]],
            ["Zulu Target", "Alpha Possible", "Beta Unknown"],
        )
        self.assertEqual(
            [row["review_priority"] for row in rows["employment"]],
            ["HIGH", "MEDIUM", "LOW"],
        )
        self.assertEqual(
            [row["company_name"] for row in rows["mission"]],
            ["Contact Gap", "Zulu Target", "Alpha Possible", "Beta Unknown"],
        )
        self.assertEqual(
            exports["employment"]["priority_counts"],
            {"HIGH": 1, "MEDIUM": 1, "LOW": 1},
        )
        self.assertEqual(
            exports["mission"]["priority_counts"],
            {"HIGH": 2, "MEDIUM": 1, "LOW": 1},
        )

        employment_ids = {row["company_id"] for row in rows["employment"]}
        mission_ids = {row["company_id"] for row in rows["mission"]}
        self.assertEqual(len(employment_ids & mission_ids), 3)
        self.assertEqual(
            mission_ids - employment_ids,
            {decisions["mission_only"].resolution.company_id},
        )
        high = rows["employment"][0]
        self.assertEqual(
            high["company_id"], decisions["high"].resolution.company_id,
        )
        self.assertEqual(high["accepted_contact_email"], "info@zulu.test")
        self.assertEqual(
            high["google_maps_url"],
            "https://www.google.com/maps/place/zulu-target",
        )
        self.assertEqual(
            json.loads(high["missing_evidence"]),
            ["usable_website_not_available"],
        )
        contact_gap = rows["mission"][0]
        self.assertEqual(contact_gap["accepted_contact_email"], "")
        self.assertIn(
            "direct_contact_not_available",
            json.loads(contact_gap["missing_evidence"]),
        )
        self.assertIn(
            "active_mission_not_established",
            json.loads(contact_gap["classification_reason_codes"]),
        )
        low = rows["employment"][-1]
        self.assertEqual(low["country"], "")
        self.assertEqual(low["address"], "")
        self.assertEqual(low["website"], "")
        self.assertIn(
            "target_relevance_not_established",
            json.loads(low["missing_evidence"]),
        )

        # Optional review files must not alter the existing three-artifact
        # authoritative verification contract.
        export_new_companies(
            self.database, "run-a", output, verified_maps_publication=True,
        )
        for purpose in ("employment", "mission"):
            export_qualified_leads(
                self.database, "run-a", output, purpose=purpose,
            )
        verified = verify_run_exports(self.database, "run-a", output)
        self.assertTrue(all(result["valid"] for result in verified.values()))

    def test_review_queue_rejects_unknown_purpose(self):
        with self.assertRaisesRegex(ValueError, "employment.*mission"):
            export_review_queue(
                self.database, "run-a", self.root / "invalid-review",
                purpose="sales",
            )
