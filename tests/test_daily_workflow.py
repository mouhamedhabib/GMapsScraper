"""Offline tests for the deterministic Daily Workflow orchestration layer."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from job_search.daily_workflow import (
    WorkflowOptions, load_report_only, publish_report, run_workflow,
    validate_dry_run,
)
from job_search.filtering import DEFAULT_POLICY_VERSION, persist_result, FilterDecision, Reason
from job_search.providers import ParsedJob
from job_search.storage import connect_database, upsert_job


def parsed(url: str, title: str = "Backend Engineer") -> ParsedJob:
    job = ParsedJob(url, "generic")
    job.title = title
    job.company_name = "Example"
    job.location_text = "Remote worldwide"
    job.description = (
        "Backend engineer building APIs with Python and FastAPI. Remote worldwide. "
        * 8
    )
    job.status = "OPEN"
    job.fetch_status = "SUCCESS"
    return job


class DailyWorkflowTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "jobs.db"
        self.reports = self.root / "reports"
        self.queries = self.root / "google_queries.txt"
        self.queries.write_text("backend engineer remote\n", encoding="utf-8")
        self.options = WorkflowOptions(
            database=self.database, report_dir=self.reports,
            job_query_file=self.queries, job_limit=1, delay=0,
            timeout=1, windowed=False, completion_limit=2,
            completion=False,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def discovery(self, url: str):
        def run(**kwargs):
            connection = connect_database(kwargs["database"])
            try:
                job_id, was_new = upsert_job(connection, parsed(url), "query")
            finally:
                connection.close()
            return {
                "new": int(was_new), "known": int(not was_new),
                "job_states": {job_id: "NEW" if was_new else "KNOWN"},
            }
        return run

    def test_successful_run_record_and_new_job_membership(self):
        report = run_workflow(
            self.options, discovery_runner=self.discovery("https://example.test/jobs/1")
        )
        self.assertEqual(report["run"]["status"], "SUCCESS")
        self.assertEqual(report["job_discovery"]["new"], 1)
        connection = connect_database(self.database)
        try:
            run = connection.execute("SELECT * FROM workflow_runs").fetchone()
            membership = connection.execute("SELECT * FROM workflow_run_jobs").fetchone()
            self.assertEqual(run["status"], "SUCCESS")
            self.assertEqual(membership["discovery_state"], "NEW")
        finally:
            connection.close()

    def test_second_run_is_known_and_does_not_duplicate_job(self):
        discover = self.discovery("https://example.test/jobs/same")
        first = run_workflow(self.options, discovery_runner=discover)
        second = run_workflow(self.options, discovery_runner=discover)
        self.assertEqual(first["job_discovery"]["new"], 1)
        self.assertEqual(second["job_discovery"]["new"], 0)
        self.assertEqual(second["job_discovery"]["known"], 1)
        connection = connect_database(self.database)
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM workflow_runs").fetchone()[0], 2)
        finally:
            connection.close()

    def test_filter_priority_and_shortlist_are_scoped_to_new_jobs(self):
        connection = connect_database(self.database)
        try:
            historical, _ = upsert_job(
                connection, parsed("https://example.test/jobs/old", "Old Engineer"), "old"
            )
        finally:
            connection.close()
        report = run_workflow(
            self.options, discovery_runner=self.discovery("https://example.test/jobs/new")
        )
        self.assertEqual(report["filter_counts"], {"PASS": 1, "REVIEW": 0, "REJECT": 0})
        self.assertEqual([item["title"] for item in report["shortlist"]], ["Backend Engineer"])
        self.assertNotIn(historical, [item["job_id"] for item in report["shortlist"]])

    def test_query_location_mismatch_never_enters_shortlist(self):
        def discovery(**kwargs):
            connection = connect_database(kwargs["database"])
            try:
                candidate = parsed("https://example.test/jobs/india")
                candidate.location_text = "Bangalore, India"
                candidate.remote_policy = "ONSITE"
                candidate.description = "Backend engineer building Python APIs. " * 8
                job_id, _ = upsert_job(connection, candidate, "backend engineer France")
            finally:
                connection.close()
            return {"new": 1, "known": 0, "job_states": {job_id: "NEW"}}

        report = run_workflow(self.options, discovery_runner=discovery)
        self.assertEqual(report["filter_counts"], {"PASS": 0, "REVIEW": 0, "REJECT": 1})
        self.assertEqual(report["query_intent_guard"]["mismatches_rejected"], 1)
        self.assertEqual(report["shortlist"], [])

    def test_default_shortlist_excludes_medium_review(self):
        self.options.hard_filter = False

        def discovery_for(url):
            def discovery(**kwargs):
                connection = connect_database(kwargs["database"])
                try:
                    job_id, _ = upsert_job(connection, parsed(url), "q")
                    decision = FilterDecision(
                        "REVIEW", "REVIEW_LOCATION_EUROPE",
                        (Reason("PASS_RELEVANT_ROLE", "role"),
                         Reason("PASS_FRESH_JOB", "fresh"),
                         Reason("REVIEW_LOCATION_EUROPE", "location"),
                         Reason("REVIEW_EXPERIENCE_PREFERRED", "experience")),
                        {"technologies": ["python"]}, None, None, "UNKNOWN",
                    )
                    with connection:
                        persist_result(
                            connection, job_id, DEFAULT_POLICY_VERSION, decision
                        )
                finally:
                    connection.close()
                return {"job_states": {job_id: "NEW"}}
            return discovery

        report = run_workflow(
            self.options,
            discovery_runner=discovery_for("https://example.test/jobs/review-one"),
        )
        self.assertEqual(report["priority_counts"]["MEDIUM"], 1)
        self.assertEqual(report["shortlist"], [])
        self.options.include_medium = True
        report = run_workflow(
            self.options,
            discovery_runner=discovery_for("https://example.test/jobs/review-two"),
        )
        self.assertEqual(len(report["shortlist"]), 1)

    def test_completion_receives_only_new_run_job_ids(self):
        self.options.completion = True
        calls = []

        def completion(connection, policy_version, **kwargs):
            calls.extend(kwargs["job_ids"])
            return {"selected": len(kwargs["job_ids"]), "failed": 0, "blocked": 0}

        report = run_workflow(
            self.options,
            discovery_runner=self.discovery("https://example.test/jobs/bounded"),
            completion_runner=completion,
        )
        self.assertEqual(calls, [report["shortlist"][0]["job_id"]])

    def test_report_json_and_latest_are_atomic_and_readable(self):
        report = run_workflow(
            self.options, discovery_runner=self.discovery("https://example.test/jobs/report")
        )
        run_path = self.reports / f"run_{report['run']['run_id']}.json"
        self.assertEqual(json.loads(run_path.read_text())["run"]["status"], "SUCCESS")
        self.assertEqual(load_report_only(self.options)["run"]["run_id"], report["run"]["run_id"])
        self.assertFalse(list(self.reports.glob(".*.json.*")))

    def test_report_only_loader_has_no_database_or_network_side_effect(self):
        payload = {"run": {"run_id": "offline", "status": "SUCCESS"}}
        publish_report(self.reports, payload)
        with patch("job_search.daily_workflow.connect_database") as connect, \
             patch("job_search.daily_workflow.discover_jobs") as discover:
            self.assertEqual(load_report_only(self.options), payload)
            connect.assert_not_called()
            discover.assert_not_called()

    def test_maps_failure_and_completion_failure_yield_partial(self):
        self.options.run_maps = True
        self.options.completion = True

        def fail_maps(options):
            raise RuntimeError("blocked")

        def one_failed(connection, policy_version, **kwargs):
            return {"selected": 1, "failed": 1, "blocked": 0}

        report = run_workflow(
            self.options, maps_runner=fail_maps,
            discovery_runner=self.discovery("https://example.test/jobs/partial"),
            completion_runner=one_failed,
        )
        self.assertEqual(report["run"]["status"], "PARTIAL")
        self.assertEqual(report["job_discovery"]["new"], 1)
        self.assertTrue((self.reports / "latest.json").exists())

    def test_keyboard_interrupt_marks_run_interrupted_without_latest(self):
        def interrupt(**kwargs):
            raise KeyboardInterrupt

        report = run_workflow(self.options, discovery_runner=interrupt)
        self.assertEqual(report["run"]["status"], "INTERRUPTED")
        connection = connect_database(self.database)
        try:
            status = connection.execute("SELECT status FROM workflow_runs").fetchone()[0]
            self.assertEqual(status, "INTERRUPTED")
        finally:
            connection.close()
        self.assertFalse((self.reports / "latest.json").exists())

    def test_database_startup_failure_returns_failed(self):
        with patch("job_search.daily_workflow.connect_database", side_effect=sqlite3.Error("no db")):
            report = run_workflow(self.options)
        self.assertEqual(report["run"]["status"], "FAILED")

    def test_dry_run_does_not_create_database_or_reports(self):
        result = validate_dry_run(self.options)
        self.assertTrue(result["valid"])
        self.assertFalse(self.database.exists())
        self.assertFalse(self.reports.exists())
