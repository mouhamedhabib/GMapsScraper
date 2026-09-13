"""Offline tests for deterministic REVIEW prioritization."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from job_search.review_priority import (
    load_review_priorities, main, priority_sort_key,
)
from job_search.storage import connect_database


def reason(code):
    return {"code": code, "message": code}


class ReviewPriorityTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.database = Path(self.temporary.name) / "jobs.db"
        self.connection = connect_database(self.database)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def add_job(
        self, job_id, *, status="REVIEW", title="Backend Developer",
        company="Example", location="Paris, France",
        description="Detailed role information. " * 20,
        published_at="2026-09-03", reasons=("PASS_RELEVANT_ROLE",),
        provider="lever", source_type="ATS", relationship="DIRECT",
    ):
        now = "2026-09-11T00:00:00+00:00"
        company_id = None
        if company:
            cursor = self.connection.execute(
                """INSERT INTO companies
                   (canonical_name, first_seen_at, last_seen_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (company, now, now, now, now),
            )
            company_id = cursor.lastrowid
        self.connection.execute(
            """INSERT INTO jobs
               (job_id, company_id, canonical_url, title, location_text,
                description, published_at, first_seen_at, last_seen_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, company_id, f"https://example.com/jobs/{job_id}", title,
             location, description, published_at, now, now, now, now),
        )
        if provider or source_type or relationship:
            self.connection.execute(
                """INSERT INTO job_sources
                   (job_id, provider, source_url, first_seen_at, last_seen_at,
                    source_type, employer_relationship)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, provider or "unknown", f"https://source.test/{job_id}",
                 now, now, source_type or "UNKNOWN", relationship or "UNKNOWN"),
            )
        payload = json.dumps([reason(code) for code in reasons])
        self.connection.execute(
            """INSERT INTO job_filter_results
               (job_id, policy_version, evaluated_at, status, primary_reason,
                reasons_json, matched_terms_json, created_at, updated_at)
               VALUES (?, 'v1.1', ?, ?, ?, ?, '{}', ?, ?)""",
            (job_id, now, status, reasons[0], payload, now, now),
        )
        self.connection.commit()

    def priorities(self):
        return load_review_priorities(self.connection, "v1.1")

    def add_run(self, run_id, memberships):
        now = "2026-09-13T07:00:33+00:00"
        self.connection.execute(
            """INSERT INTO workflow_runs
               (run_id, started_at, finished_at, status, mode, maps_enabled,
                job_discovery_enabled, completion_enabled, filter_enabled,
                priority_enabled, created_at)
               VALUES (?, ?, ?, 'SUCCESS', 'JOBS_ONLY', 0, 1, 1, 1, 1, ?)""",
            (run_id, now, now, now),
        )
        self.connection.executemany(
            """INSERT INTO workflow_run_jobs
               (run_id, job_id, discovery_state, created_at)
               VALUES (?, ?, ?, ?)""",
            [(run_id, job_id, state, now) for job_id, state in memberships],
        )
        self.connection.commit()

    def run_cli(self, *arguments):
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main([
                "--database", str(self.database), "--policy-version", "v1.1",
                *arguments,
            ])
        self.assertEqual(exit_code, 0)
        return output.getvalue()

    def test_job_84_shaped_case_is_high(self):
        self.add_job(84, title="Java Developer (Infrastructure)", company="Ajax Systems",
                     location="Kyiv", reasons=("REVIEW_EXPERIENCE_3_YEARS",
                     "PASS_RELEVANT_ROLE", "PASS_FRESH_JOB"), relationship="UNKNOWN")
        result = self.priorities()[0]
        self.assertEqual(result.priority, "HIGH")
        self.assertEqual(result.review_priority, "HIGH")
        self.assertIn("PRIORITY_SINGLE_MODERATE_GAP", result.priority_reasons)
        self.assertEqual(result.priority_reasons, result.review_priority_reasons)
        self.assertIn("PRIORITY_ATS_SOURCE", result.priority_reasons)

    def test_job_83_shaped_case_is_medium(self):
        self.add_job(83, title="Jobgether - AWS Back-end Developer",
                     company="", location="", description="", published_at="",
                     reasons=("REVIEW_LOCATION_UNKNOWN", "REVIEW_DESCRIPTION_MISSING",
                              "PASS_RELEVANT_ROLE"), relationship="RECRUITER")
        result = self.priorities()[0]
        self.assertEqual(result.priority, "MEDIUM")
        self.assertIn("PRIORITY_RECRUITER_SOURCE", result.priority_reasons)
        self.assertIn("PRIORITY_MULTIPLE_UNKNOWNS", result.priority_reasons)

    def test_role_unclear_with_missing_fields_is_low(self):
        self.add_job(1, title="Opportunity", company="", location="", description="",
                     published_at="", reasons=("REVIEW_ROLE_UNCLEAR",
                     "REVIEW_LOCATION_UNKNOWN", "REVIEW_DESCRIPTION_MISSING"),
                     provider="", source_type="", relationship="")
        result = self.priorities()[0]
        self.assertEqual(result.priority, "LOW")
        self.assertIn("PRIORITY_ROLE_UNCLEAR", result.priority_reasons)
        self.assertIn("PRIORITY_WEAK_SOURCE", result.priority_reasons)

    def test_three_independent_review_uncertainties_are_low(self):
        self.add_job(7, location="", description="", reasons=(
            "REVIEW_LOCATION_UNKNOWN", "REVIEW_DESCRIPTION_MISSING",
            "REVIEW_EXPERIENCE_3_YEARS", "PASS_RELEVANT_ROLE"),
            relationship="DIRECT")
        result = self.priorities()[0]
        self.assertEqual(result.priority, "LOW")
        self.assertIn("PRIORITY_MULTIPLE_UNKNOWNS", result.priority_reasons)

    def test_fresh_relevant_single_location_review_is_high(self):
        self.add_job(2, reasons=("REVIEW_LOCATION_EUROPE", "PASS_RELEVANT_ROLE",
                                "PASS_FRESH_JOB"), source_type="COMPANY_SITE")
        self.assertEqual(self.priorities()[0].priority, "HIGH")

    def test_source_type_alone_is_not_decisive(self):
        self.add_job(3, location="", description="", reasons=(
            "REVIEW_LOCATION_UNKNOWN", "REVIEW_DESCRIPTION_MISSING",
            "PASS_RELEVANT_ROLE"), source_type="ATS", relationship="RECRUITER")
        self.add_job(4, reasons=("REVIEW_LOCATION_EUROPE", "PASS_RELEVANT_ROLE",
                                "PASS_FRESH_JOB"), provider="board",
                     source_type="JOB_PLATFORM", relationship="UNKNOWN")
        by_id = {item.job_id: item for item in self.priorities()}
        self.assertEqual(by_id[3].priority, "MEDIUM")
        self.assertNotEqual(by_id[3].priority, "LOW")
        self.assertEqual(by_id[4].priority, "MEDIUM")
        self.assertNotEqual(by_id[4].priority, "LOW")

    def test_pass_and_reject_jobs_are_not_prioritized(self):
        self.add_job(5, status="PASS")
        self.add_job(6, status="REJECT", reasons=("REJECT_STALE_JOB",))
        self.assertEqual(self.priorities(), [])

    def test_ordering_and_rerun_are_deterministic(self):
        common = ("REVIEW_LOCATION_EUROPE", "PASS_RELEVANT_ROLE", "PASS_FRESH_JOB")
        self.add_job(12, reasons=common, published_at="2026-09-01",
                     source_type="JOB_PLATFORM", relationship="UNKNOWN")
        self.add_job(11, reasons=common, published_at="2026-09-03")
        self.add_job(10, reasons=common, published_at="2026-09-03")
        first = self.priorities()
        second = self.priorities()
        self.assertEqual(first, second)
        self.assertEqual([item.job_id for item in first], [10, 11, 12])

        base = first[0]
        self.assertLess(
            priority_sort_key(replace(base, fresh=True)),
            priority_sort_key(replace(base, fresh=False)),
        )
        self.assertLess(
            priority_sort_key(replace(base, review_reason_count=1)),
            priority_sort_key(replace(base, review_reason_count=2)),
        )
        self.assertLess(
            priority_sort_key(replace(base, source_strength=4)),
            priority_sort_key(replace(base, source_strength=1)),
        )
        self.assertLess(
            priority_sort_key(replace(base, published_at="2026-09-03")),
            priority_sort_key(replace(base, published_at="2026-09-01")),
        )
        self.assertLess(
            priority_sort_key(replace(base, job_id=10)),
            priority_sort_key(replace(base, job_id=11)),
        )

    def test_no_run_id_preserves_database_wide_behavior(self):
        self.add_job(20)
        self.add_job(21)
        self.add_run("run-one", [(20, "NEW")])

        self.assertEqual(
            [item.job_id for item in load_review_priorities(self.connection, "v1.1")],
            [20, 21],
        )

    def test_run_id_isolates_exact_run_membership(self):
        for job_id in (30, 31, 32):
            self.add_job(job_id)
        self.add_run("run-one", [(30, "NEW")])
        self.add_run("run-two", [(31, "KNOWN")])

        results = load_review_priorities(
            self.connection, "v1.1", run_id="run-one"
        )

        self.assertEqual([item.job_id for item in results], [30])

    def assert_status_filter(self, status, expected_job_id):
        for job_id in (40, 41, 42):
            self.add_job(job_id)
        self.add_run(
            "status-run", [(40, "NEW"), (41, "UPDATED"), (42, "KNOWN")]
        )

        results = load_review_priorities(
            self.connection, "v1.1", run_id="status-run",
            observation_status=status,
        )
        output = self.run_cli(
            "--run-id", "status-run", "--observation-status", status
        )

        self.assertEqual([item.job_id for item in results], [expected_job_id])
        self.assertEqual(
            [line for line in output.splitlines() if line.startswith("job_id:")],
            [f"job_id: {expected_job_id}"],
        )

    def test_new_observation_status_filter(self):
        self.assert_status_filter("NEW", 40)

    def test_updated_observation_status_filter(self):
        self.assert_status_filter("UPDATED", 41)

    def test_known_observation_status_filter(self):
        self.assert_status_filter("KNOWN", 42)

    def test_priority_display_flags_work_with_run_scope(self):
        self.add_job(
            50, reasons=("REVIEW_LOCATION_EUROPE", "PASS_RELEVANT_ROLE",
                         "PASS_FRESH_JOB"),
        )
        self.add_job(
            51, location="", description="", relationship="RECRUITER",
            reasons=("REVIEW_LOCATION_UNKNOWN", "REVIEW_DESCRIPTION_MISSING",
                     "PASS_RELEVANT_ROLE"),
        )
        self.add_job(
            52, location="", description="", provider="", source_type="",
            relationship="", reasons=("REVIEW_ROLE_UNCLEAR",
                                       "REVIEW_LOCATION_UNKNOWN",
                                       "REVIEW_DESCRIPTION_MISSING"),
        )
        self.add_run(
            "priority-run", [(50, "NEW"), (51, "UPDATED"), (52, "KNOWN")]
        )

        for flag, expected_job_id in (
            ("--show-high", 50),
            ("--show-medium", 51),
            ("--show-low", 52),
        ):
            with self.subTest(flag=flag):
                output = self.run_cli("--run-id", "priority-run", flag)
                self.assertIn("HIGH: 1\nMEDIUM: 1\nLOW: 1", output)
                self.assertEqual(
                    [line for line in output.splitlines() if line.startswith("job_id:")],
                    [f"job_id: {expected_job_id}"],
                )

    def test_nonexistent_run_id_fails_clearly(self):
        errors = StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            main([
                "--database", str(self.database), "--policy-version", "v1.1",
                "--run-id", "missing-run",
            ])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("workflow run not found: missing-run", errors.getvalue())

    def test_repeated_run_scoped_execution_is_read_only_and_idempotent(self):
        self.add_job(60)
        self.add_run("repeat-run", [(60, "NEW")])
        before = "\n".join(self.connection.iterdump())

        first = self.run_cli("--run-id", "repeat-run", "--show-high")
        second = self.run_cli("--run-id", "repeat-run", "--show-high")
        after = "\n".join(self.connection.iterdump())

        self.assertEqual(first, second)
        self.assertEqual(before, after)
