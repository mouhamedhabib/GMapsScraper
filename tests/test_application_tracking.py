"""Offline tests for deterministic application action tracking."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from job_search.application_tracking import (
    prepare_application_actions, transition_application_action,
)
from job_search.storage import connect_database, utc_now


class ApplicationTrackingTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = connect_database(Path(self.temp.name) / "jobs.db")
        self.addCleanup(self.connection.close)

    def add_job(
        self, *, qualification="QUALIFIED", contact_outcome="APPLY_ONLY",
        application_status="CONFIRMED", application_url=None, provider="lever",
        title="Software Engineer", company="Acme", activity="ACTIVE",
        destination=True,
    ):
        now = utc_now()
        job_id = self.connection.execute(
            "SELECT COALESCE(MAX(job_id),0)+1 FROM jobs"
        ).fetchone()[0]
        company_row = self.connection.execute(
            "SELECT company_id FROM companies WHERE canonical_name=?", (company,),
        ).fetchone()
        company_id = company_row[0] if company_row else self.connection.execute(
            """INSERT INTO companies
               (canonical_name,first_seen_at,last_seen_at,created_at,updated_at)
               VALUES (?,?,?,?,?)""", (company, now, now, now, now),
        ).lastrowid
        job_url = f"https://company.test/jobs/{job_id}"
        app_url = application_url
        if app_url is None:
            app_url = f"https://jobs.lever.co/acme/{job_id}"
        self.connection.execute(
            """INSERT INTO jobs
               (job_id,company_id,canonical_url,title,description,first_seen_at,last_seen_at,
                status,content_hash,created_at,updated_at)
               VALUES (?,?,?,?, 'Build software',?,?,'OPEN',?,?,?)""",
            (job_id, company_id, job_url, title, now, now, f"job-{job_id}", now, now),
        )
        self.connection.execute(
            """INSERT INTO job_qualifications
               (job_id,policy_version,qualification_status,activity_status,employer_status,
                application_channel,application_url,reason_codes_json,evidence_json,
                input_evidence_hash,qualified_at,created_at,updated_at)
               VALUES (?,'q1',?,?,'CONFIRMED','ATS',?,'[]','[]',?,?,?,?)""",
            (job_id, qualification, activity, app_url, f"q-{job_id}", now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_contact_outcomes
               (job_id,policy_version,outcome,application_status,application_channel,
                reason_codes_json,input_fingerprint,created_at,updated_at)
               VALUES (?,'contact-outcome-v1',?,'CONFIRMED','ATS','[]',?,?,?)""",
            (job_id, contact_outcome, f"outcome-{job_id}", now, now),
        )
        if destination:
            self.connection.execute(
                """INSERT INTO job_application_destinations
                   (job_id,policy_version,application_status,application_channel,application_url,
                    provider,source_type,employer_relationship,authoritative_source_url,
                    evidence_method,evidence_json,input_fingerprint,fetch_result,resolved_at,
                    created_at,updated_at)
                   VALUES (?,'destination-v1',?,'ATS',?,?,'ATS','DIRECT',?,
                           'ATS_POSTING','[]',?,'NOT_REQUIRED',?,?,?)""",
                (job_id, application_status, app_url, provider, app_url,
                 f"destination-{job_id}", now, now, now),
            )
        self.connection.execute(
            """INSERT INTO job_activity_evidence
               (job_id,policy_version,activity_status,source_type,provider,evidence_method,
                raw_evidence_summary,evidence_json,fetch_status,input_fingerprint,checked_at,
                created_at,updated_at)
               VALUES (?,'activity-v1',?,'ATS',?,'ATS_STATUS','test','[]','FETCHED',?,?,?,?)""",
            (job_id, activity, provider, f"activity-{job_id}", now, now, now),
        )
        self.connection.commit()
        return job_id

    def prepare_one(self, job_id):
        return prepare_application_actions(self.connection, [job_id])[0]

    def event_states(self, job_id):
        return [
            (row["old_state"], row["new_state"])
            for row in self.connection.execute(
                "SELECT old_state,new_state FROM job_application_events WHERE job_id=? ORDER BY application_event_id",
                (job_id,),
            )
        ]

    def test_apply_only_is_ready(self):
        job_id = self.add_job()
        self.assertEqual(self.prepare_one(job_id).state, "READY_TO_APPLY")

    def test_email_discovery_ready_is_also_ready(self):
        job_id = self.add_job(contact_outcome="EMAIL_DISCOVERY_READY")
        self.assertEqual(self.prepare_one(job_id).state, "READY_TO_APPLY")

    def test_review_disqualified_and_contact_review_are_excluded(self):
        ids = (
            self.add_job(qualification="REVIEW"),
            self.add_job(qualification="DISQUALIFIED"),
            self.add_job(contact_outcome="CONTACT_REVIEW"),
        )
        self.assertEqual(prepare_application_actions(self.connection, ids), [])

    def test_missing_or_unconfirmed_destination_is_not_ready(self):
        missing = self.add_job(destination=False)
        unknown = self.add_job(application_status="UNKNOWN")
        blank = self.add_job(application_url="")
        self.assertEqual(prepare_application_actions(self.connection, [missing, unknown, blank]), [])
        count = self.connection.execute("SELECT count(*) FROM job_application_actions").fetchone()[0]
        self.assertEqual(count, 0)

    def test_tracking_parameters_do_not_defeat_duplicate_protection(self):
        first = self.add_job(application_url="https://jobs.lever.co/acme/shared?utm_source=one")
        second = self.add_job(application_url="https://jobs.lever.co/acme/shared?utm_campaign=two")
        self.prepare_one(first)
        transition_application_action(self.connection, first, "APPLIED")
        duplicate = self.prepare_one(second)
        self.assertEqual(duplicate.duplicate_status, "ALREADY_APPLIED")
        self.assertNotEqual(duplicate.state, "READY_TO_APPLY")
        self.assertEqual(duplicate.canonical_application_url, "https://jobs.lever.co/acme/shared")
        original = self.prepare_one(first)
        self.assertEqual((original.state, original.duplicate_status), ("APPLIED", "NO_DUPLICATE"))

    def test_provider_job_id_duplicate_protection(self):
        first = self.add_job(application_url="https://jobs.lever.co/acme/shared")
        second = self.add_job(application_url="https://jobs.lever.co/acme/shared/apply")
        self.prepare_one(first)
        transition_application_action(self.connection, first, "APPLIED")
        duplicate = self.prepare_one(second)
        self.assertEqual(duplicate.provider_job_id, "shared")
        self.assertEqual(duplicate.duplicate_status, "ALREADY_APPLIED")

    def test_unsubmitted_identity_overlap_requires_manual_review(self):
        first = self.add_job(application_url="https://jobs.lever.co/acme/shared")
        second = self.add_job(application_url="https://jobs.lever.co/acme/shared?utm_source=x")
        self.prepare_one(first)
        duplicate = self.prepare_one(second)
        self.assertEqual((duplicate.state, duplicate.duplicate_status),
                         ("SKIPPED", "POSSIBLE_DUPLICATE"))

    def test_disagreeing_strong_identity_is_conflict(self):
        first = self.add_job(application_url="https://jobs.lever.co/acme/shared")
        second = self.add_job(application_url="https://jobs.lever.co/acme/shared?utm_source=x")
        self.prepare_one(first)
        self.connection.execute(
            "UPDATE job_application_actions SET provider_job_id='different-strong-id' WHERE job_id=?",
            (first,),
        )
        self.connection.commit()
        duplicate = self.prepare_one(second)
        self.assertEqual((duplicate.state, duplicate.duplicate_status),
                         ("SKIPPED", "IDENTITY_CONFLICT"))

    def test_different_jobs_at_same_company_remain_independent(self):
        first = self.add_job(title="Backend Engineer")
        second = self.add_job(title="Frontend Engineer")
        actions = prepare_application_actions(self.connection, [first, second])
        self.assertEqual([item.duplicate_status for item in actions], ["NO_DUPLICATE", "NO_DUPLICATE"])
        self.assertEqual([item.state for item in actions], ["READY_TO_APPLY", "READY_TO_APPLY"])

    def test_ready_applying_applied_and_withdrawn_transitions(self):
        job_id = self.add_job()
        self.prepare_one(job_id)
        self.assertEqual(transition_application_action(self.connection, job_id, "APPLYING").state, "APPLYING")
        applied = transition_application_action(self.connection, job_id, "APPLIED")
        self.assertTrue(applied.applied_at)
        withdrawn = transition_application_action(self.connection, job_id, "WITHDRAWN")
        self.assertTrue(withdrawn.withdrawn_at)
        self.assertEqual(self.event_states(job_id), [
            (None, "READY_TO_APPLY"), ("READY_TO_APPLY", "APPLYING"),
            ("APPLYING", "APPLIED"), ("APPLIED", "WITHDRAWN"),
        ])

    def test_manual_applied_shortcut(self):
        job_id = self.add_job()
        self.prepare_one(job_id)
        self.assertEqual(transition_application_action(self.connection, job_id, "APPLIED").state, "APPLIED")

    def test_skipped_and_failed_retry_transitions(self):
        skipped = self.add_job()
        self.prepare_one(skipped)
        self.assertEqual(
            transition_application_action(self.connection, skipped, "SKIPPED", "not pursuing").state,
            "SKIPPED",
        )
        failed = self.add_job()
        self.prepare_one(failed)
        transition_application_action(self.connection, failed, "APPLYING")
        transition_application_action(self.connection, failed, "FAILED", "form unavailable")
        retried = self.prepare_one(failed)
        self.assertEqual(retried.state, "READY_TO_APPLY")
        self.assertIn(("FAILED", "READY_TO_APPLY"), self.event_states(failed))
        failure_event = self.connection.execute(
            """SELECT reason FROM job_application_events
               WHERE job_id=? AND new_state='FAILED'""", (failed,),
        ).fetchone()
        self.assertEqual(failure_event["reason"], "form unavailable")

    def test_invalid_transition_is_rejected_without_event(self):
        job_id = self.add_job()
        self.prepare_one(job_id)
        transition_application_action(self.connection, job_id, "APPLIED")
        count = len(self.event_states(job_id))
        with self.assertRaisesRegex(ValueError, "invalid application transition"):
            transition_application_action(self.connection, job_id, "APPLYING")
        self.assertEqual(len(self.event_states(job_id)), count)

    def test_terminal_states_survive_repeated_prepare(self):
        transitions = (("APPLIED", "APPLIED"), ("SKIPPED", "SKIPPED"))
        for target, expected in transitions:
            with self.subTest(target=target):
                job_id = self.add_job()
                self.prepare_one(job_id)
                transition_application_action(self.connection, job_id, target)
                events = len(self.event_states(job_id))
                rerun = self.prepare_one(job_id)
                self.assertEqual(rerun.state, expected)
                self.assertEqual(len(self.event_states(job_id)), events)
        withdrawn = self.add_job()
        self.prepare_one(withdrawn)
        transition_application_action(self.connection, withdrawn, "APPLIED")
        transition_application_action(self.connection, withdrawn, "WITHDRAWN")
        events = len(self.event_states(withdrawn))
        self.assertEqual(self.prepare_one(withdrawn).state, "WITHDRAWN")
        self.assertEqual(len(self.event_states(withdrawn)), events)

    def test_closed_before_apply_but_applied_history_survives_closure(self):
        ready = self.add_job()
        self.prepare_one(ready)
        self.connection.execute(
            "UPDATE job_activity_evidence SET activity_status='INACTIVE',input_fingerprint='closed' WHERE job_id=?",
            (ready,),
        )
        self.connection.commit()
        self.assertEqual(self.prepare_one(ready).state, "CLOSED_BEFORE_APPLY")
        applied = self.add_job()
        self.prepare_one(applied)
        transition_application_action(self.connection, applied, "APPLIED")
        self.connection.execute(
            "UPDATE job_activity_evidence SET activity_status='INACTIVE',input_fingerprint='closed' WHERE job_id=?",
            (applied,),
        )
        self.connection.commit()
        self.assertEqual(self.prepare_one(applied).state, "APPLIED")

    def test_idempotent_prepare_and_append_only_events(self):
        job_id = self.add_job()
        first = self.prepare_one(job_id)
        stored_first = tuple(self.connection.execute(
            "SELECT application_action_id,ready_at,created_at,updated_at FROM job_application_actions WHERE job_id=?",
            (job_id,),
        ).fetchone())
        second = self.prepare_one(job_id)
        stored_second = tuple(self.connection.execute(
            "SELECT application_action_id,ready_at,created_at,updated_at FROM job_application_actions WHERE job_id=?",
            (job_id,),
        ).fetchone())
        self.assertEqual(first.application_action_id, second.application_action_id)
        self.assertTrue(second.reused)
        self.assertEqual(stored_first, stored_second)
        self.assertEqual(len(self.event_states(job_id)), 1)
        reason = self.connection.execute(
            "SELECT reason FROM job_application_events WHERE job_id=?", (job_id,),
        ).fetchone()[0]
        self.assertEqual(reason, "APPLICATION_PREPARED")

    def test_prepare_has_no_network_and_preserves_upstream(self):
        job_id = self.add_job()
        upstream = (
            "job_qualifications", "job_contact_outcomes", "job_application_destinations",
            "job_activity_evidence",
        )
        before = {
            table: [tuple(row) for row in self.connection.execute(
                f"SELECT * FROM {table} WHERE job_id=?", (job_id,),
            )]
            for table in upstream
        }
        with patch("requests.sessions.Session.request") as request:
            self.prepare_one(job_id)
            request.assert_not_called()
        after = {
            table: [tuple(row) for row in self.connection.execute(
                f"SELECT * FROM {table} WHERE job_id=?", (job_id,),
            )]
            for table in upstream
        }
        self.assertEqual(before, after)

    def test_duplicate_marking_is_blocked(self):
        first = self.add_job(application_url="https://jobs.lever.co/acme/shared")
        second = self.add_job(application_url="https://jobs.lever.co/acme/shared?utm_source=x")
        self.prepare_one(first)
        transition_application_action(self.connection, first, "APPLIED")
        duplicate = self.prepare_one(second)
        events = len(self.event_states(second))
        with self.assertRaisesRegex(ValueError, "invalid application transition"):
            transition_application_action(self.connection, second, "APPLIED")
        self.assertEqual(duplicate.duplicate_status, "ALREADY_APPLIED")
        self.assertEqual(len(self.event_states(second)), events)

    def test_explicit_scope_is_required(self):
        self.add_job()
        with self.assertRaisesRegex(ValueError, "provide at least one"):
            prepare_application_actions(self.connection)
