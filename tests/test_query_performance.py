"""Regression tests for deterministic, read-only query analytics."""

from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from job_search.query_performance import (
    QueryPerformance, _readonly_connection, aggregate_categories, analyze,
    classify_performance, main, select_run_ids,
)
from job_search.storage import connect_database, utc_now


class QueryPerformanceTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "jobs.db"
        self.connection = connect_database(self.database)
        self.addCleanup(self.connection.close)

    def add_run(self, run_id: str, started: str, status: str = "SUCCESS") -> None:
        self.connection.execute(
            """INSERT INTO workflow_runs
               (run_id, started_at, finished_at, status, mode, maps_enabled,
                job_discovery_enabled, completion_enabled, filter_enabled,
                priority_enabled, created_at)
               VALUES (?, ?, ?, ?, 'DAILY', 0, 1, 1, 1, 1, ?)""",
            (run_id, started, started, status, started),
        )

    def query(self, run_id: str, query: str, **values) -> None:
        defaults = dict(
            category="GENERIC", pages=1, results=10, candidates=2, new=1,
            known=0, rejected=1, failures=0, browser=0, fetches=1, duration=10.0,
        )
        defaults.update(values)
        self.connection.execute(
            """INSERT INTO workflow_run_queries
               (run_id, source_query, status, pages_inspected, results_inspected,
                new_jobs, created_at, updated_at, query_category, job_candidates,
                known_jobs, rejected_noise, resolution_failures,
                browser_resolutions, http_job_fetches, duration_seconds)
               VALUES (?, ?, 'COMPLETED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, query, defaults["pages"], defaults["results"], defaults["new"],
             utc_now(), utc_now(), defaults["category"], defaults["candidates"],
             defaults["known"], defaults["rejected"], defaults["failures"],
             defaults["browser"], defaults["fetches"], defaults["duration"]),
        )

    def job(self, run_id: str, query: str, decision: str = "PASS", *,
            primary: bool = True, priority: str = "") -> int:
        now = utc_now()
        cursor = self.connection.execute(
            """INSERT INTO jobs
               (canonical_url, title, location_text, description, first_seen_at,
                last_seen_at, status, created_at, updated_at)
               VALUES (?, 'Backend Engineer', 'Paris, France', ?, ?, ?, 'OPEN', ?, ?)""",
            (f"https://example.test/jobs/{run_id}-{query}-{decision}-{priority}",
             "Backend engineer building APIs. " * 20, now, now, now, now),
        )
        job_id = cursor.lastrowid
        source = self.connection.execute(
            """INSERT INTO job_sources
               (job_id, provider, source_url, source_query, first_seen_at,
                last_seen_at, source_type, employer_relationship)
               VALUES (?, 'greenhouse', ?, ?, ?, ?, 'ATS', 'DIRECT')""",
            (job_id, f"https://example.test/jobs/{job_id}", query, now, now),
        ).lastrowid
        reasons = [{"code": "PASS_RELEVANT_ROLE"}, {"code": "PASS_FRESH_JOB"}]
        if decision == "REVIEW":
            review_code = {
                "HIGH": "REVIEW_EXPERIENCE_3_YEARS",
                "MEDIUM": "REVIEW_EXPERIENCE_PREFERRED",
                "LOW": "REVIEW_ROLE_UNCLEAR",
            }.get(priority, "REVIEW_EXPERIENCE_PREFERRED")
            reasons.append({"code": review_code})
            if priority == "MEDIUM":
                reasons.append({"code": "REVIEW_LOCATION_EUROPE"})
        self.connection.execute(
            """INSERT INTO job_filter_results
               (job_id, policy_version, evaluated_at, status, primary_reason,
                reasons_json, matched_terms_json, detected_remote_policy,
                created_at, updated_at)
               VALUES (?, 'v1.1', ?, ?, 'TEST', ?, '{}', 'UNKNOWN', ?, ?)""",
            (job_id, now, decision, json.dumps(reasons), now, now),
        )
        self.connection.execute(
            """INSERT INTO workflow_run_job_queries
               (run_id, job_id, source_query, discovery_state,
                is_primary_new_source, observed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, job_id, query, "NEW" if primary else "KNOWN", int(primary), now),
        )
        return job_id

    def commit_analyze(self, recent: int = 10):
        self.connection.commit()
        ids = select_run_ids(self.connection, recent_runs=recent)
        return analyze(self.connection, ids)

    def test_productive_query_is_keep_and_pass_is_shortlisted(self):
        for index in (1, 2):
            run_id = f"r{index}"
            self.add_run(run_id, f"2026-01-0{index}T00:00:00+00:00")
            self.query(run_id, "backend developer France", category="ROLE_LOCATION", new=1)
            self.job(run_id, "backend developer France", "PASS")
        item = self.commit_analyze()[0]
        self.assertEqual(item.recommendation, "KEEP")
        self.assertEqual((item.pass_jobs, item.shortlisted_jobs), (2, 2))

    def test_noisy_repeated_zero_yield_is_drop_and_listing_noise_is_scoped(self):
        for index in (1, 2):
            run_id = f"noise-{index}"
            self.add_run(run_id, f"2026-02-0{index}T00:00:00+00:00")
            self.query(run_id, "jobs", results=15, candidates=0, new=0, rejected=14)
            self.query(run_id, "clean", results=10, new=0, rejected=0)
        by_query = {item.query: item for item in self.commit_analyze()}
        self.assertEqual(by_query["jobs"].recommendation, "DROP")
        self.assertEqual(by_query["jobs"].rejected_noise, 28)
        self.assertEqual(by_query["clean"].rejected_noise, 0)

    def test_high_goto_failure_query_is_drop(self):
        for index in (1, 2):
            run_id = f"goto-{index}"
            self.add_run(run_id, f"2026-03-0{index}T00:00:00+00:00")
            self.query(run_id, "goto query", results=10, candidates=0, new=0,
                       rejected=10, failures=8)
        item = self.commit_analyze()[0]
        self.assertEqual(item.recommendation, "DROP")
        self.assertIn("QUERY_HIGH_RESOLUTION_FAILURE", item.reasons)

    def test_expensive_low_yield_is_review(self):
        self.add_run("slow", "2026-04-01T00:00:00+00:00")
        self.query("slow", "slow query", results=50, duration=500, new=1)
        self.job("slow", "slow query", "PASS")
        item = self.commit_analyze()[0]
        self.assertEqual(item.recommendation, "REVIEW")
        self.assertIn("QUERY_EXPENSIVE", item.reasons)

    def test_small_sample_cannot_drop(self):
        self.add_run("small", "2026-05-01T00:00:00+00:00")
        self.query("small", "small query", results=5, candidates=0, new=0,
                   rejected=5, failures=5)
        item = self.commit_analyze()[0]
        self.assertEqual(item.recommendation, "REVIEW")
        self.assertIn("QUERY_INSUFFICIENT_SAMPLE", item.reasons)

    def test_same_job_secondary_query_does_not_duplicate_pass(self):
        self.add_run("multi", "2026-06-01T00:00:00+00:00")
        self.query("multi", "first", new=1)
        self.query("multi", "second", new=0, known=1)
        job_id = self.job("multi", "first", "PASS")
        self.connection.execute(
            """INSERT INTO workflow_run_job_queries
               VALUES ('multi', ?, 'second', 'KNOWN', 0, ?)""", (job_id, utc_now())
        )
        by_query = {item.query: item for item in self.commit_analyze()}
        self.assertEqual(by_query["first"].pass_jobs, 1)
        self.assertEqual(by_query["second"].pass_jobs, 0)

    def test_high_review_attribution_and_shortlist(self):
        self.add_run("review", "2026-07-01T00:00:00+00:00")
        self.query("review", "review query")
        self.job("review", "review query", "REVIEW", priority="HIGH")
        item = self.commit_analyze()[0]
        self.assertEqual((item.review_jobs, item.high_reviews, item.shortlisted_jobs),
                         (1, 1, 1))

    def test_reject_and_remaining_review_priorities_are_counted(self):
        self.add_run("outcomes", "2026-07-02T00:00:00+00:00")
        self.query("outcomes", "outcome query", new=3)
        self.job("outcomes", "outcome query", "REJECT")
        self.job("outcomes", "outcome query", "REVIEW", priority="MEDIUM")
        self.job("outcomes", "outcome query", "REVIEW", priority="LOW")
        item = self.commit_analyze()[0]
        self.assertEqual(
            (item.reject_jobs, item.review_jobs, item.medium_reviews,
             item.low_reviews, item.shortlisted_jobs),
            (1, 2, 1, 1, 0),
        )

    def test_run_and_recent_scoping(self):
        for index in (1, 2, 3):
            run_id = f"scope-{index}"
            self.add_run(run_id, f"2026-08-0{index}T00:00:00+00:00")
            self.query(run_id, f"q{index}", new=0)
        self.connection.commit()
        self.assertEqual(select_run_ids(self.connection, run_id="scope-1"), ["scope-1"])
        self.assertEqual(select_run_ids(self.connection, recent_runs=2), ["scope-3", "scope-2"])

    def test_category_aggregation_and_unavailable_metrics(self):
        self.add_run("category", "2026-09-01T00:00:00+00:00")
        self.query("category", "q1", category="ROLE_TECH", new=0, results=5)
        self.query("category", "q2", category="ROLE_TECH", new=0, results=7)
        self.connection.execute(
            "UPDATE workflow_run_queries SET rejected_noise=NULL WHERE source_query='q2'"
        )
        category = aggregate_categories(self.commit_analyze())[0]
        self.assertEqual(category.results_inspected, 12)
        self.assertIsNone(category.rejected_noise)
        self.assertIsNone(category.ratio("noise_rate"))

    def test_division_by_zero_is_unavailable(self):
        item = QueryPerformance("q", "GENERIC", 2, results_inspected=0,
                                new_jobs=0, job_candidates=0)
        self.assertIsNone(item.ratio("candidate_yield"))
        self.assertIsNone(item.ratio("results_per_new_job"))

    def test_cli_is_read_only_and_idempotent(self):
        self.add_run("readonly", "2026-10-01T00:00:00+00:00")
        self.query("readonly", "query", new=0)
        self.connection.commit()
        before = self.database.read_bytes()
        with redirect_stdout(StringIO()):
            first_result = main(
                ["--database", str(self.database), "--run-id", "readonly"]
            )
        self.assertEqual(first_result, 0)
        middle = self.database.read_bytes()
        with redirect_stdout(StringIO()):
            second_result = main(
                ["--database", str(self.database), "--run-id", "readonly"]
            )
        self.assertEqual(second_result, 0)
        self.assertEqual(before, middle)
        self.assertEqual(middle, self.database.read_bytes())
        read_only = _readonly_connection(self.database)
        self.addCleanup(read_only.close)
        with self.assertRaises(Exception):
            read_only.execute("UPDATE workflow_runs SET status='FAILED'")
