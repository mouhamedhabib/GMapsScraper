"""Offline tests for official employer-website resolution."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from job_search.company_website_resolution import (
    WEBSITE_POLICY_VERSION, evaluate_website, resolve_company_websites,
)
from job_search.contact_discovery import discover_contacts
from job_search.network import NetworkProtectionRelay
from job_search.providers import ParsedJob, parse_job_html
from job_search.storage import connect_database, utc_now


def row(company="Acme", website=""):
    return {
        "job_id": 1, "company": company, "actual_employer": company,
        "existing_website_url": website,
    }


def source(url="https://jobs.lever.co/acme/123", *, source_type="ATS", relationship="DIRECT"):
    return {
        "job_source_id": 1, "provider": "lever", "source_job_id": "123",
        "source_url": url, "apply_url": "", "fetch_status": "FETCHED",
        "raw_content_hash": "raw", "source_type": source_type,
        "employer_relationship": relationship, "last_fetched_at": "",
    }


class WebsiteEvidenceTests(TestCase):
    def test_direct_company_website(self):
        direct = source("https://www.acme.test/careers/engineer", source_type="COMPANY_SITE")
        result = evaluate_website(row(), [direct])
        self.assertEqual((result.status, result.canonical_domain), ("CONFIRMED", "acme.test"))
        self.assertEqual(result.evidence_method, "DIRECT_COMPANY_SOURCE")

    def test_ats_structured_employer_website(self):
        parsed = ParsedJob("https://jobs.lever.co/acme/123", "lever")
        parsed.company_name = "Acme"
        parsed.company_url = "https://www.acme.test"
        parsed.evidence_sources["company_url"] = "provider_company_url"
        result = evaluate_website(row(), [source()], parsed)
        self.assertEqual(result.status, "CONFIRMED")
        self.assertEqual(result.evidence_method, "ATS_STRUCTURED_COMPANY_WEBSITE")

    def test_jobposting_hiring_organization_url_and_same_as(self):
        for key, value in (("url", "https://acme.test"), ("sameAs", ["https://acme.test"])):
            with self.subTest(key=key, value=value):
                html = json.dumps({
                    "@context": "https://schema.org", "@type": "JobPosting",
                    "title": "Software Engineer", "description": "Build software",
                    "hiringOrganization": {
                        "@type": "Organization", "name": "Acme", key: value,
                    },
                })
                parsed = parse_job_html(
                    "https://jobs.lever.co/acme/123",
                    f'<script type="application/ld+json">{html}</script>', "lever",
                )
                result = evaluate_website(row(), [source()], parsed)
                self.assertEqual(result.status, "CONFIRMED")
                self.assertIn("JSONLD_HIRING_ORGANIZATION", result.evidence_method)

    def test_associated_organization_jsonld(self):
        html = """<script type="application/ld+json">{
          "@graph": [
            {"@type":"JobPosting","title":"Software Engineer","description":"Build",
             "hiringOrganization":{"@type":"Organization","name":"Acme"}},
            {"@type":"Organization","name":"Acme","url":"https://acme.test"}
          ]}</script>"""
        parsed = parse_job_html("https://jobs.lever.co/acme/123", html, "lever")
        result = evaluate_website(row(), [source()], parsed)
        self.assertEqual(result.status, "CONFIRMED")
        self.assertEqual(result.evidence_method, "JSONLD_ORGANIZATION_WEBSITE")

    def test_company_logo_link_is_explicit_employer_evidence(self):
        html = """<script type="application/ld+json">{
          "@type":"JobPosting","title":"Software Engineer","description":"Build",
          "hiringOrganization":{"@type":"Organization","name":"Acme"}
        }</script><a class="logo" href="https://acme.test/">
          <img alt="Acme Logo"></a>"""
        parsed = parse_job_html("https://boards.greenhouse.io/acme/jobs/123", html, "greenhouse")
        result = evaluate_website(row(), [source("https://boards.greenhouse.io/acme/jobs/123")], parsed)
        self.assertEqual((result.status, result.canonical_domain), ("CONFIRMED", "acme.test"))
        self.assertEqual(result.evidence_method, "ATS_EXPLICIT_EMPLOYER_LINK")

    def test_named_home_page_link_is_explicit_employer_evidence(self):
        html = """<script type="application/ld+json">{
          "@type":"JobPosting","title":"Software Engineer","description":"Build",
          "hiringOrganization":{"@type":"Organization","name":"Acme"}
        }</script><a href="https://www.acme.test/">Acme Home Page</a>"""
        parsed = parse_job_html("https://jobs.lever.co/acme/123", html, "lever")
        result = evaluate_website(row(), [source()], parsed)
        self.assertEqual((result.status, result.canonical_domain), ("CONFIRMED", "acme.test"))
        self.assertEqual(result.evidence_method, "ATS_EXPLICIT_EMPLOYER_LINK")

    def test_explicit_employee_recruiter_domain_statement(self):
        html = """<script type="application/ld+json">{
          "@type":"JobPosting","title":"Software Engineer","description":"Build",
          "hiringOrganization":{"@type":"Organization","name":"Acme LLC"}
        }</script><p>Emails from genuine Acme recruiters who are employees of the company
          always use an @acme.test domain.</p>"""
        parsed = parse_job_html("https://boards.greenhouse.io/acme/jobs/123", html, "greenhouse")
        result = evaluate_website(row("Acme LLC"), [source("https://boards.greenhouse.io/acme/jobs/123")], parsed)
        self.assertEqual((result.status, result.canonical_domain), ("CONFIRMED", "acme.test"))
        self.assertEqual(result.evidence_method, "ATS_EXPLICIT_EMPLOYER_DOMAIN")

    def test_blocked_ats_recruiter_platform_and_social_domains(self):
        urls = (
            "https://job-boards.greenhouse.io/acme", "https://jobs.lever.co/acme",
            "https://jobgether.com", "https://indeed.com/cmp/acme",
            "https://linkedin.com/company/acme", "https://github.com/acme",
        )
        for url in urls:
            with self.subTest(url=url):
                parsed = ParsedJob("https://jobs.lever.co/acme/123", "lever")
                parsed.company_name = "Acme"
                parsed.company_website_evidence = [{
                    "url": url, "company_name": "Acme",
                    "source": "jsonld_hiringOrganization.url",
                }]
                self.assertEqual(evaluate_website(row(), [source()], parsed).status, "UNKNOWN")

    def test_redirect_to_official_company_accepted(self):
        redirected = {
            "original_url": "https://go.test/acme", "final_url": "https://www.acme.test/about",
            "redirect_chain": ["https://go.test/acme", "https://www.acme.test/about"],
            "page_identities": ["Acme"],
        }
        result = evaluate_website(row(), [], redirected_candidate=redirected)
        self.assertEqual((result.status, result.canonical_domain), ("CONFIRMED", "acme.test"))
        self.assertEqual(len(result.redirect_chain), 2)

    def test_redirect_to_unrelated_company_rejected(self):
        redirected = {
            "original_url": "https://go.test/acme", "final_url": "https://other.test",
            "redirect_chain": [], "page_identities": ["Other Company"],
        }
        self.assertEqual(
            evaluate_website(row(), [], redirected_candidate=redirected).status, "UNKNOWN",
        )

    def test_conflicting_strong_domains(self):
        parsed = ParsedJob("https://jobs.lever.co/acme/123", "lever")
        parsed.company_name = "Acme"
        parsed.company_website_evidence = [
            {"url": "https://acme.test", "company_name": "Acme", "source": "jsonld_hiringOrganization.url"},
            {"url": "https://different.test", "company_name": "Acme", "source": "jsonld_hiringOrganization.sameAs"},
        ]
        self.assertEqual(evaluate_website(row(), [source()], parsed).status, "CONFLICT")


class WebsiteResolverTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = connect_database(Path(self.temp.name) / "jobs.db")
        self.addCleanup(self.connection.close)
        self.relay = NetworkProtectionRelay(enabled=False)

    def add_job(self, *, qualification="QUALIFIED", source_url="https://jobs.lever.co/acme/123"):
        now = utc_now()
        company_id = self.connection.execute(
            """INSERT INTO companies
               (canonical_name,normalized_domain,first_seen_at,last_seen_at,created_at,updated_at)
               VALUES ('Acme',NULL,?,?,?,?)""", (now, now, now, now),
        ).lastrowid
        job_id = self.connection.execute("SELECT COALESCE(MAX(job_id),0)+1 FROM jobs").fetchone()[0]
        self.connection.execute(
            """INSERT INTO jobs
               (job_id,company_id,canonical_url,title,description,first_seen_at,last_seen_at,
                status,content_hash,created_at,updated_at)
               VALUES (?,?,?,'Software Engineer','Build',?,?,'OPEN','hash',?,?)""",
            (job_id, company_id, source_url, now, now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_sources
               (job_id,provider,source_job_id,source_url,first_seen_at,last_seen_at,
                fetch_status,raw_content_hash,source_type,employer_relationship)
               VALUES (?,'lever',?, ?,?,?,'FETCHED','raw','ATS','DIRECT')""",
            (job_id, f"id-{job_id}", source_url, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_qualifications
               (job_id,policy_version,qualification_status,activity_status,employer_status,
                application_channel,reason_codes_json,evidence_json,input_evidence_hash,
                qualified_at,created_at,updated_at)
               VALUES (?,'q1',?,'ACTIVE','CONFIRMED','ATS','[]','[]','qh',?,?,?)""",
            (job_id, qualification, now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_employer_evidence
               (job_id,policy_version,employer_status,actual_employer,source_type,
                employer_relationship,provider,evidence_method,raw_evidence_json,
                input_fingerprint,fetch_result,resolved_at,created_at,updated_at)
               VALUES (?,'e1','CONFIRMED','Acme','ATS','DIRECT','lever',
                       'PROVIDER_STRUCTURED_EMPLOYER','[]','eh','FETCHED',?,?,?)""",
            (job_id, now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_contact_strategies
               (job_id,policy_version,primary_role,secondary_roles_json,avoid_roles_json,
                confidence,rationale,reason_codes_json,input_fingerprint,resolved_at,created_at,updated_at)
               VALUES (?,'s1','Engineering Manager','[\"Technical Recruiter\"]','[\"CEO\"]',
                       'HIGH','test','[]','sf',?,?,?)""", (job_id, now, now, now),
        )
        self.connection.commit()
        return job_id

    @staticmethod
    def parsed(url, *, failed=False):
        result = ParsedJob(url, "lever")
        result.fetch_status = "FAILED" if failed else "FETCHED"
        result.fetch_error = "503 temporary" if failed else ""
        result.company_name = "Acme"
        if not failed:
            result.company_website_evidence = [{
                "url": "https://acme.test", "company_name": "Acme",
                "source": "jsonld_hiringOrganization.url",
            }]
        return result

    def test_temporary_failure_unknown_and_one_bounded_operation(self):
        job_id = self.add_job()
        fetcher = Mock(return_value=self.parsed("https://jobs.lever.co/acme/123", failed=True))
        before = tuple(self.connection.execute(
            "SELECT qualification_status,updated_at FROM job_qualifications WHERE job_id=?", (job_id,),
        ).fetchone())
        result = resolve_company_websites(
            self.connection, [job_id], fetcher=fetcher, network_relay=self.relay,
        )[0]
        after = tuple(self.connection.execute(
            "SELECT qualification_status,updated_at FROM job_qualifications WHERE job_id=?", (job_id,),
        ).fetchone())
        self.assertEqual(result.status, "UNKNOWN")
        self.assertTrue(result.network_used)
        self.assertEqual(fetcher.call_count, 1)
        self.assertEqual(before, after)

    def test_fingerprint_idempotence_does_not_refetch(self):
        job_id = self.add_job()
        fetcher = Mock(return_value=self.parsed("https://jobs.lever.co/acme/123"))
        first = resolve_company_websites(
            self.connection, [job_id], fetcher=fetcher, network_relay=self.relay,
        )[0]
        stored_first = tuple(self.connection.execute(
            "SELECT company_website_id,created_at,updated_at FROM job_company_websites WHERE job_id=?",
            (job_id,),
        ).fetchone())
        second = resolve_company_websites(
            self.connection, [job_id], fetcher=fetcher, network_relay=self.relay,
        )[0]
        stored_second = tuple(self.connection.execute(
            "SELECT company_website_id,created_at,updated_at FROM job_company_websites WHERE job_id=?",
            (job_id,),
        ).fetchone())
        self.assertEqual(first.status, "CONFIRMED")
        self.assertTrue(second.reused)
        self.assertEqual(fetcher.call_count, 1)
        self.assertEqual(stored_first, stored_second)

    def test_qualified_only_default_and_include_review(self):
        qualified_id = self.add_job()
        review_id = self.add_job(
            qualification="REVIEW", source_url="https://jobs.lever.co/acme/review",
        )
        fetcher = Mock(side_effect=lambda url, timeout: self.parsed(url))
        default = resolve_company_websites(
            self.connection, [qualified_id, review_id], fetcher=fetcher,
            network_relay=self.relay,
        )
        included = resolve_company_websites(
            self.connection, [qualified_id, review_id], include_review=True,
            fetcher=fetcher, network_relay=self.relay,
        )
        self.assertEqual([result.job_id for result in default], [qualified_id])
        self.assertEqual([result.job_id for result in included], [qualified_id, review_id])

    def test_explicit_scope_is_required(self):
        self.add_job()
        with self.assertRaisesRegex(ValueError, "provide at least one"):
            resolve_company_websites(self.connection, network_relay=self.relay)

    def test_contact_discovery_consumes_confirmed_only(self):
        job_id = self.add_job()
        now = utc_now()
        for status in ("UNKNOWN", "CONFIRMED"):
            self.connection.execute(
                """INSERT INTO job_company_websites
                   (job_id,policy_version,status,company_name,website_url,canonical_domain,
                    redirect_chain_json,evidence_method,source_type,reason_codes_json,evidence_json,
                    fetch_result,network_used,input_fingerprint,resolved_at,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,'[]','test','TEST','[]','[]','NOT_REQUIRED',0,?,?,?,?)""",
                (job_id, f"website-{status}", status, "Acme",
                 "https://unknown.test" if status == "UNKNOWN" else "https://acme.test",
                 "unknown.test" if status == "UNKNOWN" else "acme.test", status, now, now, now),
            )
        self.connection.commit()
        observed = []

        def first_party(input_row, limit):
            observed.append(input_row["company_website"])
            return [], 0

        discover_contacts(
            self.connection, [job_id], max_search_queries=1,
            searcher=Mock(return_value=[]), first_party_discoverer=first_party,
        )
        self.assertEqual(observed, ["https://acme.test"])

    def test_no_email_or_outreach_fields(self):
        fields = {row["name"] for row in self.connection.execute("PRAGMA table_info(job_company_websites)")}
        self.assertFalse(any(term in field.casefold() for field in fields for term in ("email", "outreach")))
