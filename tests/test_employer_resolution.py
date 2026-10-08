"""Offline regressions for deterministic employer identity resolution."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from job_search.employer_resolution import evaluate_employer, resolve_employers
from job_search.network import NetworkProtectionRelay
from job_search.providers import ParsedJob, parse_job_html
from job_search.qualification import qualify_jobs
from job_search.relationship_resolution import normalize_company_identity
from job_search.storage import connect_database, utc_now


def source(*, provider="greenhouse", tenant="acme", relationship="DIRECT", source_type="ATS"):
    hosts = {
        "greenhouse": f"https://job-boards.greenhouse.io/{tenant}/jobs/123",
        "lever": f"https://jobs.lever.co/{tenant}/123",
        "ashby": f"https://jobs.ashbyhq.com/{tenant}/123",
    }
    url = hosts.get(provider, f"https://{tenant}.test/jobs/123")
    return {
        "job_source_id": 1, "provider": provider, "source_job_id": "123",
        "source_url": url, "apply_url": url, "fetch_status": "FETCHED",
        "fetch_error": "", "last_fetched_at": "", "raw_content_hash": "hash",
        "source_type": source_type, "employer_relationship": relationship,
    }


def row(company="Acme", description=""):
    return {
        "job_id": 1, "canonical_url": "https://job-boards.greenhouse.io/acme/jobs/123",
        "title": "Backend Engineer", "description": description,
        "content_hash": "hash", "updated_at": "2026-01-01", "company": company,
    }


class EmployerEvidenceRuleTests(TestCase):
    def test_direct_relationship_and_matching_company_is_confirmed(self):
        result = evaluate_employer(row(), [source()])
        self.assertEqual((result.employer_status, result.actual_employer), ("CONFIRMED", "Acme"))

    def test_supported_provider_structured_employer_evidence(self):
        cases = (
            ("greenhouse", "<div class='company-name'>Acme, Inc.</div>"),
            ("lever", "<div class='main-header-logo'>Acme, Inc.</div>"),
            ("ashby", '<script type="application/ld+json">'
                      '{"@type":"JobPosting","title":"Engineer",'
                      '"hiringOrganization":{"name":"Acme, Inc."}}</script>'),
        )
        for provider, html in cases:
            with self.subTest(provider=provider):
                selected = source(provider=provider, relationship="UNKNOWN")
                parsed = parse_job_html(selected["source_url"], html, provider)
                result = evaluate_employer(row(company=""), [selected], parsed=parsed)
                self.assertEqual((result.employer_status, result.actual_employer),
                                 ("CONFIRMED", "Acme, Inc."))

    def test_recruiter_tenant_is_not_employer(self):
        selected = source(provider="lever", tenant="jobgether", relationship="RECRUITER")
        parsed = ParsedJob(selected["source_url"], "lever")
        parsed.company_name = "Jobgether"
        parsed.evidence_sources["company_name"] = "provider_company_field"
        result = evaluate_employer(row("Jobgether"), [selected], parsed=parsed)
        self.assertEqual((result.employer_status, result.actual_employer), ("UNKNOWN", ""))

    def test_explicit_recruiter_client_is_confirmed(self):
        selected = source(provider="lever", tenant="jobgether", relationship="RECRUITER")
        parsed = ParsedJob(selected["source_url"], "lever")
        parsed.description = "Our client, Northstar Labs, is hiring a backend engineer."
        result = evaluate_employer(
            row("Jobgether"), [selected], parsed=parsed,
        )
        self.assertEqual((result.employer_status, result.actual_employer),
                         ("CONFIRMED", "Northstar Labs"))

    def test_aggregator_and_platform_hostnames_are_not_employers(self):
        for host in ("indeed.com", "jobs.example-platform.test"):
            with self.subTest(host=host):
                selected = source(provider="generic", tenant=host, relationship="AGGREGATOR", source_type="JOB_PLATFORM")
                selected["source_url"] = selected["apply_url"] = f"https://{host}/jobs/123"
                result = evaluate_employer(row(company=""), [selected])
                self.assertEqual(result.employer_status, "UNKNOWN")

    def test_conflicting_strong_evidence_is_conflict_and_preserved(self):
        parsed = ParsedJob(source()["source_url"], "greenhouse")
        parsed.employer_evidence = [
            {"value": "Company A", "source": "provider_company_field"},
            {"value": "Company B", "source": "jsonld_hiringOrganization"},
        ]
        result = evaluate_employer(row(company=""), [source(relationship="UNKNOWN")], parsed=parsed)
        self.assertEqual((result.employer_status, result.actual_employer), ("CONFLICT", ""))
        self.assertEqual({item["raw_value"] for item in result.evidence}, {"Company A", "Company B"})

    def test_normalization_is_conservative(self):
        self.assertEqual(normalize_company_identity("ÁCME, L.L.C."), "acme")
        self.assertEqual(normalize_company_identity("Acme-Inc"), "acme")
        self.assertNotEqual(normalize_company_identity("Acme Labs"),
                            normalize_company_identity("Acme Systems"))

    def test_missing_employer_is_unknown(self):
        self.assertEqual(evaluate_employer(row(company=""), [source(relationship="UNKNOWN")]).employer_status,
                         "UNKNOWN")


class EmployerResolverTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = connect_database(Path(self.temp.name) / "jobs.db")
        self.addCleanup(self.connection.close)
        self.relay = NetworkProtectionRelay(enabled=False)

    def add_job(self, *, company="", description="", relationship="UNKNOWN",
                provider="lever", tenant="unknown", source_type="ATS",
                fetch_status="FAILED", add_filter=False):
        now = utc_now()
        next_job_id = self.connection.execute(
            "SELECT COALESCE(MAX(job_id),0)+1 FROM jobs"
        ).fetchone()[0]
        company_id = None
        if company:
            company_id = self.connection.execute(
                """INSERT INTO companies (canonical_name,first_seen_at,last_seen_at,created_at,updated_at)
                   VALUES (?,?,?,?,?)""", (company, now, now, now, now),
            ).lastrowid
        selected = source(provider=provider, tenant=tenant, relationship=relationship, source_type=source_type)
        selected["source_url"] = selected["source_url"].rstrip("/") + f"-{next_job_id}"
        selected["apply_url"] = selected["source_url"]
        job_id = self.connection.execute(
            """INSERT INTO jobs (company_id,canonical_url,title,location_text,country,region,city,
                   remote_policy,description,first_seen_at,last_seen_at,status,content_hash,created_at,updated_at)
               VALUES (?,?,?,'Tunis, Tunisia','Tunisia','AFRICA','Tunis','ONSITE',?,?,?,'OPEN','hash',?,?)""",
            (company_id, selected["source_url"], "Backend Engineer", description, now, now, now, now),
        ).lastrowid
        self.connection.execute(
            """INSERT INTO job_sources (job_id,provider,source_job_id,source_url,apply_url,first_seen_at,
                   last_seen_at,fetch_status,raw_content_hash,source_type,employer_relationship)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, provider, f"123-{job_id}", selected["source_url"], selected["apply_url"], now,
             now, fetch_status, "raw", source_type, relationship),
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

    def fetched(self, job_id, *, employer="", source_name="jsonld_hiringOrganization"):
        url = self.connection.execute("SELECT canonical_url FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        parsed = ParsedJob(url, "lever")
        parsed.fetch_status = "FETCHED"
        parsed.company_name = employer
        if employer:
            parsed.evidence_sources["company_name"] = source_name
        return parsed

    def test_one_bounded_relay_fetch_and_provenance(self):
        job_id = self.add_job()
        relay = Mock()
        relay.protect.side_effect = lambda callback, context: callback()
        fetcher = Mock(return_value=self.fetched(job_id, employer="Acme"))
        result = resolve_employers(self.connection, [job_id], fetcher=fetcher, network_relay=relay)[0]
        self.assertEqual(result.employer_status, "CONFIRMED")
        self.assertEqual((relay.protect.call_count, fetcher.call_count), (1, 1))
        stored = self.connection.execute("SELECT raw_evidence_json FROM job_employer_evidence WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(json.loads(stored[0])[0]["raw_value"], "Acme")

    def test_403_timeout_and_dns_are_unknown(self):
        for failure_name in ("403", "timeout", "dns"):
            job_id = self.add_job()
            if failure_name == "403":
                failure = self.fetched(job_id, employer="")
                failure.fetch_status, failure.fetch_error = "FAILED", "403 Forbidden"
            elif failure_name == "timeout":
                failure = TimeoutError("timed out")
            else:
                failure = OSError("DNS resolution failed")
            fetcher = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
            result = resolve_employers(self.connection, [job_id], fetcher=fetcher, network_relay=self.relay)[0]
            self.assertEqual(result.employer_status, "UNKNOWN")

    def test_no_invented_url_and_non_individual_url_is_not_fetched(self):
        job_id = self.add_job(provider="generic", tenant="platform", source_type="JOB_PLATFORM",
                              relationship="AGGREGATOR")
        self.connection.execute("UPDATE job_sources SET source_job_id=NULL,source_url='https://indeed.com/jobs',apply_url=NULL WHERE job_id=?", (job_id,))
        self.connection.commit()
        fetcher = Mock()
        result = resolve_employers(self.connection, [job_id], fetcher=fetcher, network_relay=self.relay)[0]
        self.assertEqual(result.authoritative_url, "https://indeed.com/jobs")
        fetcher.assert_not_called()

    def test_browser_fallback_is_opt_in(self):
        job_id = self.add_job()
        browser = Mock(return_value=self.fetched(job_id, employer="Acme"))
        driver = Mock()
        resolve_employers(self.connection, [job_id], fetcher=Mock(side_effect=TimeoutError()),
                          network_relay=self.relay, browser_fetcher=browser)
        browser.assert_not_called()
        self.connection.execute("DELETE FROM job_employer_evidence WHERE job_id=?", (job_id,))
        self.connection.commit()
        result = resolve_employers(
            self.connection, [job_id], fetcher=Mock(side_effect=TimeoutError()),
            network_relay=self.relay, browser_fallback=True,
            driver_factory=Mock(return_value=driver), browser_fetcher=browser,
        )[0]
        self.assertEqual(result.employer_status, "CONFIRMED")
        browser.assert_called_once()

    def test_idempotent_rerun_skips_fetch_and_mutation(self):
        job_id = self.add_job(description="Our client, Acme Labs, is hiring an engineer.")
        first = resolve_employers(self.connection, [job_id], network_relay=self.relay)[0]
        updated = self.connection.execute("SELECT updated_at FROM job_employer_evidence WHERE job_id=?", (job_id,)).fetchone()[0]
        fetcher = Mock()
        second = resolve_employers(self.connection, [job_id], fetcher=fetcher, network_relay=self.relay)[0]
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(self.connection.execute("SELECT updated_at FROM job_employer_evidence WHERE job_id=?", (job_id,)).fetchone()[0], updated)
        fetcher.assert_not_called()

    def test_qualification_consumes_confirmed_unknown_and_conflict(self):
        cases = (
            ("Our client, Acme Labs, is hiring an engineer.", None, "CONFIRMED", "QUALIFIED_EMPLOYER_CONFIRMED"),
            ("", None, "UNKNOWN", "REVIEW_EMPLOYER_UNKNOWN"),
            ("", [
                {"value": "Company A", "source": "provider_company_field"},
                {"value": "Company B", "source": "jsonld_hiringOrganization"},
            ], "CONFLICT", "REVIEW_EMPLOYER_CONFLICT"),
        )
        for description, evidence, status, reason in cases:
            with self.subTest(status=status):
                job_id = self.add_job(description=description, fetch_status="FETCHED", add_filter=True)
                fetcher = Mock(side_effect=TimeoutError("timed out"))
                if evidence:
                    parsed = self.fetched(job_id)
                    parsed.employer_evidence = evidence
                    fetcher = Mock(return_value=parsed)
                    self.connection.execute("UPDATE job_sources SET fetch_status='FAILED' WHERE job_id=?", (job_id,))
                    self.connection.commit()
                resolve_employers(self.connection, [job_id], fetcher=fetcher, network_relay=self.relay)
                result = qualify_jobs(self.connection, job_ids=[job_id], network_relay=self.relay)[0]
                self.assertEqual(result.employer_status, status)
                self.assertIn(reason, result.reason_codes)
                if status != "CONFIRMED":
                    self.assertEqual(result.qualification_status, "REVIEW")
