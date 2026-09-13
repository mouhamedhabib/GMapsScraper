"""Offline regressions for deterministic Job Qualification v1."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from job_search.network import NetworkProtectionRelay
from job_search.providers import ParsedJob
from job_search.qualification import qualify_jobs
from job_search.storage import connect_database, utc_now


class QualificationTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "jobs.db"
        self.connection = connect_database(self.database)
        self.addCleanup(self.connection.close)
        self.no_network = NetworkProtectionRelay(enabled=False)

    def add_job(
        self, *, decision="PASS", reasons=None, status="OPEN", company="Acme",
        source_type="COMPANY_SITE", relationship="DIRECT", provider="generic",
        fetch_status="FETCHED", fetch_error="", apply=True,
        url=None, title="Backend Engineer", location="Tunis, Tunisia",
    ) -> int:
        now = utc_now()
        job_id = self.connection.execute("SELECT COALESCE(MAX(job_id), 0)+1 FROM jobs").fetchone()[0]
        canonical = url or f"https://acme.test/jobs/{job_id}"
        company_id = None
        if company:
            company_id = self.connection.execute(
                """INSERT INTO companies
                   (canonical_name, first_seen_at, last_seen_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""", (company, now, now, now, now)
            ).lastrowid
        self.connection.execute(
            """INSERT INTO jobs
               (job_id, company_id, canonical_url, title, location_text, country,
                region, city, remote_policy, description, first_seen_at,
                last_seen_at, status, content_hash, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'Tunisia', 'AFRICA', 'Tunis', 'ONSITE', ?,
                       ?, ?, ?, 'hash', ?, ?)""",
            (job_id, company_id, canonical, title, location,
             "Backend engineer building APIs. " * 20, now, now, status, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_sources
               (job_id, provider, source_url, apply_url, first_seen_at,
                last_seen_at, last_fetched_at, fetch_status, fetch_error,
                source_type, employer_relationship)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?, ?)""",
            (job_id, provider, canonical, canonical if apply else None, now, now,
             now, fetch_status, fetch_error, source_type, relationship),
        )
        reason_codes = reasons or ["PASS_RELEVANT_ROLE", "PASS_LOCATION_TUNISIA"]
        self.connection.execute(
            """INSERT INTO job_filter_results
               (job_id, policy_version, evaluated_at, status, primary_reason,
                reasons_json, matched_terms_json, detected_remote_policy,
                created_at, updated_at)
               VALUES (?, 'v1.1', ?, ?, ?, ?, '{}', 'ONSITE', ?, ?)""",
            (job_id, now, decision, reason_codes[0], json.dumps([
                {"code": code, "message": code} for code in reason_codes
            ]), now, now),
        )
        self.connection.commit()
        return job_id

    def parsed(self, job_id, *, fetch_status="FETCHED", fetch_error="",
               status="OPEN", structured=True, apply_url=""):
        row = self.connection.execute(
            "SELECT canonical_url FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        result = ParsedJob(row["canonical_url"], "generic")
        result.title = "Backend Engineer"
        result.company_name = "Acme"
        result.location_text = "Tunis, Tunisia"
        result.status = status
        result.fetch_status = fetch_status
        result.fetch_error = fetch_error
        result.has_structured_job_posting = structured
        result.apply_url = apply_url
        result.evidence_sources["company_name"] = "jsonld_hiringOrganization"
        return result

    def qualify(self, job_id, **kwargs):
        return qualify_jobs(
            self.connection, job_ids=[job_id], network_relay=self.no_network,
            **kwargs,
        )

    def test_active_direct_company_posting_is_qualified_without_fetch(self):
        job_id = self.add_job()
        fetcher = Mock()
        result = self.qualify(job_id, fetcher=fetcher)[0]
        self.assertEqual(result.qualification_status, "QUALIFIED")
        self.assertEqual(result.application_channel, "DIRECT_COMPANY")
        fetcher.assert_not_called()

    def test_active_ats_posting_uses_ats_channel_without_assuming_relationship(self):
        job_id = self.add_job(source_type="ATS", relationship="DIRECT", provider="lever")
        result = self.qualify(job_id)[0]
        self.assertEqual((result.qualification_status, result.application_channel),
                         ("QUALIFIED", "ATS"))

    def test_expired_stored_posting_is_disqualified(self):
        job_id = self.add_job(status="CLOSED")
        result = self.qualify(job_id)[0]
        self.assertEqual((result.qualification_status, result.activity_status),
                         ("DISQUALIFIED", "INACTIVE"))

    def test_http_404_and_410_are_inactive(self):
        for code in (404, 410):
            with self.subTest(code=code):
                job_id = self.add_job(status="UNKNOWN", source_type="UNKNOWN",
                                      relationship="UNKNOWN", fetch_status="FAILED",
                                      apply=False)
                parsed = self.parsed(job_id, fetch_status="FAILED",
                                     fetch_error=f"HTTPError: {code} Client Error")
                result = self.qualify(job_id, fetcher=Mock(return_value=parsed))[0]
                self.assertEqual(result.activity_status, "INACTIVE")
                self.assertEqual(result.qualification_status, "DISQUALIFIED")

    def test_403_timeout_and_captcha_are_unknown_not_inactive(self):
        failures = (
            lambda job_id: self.parsed(job_id, fetch_status="FAILED", fetch_error="403 Forbidden"),
            lambda job_id: TimeoutError("timed out"),
            lambda job_id: self.parsed(job_id, fetch_status="FAILED", fetch_error="CAPTCHA verification"),
        )
        for index, failure in enumerate(failures):
            with self.subTest(index=index):
                job_id = self.add_job(status="UNKNOWN", source_type="UNKNOWN",
                                      relationship="UNKNOWN", fetch_status="FAILED",
                                      apply=False)
                value = failure(job_id)
                fetcher = Mock(side_effect=value) if isinstance(value, Exception) else Mock(return_value=value)
                result = self.qualify(job_id, fetcher=fetcher)[0]
                self.assertEqual(result.activity_status, "UNKNOWN")
                self.assertEqual(result.qualification_status, "REVIEW")

    def test_listing_page_is_disqualified(self):
        job_id = self.add_job(
            url="https://example.test/jobs/search-jobs", title="39 open jobs",
        )
        result = self.qualify(job_id)[0]
        self.assertEqual(result.qualification_status, "DISQUALIFIED")
        self.assertIn("DISQUALIFIED_LISTING_PAGE", result.reason_codes)

    def test_recruiter_aggregator_unknown_employer_and_application_channels(self):
        cases = (
            ("UNKNOWN", "RECRUITER", "RECRUITER"),
            ("JOB_PLATFORM", "AGGREGATOR", "JOB_PLATFORM"),
        )
        for source_type, relationship, channel in cases:
            with self.subTest(channel=channel):
                job_id = self.add_job(source_type=source_type, relationship=relationship)
                result = self.qualify(job_id)[0]
                self.assertEqual(result.application_channel, channel)
                self.assertEqual(result.employer_status, "UNKNOWN")
                self.assertEqual(result.qualification_status, "REVIEW")

    def test_unknown_employer_and_missing_application_channel_review(self):
        job_id = self.add_job(
            company="", source_type="UNKNOWN", relationship="UNKNOWN",
            status="UNKNOWN", fetch_status="FAILED", apply=False,
        )
        parsed = self.parsed(job_id, fetch_status="FETCHED", apply_url="")
        parsed.company_name = ""
        parsed.evidence_sources.clear()
        result = self.qualify(job_id, fetcher=Mock(return_value=parsed))[0]
        self.assertEqual(result.employer_status, "UNKNOWN")
        self.assertEqual(result.application_channel, "UNKNOWN")
        self.assertIn("REVIEW_APPLICATION_CHANNEL_UNKNOWN", result.reason_codes)

    def test_run_and_observation_status_scoping(self):
        new_job = self.add_job()
        known_job = self.add_job()
        updated_job = self.add_job()
        now = utc_now()
        self.connection.execute(
            """INSERT INTO workflow_runs
               (run_id, started_at, finished_at, status, mode, maps_enabled,
                job_discovery_enabled, completion_enabled, filter_enabled,
                priority_enabled, created_at)
               VALUES ('scope', ?, ?, 'SUCCESS', 'DAILY', 0, 1, 1, 1, 1, ?)""",
            (now, now, now),
        )
        self.connection.executemany(
            "INSERT INTO workflow_run_jobs VALUES ('scope', ?, ?, ?)",
            ((new_job, "NEW", now), (known_job, "KNOWN", now),
             (updated_job, "UPDATED", now)),
        )
        self.connection.commit()
        results = qualify_jobs(
            self.connection, run_id="scope", observation_status="NEW",
            network_relay=self.no_network,
        )
        self.assertEqual([item.job_id for item in results], [new_job])
        known = qualify_jobs(
            self.connection, run_id="scope", observation_status="KNOWN",
            network_relay=self.no_network,
        )
        self.assertEqual([item.job_id for item in known], [known_job])
        updated = qualify_jobs(
            self.connection, run_id="scope", observation_status="UPDATED",
            network_relay=self.no_network,
        )
        self.assertEqual([item.job_id for item in updated], [updated_job])

    def test_medium_excluded_then_included_without_reinterpreting_experience(self):
        job_id = self.add_job(
            decision="REVIEW", reasons=[
                "REVIEW_EXPERIENCE_3_YEARS", "REVIEW_LOCATION_PREFERRED_MARKET",
                "PASS_RELEVANT_ROLE", "PASS_FRESH_JOB",
            ], source_type="ATS", relationship="UNKNOWN", provider="generic",
            location="CH",
        )
        self.assertEqual(self.qualify(job_id), [])
        result = self.qualify(job_id, include_medium=True)[0]
        self.assertEqual(result.qualification_status, "REVIEW")
        self.assertIn("REVIEW_EXPERIENCE_3_YEARS",
                      result.evidence["filter_reason_codes"])
        self.assertNotIn("DISQUALIFIED", " ".join(result.reason_codes))

    def test_reject_jobs_are_never_selected(self):
        job_id = self.add_job(decision="REJECT", reasons=["REJECT_SENIORITY"])
        self.assertEqual(self.qualify(job_id, include_medium=True), [])

    def test_idempotence_and_provenance_preservation(self):
        job_id = self.add_job()
        first = self.qualify(job_id)[0]
        stored_before = dict(self.connection.execute(
            "SELECT * FROM job_qualifications WHERE job_id=?", (job_id,)
        ).fetchone())
        second = self.qualify(job_id, fetcher=Mock(side_effect=AssertionError))[0]
        stored_after = dict(self.connection.execute(
            "SELECT * FROM job_qualifications WHERE job_id=?", (job_id,)
        ).fetchone())
        self.assertEqual(stored_before, stored_after)
        self.assertTrue(second.reused)
        self.assertEqual(first.evidence["selected_source"]["source_url"],
                         f"https://acme.test/jobs/{job_id}")
        self.assertEqual(first.evidence["sources"][0]["job_source_id"], job_id)

    def test_network_relay_wraps_the_single_verification_fetch(self):
        job_id = self.add_job(status="UNKNOWN", source_type="UNKNOWN",
                              relationship="UNKNOWN", fetch_status="FAILED", apply=False)
        parsed = self.parsed(job_id, fetch_status="FAILED", fetch_error="403")
        relay = Mock()
        relay.protect.side_effect = lambda operation, **_: operation()
        fetcher = Mock(return_value=parsed)
        qualify_jobs(self.connection, job_ids=[job_id], fetcher=fetcher,
                     network_relay=relay)
        relay.protect.assert_called_once()
        fetcher.assert_called_once()

    def test_browser_fallback_is_explicit_and_reuses_one_driver(self):
        job_id = self.add_job(status="UNKNOWN", source_type="UNKNOWN",
                              relationship="UNKNOWN", fetch_status="FAILED", apply=False)
        failed = self.parsed(job_id, fetch_status="FAILED", fetch_error="403")
        rendered = self.parsed(job_id, fetch_status="FETCHED",
                               apply_url="https://acme.test/apply/1")
        driver = Mock()
        result = self.qualify(
            job_id, fetcher=Mock(return_value=failed), browser_fallback=True,
            driver_factory=Mock(return_value=driver),
            browser_fetcher=Mock(return_value=rendered),
        )[0]
        self.assertEqual(result.activity_status, "ACTIVE")
        driver.quit.assert_called_once()

    def test_application_channel_precedence_prefers_direct_company(self):
        job_id = self.add_job(source_type="JOB_PLATFORM", relationship="AGGREGATOR")
        now = utc_now()
        self.connection.execute(
            """INSERT INTO job_sources
               (job_id, provider, source_url, apply_url, first_seen_at, last_seen_at,
                fetch_status, source_type, employer_relationship)
               VALUES (?, 'lever', 'https://jobs.lever.co/acme/1',
                       'https://jobs.lever.co/acme/1', ?, ?, 'FETCHED', 'ATS', 'UNKNOWN')""",
            (job_id, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_sources
               (job_id, provider, source_url, apply_url, first_seen_at, last_seen_at,
                fetch_status, source_type, employer_relationship)
               VALUES (?, 'generic', 'https://company.test/jobs/1',
                       'https://company.test/apply/1', ?, ?, 'FETCHED',
                       'COMPANY_SITE', 'DIRECT')""",
            (job_id, now, now),
        )
        self.connection.commit()
        result = self.qualify(job_id)[0]
        self.assertEqual(result.application_channel, "DIRECT_COMPANY")
        self.assertEqual(result.application_url, "https://company.test/apply/1")
