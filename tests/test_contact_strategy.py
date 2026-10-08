"""Offline tests for deterministic contact-role selection."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from job_search.contact_strategy import (
    CONTACT_POLICY_VERSION, evaluate_contact_strategy, resolve_contact_strategies,
)
from job_search.storage import connect_database, utc_now


def job_row(title="Software Engineer", description="Build reliable software.", **extra):
    row = {
        "job_id": 1, "title": title, "description": description,
        "company": "Acme", "company_website": "https://acme.test",
    }
    row.update(extra)
    return row


def source(relationship="DIRECT"):
    return {"employer_relationship": relationship}


class ContactPolicyTests(TestCase):
    def test_junior_role(self):
        result = evaluate_contact_strategy(job_row("Junior Software Engineer"))
        self.assertEqual(result.primary_role, "Early Careers Recruiter")
        self.assertIn("Engineering Manager", result.secondary_roles)

    def test_internship(self):
        result = evaluate_contact_strategy(job_row("Software Engineering Internship"))
        self.assertEqual(result.primary_role, "Campus Recruiter")
        self.assertIn("Early Careers Recruiter", result.secondary_roles)

    def test_standard_software_engineer(self):
        result = evaluate_contact_strategy(job_row())
        self.assertEqual(result.primary_role, "Engineering Manager")
        self.assertEqual(result.secondary_roles[0], "Technical Recruiter")

    def test_specialized_engineering_role(self):
        result = evaluate_contact_strategy(job_row("Full Stack Engineer"))
        self.assertEqual(result.primary_role, "Engineering Manager")
        self.assertEqual(result.secondary_roles, ("Head of Engineering", "Technical Recruiter"))

    def test_small_startup_prefers_technical_leaders_and_not_ceo(self):
        result = evaluate_contact_strategy(job_row(
            description="We are an early-stage startup with 18 employees.",
        ))
        self.assertEqual(result.primary_role, "CTO")
        self.assertEqual(result.secondary_roles, ("VP Engineering", "Technical Founder"))
        self.assertIn("CEO", result.avoid_roles)
        self.assertNotIn("CEO", (result.primary_role, *result.secondary_roles))

    def test_large_company_prefers_technical_recruiting(self):
        result = evaluate_contact_strategy(job_row(
            description="Join our global organization of 12,000 employees.",
        ))
        self.assertEqual(result.primary_role, "Technical Recruiter")
        self.assertIn("Talent Acquisition Partner", result.secondary_roles)
        self.assertIn("Generic HR", result.avoid_roles)

    def test_explicit_named_recruiter_has_highest_priority(self):
        result = evaluate_contact_strategy(job_row(
            description="Recruiter: Sarah Jones. Build reliable software.",
        ))
        self.assertEqual(result.primary_role, "EXPLICIT_CONTACT")
        self.assertEqual(result.confidence, "HIGH")
        self.assertIn("CONTACT_EXPLICIT_POSTING", result.reason_codes)

    def test_generic_recruiter_boilerplate_is_not_explicit_contact(self):
        result = evaluate_contact_strategy(job_row(
            description="Our genuine recruiters will only contact candidates from official domains.",
        ))
        self.assertEqual(result.primary_role, "Engineering Manager")

    def test_recruiter_hosted_posting_preserves_distinction(self):
        result = evaluate_contact_strategy(job_row(), [source("RECRUITER")])
        self.assertEqual(result.primary_role, "Engineering Manager")
        self.assertIn("Recruiter Platform Staff (unless explicitly named)", result.avoid_roles)
        self.assertIn("CONTACT_RECRUITER_HOSTED_DISTINCTION", result.reason_codes)

    def test_generic_hr_is_lower_priority_and_targets_are_bounded(self):
        result = evaluate_contact_strategy(job_row())
        self.assertNotEqual(result.primary_role, "Generic HR")
        self.assertIn("Generic HR", result.avoid_roles)
        self.assertEqual(len((result.primary_role,)), 1)
        self.assertLessEqual(len(result.secondary_roles), 2)


class ContactResolverTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = connect_database(Path(self.temp.name) / "jobs.db")
        self.addCleanup(self.connection.close)

    def add_job(self, title, qualification_status, *, relationship="DIRECT"):
        now = utc_now()
        cursor = self.connection.execute(
            """INSERT INTO companies
               (canonical_name,normalized_domain,website_url,first_seen_at,last_seen_at,created_at,updated_at)
               VALUES (?,NULL,?,?, ?,?,?)""",
            (f"Company {title}", "https://company.test", now, now, now, now),
        )
        company_id = cursor.lastrowid
        job_id = self.connection.execute("SELECT COALESCE(MAX(job_id),0)+1 FROM jobs").fetchone()[0]
        url = f"https://company.test/jobs/{job_id}"
        self.connection.execute(
            """INSERT INTO jobs
               (job_id,company_id,canonical_url,title,description,first_seen_at,last_seen_at,
                status,content_hash,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'OPEN','hash',?,?)""",
            (job_id, company_id, url, title, "Build software.", now, now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_sources
               (job_id,provider,source_url,first_seen_at,last_seen_at,source_type,employer_relationship)
               VALUES (?,'generic',?,?,?,'COMPANY_SITE',?)""",
            (job_id, url, now, now, relationship),
        )
        self.connection.execute(
            """INSERT INTO job_qualifications
               (job_id,policy_version,qualification_status,activity_status,employer_status,
                application_channel,reason_codes_json,evidence_json,input_evidence_hash,
                qualified_at,created_at,updated_at)
               VALUES (?,'test-policy',?,'ACTIVE','CONFIRMED','DIRECT_COMPANY','[]','[]','qhash',?,?,?)""",
            (job_id, qualification_status, now, now, now),
        )
        self.connection.commit()
        return job_id

    def test_qualified_only_default_and_optional_review(self):
        qualified = self.add_job("Software Engineer", "QUALIFIED")
        review = self.add_job("Backend Engineer", "REVIEW")
        disqualified = self.add_job("Frontend Engineer", "DISQUALIFIED")
        self.assertEqual(
            [item.job_id for item in resolve_contact_strategies(self.connection)], [qualified],
        )
        included = resolve_contact_strategies(self.connection, include_review=True)
        self.assertEqual([item.job_id for item in included], [qualified, review])
        self.assertNotIn(disqualified, [item.job_id for item in included])

    def test_requested_ineligible_job_is_excluded(self):
        review = self.add_job("Software Engineer", "REVIEW")
        self.assertEqual(resolve_contact_strategies(self.connection, [review]), [])

    def test_deterministic_idempotent_output(self):
        job_id = self.add_job("Software Engineer II", "QUALIFIED")
        first = resolve_contact_strategies(self.connection, [job_id])[0]
        stored_first = self.connection.execute(
            """SELECT contact_strategy_id,created_at,updated_at,input_fingerprint
               FROM job_contact_strategies WHERE job_id=? AND policy_version=?""",
            (job_id, CONTACT_POLICY_VERSION),
        ).fetchone()
        second = resolve_contact_strategies(self.connection, [job_id])[0]
        stored_second = self.connection.execute(
            """SELECT contact_strategy_id,created_at,updated_at,input_fingerprint
               FROM job_contact_strategies WHERE job_id=? AND policy_version=?""",
            (job_id, CONTACT_POLICY_VERSION),
        ).fetchone()
        self.assertEqual(first.primary_role, second.primary_role)
        self.assertTrue(second.reused)
        self.assertEqual(tuple(stored_first), tuple(stored_second))
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM job_contact_strategies").fetchone()[0], 1,
        )

    def test_strategy_json_fields_round_trip(self):
        job_id = self.add_job("Junior Software Engineer", "QUALIFIED")
        result = resolve_contact_strategies(self.connection, [job_id])[0]
        stored = self.connection.execute(
            "SELECT secondary_roles_json,avoid_roles_json,reason_codes_json FROM job_contact_strategies"
        ).fetchone()
        self.assertEqual(json.loads(stored["secondary_roles_json"]), list(result.secondary_roles))
        self.assertEqual(json.loads(stored["avoid_roles_json"]), list(result.avoid_roles))
        self.assertEqual(json.loads(stored["reason_codes_json"]), list(result.reason_codes))
