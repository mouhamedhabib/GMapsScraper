"""Offline tests for deterministic Phase 2 job filtering."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from job_search.filtering import (
    DEFAULT_POLICY_VERSION,
    evaluate_job,
    extract_experience,
    filter_stored_jobs,
)
from job_search.geography import normalize_geography
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

    def test_programmer_ai_and_reactjs_developer_titles_are_relevant(self):
        for title in (
            "[Hiring] Junior Java Programmer @EUROPEAN DYNAMICS",
            "[Hiring] AI Developer @ECS Tech Inc",
            "Frontend ReactJS Developer with French | SNI",
        ):
            with self.subTest(title=title):
                result = self.decision(title)
                self.assertReason(result, "PASS_RELEVANT_ROLE")
                self.assertNotIn("REVIEW_ROLE_UNCLEAR", [reason.code for reason in result.reasons])
        researcher = self.decision("AI Researcher")
        self.assertEqual(researcher.status, "REJECT")
        self.assertNotIn("PASS_RELEVANT_ROLE", [reason.code for reason in researcher.reasons])

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

    def test_french_experience_extraction(self):
        fixed_cases = (
            ("5 ans d'expérience", 5),
            ("5 ans d’expérience", 5),
            ("au moins 5 ans d'expérience", 5),
            ("au moins cinq ans", 5),
            ("minimum de 5 ans", 5),
            ("un minimum de 5 ans", 5),
            ("5 années d'expérience", 5),
            ("expérience de 5 ans minimum", 5),
            ("vous justifiez de 5 ans d'expérience", 5),
            ("vous disposez de 5 ans d'expérience", 5),
            ("vous avez 5 ans d'expérience", 5),
            ("expérience professionnelle de 5 ans", 5),
            ("trois ans d'expérience", 3),
        )
        for description, expected in fixed_cases:
            with self.subTest(description=description):
                evidence = extract_experience(description)
                self.assertEqual(len(evidence), 1)
                self.assertEqual(evidence[0].minimum, expected)
                self.assertIsNone(evidence[0].maximum)
                self.assertFalse(evidence[0].preferred)

        range_cases = (
            ("3 à 5 ans d'expérience", 3, 5),
            ("3-5 ans d'expérience", 3, 5),
            ("entre 3 et 5 ans d'expérience", 3, 5),
            ("1 à 2 ans", 1, 2),
            ("1-2 ans d'expérience", 1, 2),
        )
        for description, minimum, maximum in range_cases:
            with self.subTest(description=description):
                evidence = extract_experience(description)
                self.assertEqual((evidence[0].minimum, evidence[0].maximum), (minimum, maximum))

    def test_french_optional_approximate_and_false_positive_context(self):
        for description in (
            "Idéalement 5 ans d'expérience",
            "5 ans serait un plus",
            "Expérience de 5 ans souhaitée",
        ):
            with self.subTest(description=description):
                evidence = extract_experience(description)
                self.assertEqual(len(evidence), 1)
                self.assertTrue(evidence[0].preferred)
                result = self.decision(description=description)
                self.assertNotEqual(result.status, "REJECT")
                self.assertReason(result, "REVIEW_EXPERIENCE_PREFERRED")

        approximate = extract_experience("environ 3 ans d'expérience")
        self.assertTrue(approximate[0].approximate)
        for description in (
            "expérience significative",
            "expérience confirmée",
            "solide expérience",
            "Java 8",
            "Angular 17",
            "équipe de 5 personnes",
            "75009 Paris",
        ):
            with self.subTest(description=description):
                self.assertEqual(extract_experience(description), ())

    def test_french_job_43_shaped_description_uses_existing_policy(self):
        description = (
            "Nous recherchons un développeur backend pour concevoir des API. "
            "Vous justifiez d'au moins 5 ans d'expérience professionnelle "
            "dans le développement logiciel. Python et PostgreSQL sont requis."
        )
        result = self.decision(description=description)
        self.assertEqual(result.status, "REJECT")
        self.assertReason(result, "REJECT_EXPERIENCE_5_PLUS")
        self.assertGreaterEqual(result.experience_min_years, 5)
        evidence = result.matched_terms["experience_evidence"]
        self.assertEqual(evidence[0]["source"], "description")
        self.assertIn("5 ans d'expérience", evidence[0]["experience_text"])

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

    def test_worldwide_remote_requires_explicit_job_eligibility(self):
        incidental_descriptions = (
            "We serve customers worldwide with credential-security products.",
            "Our globally distributed customers rely on this platform.",
            "Join an international company with offices on three continents.",
            "Our products are used worldwide by millions of people.",
            "The company operates globally and builds an international brand.",
            "Open to candidates worldwide.",
            "Open to candidates worldwide to build remote-monitoring products.",
        )
        for description in incidental_descriptions:
            with self.subTest(description=description):
                result = self.decision(description=description, location="Paris, France")
                self.assertNotIn(
                    "PASS_REMOTE_WORLDWIDE",
                    [reason.code for reason in result.reasons],
                )
                self.assertReason(result, "REVIEW_LOCATION_PREFERRED_MARKET")

        for title, location in (
            ("Worldwide Backend Developer", "Paris, France"),
            ("Backend Developer", "Worldwide"),
        ):
            with self.subTest(title=title, location=location):
                result = self.decision(title, location=location)
                self.assertNotIn(
                    "PASS_REMOTE_WORLDWIDE",
                    [reason.code for reason in result.reasons],
                )

        seattle = self.decision(
            description="We support customers worldwide and operate globally.",
            location="Seattle", city="Seattle",
        )
        self.assertEqual(seattle.status, "REJECT")
        self.assertReason(seattle, "REJECT_LOCATION_US_ONLY")
        self.assertNotIn(
            "PASS_REMOTE_WORLDWIDE", [reason.code for reason in seattle.reasons]
        )

        for description, location in (
            ("Work remotely from anywhere in Quebec.", "Montreal, Canada"),
            ("This role is remote anywhere within Europe.", "Remote in Europe"),
        ):
            with self.subTest(description=description):
                result = self.decision(description=description, location=location)
                self.assertNotIn(
                    "PASS_REMOTE_WORLDWIDE",
                    [reason.code for reason in result.reasons],
                )

    def test_explicit_worldwide_remote_phrases_remain_eligible(self):
        descriptions = (
            "This role is remote worldwide.",
            "You may work remotely from anywhere in the world.",
            "This position lets you work from anywhere.",
            "The job is remote anywhere in the world.",
            "We are hiring worldwide for a remote position.",
            "This remote role is open to candidates worldwide.",
            "Open to candidates worldwide. This is a remote role.",
            "This job can be performed remote globally.",
        )
        for description in descriptions:
            with self.subTest(description=description):
                result = self.decision(description=description, location="")
                self.assertEqual(result.status, "PASS")
                self.assertReason(result, "PASS_REMOTE_WORLDWIDE")

        for location in ("Remote - Worldwide", "Location: Worldwide / Remote"):
            with self.subTest(location=location):
                result = self.decision(location=location)
                self.assertEqual(result.status, "PASS")
                self.assertReason(result, "PASS_REMOTE_WORLDWIDE")

        structured = self.decision(location="Worldwide", remote_policy="REMOTE")
        self.assertEqual(structured.status, "PASS")
        self.assertReason(structured, "PASS_REMOTE_WORLDWIDE")

    def test_worldwide_remote_regression_uses_job_content_not_an_id(self):
        dashlane_shaped = self.decision(
            "Software Engineer - Security Features",
            description=(
                "About the company: millions of consumers and over 25,000 brands "
                "worldwide trust our products. We have grown to more than 300 "
                "employees globally. About the role: join our product development "
                "team based in Paris."
            ),
            location="Paris, France", country="France", city="Paris",
        )
        self.assertEqual(dashlane_shaped.status, "REVIEW")
        self.assertReason(dashlane_shaped, "REVIEW_LOCATION_PREFERRED_MARKET")
        self.assertNotIn(
            "PASS_REMOTE_WORLDWIDE",
            [reason.code for reason in dashlane_shaped.reasons],
        )

        europe = self.decision(
            description="This is a remote role within Europe for an international company.",
            location="Remote in Europe",
        )
        self.assertEqual(europe.status, "REVIEW")
        self.assertReason(europe, "REVIEW_LOCATION_EUROPE")
        self.assertNotIn(
            "PASS_REMOTE_WORLDWIDE", [reason.code for reason in europe.reasons]
        )

    def test_normalized_geography_does_not_use_description_substrings(self):
        cases = (
            (("Seattle", "Seattle", "", ""), ("United States", "UNITED_STATES")),
            (("Sliema, Malta", "Sliema", "", ""), ("Malta", "EUROPE")),
            (("Iași, Romania", "Iași", "", ""), ("Romania", "EUROPE")),
            (("Breda, Netherlands", "Breda", "", ""), ("Netherlands", "EUROPE")),
            (("Unlisted City", "Unlisted City", "", ""), ("", "UNKNOWN")),
        )
        for values, expected in cases:
            with self.subTest(location=values[0]):
                result = normalize_geography(*values)
                self.assertEqual((result.country, result.region), expected)

        seattle = self.decision(
            location="Seattle", city="Seattle",
            description="We serve customers in the U.S., Canada, Europe, and Australia.",
        )
        self.assertNotIn("REVIEW_LOCATION_EUROPE", [reason.code for reason in seattle.reasons])
        self.assertReason(seattle, "REJECT_LOCATION_US_ONLY")
        self.assertEqual(seattle.matched_terms["normalized_country"], ["United States"])

        false_location = self.decision(
            "[Hiring] AI Developer @ECS Tech Inc", location="ai", city="ai",
        )
        self.assertReason(false_location, "REVIEW_LOCATION_UNKNOWN")
        self.assertEqual(false_location.matched_terms["normalized_country"], [])

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

    def test_live_general_careers_and_vacancies_pages_are_rejected(self):
        cases = (
            (
                "Software Engineering Careers & Job Opportunities | Accenture",
                "Explore careers and job opportunities in software engineering.",
                "https://www.accenture.com/be-en/careers/explore-careers/area-of-interest/software-engineering-careers",
            ),
            (
                "Junior developer jobs - 29 vacancies on JobScout24",
                "Search open roles from multiple employers.",
                "https://www.jobscout24.ch/en/jobs/junior%20developer",
            ),
        )
        for title, description, url in cases:
            with self.subTest(title=title):
                result = evaluate_job(job(
                    title, description, location="", canonical_url=url,
                ), NOW)
                self.assertEqual(result.status, "REJECT")
                self.assertEqual(result.primary_reason, "GENERIC_JOB_LISTING_PAGE")

    def test_shallow_role_jobs_title_is_a_collection_after_fetch(self):
        cases = (
            ("Remote Software Engineer Jobs", "software-engineer"),
            ("Backend Developer Jobs", "backend-developer"),
        )
        for title, slug in cases:
            with self.subTest(title=title):
                result = evaluate_job(job(
                    title, "", location="",
                    canonical_url=f"https://careers.example.test/jobs/{slug}",
                ), NOW)
                self.assertEqual(result.status, "REJECT")
                self.assertEqual(
                    result.primary_reason, "GENERIC_JOB_LISTING_PAGE",
                )

    def test_stored_structured_posting_status_overrides_weak_listing_shape(self):
        result = evaluate_job(job(
            "Backend Developer Jobs", "Build APIs for Acme.",
            canonical_url="https://careers.example.test/jobs/backend-developer",
            status="OPEN",
        ), NOW)
        self.assertNotEqual(result.primary_reason, "GENERIC_JOB_LISTING_PAGE")

    def test_live_transformation_consulting_titles_are_rejected(self):
        titles = (
            "Consultant(e) Débutant(e) en projets de transformation métier et IT, secteur Assurance (H/F) 1",
            "Consultant.e Junior en Transformation Digitale -Boosting CTO - Audit IT",
        )
        for title in titles:
            with self.subTest(title=title):
                result = self.decision(title, description="Accompagner les transformations métier et IT.")
                self.assertEqual(result.status, "REJECT")
                self.assertEqual(result.primary_reason, "REJECT_ROLE_CONSULTING")

        developer = self.decision(
            "Backend Developer",
            description="Collaborate with consulting teams on transformation projects.",
        )
        self.assertNotEqual(developer.status, "REJECT")
        self.assertReason(developer, "PASS_RELEVANT_ROLE")


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
