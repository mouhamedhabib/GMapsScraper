"""Offline regressions for deterministic job activity resolution."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from job_search.activity_resolution import (
    ActivityResolution, AuthoritativeSource, choose_authoritative_source,
    evaluate_fetched_activity, resolve_activity,
)
from job_search.network import NetworkProtectionRelay
from job_search.providers import ParsedJob
from job_search.qualification import qualify_jobs
from job_search.storage import connect_database, utc_now


def source(provider="greenhouse", url="https://boards.greenhouse.io/acme/jobs/123"):
    return AuthoritativeSource(
        url=url, source_type="ATS", provider=provider, job_source_id=1,
        is_source_url=True, source_job_id="123", fetch_status="",
        fetch_error="", raw_content_hash="",
    )


class ActivityRuleTests(TestCase):
    def parsed(self, provider, url):
        result = ParsedJob(url, provider, "123")
        result.fetch_status = "FETCHED"
        result.title = "Backend Engineer"
        result.description = "Build and maintain production services."
        return result

    def test_supported_ats_individual_postings_are_active(self):
        cases = (
            ("greenhouse", "https://boards.greenhouse.io/acme/jobs/123"),
            ("lever", "https://jobs.lever.co/acme/123"),
            ("ashby", "https://jobs.ashbyhq.com/acme/123"),
        )
        for provider, url in cases:
            with self.subTest(provider=provider):
                parsed = self.parsed(provider, url)
                result = evaluate_fetched_activity(1, source(provider, url), parsed)
                self.assertEqual(result.activity_status, "ACTIVE")

    def test_explicit_closed_posting_is_inactive(self):
        selected = source()
        parsed = self.parsed(selected.provider, selected.url)
        parsed.description = "This position has been filled."
        result = evaluate_fetched_activity(1, selected, parsed)
        self.assertEqual(result.activity_status, "INACTIVE")

    def test_authoritative_404_and_410_are_inactive(self):
        for code in (404, 410):
            with self.subTest(code=code):
                parsed = ParsedJob(source().url, "greenhouse", "123")
                parsed.fetch_status = "FAILED"
                parsed.fetch_error = f"HTTPError: {code} Client Error"
                result = evaluate_fetched_activity(1, source(), parsed)
                self.assertEqual((result.activity_status, result.http_status), ("INACTIVE", code))

    def test_temporary_and_protected_failures_are_unknown(self):
        failures = (
            "403 Forbidden", "request timed out", "DNS resolution failed",
            "connection reset", "503 Service Unavailable", "CAPTCHA challenge",
        )
        for error in failures:
            with self.subTest(error=error):
                parsed = ParsedJob(source().url, "greenhouse", "123")
                parsed.fetch_status, parsed.fetch_error = "FAILED", error
                self.assertEqual(
                    evaluate_fetched_activity(1, source(), parsed).activity_status,
                    "UNKNOWN",
                )

    def test_platform_source_with_persisted_official_ats_apply_url_uses_ats(self):
        rows = [{
            "job_source_id": 1, "provider": "generic", "source_job_id": "",
            "source_url": "https://example-platform.test/jobs/acme-1",
            "apply_url": "https://jobs.lever.co/acme/abc-123",
            "fetch_status": "FETCHED", "fetch_error": "", "raw_content_hash": "x",
            "source_type": "JOB_PLATFORM", "employer_relationship": "AGGREGATOR",
        }]
        selected = choose_authoritative_source(rows)
        self.assertEqual((selected.source_type, selected.provider, selected.url),
                         ("ATS", "lever", rows[0]["apply_url"]))

    def test_company_homepage_does_not_hide_official_individual_ats_source(self):
        rows = [
            {
                "job_source_id": 1, "provider": "generic", "source_job_id": "",
                "source_url": "https://company.test/", "apply_url": "",
                "fetch_status": "FETCHED", "fetch_error": "", "raw_content_hash": "x",
                "source_type": "COMPANY_SITE", "employer_relationship": "DIRECT",
            },
            {
                "job_source_id": 2, "provider": "greenhouse", "source_job_id": "456",
                "source_url": "https://boards.greenhouse.io/company/jobs/456", "apply_url": "",
                "fetch_status": "FETCHED", "fetch_error": "", "raw_content_hash": "y",
                "source_type": "ATS", "employer_relationship": "DIRECT",
            },
        ]
        selected = choose_authoritative_source(rows)
        self.assertEqual((selected.source_type, selected.provider), ("ATS", "greenhouse"))

    def test_loaded_listing_page_is_not_active(self):
        selected = AuthoritativeSource(
            "https://company.test/jobs", "DIRECT_COMPANY", "generic", 1,
            True, "", "", "", "",
        )
        parsed = ParsedJob(selected.url, "generic")
        parsed.fetch_status = "FETCHED"
        parsed.title = "Open jobs"
        parsed.description = "Browse all current opportunities."
        self.assertEqual(
            evaluate_fetched_activity(1, selected, parsed).activity_status,
            "UNKNOWN",
        )


class ActivityResolverTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = connect_database(Path(self.temp.name) / "jobs.db")
        self.addCleanup(self.connection.close)
        self.relay = NetworkProtectionRelay(enabled=False)

    def add_job(
        self, *, provider="greenhouse", url=None, job_status="UNKNOWN",
        fetch_status="FAILED", fetch_error="", relationship="DIRECT",
        source_type="ATS", source_job_id="123", description="Stored full job description.",
    ):
        now = utc_now()
        job_id = self.connection.execute("SELECT COALESCE(MAX(job_id),0)+1 FROM jobs").fetchone()[0]
        effective_source_job_id = f"{source_job_id}-{job_id}"
        url = url or f"https://boards.greenhouse.io/acme/jobs/{effective_source_job_id}"
        company_id = self.connection.execute(
            """INSERT INTO companies (canonical_name,first_seen_at,last_seen_at,created_at,updated_at)
               VALUES ('Acme',?,?,?,?)""", (now, now, now, now),
        ).lastrowid
        self.connection.execute(
            """INSERT INTO jobs
               (job_id,company_id,canonical_url,title,location_text,country,city,remote_policy,
                description,first_seen_at,last_seen_at,status,content_hash,created_at,updated_at)
               VALUES (?,?,?,'Backend Engineer','Tunis, Tunisia','Tunisia','Tunis','ONSITE',
                       ?,?,?,?,'content-hash',?,?)""",
            (job_id, company_id, url, description, now, now, job_status, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_sources
               (job_id,provider,source_job_id,source_url,apply_url,first_seen_at,last_seen_at,
                last_fetched_at,fetch_status,fetch_error,raw_content_hash,source_type,employer_relationship)
               VALUES (?,?,?,?,?,?,?,?,?,NULLIF(?,''),'raw-hash',?,?)""",
            (job_id, provider, effective_source_job_id, url, url, now, now, now,
             fetch_status, fetch_error, source_type, relationship),
        )
        reasons = [{"code": "PASS_RELEVANT_ROLE"}, {"code": "PASS_LOCATION_TUNISIA"}]
        self.connection.execute(
            """INSERT INTO job_filter_results
               (job_id,policy_version,evaluated_at,status,primary_reason,reasons_json,
                matched_terms_json,detected_remote_policy,created_at,updated_at)
               VALUES (?,'v1.1',?,'PASS','PASS_RELEVANT_ROLE',?,'{}','ONSITE',?,?)""",
            (job_id, now, json.dumps(reasons), now, now),
        )
        self.connection.commit()
        return job_id

    def fetched(self, job_id, *, status="FETCHED", error="", closed=False):
        row = self.connection.execute("SELECT canonical_url FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        parsed = ParsedJob(row["canonical_url"], "greenhouse", "123")
        parsed.fetch_status, parsed.fetch_error = status, error
        parsed.title = "Backend Engineer"
        parsed.description = "This position has been filled." if closed else "Build production services."
        return parsed

    def test_persisted_authoritative_ats_fetch_is_active_without_network(self):
        job_id = self.add_job(fetch_status="FETCHED")
        fetcher = Mock()
        result = resolve_activity(self.connection, [job_id], fetcher=fetcher, network_relay=self.relay)[0]
        self.assertEqual((result.activity_status, result.evidence_method),
                         ("ACTIVE", "PERSISTED_AUTHORITATIVE_INDIVIDUAL_FETCH"))
        fetcher.assert_not_called()

    def test_persisted_authoritative_http_gone_is_inactive(self):
        for code in (404, 410):
            with self.subTest(code=code):
                job_id = self.add_job(fetch_error=f"HTTPError: {code} Client Error")
                result = resolve_activity(self.connection, [job_id], network_relay=self.relay)[0]
                self.assertEqual(result.activity_status, "INACTIVE")

    def test_persisted_explicit_closed_message_is_inactive(self):
        job_id = self.add_job(
            fetch_status="FETCHED", description="This job is no longer available."
        )
        result = resolve_activity(self.connection, [job_id], network_relay=self.relay)[0]
        self.assertEqual((result.activity_status, result.evidence_method),
                         ("INACTIVE", "PERSISTED_EXPLICIT_CLOSED_STATE"))

    def test_network_relay_wraps_one_fetch(self):
        job_id = self.add_job()
        relay = Mock()
        relay.protect.side_effect = lambda callback, context: callback()
        fetcher = Mock(return_value=self.fetched(job_id))
        result = resolve_activity(self.connection, [job_id], fetcher=fetcher, network_relay=relay)[0]
        self.assertEqual(result.activity_status, "ACTIVE")
        self.assertEqual((relay.protect.call_count, fetcher.call_count), (1, 1))

    def test_browser_fallback_is_opt_in(self):
        job_id = self.add_job()
        driver = Mock()
        browser_fetcher = Mock(return_value=self.fetched(job_id))
        result = resolve_activity(
            self.connection, [job_id], fetcher=Mock(side_effect=TimeoutError("timed out")),
            network_relay=self.relay, browser_fallback=True,
            driver_factory=Mock(return_value=driver), browser_fetcher=browser_fetcher,
        )[0]
        self.assertEqual((result.activity_status, result.fetch_status), ("ACTIVE", "BROWSER_FETCHED"))
        driver.quit.assert_called_once()

    def test_idempotent_rerun_preserves_provenance(self):
        job_id = self.add_job(fetch_status="FETCHED")
        first = resolve_activity(self.connection, [job_id], network_relay=self.relay)[0]
        second = resolve_activity(self.connection, [job_id], network_relay=self.relay)[0]
        self.assertTrue(second.reused)
        self.assertEqual((first.checked_at, first.evidence), (second.checked_at, second.evidence))
        self.assertEqual(
            set(("job_id", "activity_status", "authoritative_url", "source_type",
                 "provider", "http_status", "evidence_method", "raw_evidence_summary",
                 "checked_at")) - set(first.evidence), set(),
        )

    def test_qualification_consumes_active_and_inactive(self):
        active_id = self.add_job()
        active_before = qualify_jobs(
            self.connection, job_ids=[active_id], fetcher=Mock(return_value=self.fetched(active_id, status="FAILED", error="timeout")),
            network_relay=self.relay,
        )[0]
        self.assertEqual(active_before.activity_status, "UNKNOWN")
        self.connection.execute("UPDATE job_sources SET fetch_status='FETCHED',fetch_error=NULL WHERE job_id=?", (active_id,))
        self.connection.commit()
        resolve_activity(self.connection, [active_id], network_relay=self.relay)
        active_after = qualify_jobs(self.connection, job_ids=[active_id], network_relay=self.relay)[0]
        self.assertEqual((active_after.activity_status, active_after.qualification_status), ("ACTIVE", "QUALIFIED"))

        inactive_id = self.add_job(fetch_error="HTTPError: 410 Client Error")
        resolve_activity(self.connection, [inactive_id], network_relay=self.relay)
        inactive = qualify_jobs(self.connection, job_ids=[inactive_id], network_relay=self.relay)[0]
        self.assertEqual((inactive.activity_status, inactive.qualification_status), ("INACTIVE", "DISQUALIFIED"))
        self.assertIn("DISQUALIFIED_POSTING_INACTIVE", inactive.reason_codes)

    def test_open_active_job_remains_qualified(self):
        job_id = self.add_job(job_status="OPEN", fetch_status="FETCHED")
        before = qualify_jobs(self.connection, job_ids=[job_id], network_relay=self.relay)[0]
        resolve_activity(self.connection, [job_id], network_relay=self.relay)
        after = qualify_jobs(self.connection, job_ids=[job_id], network_relay=self.relay)[0]
        self.assertEqual((before.qualification_status, after.qualification_status),
                         ("QUALIFIED", "QUALIFIED"))

    def test_run_id_scopes_activity_resolution(self):
        selected = self.add_job(fetch_status="FETCHED")
        other = self.add_job(fetch_status="FETCHED")
        now = utc_now()
        self.connection.execute(
            """INSERT INTO workflow_runs
               (run_id,started_at,status,mode,maps_enabled,job_discovery_enabled,
                completion_enabled,filter_enabled,priority_enabled,created_at)
               VALUES ('activity-run',?,'RUNNING','test',0,1,0,1,0,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO workflow_run_jobs
               (run_id,job_id,discovery_state,created_at)
               VALUES ('activity-run',?,'NEW',?)""", (selected, now),
        )
        self.connection.commit()
        results = resolve_activity(
            self.connection, run_id="activity-run", network_relay=self.relay,
        )
        self.assertEqual([result.job_id for result in results], [selected])
        self.assertNotEqual(selected, other)
