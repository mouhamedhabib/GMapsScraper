"""Offline regressions for deterministic location eligibility evidence."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from job_search.location_eligibility import (
    extract_location_evidence, resolve_location_eligibility,
)
from job_search.network import NetworkProtectionRelay
from job_search.providers import ParsedJob
from job_search.qualification import qualify_jobs
from job_search.storage import connect_database, utc_now


class ExtractionTests(TestCase):
    def extract(self, text, **kwargs):
        return extract_location_evidence(description=text, resolved_at="2026-01-01T00:00:00+00:00", **kwargs)

    def test_remote_scopes(self):
        cases = (
            ("This is a fully remote role, available worldwide.", "WORLDWIDE", "", ""),
            ("Fully remote-first environment within Europe.", "EUROPE", "", "EUROPE"),
            ("Remote within the EU.", "EU", "", "EU"),
            ("Remote in France only.", "COUNTRY", "France", ""),
        )
        for text, scope, country, region in cases:
            with self.subTest(text=text):
                result = self.extract(text)
                self.assertEqual((result.work_model, result.remote_scope), ("REMOTE", scope))
                self.assertEqual((result.required_country, result.required_region), (country, region))
                self.assertEqual(result.location_eligibility_status, "KNOWN")

    def test_bare_remote_is_accepted_only_as_an_arrangement_value(self):
        self.assertEqual(self.extract("Work model: Remote").work_model, "REMOTE")
        self.assertEqual(self.extract("We offer remote monitoring.").work_model, "UNKNOWN")

    def test_hybrid_and_onsite_use_workplace_location_without_inventing_residency(self):
        hybrid = self.extract("This is a hybrid role with 3 days per week in the office.", country="France", city="Paris")
        onsite = self.extract("This is an office-based role.", country="Portugal", city="Lisbon")
        self.assertEqual((hybrid.work_model, hybrid.required_country, hybrid.residency_requirement),
                         ("HYBRID", "France", "NOT_STATED"))
        self.assertEqual((onsite.work_model, onsite.required_country), ("ONSITE", "Portugal"))

    def test_explicit_residency_and_work_authorization(self):
        residency = self.extract("Candidates must be based in France.")
        authorization = self.extract("You must be authorized to work in the UK.")
        self.assertEqual((residency.residency_requirement, residency.required_country), ("REQUIRED", "France"))
        self.assertEqual((authorization.work_authorization_requirement,
                          authorization.work_authorization_jurisdiction),
                         ("REQUIRED", "United Kingdom"))

    def test_sponsorship_and_relocation_polarities(self):
        cases = (
            ("Visa sponsorship available.", "visa_sponsorship", "AVAILABLE"),
            ("We do not provide visa sponsorship.", "visa_sponsorship", "NOT_AVAILABLE"),
            ("Relocation assistance available.", "relocation_support", "AVAILABLE"),
            ("No relocation support.", "relocation_support", "NOT_AVAILABLE"),
        )
        for text, field, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(getattr(self.extract(text), field), expected)

    def test_absence_is_not_stated_not_negative(self):
        result = self.extract("We build useful software for customers.")
        self.assertEqual(result.visa_sponsorship, "NOT_STATED")
        self.assertEqual(result.relocation_support, "NOT_STATED")
        self.assertEqual(result.work_authorization_requirement, "NOT_STATED")

    def test_false_positive_marketing_and_product_language(self):
        phrases = (
            "customers worldwide", "global company", "distributed customers",
            "remote monitoring", "international team", "European customers", "global platform",
        )
        result = self.extract(". ".join(phrases) + ".")
        self.assertEqual((result.work_model, result.remote_scope, result.location_eligibility_status),
                         ("UNKNOWN", "UNKNOWN", "UNKNOWN"))

    def test_other_roles_and_remote_team_do_not_override_current_job(self):
        result = self.extract(
            "This is a hybrid role for our London office. "
            "We offer remote working for certain roles and a work from anywhere program.",
            country="United Kingdom", city="London",
        )
        onsite = self.extract(
            "You are the on-site technical resource connected to a distributed remote-first team.",
            country="Portugal", city="Lisbon",
        )
        self.assertEqual((result.work_model, result.location_eligibility_status), ("HYBRID", "KNOWN"))
        self.assertEqual((onsite.work_model, onsite.location_eligibility_status), ("ONSITE", "KNOWN"))

    def test_french_patterns(self):
        remote = self.extract("Poste en 100 % télétravail partout en Europe.")
        hybrid = self.extract("Poste en mode hybride, 2 jours sur site.", country="France")
        residency = self.extract("Vous devez résider en France.")
        authorization = self.extract("Vous devez avoir le droit de travailler en France.")
        sponsorship = self.extract("Le parrainage de visa est disponible.")
        self.assertEqual((remote.work_model, remote.remote_scope), ("REMOTE", "EUROPE"))
        self.assertEqual(hybrid.work_model, "HYBRID")
        self.assertEqual((residency.residency_requirement, residency.required_country), ("REQUIRED", "France"))
        self.assertEqual(authorization.work_authorization_jurisdiction, "France")
        self.assertEqual(sponsorship.visa_sponsorship, "AVAILABLE")

    def test_conflicting_work_models_are_partial(self):
        result = self.extract("This role is fully remote. This is a hybrid role.", country="France")
        self.assertEqual((result.work_model, result.location_eligibility_status), ("UNKNOWN", "PARTIAL"))

    def test_remote_radius_is_not_generalized(self):
        result = self.extract("This remote role is available within 2 hours of London.")
        self.assertNotIn(result.remote_scope, {"WORLDWIDE", "EUROPE", "EU", "EEA"})


class ResolverTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = connect_database(Path(self.temp.name) / "jobs.db")
        self.addCleanup(self.connection.close)
        self.relay = NetworkProtectionRelay(enabled=False)

    def add_job(self, description="", *, remote_policy="", status="OPEN"):
        now = utc_now()
        job_id = self.connection.execute("SELECT COALESCE(MAX(job_id),0)+1 FROM jobs").fetchone()[0]
        url = f"https://example.test/jobs/{job_id}"
        company_id = self.connection.execute(
            """INSERT INTO companies (canonical_name,first_seen_at,last_seen_at,created_at,updated_at)
               VALUES ('Acme',?,?,?,?)""", (now, now, now, now),
        ).lastrowid
        self.connection.execute(
            """INSERT INTO jobs
               (job_id,company_id,canonical_url,title,location_text,country,city,remote_policy,
                description,first_seen_at,last_seen_at,status,content_hash,created_at,updated_at)
               VALUES (?,?,?,'Engineer','Paris, France','France','Paris',?,?,?, ?,?,'hash',?,?)""",
            (job_id, company_id, url, remote_policy, description, now, now, status, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_sources
               (job_id,provider,source_url,apply_url,first_seen_at,last_seen_at,last_fetched_at,
                fetch_status,source_type,employer_relationship)
               VALUES (?,'generic',?,?, ?,?,?,'FETCHED','COMPANY_SITE','DIRECT')""",
            (job_id, url, url, now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_filter_results
               (job_id,policy_version,evaluated_at,status,primary_reason,reasons_json,
                matched_terms_json,detected_remote_policy,created_at,updated_at)
               VALUES (?,'v1.1',?,'PASS','PASS_RELEVANT_ROLE',?,'{}','UNKNOWN',?,?)""",
            (job_id, now, json.dumps([{"code": "PASS_RELEVANT_ROLE"}]), now, now),
        )
        self.connection.commit()
        return job_id

    def test_idempotent_rerun_preserves_provenance_and_does_not_refetch(self):
        job_id = self.add_job("This hybrid role has 3 days per week in the office.")
        fetcher = Mock()
        first = resolve_location_eligibility(self.connection, [job_id], fetcher=fetcher, network_relay=self.relay)[0]
        second = resolve_location_eligibility(self.connection, [job_id], fetcher=fetcher, network_relay=self.relay)[0]
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(first.evidence, second.evidence)
        evidence = first.evidence[0]
        self.assertEqual(set(("job_id", "field", "normalized_value", "raw_evidence_text",
                              "evidence_source", "method", "timestamp")) - set(evidence), set())
        fetcher.assert_not_called()

    def test_unknown_fetch_failures_remain_unknown(self):
        for failure in ("403 Forbidden", "CAPTCHA challenge", "timeout"):
            with self.subTest(failure=failure):
                job_id = self.add_job("Customers worldwide use our global platform.")
                parsed = ParsedJob(f"https://example.test/jobs/{job_id}", "generic")
                parsed.fetch_status, parsed.fetch_error = "FAILED", failure
                result = resolve_location_eligibility(
                    self.connection, [job_id], fetcher=Mock(return_value=parsed), network_relay=self.relay,
                )[0]
                self.assertEqual(result.location_eligibility_status, "UNKNOWN")

    def test_network_relay_wraps_single_bounded_fetch(self):
        job_id = self.add_job("")
        parsed = ParsedJob(f"https://example.test/jobs/{job_id}", "generic")
        parsed.fetch_status = "FETCHED"
        parsed.description = "Remote worldwide role."
        relay = Mock()
        relay.protect.side_effect = lambda callback, context: callback()
        fetcher = Mock(return_value=parsed)
        result = resolve_location_eligibility(self.connection, [job_id], fetcher=fetcher, network_relay=relay)[0]
        self.assertEqual(result.location_eligibility_status, "KNOWN")
        self.assertEqual(fetcher.call_count, 1)
        self.assertEqual(relay.protect.call_count, 1)

    def test_browser_fallback_is_opt_in_after_http_exception(self):
        job_id = self.add_job("")
        parsed = ParsedJob(f"https://example.test/jobs/{job_id}", "generic")
        parsed.fetch_status = "FETCHED"
        parsed.description = "Remote worldwide role."
        driver = Mock()
        result = resolve_location_eligibility(
            self.connection, [job_id], fetcher=Mock(side_effect=TimeoutError("timed out")),
            network_relay=self.relay, browser_fallback=True,
            driver_factory=Mock(return_value=driver), browser_fetcher=Mock(return_value=parsed),
        )[0]
        self.assertEqual((result.location_eligibility_status, result.fetch_status),
                         ("KNOWN", "BROWSER_FETCHED"))
        driver.quit.assert_called_once()

    def test_run_id_scopes_jobs(self):
        selected, other = self.add_job("Hybrid role."), self.add_job("Hybrid role.")
        now = utc_now()
        self.connection.execute(
            """INSERT INTO workflow_runs
               (run_id,started_at,status,mode,maps_enabled,job_discovery_enabled,
                completion_enabled,filter_enabled,priority_enabled,created_at)
               VALUES ('run-1',?,'RUNNING','test',0,1,0,1,0,?)""", (now, now),
        )
        self.connection.execute(
            "INSERT INTO workflow_run_jobs (run_id,job_id,discovery_state,created_at) VALUES ('run-1',?,'NEW',?)",
            (selected, now),
        )
        self.connection.commit()
        results = resolve_location_eligibility(self.connection, run_id="run-1", network_relay=self.relay)
        self.assertEqual([item.job_id for item in results], [selected])
        self.assertNotEqual(selected, other)

    def test_qualification_consumes_known_job_evidence_not_candidate_data(self):
        job_id = self.add_job("This hybrid role has 3 days per week in the office.")
        before = qualify_jobs(self.connection, job_ids=[job_id], network_relay=self.relay)[0]
        self.assertIn("REVIEW_LOCATION_ELIGIBILITY_UNKNOWN", before.reason_codes)
        resolve_location_eligibility(self.connection, [job_id], network_relay=self.relay)
        after = qualify_jobs(self.connection, job_ids=[job_id], network_relay=self.relay)[0]
        self.assertNotIn("REVIEW_LOCATION_ELIGIBILITY_UNKNOWN", after.reason_codes)
        self.assertEqual(after.qualification_status, "QUALIFIED")
