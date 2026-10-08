"""Offline tests for the deterministic contact outcome policy."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from job_search.contact_outcome import resolve_contact_outcomes
from job_search.storage import connect_database, utc_now


class ContactOutcomeTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = connect_database(Path(self.temp.name) / "jobs.db")
        self.addCleanup(self.connection.close)

    def add_job(
        self, *, qualification="QUALIFIED", discovery="NO_CONFIDENT_CONTACT",
        confidence=None, selection_status="SELECTED_PRIMARY",
        application_status="CONFIRMED", application_url="https://jobs.lever.co/acme/1",
    ):
        now = utc_now()
        job_id = self.connection.execute(
            "SELECT COALESCE(MAX(job_id),0)+1 FROM jobs"
        ).fetchone()[0]
        self.connection.execute(
            """INSERT INTO jobs
               (job_id,canonical_url,title,description,first_seen_at,last_seen_at,status,
                content_hash,created_at,updated_at)
               VALUES (?,?, 'Software Engineer','Build software',?,?,'OPEN','hash',?,?)""",
            (job_id, f"https://jobs.lever.co/acme/{job_id}", now, now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_qualifications
               (job_id,policy_version,qualification_status,activity_status,employer_status,
                application_channel,application_url,reason_codes_json,evidence_json,
                input_evidence_hash,qualified_at,created_at,updated_at)
               VALUES (?,'q1',?,'ACTIVE','CONFIRMED','ATS',?,'[]','[]',?,?,?,?)""",
            (job_id, qualification, application_url, f"q-{job_id}", now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_contact_strategies
               (job_id,policy_version,primary_role,secondary_roles_json,avoid_roles_json,
                confidence,rationale,reason_codes_json,input_fingerprint,resolved_at,
                created_at,updated_at)
               VALUES (?,'strategy-v1','Engineering Manager','["Technical Recruiter"]',
                       '["CEO"]','HIGH','test','[]',?,?,?,?)""",
            (job_id, f"strategy-{job_id}", now, now, now),
        )
        primary_id = None
        if confidence is not None:
            primary_id = self.connection.execute(
                """INSERT INTO job_contact_candidates
                   (job_id,policy_version,person_key,person_name,current_title,company,
                    target_role_category,source_url,source_type,relationship_to_job,
                    confidence,selection_status,reason_codes_json,evidence_json,
                    discovery_query,input_fingerprint,checked_at,discovered_at,updated_at)
                   VALUES (?,'discovery-v1',?,'Alice Martin','Engineering Manager','Acme',
                           'Engineering Manager','https://acme.test/team','OFFICIAL_COMPANY',
                           'CURRENT_EMPLOYEE',?,?,'[]','[]','',?,?,?,?)""",
                (job_id, f"person-{job_id}", confidence, selection_status,
                 f"candidate-{job_id}", now, now, now),
            ).lastrowid
        self.connection.execute(
            """INSERT INTO job_selected_contacts
               (job_id,policy_version,discovery_status,primary_contact_candidate_id,
                backup_contact_candidate_id,search_queries_used,people_inspected,
                input_fingerprint,discovered_at,created_at,updated_at,
                first_party_pages_inspected)
               VALUES (?,'discovery-v1',?,?,NULL,0,?, ?,?,?,?,0)""",
            (job_id, discovery, primary_id, int(primary_id is not None),
             f"discovery-{job_id}", now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_application_destinations
               (job_id,policy_version,application_status,application_channel,application_url,
                provider,source_type,employer_relationship,authoritative_source_url,
                evidence_method,evidence_json,input_fingerprint,fetch_result,resolved_at,
                created_at,updated_at)
               VALUES (?,'application-v1',?,'ATS',?,'lever','ATS','DIRECT',?,
                       'ATS_POSTING','[]',?,'NOT_REQUIRED',?,?,?)""",
            (job_id, application_status, application_url, application_url,
             f"application-{job_id}", now, now, now),
        )
        self.connection.commit()
        return job_id

    def resolve_one(self, job_id):
        return resolve_contact_outcomes(self.connection, [job_id])[0]

    def test_high_selected_contact_is_email_discovery_ready(self):
        job_id = self.add_job(discovery="CONTACTS_SELECTED", confidence="HIGH")
        result = self.resolve_one(job_id)
        self.assertEqual(result.outcome, "EMAIL_DISCOVERY_READY")
        self.assertIn("OUTCOME_CONTACT_CONFIDENCE_ACCEPTABLE", result.reason_codes)

    def test_medium_selected_contact_is_email_discovery_ready(self):
        job_id = self.add_job(discovery="CONTACTS_SELECTED", confidence="MEDIUM")
        self.assertEqual(self.resolve_one(job_id).outcome, "EMAIL_DISCOVERY_READY")

    def test_low_contact_never_qualifies(self):
        job_id = self.add_job(discovery="CONTACTS_SELECTED", confidence="LOW")
        result = self.resolve_one(job_id)
        self.assertEqual(result.outcome, "CONTACT_REVIEW")
        self.assertNotIn("OUTCOME_CONTACT_CONFIDENCE_ACCEPTABLE", result.reason_codes)

    def test_rejected_contact_never_qualifies(self):
        job_id = self.add_job(
            discovery="CONTACTS_SELECTED", confidence="HIGH",
            selection_status="REJECTED_LOW_CONFIDENCE",
        )
        self.assertEqual(self.resolve_one(job_id).outcome, "CONTACT_REVIEW")

    def test_no_contact_and_confirmed_application_is_apply_only(self):
        job_id = self.add_job()
        result = self.resolve_one(job_id)
        self.assertEqual(result.outcome, "APPLY_ONLY")
        self.assertIn("OUTCOME_APPLICATION_DESTINATION_CONFIRMED", result.reason_codes)

    def test_no_contact_and_unknown_application_is_review(self):
        job_id = self.add_job(application_status="UNKNOWN", application_url="")
        result = self.resolve_one(job_id)
        self.assertEqual(result.outcome, "CONTACT_REVIEW")
        self.assertIn("OUTCOME_APPLICATION_DESTINATION_UNKNOWN", result.reason_codes)

    def test_confirmed_application_without_url_is_review(self):
        job_id = self.add_job(application_url="")
        self.assertEqual(self.resolve_one(job_id).outcome, "CONTACT_REVIEW")

    def test_missing_contact_strategy_is_review(self):
        job_id = self.add_job()
        self.connection.execute("DELETE FROM job_contact_strategies WHERE job_id=?", (job_id,))
        self.connection.commit()
        result = self.resolve_one(job_id)
        self.assertEqual(result.outcome, "CONTACT_REVIEW")
        self.assertIn("OUTCOME_CONTACT_DISCOVERY_INCOMPLETE", result.reason_codes)

    def test_review_and_disqualified_jobs_are_excluded(self):
        review_id = self.add_job(qualification="REVIEW")
        disqualified_id = self.add_job(qualification="DISQUALIFIED")
        self.assertEqual(
            resolve_contact_outcomes(self.connection, [review_id, disqualified_id]), [],
        )

    def test_idempotence_and_upstream_tables_unchanged(self):
        job_id = self.add_job()
        tables = (
            "job_qualifications", "job_contact_strategies", "job_selected_contacts",
            "job_contact_candidates", "job_application_destinations",
        )
        before = {
            table: [tuple(row) for row in self.connection.execute(
                f"SELECT * FROM {table} WHERE job_id=?", (job_id,),
            )]
            for table in tables
        }
        first = self.resolve_one(job_id)
        stored_first = tuple(self.connection.execute(
            """SELECT contact_outcome_id,created_at,updated_at
               FROM job_contact_outcomes WHERE job_id=?""", (job_id,),
        ).fetchone())
        second = self.resolve_one(job_id)
        stored_second = tuple(self.connection.execute(
            """SELECT contact_outcome_id,created_at,updated_at
               FROM job_contact_outcomes WHERE job_id=?""", (job_id,),
        ).fetchone())
        after = {
            table: [tuple(row) for row in self.connection.execute(
                f"SELECT * FROM {table} WHERE job_id=?", (job_id,),
            )]
            for table in tables
        }
        self.assertEqual(first.outcome, "APPLY_ONLY")
        self.assertTrue(second.reused)
        self.assertEqual(stored_first, stored_second)
        self.assertEqual(before, after)

    def test_explicit_scope_is_required(self):
        self.add_job()
        with self.assertRaisesRegex(ValueError, "provide at least one"):
            resolve_contact_outcomes(self.connection)
