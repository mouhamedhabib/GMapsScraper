"""Offline regressions for deterministic application destination resolution."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from job_search.application_destination import (
    evaluate_application_destination, resolve_application_destinations,
)
from job_search.network import NetworkProtectionRelay
from job_search.providers import ParsedJob, fetch_job
from job_search.qualification import qualify_jobs
from job_search.storage import connect_database, utc_now


def job_row(title="Backend Engineer", description="Build production APIs."):
    return {
        "job_id": 1, "canonical_url": "https://acme.test/jobs/123-backend-engineer",
        "title": title, "description": description, "content_hash": "hash",
        "updated_at": "2026-01-01",
    }


def source(url, *, source_id=1, source_type="ATS", relationship="DIRECT",
           apply_url="", fetch_status="FETCHED"):
    from job_search.providers import detect_provider, extract_source_job_id
    provider = detect_provider(url)
    return {
        "job_source_id": source_id, "provider": provider,
        "source_job_id": extract_source_job_id(provider, url),
        "source_url": url, "apply_url": apply_url,
        "fetch_status": fetch_status, "fetch_error": "", "last_fetched_at": "",
        "raw_content_hash": "raw", "source_type": source_type,
        "employer_relationship": relationship,
    }


class DestinationRuleTests(TestCase):
    def test_direct_company_individual_application_destination(self):
        item = source(
            "https://acme.test/careers/backend-engineer",
            source_type="COMPANY_SITE", relationship="DIRECT",
            apply_url="https://acme.test/apply/backend-engineer",
        )
        result = evaluate_application_destination(job_row(), [item])
        self.assertEqual(
            (result.application_status, result.application_channel, result.application_url),
            ("CONFIRMED", "DIRECT_COMPANY", item["apply_url"]),
        )

    def test_all_supported_ats_individual_postings(self):
        urls = {
            "greenhouse": "https://job-boards.greenhouse.io/acme/jobs/123",
            "lever": "https://jobs.lever.co/acme/123",
            "ashby": "https://jobs.ashbyhq.com/acme/123",
            "workable": "https://apply.workable.com/acme/j/ABC123/",
            "smartrecruiters": "https://jobs.smartrecruiters.com/Acme/743999-engineer",
            "teamtailor": "https://acme.teamtailor.com/jobs/123-engineer",
        }
        for provider, url in urls.items():
            with self.subTest(provider=provider):
                result = evaluate_application_destination(job_row(), [source(url)])
                self.assertEqual((result.application_status, result.application_channel),
                                 ("CONFIRMED", "ATS"))

    def test_recruiter_destination_does_not_resolve_employer(self):
        item = source(
            "https://jobs.lever.co/jobgether/123", relationship="RECRUITER",
        )
        result = evaluate_application_destination(job_row(), [item])
        self.assertEqual(result.application_channel, "RECRUITER")
        self.assertEqual(result.employer_relationship, "RECRUITER")
        self.assertNotIn("employer_status", result.__dataclass_fields__)

    def test_platform_individual_and_generic_pages(self):
        accepted = source(
            "https://indeed.com/viewjob/12345", source_type="JOB_PLATFORM",
            relationship="AGGREGATOR",
        )
        # Use a canonical /job/<id> individual shape supported by the generic classifier.
        accepted["source_url"] = "https://platform.test/job/12345/backend-engineer"
        result = evaluate_application_destination(job_row(), [accepted])
        self.assertEqual((result.application_status, result.application_channel),
                         ("CONFIRMED", "JOB_PLATFORM"))

        rejected_urls = (
            "https://company.test/careers",
            "https://platform.test/jobs/software-engineer",
            "https://platform.test/search/jobs",
        )
        for url in rejected_urls:
            with self.subTest(url=url):
                item = source(url, source_type="JOB_PLATFORM", relationship="AGGREGATOR")
                self.assertEqual(evaluate_application_destination(job_row(), [item]).application_status,
                                 "UNKNOWN")

    def test_precedence_direct_then_ats_then_platform(self):
        platform = source(
            "https://platform.test/job/123/backend", source_id=1,
            source_type="JOB_PLATFORM", relationship="AGGREGATOR",
        )
        ats = source("https://jobs.lever.co/acme/123", source_id=2)
        direct = source(
            "https://acme.test/careers/backend", source_id=3,
            source_type="COMPANY_SITE", relationship="DIRECT",
            apply_url="https://acme.test/apply/backend",
        )
        self.assertEqual(
            evaluate_application_destination(job_row(), [platform, ats]).application_channel,
            "ATS",
        )
        self.assertEqual(
            evaluate_application_destination(job_row(), [platform, ats, direct]).application_channel,
            "DIRECT_COMPANY",
        )

    def test_tracking_removed_and_identity_parameters_preserved(self):
        item = source("https://jobs.lever.co/acme/123?utm_source=x&token=identity&ref=feed")
        result = evaluate_application_destination(job_row(), [item])
        self.assertEqual(result.application_url, "https://jobs.lever.co/acme/123?token=identity")

    def test_redirect_to_ats_uses_final_authoritative_surface(self):
        item = source(
            "https://platform.test/search/jobs", source_type="JOB_PLATFORM",
            relationship="AGGREGATOR", fetch_status="FAILED",
        )
        parsed = ParsedJob("https://jobs.lever.co/acme/redirected-id", "lever", "redirected-id")
        parsed.fetch_status = "FETCHED"
        parsed.title = "Backend Engineer"
        result = evaluate_application_destination(
            job_row(), [item], parsed, fetch_result="FETCHED",
        )
        self.assertEqual(
            (result.application_status, result.application_channel, result.application_url),
            ("CONFIRMED", "ATS", parsed.canonical_url),
        )

    def test_http_fetch_preserves_normal_redirect_destination(self):
        response = Mock()
        response.url = "https://jobs.lever.co/acme/redirected-id?utm_source=platform"
        response.text = "<main><h1>Backend Engineer</h1></main>"
        session = Mock()
        session.get.return_value = response
        parsed = fetch_job("https://platform.test/job/123", session=session)
        self.assertEqual(parsed.canonical_url, "https://jobs.lever.co/acme/redirected-id")
        self.assertEqual(parsed.provider, "lever")

    def test_conflicting_equal_strength_destinations(self):
        first = source("https://jobs.lever.co/acme/one", source_id=1,
                       apply_url="https://jobs.lever.co/acme/one")
        second = source("https://jobs.lever.co/acme/two", source_id=2,
                        apply_url="https://jobs.lever.co/acme/two")
        result = evaluate_application_destination(job_row(), [first, second])
        self.assertEqual((result.application_status, result.application_url), ("CONFLICT", ""))
        self.assertEqual({item["canonical_url"] for item in result.evidence if item["accepted"]},
                         {first["source_url"], second["source_url"]})


class DestinationResolverTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = connect_database(Path(self.temp.name) / "jobs.db")
        self.addCleanup(self.connection.close)
        self.relay = NetworkProtectionRelay(enabled=False)

    def add_job(self, *, url="", source_type="UNKNOWN", relationship="UNKNOWN",
                fetch_status="FAILED", add_filter=False):
        now = utc_now()
        job_id = self.connection.execute("SELECT COALESCE(MAX(job_id),0)+1 FROM jobs").fetchone()[0]
        url = url or f"https://unknown-{job_id}.test/position/backend"
        self.connection.execute(
            """INSERT INTO jobs (job_id,canonical_url,title,location_text,country,region,city,
                   remote_policy,description,first_seen_at,last_seen_at,status,content_hash,created_at,updated_at)
               VALUES (?,?,?,'Tunis, Tunisia','Tunisia','AFRICA','Tunis','ONSITE',?,?,?,'OPEN','hash',?,?)""",
            (job_id, url, "Backend Engineer", "Build production APIs.", now, now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_sources (job_id,provider,source_job_id,source_url,first_seen_at,
                   last_seen_at,fetch_status,raw_content_hash,source_type,employer_relationship)
               VALUES (?,'generic',NULL,?,?,?,?,?,?,?)""",
            (job_id, url, now, now, fetch_status, "raw", source_type, relationship),
        )
        if add_filter:
            reasons = [{"code": "PASS_RELEVANT_ROLE"}, {"code": "PASS_LOCATION_TUNISIA"}]
            self.connection.execute(
                """INSERT INTO job_filter_results (job_id,policy_version,evaluated_at,status,
                   primary_reason,reasons_json,matched_terms_json,detected_remote_policy,created_at,updated_at)
                   VALUES (?,'v1.1',?,'PASS','PASS_RELEVANT_ROLE',?,'{}','ONSITE',?,?)""",
                (job_id, now, json.dumps(reasons), now, now),
            )
        self.connection.commit()
        return job_id

    def parsed(self, job_id, *, status="FETCHED", error="", final_url=""):
        original = self.connection.execute("SELECT canonical_url FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        parsed = ParsedJob(final_url or original, "generic")
        parsed.fetch_status, parsed.fetch_error = status, error
        parsed.title, parsed.description = "Backend Engineer", "Build production APIs."
        return parsed

    def test_temporary_failures_remain_unknown(self):
        failures = (
            self.parsed(self.add_job(), status="FAILED", error="403 Forbidden"),
            TimeoutError("timed out"), OSError("DNS resolution failed"),
            self.parsed(self.add_job(), status="FAILED", error="503 Service Unavailable"),
            self.parsed(self.add_job(), status="FAILED", error="CAPTCHA challenge"),
        )
        for failure in failures:
            job_id = self.add_job()
            fetcher = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
            result = resolve_application_destinations(
                self.connection, [job_id], fetcher=fetcher, network_relay=self.relay,
            )[0]
            self.assertEqual(result.application_status, "UNKNOWN")
        direct_id = self.add_job(source_type="COMPANY_SITE", relationship="DIRECT")
        failed = self.parsed(direct_id, status="FAILED", error="403 Forbidden")
        direct = resolve_application_destinations(
            self.connection, [direct_id], fetcher=Mock(return_value=failed),
            network_relay=self.relay,
        )[0]
        self.assertEqual(direct.application_status, "UNKNOWN")

    def test_one_bounded_fetch_uses_relay_and_preserves_provenance(self):
        job_id = self.add_job()
        relay = Mock()
        relay.protect.side_effect = lambda callback, context: callback()
        fetched = self.parsed(job_id, final_url="https://jobs.lever.co/acme/redirected")
        fetcher = Mock(return_value=fetched)
        result = resolve_application_destinations(
            self.connection, [job_id], fetcher=fetcher, network_relay=relay,
        )[0]
        self.assertEqual(result.application_status, "CONFIRMED")
        self.assertEqual((relay.protect.call_count, fetcher.call_count), (1, 1))
        stored = self.connection.execute(
            "SELECT evidence_json FROM job_application_destinations WHERE job_id=?", (job_id,),
        ).fetchone()[0]
        self.assertTrue(any(item["source"] == "bounded_fetch.final_url" for item in json.loads(stored)))

    def test_no_url_means_no_fetch_or_invention(self):
        job_id = self.add_job()
        self.connection.execute("UPDATE job_sources SET source_url='' WHERE job_id=?", (job_id,))
        self.connection.commit()
        fetcher = Mock()
        result = resolve_application_destinations(
            self.connection, [job_id], fetcher=fetcher, network_relay=self.relay,
        )[0]
        self.assertEqual((result.application_status, result.application_url), ("UNKNOWN", ""))
        fetcher.assert_not_called()

    def test_browser_fallback_is_opt_in(self):
        job_id = self.add_job()
        browser = Mock(return_value=self.parsed(
            job_id, final_url="https://jobs.lever.co/acme/browser-result",
        ))
        driver = Mock()
        resolve_application_destinations(
            self.connection, [job_id], fetcher=Mock(side_effect=TimeoutError()),
            network_relay=self.relay, browser_fetcher=browser,
        )
        browser.assert_not_called()
        self.connection.execute("DELETE FROM job_application_destinations WHERE job_id=?", (job_id,))
        self.connection.commit()
        result = resolve_application_destinations(
            self.connection, [job_id], fetcher=Mock(side_effect=TimeoutError()),
            network_relay=self.relay, browser_fallback=True,
            driver_factory=Mock(return_value=driver), browser_fetcher=browser,
        )[0]
        self.assertEqual(result.application_status, "CONFIRMED")
        browser.assert_called_once()

    def test_idempotent_rerun_does_not_fetch_or_mutate(self):
        job_id = self.add_job(
            url="https://jobs.lever.co/acme/idempotent", source_type="ATS",
            relationship="DIRECT", fetch_status="FETCHED",
        )
        first = resolve_application_destinations(self.connection, [job_id], network_relay=self.relay)[0]
        before = dict(self.connection.execute(
            "SELECT * FROM job_application_destinations WHERE job_id=?", (job_id,),
        ).fetchone())
        fetcher = Mock()
        second = resolve_application_destinations(
            self.connection, [job_id], fetcher=fetcher, network_relay=self.relay,
        )[0]
        after = dict(self.connection.execute(
            "SELECT * FROM job_application_destinations WHERE job_id=?", (job_id,),
        ).fetchone())
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(before, after)
        fetcher.assert_not_called()

    def test_qualification_consumes_destination_without_changing_other_resolvers(self):
        job_id = self.add_job(
            url="https://jobs.lever.co/jobgether/specific", source_type="ATS",
            relationship="RECRUITER", fetch_status="FETCHED", add_filter=True,
        )
        now = utc_now()
        self.connection.execute(
            """INSERT INTO job_employer_evidence
               (job_id,policy_version,employer_status,source_type,employer_relationship,
                provider,evidence_method,raw_evidence_json,input_fingerprint,fetch_result,
                resolved_at,created_at,updated_at)
               VALUES (?,'employer-evidence-v1','UNKNOWN','RECRUITER','RECRUITER','lever',
                       'TEST','[]','fingerprint','NOT_REQUIRED',?,?,?)""",
            (job_id, now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_activity_evidence
               (job_id,policy_version,activity_status,source_type,provider,evidence_method,
                raw_evidence_summary,evidence_json,fetch_status,input_fingerprint,checked_at,
                created_at,updated_at)
               VALUES (?,'activity-evidence-v1','ACTIVE','RECRUITER','lever','TEST','active',
                       '{}','FETCHED','fingerprint',?,?,?)""",
            (job_id, now, now, now),
        )
        self.connection.commit()
        activity_before = dict(self.connection.execute(
            "SELECT * FROM job_activity_evidence WHERE job_id=?", (job_id,),
        ).fetchone())
        employer_before = dict(self.connection.execute(
            "SELECT * FROM job_employer_evidence WHERE job_id=?", (job_id,),
        ).fetchone())
        destination = resolve_application_destinations(
            self.connection, [job_id], network_relay=self.relay,
        )[0]
        qualified = qualify_jobs(
            self.connection, job_ids=[job_id], network_relay=self.relay,
        )[0]
        self.assertEqual(destination.application_channel, "RECRUITER")
        self.assertEqual(qualified.application_channel, "RECRUITER")
        self.assertEqual(qualified.employer_status, "UNKNOWN")
        self.assertEqual(qualified.activity_status, "ACTIVE")
        self.assertEqual(activity_before, dict(self.connection.execute(
            "SELECT * FROM job_activity_evidence WHERE job_id=?", (job_id,),
        ).fetchone()))
        self.assertEqual(employer_before, dict(self.connection.execute(
            "SELECT * FROM job_employer_evidence WHERE job_id=?", (job_id,),
        ).fetchone()))

    def test_qualification_keeps_destination_conflict_in_review(self):
        job_id = self.add_job(
            url="https://jobs.lever.co/acme/one", source_type="ATS",
            relationship="DIRECT", fetch_status="FETCHED", add_filter=True,
        )
        now = utc_now()
        self.connection.execute(
            """INSERT INTO job_sources
               (job_id,provider,source_job_id,source_url,apply_url,first_seen_at,last_seen_at,
                fetch_status,source_type,employer_relationship)
               VALUES (?,'lever','two','https://jobs.lever.co/acme/two',
                       'https://jobs.lever.co/acme/two',?,?,'FETCHED','ATS','DIRECT')""",
            (job_id, now, now),
        )
        self.connection.execute(
            "UPDATE job_sources SET apply_url=source_url WHERE job_id=?", (job_id,),
        )
        self.connection.commit()
        destination = resolve_application_destinations(
            self.connection, [job_id], network_relay=self.relay,
        )[0]
        result = qualify_jobs(
            self.connection, job_ids=[job_id], network_relay=self.relay,
        )[0]
        self.assertEqual(destination.application_status, "CONFLICT")
        self.assertEqual(result.qualification_status, "REVIEW")
        self.assertIn("REVIEW_APPLICATION_DESTINATION_CONFLICT", result.reason_codes)
