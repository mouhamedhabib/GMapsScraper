"""Offline tests for deterministic Phase 2 job filtering."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from job_search.filtering import DEFAULT_POLICY_VERSION, evaluate_job, filter_stored_jobs
from job_search.maintenance import repair_incomplete_generic_jobs
from job_search.providers import ParsedJob
from job_search.storage import connect_database, upsert_job


NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)


def job(title="Backend Developer", description="Build REST APIs with Python.", location="Tunis, Tunisia", **values):
    result = {
        "title": title, "description": description, "location_text": location,
        "country": "", "city": "", "remote_policy": "", "published_at": "",
        "last_seen_at": "",
    }
    result.update(values)
    return result


class RuleTests(TestCase):
    def decision(self, *args, **kwargs):
        return evaluate_job(job(*args, **kwargs), NOW)

    def assertReason(self, decision, code):
        self.assertIn(code, [reason.code for reason in decision.reasons])

    def test_role_and_seniority(self):
        junior = self.decision("Junior Backend Developer")
        self.assertEqual(junior.status, "PASS")
        self.assertReason(junior, "PASS_JUNIOR_ROLE")
        self.assertEqual(self.decision("Senior Backend Developer").status, "REJECT")
        self.assertEqual(self.decision("Lead Software Engineer").status, "REJECT")
        self.assertNotEqual(self.decision("Software Engineer").status, "REJECT")
        for title in (
            "Développeur full stack Junior",
            "Développeur Full Stack Java Junior",
            "Développeur Full Stack Junior",
            "developpeur backend junior",
        ):
            with self.subTest(title=title):
                result = self.decision(title)
                self.assertNotEqual(result.status, "REJECT")
                self.assertReason(result, "PASS_RELEVANT_ROLE")

    def test_observed_fullstack_and_cloud_developer_titles_are_relevant(self):
        for title in (
            "Full stack Cloud developers (relocation)",
            "Fullstack .NET & Angular Developer (Regular)",
            "Full-stack Java Developer",
            "Cloud Developer",
            "Cloud Developers",
        ):
            with self.subTest(title=title):
                result = self.decision(title)
                self.assertNotEqual(result.status, "REJECT")
                self.assertReason(result, "PASS_RELEVANT_ROLE")
        cloud_infrastructure = self.decision("Cloud Engineer")
        self.assertReason(cloud_infrastructure, "REVIEW_ROLE_UNCLEAR")

    def test_experience_thresholds_and_optional_experience(self):
        for years in (1, 2):
            result = self.decision(description=f"{years} years of experience required.")
            self.assertEqual(result.status, "PASS")
            self.assertReason(result, "PASS_EXPERIENCE_0_2")
        three = self.decision(description="At least three years of experience.")
        self.assertEqual(three.status, "REVIEW")
        self.assertReason(three, "REVIEW_EXPERIENCE_3_YEARS")
        five = self.decision(description="Minimum 5+ years of experience required.")
        self.assertEqual(five.status, "REJECT")
        self.assertReason(five, "REJECT_EXPERIENCE_5_PLUS")
        preferred = self.decision(description="5 years preferred. Python is used.")
        self.assertNotEqual(preferred.status, "REJECT")
        self.assertReason(preferred, "REVIEW_EXPERIENCE_PREFERRED")
        ranged = self.decision(description="The role requires 1–3 years.")
        self.assertEqual((ranged.experience_min_years, ranged.experience_max_years), (1, 3))

    def test_irrelevant_role_only_uses_title(self):
        self.assertReason(self.decision("DevOps Engineer"), "REJECT_ROLE_DEVOPS")
        self.assertReason(self.decision("QA Engineer"), "REJECT_ROLE_QA")
        backend = self.decision(description="Collaborate with QA and DevOps on testing.")
        self.assertNotEqual(backend.status, "REJECT")

    def test_clear_non_target_title_families_are_rejected(self):
        cases = {
            "Product Manager": "REJECT_ROLE_PRODUCT",
            "Product Owner": "REJECT_ROLE_PRODUCT",
            "Project Manager": "REJECT_ROLE_PRODUCT",
            "Scrum Master": "REJECT_ROLE_PRODUCT",
            "Sales": "REJECT_ROLE_SALES",
            "Account Executive": "REJECT_ROLE_SALES",
            "Business Development Representative": "REJECT_ROLE_SALES",
            "Customer Success Manager": "REJECT_ROLE_SALES",
            "UX Designer": "REJECT_ROLE_DESIGN",
            "UI Designer": "REJECT_ROLE_DESIGN",
            "Graphic Designer": "REJECT_ROLE_DESIGN",
            "Data Analyst": "REJECT_ROLE_DATA",
            "Data Scientist": "REJECT_ROLE_DATA",
            "AI Researcher": "REJECT_ROLE_NON_SOFTWARE",
            "ML Researcher": "REJECT_ROLE_NON_SOFTWARE",
            "Value Engineer": "REJECT_ROLE_CONSULTING",
            "Solutions Consultant": "REJECT_ROLE_CONSULTING",
            "Pre-Sales Engineer": "REJECT_ROLE_CONSULTING",
            "Security Analyst": "REJECT_ROLE_NON_SOFTWARE",
            "SOC Analyst": "REJECT_ROLE_NON_SOFTWARE",
            "Associate Applied (AI) Value Engineer": "REJECT_ROLE_CONSULTING",
        }
        for title, reason in cases.items():
            with self.subTest(title=title):
                result = self.decision(title)
                self.assertEqual(result.status, "REJECT")
                self.assertEqual(result.primary_reason, reason)

        software = self.decision(
            "Junior Backend Developer",
            description="Work with product managers, data scientists, and sales.",
        )
        self.assertNotEqual(software.status, "REJECT")

    def test_location_and_authorization(self):
        us = self.decision(location="Remote - US only")
        self.assertEqual(us.status, "REJECT")
        self.assertReason(us, "REJECT_LOCATION_US_ONLY")
        worldwide = self.decision(location="Worldwide remote")
        self.assertEqual(worldwide.status, "PASS")
        self.assertReason(worldwide, "PASS_REMOTE_WORLDWIDE")
        europe = self.decision(location="Remote in Europe")
        self.assertEqual(europe.status, "REVIEW")
        self.assertReason(europe, "REVIEW_LOCATION_EUROPE")
        no_sponsor = self.decision(description="Applicants must be authorized to work in the US. No sponsorship.")
        self.assertReason(no_sponsor, "REJECT_WORK_AUTHORIZATION")
        sponsor = self.decision(description="Applicants must be authorized to work in France. Visa sponsorship is available.")
        self.assertNotEqual(sponsor.status, "REJECT")
        local = self.decision(description="No sponsorship is offered for this role in Tunisia.")
        self.assertNotEqual(local.status, "REJECT")
        structured = self.decision(location="Paris", country="FR")
        self.assertEqual(structured.status, "REVIEW")

    def test_quality_age_and_extraction(self):
        missing = self.decision(description="")
        self.assertEqual(missing.status, "REVIEW")
        self.assertReason(missing, "REVIEW_DESCRIPTION_MISSING")
        old = self.decision(
            published_at=(NOW - timedelta(days=40)).isoformat(),
            last_seen_at=NOW.isoformat(),
        )
        self.assertReason(old, "REVIEW_OLD_JOB")
        no_date = self.decision(published_at="")
        self.assertNotIn("REVIEW_OLD_JOB", [reason.code for reason in no_date.reasons])
        tech = self.decision(description="Python, FastAPI, TypeScript, React, PostgreSQL, Docker, REST and Git")
        self.assertTrue({"Python", "FastAPI", "TypeScript", "React", "PostgreSQL", "Docker", "REST", "Git"}.issubset(tech.matched_terms["technologies"]))
        mixed = self.decision("C++ Backend Developer")
        self.assertEqual(mixed.status, "REVIEW")
        self.assertReason(mixed, "REVIEW_STACK_MIXED")

    def test_policy_v1_1_published_date_age_bands(self):
        self.assertEqual(DEFAULT_POLICY_VERSION, "v1.1")
        for days in (0, 14):
            with self.subTest(days=days):
                result = self.decision(published_at=(NOW - timedelta(days=days)).isoformat())
                self.assertReason(result, "PASS_FRESH_JOB")
        for days in (15, 30):
            with self.subTest(days=days):
                result = self.decision(published_at=(NOW - timedelta(days=days)).isoformat())
                self.assertNotIn(
                    result.primary_reason, {"REVIEW_OLD_JOB", "REJECT_STALE_JOB"}
                )
        for days in (31, 60):
            with self.subTest(days=days):
                result = self.decision(published_at=(NOW - timedelta(days=days)).isoformat())
                self.assertEqual(result.status, "REVIEW")
                self.assertReason(result, "REVIEW_OLD_JOB")
        stale = self.decision(
            published_at=(NOW - timedelta(days=61)).isoformat(),
            last_seen_at=NOW.isoformat(),
        )
        self.assertEqual(stale.status, "REJECT")
        self.assertReason(stale, "REJECT_STALE_JOB")
        future = self.decision(published_at=(NOW + timedelta(days=1)).isoformat())
        self.assertEqual(future.status, "REVIEW")
        self.assertReason(future, "REVIEW_INVALID_PUBLISHED_DATE")
        missing = self.decision(
            published_at="", last_seen_at=(NOW - timedelta(days=500)).isoformat()
        )
        self.assertNotIn(
            "REJECT_STALE_JOB", [reason.code for reason in missing.reasons]
        )

    def test_reject_precedence_is_policy_order(self):
        role_before_seniority = self.decision("Senior Product Manager")
        self.assertEqual(role_before_seniority.primary_reason, "REJECT_ROLE_PRODUCT")
        senior_before_experience = self.decision(
            "Senior Backend Developer", description="Minimum 8 years of experience."
        )
        self.assertEqual(senior_before_experience.primary_reason, "REJECT_SENIORITY")
        stale_before_location = self.decision(
            location="Remote - US only",
            published_at=(NOW - timedelta(days=61)).isoformat(),
        )
        self.assertEqual(stale_before_location.primary_reason, "REJECT_STALE_JOB")
        authorization_first = self.decision(
            "Senior Product Manager",
            description="Must be authorized to work in the US. No sponsorship.",
        )
        self.assertEqual(authorization_first.primary_reason, "REJECT_WORK_AUTHORIZATION")

    def test_existing_generic_listing_is_excluded_without_deletion(self):
        result = evaluate_job(job(
            "NestJS jobs in Paris, France | 39 open jobs",
            "Browse 39 fresh NestJS jobs in Paris.",
            location="",
            canonical_url="https://www.wearedevelopers.com/jobs/ls/france/paris/nestjs",
        ), NOW)
        self.assertEqual(result.status, "REJECT")
        self.assertEqual(result.primary_reason, "GENERIC_JOB_LISTING_PAGE")


class PersistenceTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.connection = connect_database(Path(self.temporary.name) / "jobs.db")
        self.addCleanup(self.connection.close)
        self.job_id, _ = upsert_job(self.connection, ParsedJob(
            canonical_url="https://jobs.lever.co/acme/one", provider="lever",
            source_job_id="one", title="Junior Backend Developer", company_name="Acme",
            location_text="Tunis, Tunisia", description="1 year of experience. Python and FastAPI.",
            fetch_status="FETCHED",
        ), "query", NOW.isoformat())

    def test_migration_and_idempotent_policy_results(self):
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(job_filter_results)")}
        self.assertTrue({"experience_min_years", "experience_max_years", "detected_remote_policy"}.issubset(columns))
        first = filter_stored_jobs(self.connection, "v1")
        second = filter_stored_jobs(self.connection, "v1")
        self.assertEqual(first["evaluated"], 1)
        self.assertEqual(second["evaluated"], 0)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM job_filter_results").fetchone()[0], 1)
        row = self.connection.execute("SELECT * FROM job_filter_results").fetchone()
        self.assertEqual(row["status"], "PASS")
        self.assertIn("Python", json.loads(row["matched_terms_json"])["technologies"])
        self.assertEqual(json.loads(row["matched_terms_json"])["source_quality"], ["ATS"])

    def test_policy_versions_and_rebuild(self):
        filter_stored_jobs(self.connection, "v1")
        filter_stored_jobs(self.connection, "v2")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM job_filter_results").fetchone()[0], 2)
        rebuilt = filter_stored_jobs(self.connection, "v1", rebuild=True)
        self.assertEqual(rebuilt["evaluated"], 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM job_filter_results").fetchone()[0], 2)

    def test_generic_repair_fills_missing_fields_without_changing_identity(self):
        original_seen = "2026-09-01T00:00:00+00:00"
        job_id, _ = upsert_job(self.connection, ParsedJob(
            canonical_url="https://careers.example.test/jobs/123/java-backend-developer",
            provider="generic", fetch_status="FAILED", fetch_error="HTTP 403",
        ), "java", original_seen)
        fetched = ParsedJob(
            canonical_url="https://careers.example.test/jobs/123/java-backend-developer",
            provider="generic", title="Java Backend Developer", company_name="Example",
            location_text="Paris, FR", country="FR", description="Build Java APIs.",
            published_at="2026-09-01", employment_type="FULL_TIME",
            fetch_status="FETCHED", status="OPEN",
        )
        summary = repair_incomplete_generic_jobs(
            self.connection, fetcher=lambda url, timeout: fetched
        )
        self.assertEqual(summary["repaired"], 1)
        row = self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(row["title"], "Java Backend Developer")
        self.assertEqual(row["description"], "Build Java APIs.")
        self.assertEqual(row["last_seen_at"], original_seen)
        self.assertEqual(row["canonical_url"], fetched.canonical_url)
