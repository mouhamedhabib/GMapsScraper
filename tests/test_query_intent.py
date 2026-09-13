"""Offline regressions for discovery-query geographic intent."""

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from job_search.filtering import evaluate_job, filter_stored_jobs
from job_search.providers import ParsedJob
from job_search.query_intent import parse_query_intent
from job_search.storage import connect_database, upsert_job


NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)


def row(location="Paris, France", **values):
    result = {
        "title": "Junior Backend Developer",
        "description": "Build Python APIs.",
        "location_text": location,
        "country": "",
        "city": "",
        "remote_policy": "",
        "published_at": "2026-09-10T00:00:00+00:00",
    }
    result.update(values)
    return result


def codes(decision):
    return {reason.code for reason in decision.reasons}


class QueryIntentRuleTests(TestCase):
    def evaluate(self, location, *queries, **values):
        return evaluate_job(row(location, **values), NOW, tuple(queries))

    def test_country_matches_and_mismatches_use_normalized_identity(self):
        cases = (
            ("Paris, FR", "backend developer France", "MATCH"),
            ("Thane District, IN", "backend developer France", "MISMATCH"),
            ("Brussels, BE", "software developer Belgium", "MATCH"),
            ("Zurich, CH", "backend developer Switzerland", "MATCH"),
        )
        for location, query, expected in cases:
            with self.subTest(location=location, query=query):
                decision = self.evaluate(location, query)
                self.assertEqual(decision.matched_terms["query_location_match"], [expected])
                if expected == "MISMATCH":
                    self.assertEqual(decision.status, "REJECT")
                    self.assertIn("REJECT_QUERY_LOCATION_MISMATCH", codes(decision))

    def test_europe_scope_accepts_netherlands_and_rejects_india_or_canada(self):
        amsterdam = self.evaluate("Amsterdam", "backend developer Europe", city="Amsterdam")
        self.assertEqual(amsterdam.matched_terms["normalized_country"], ["Netherlands"])
        self.assertEqual(amsterdam.matched_terms["query_location_match"], ["MATCH"])
        for location in ("Bangalore, India", "Halifax, Canada"):
            with self.subTest(location=location):
                decision = self.evaluate(location, "backend developer Europe")
                self.assertEqual(decision.status, "REJECT")
                self.assertIn("REJECT_QUERY_LOCATION_MISMATCH", codes(decision))

    def test_remote_europe_requires_compatible_remote_scope(self):
        accepted = self.evaluate(
            "Remote in Europe", "junior software engineer remote Europe",
            remote_policy="REMOTE",
        )
        self.assertEqual(accepted.matched_terms["query_location_match"], ["MATCH"])
        india = self.evaluate(
            "Remote - India", "junior software engineer remote Europe",
            remote_policy="REMOTE",
        )
        self.assertEqual(india.status, "REJECT")
        us = self.evaluate(
            "US remote", "junior software engineer remote Europe",
            remote_policy="REMOTE",
        )
        self.assertEqual(us.status, "REJECT")

    def test_worldwide_query_requires_verified_worldwide_remote(self):
        accepted = self.evaluate(
            "Toronto, Canada", "backend developer worldwide remote",
            remote_policy="REMOTE", description="This role is remote worldwide.",
        )
        self.assertEqual(accepted.matched_terms["query_location_match"], ["MATCH"])
        unverified = self.evaluate(
            "Toronto, Canada", "backend developer worldwide remote",
            remote_policy="REMOTE", description="Remote role for Canada.",
        )
        self.assertEqual(unverified.status, "REVIEW")
        self.assertIn("REVIEW_QUERY_LOCATION_UNKNOWN", codes(unverified))

    def test_unknown_location_reviews_and_no_geo_query_is_neutral(self):
        unknown = self.evaluate("", "backend developer France", city="", country="")
        self.assertEqual(unknown.status, "REVIEW")
        self.assertIn("REVIEW_QUERY_LOCATION_UNKNOWN", codes(unknown))
        neutral = self.evaluate("Bangalore, India", "NestJS developer")
        self.assertNotIn("REJECT_QUERY_LOCATION_MISMATCH", codes(neutral))
        self.assertEqual(neutral.matched_terms["query_location_match"], ["NEUTRAL"])

    def test_ats_scope_is_removed_from_role_and_provider_is_parsed(self):
        intent = parse_query_intent(
            "site:job-boards.greenhouse.io software engineer France"
        )
        self.assertEqual(intent.provider_scope, "greenhouse")
        self.assertEqual(intent.country, "France")
        self.assertEqual(intent.role_terms, ("software", "engineer"))

    def test_multi_query_any_match_and_all_mismatch(self):
        france = self.evaluate(
            "Paris, France", "backend developer Germany", "software engineer France"
        )
        self.assertEqual(france.matched_terms["query_location_match"], ["MATCH"])
        self.assertEqual(
            france.matched_terms["matched_source_query"], ["software engineer France"]
        )
        india = self.evaluate(
            "Bangalore, India", "developer France", "developer Belgium", "developer Europe"
        )
        self.assertEqual(india.status, "REJECT")
        unknown = self.evaluate("", "developer France", "developer Europe")
        self.assertEqual(unknown.status, "REVIEW")
        self.assertIn("REVIEW_QUERY_LOCATION_UNKNOWN", codes(unknown))

    def test_live_false_pass_shapes_are_rejected(self):
        cases = (
            ("Junior Python Developer", "Thane District, MH, IN", "junior software developer France"),
            ("Software Engineer", "Bangalore, en-in", "software developer Belgium"),
            ("PHP Laravel Developer", "Shikargarh Enclave, RJ, IN", "Laravel developer France"),
        )
        for title, location, query in cases:
            with self.subTest(title=title):
                decision = self.evaluate(location, query, title=title)
                self.assertEqual(decision.status, "REJECT")
                self.assertIn("REJECT_QUERY_LOCATION_MISMATCH", codes(decision))

    def test_missing_historical_provenance_is_neutral(self):
        decision = evaluate_job(row("Bangalore, India"), NOW)
        self.assertEqual(decision.matched_terms["query_location_match"], ["NEUTRAL"])
        self.assertNotIn("REJECT_QUERY_LOCATION_MISMATCH", codes(decision))


class QueryIntentPersistenceTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.connection = connect_database(Path(self.temporary.name) / "jobs.db")
        self.addCleanup(self.connection.close)

    def test_all_stored_source_queries_are_used_idempotently(self):
        parsed = ParsedJob(
            canonical_url="https://jobs.lever.co/acme/one", provider="lever",
            source_job_id="one", title="Junior Backend Developer",
            company_name="Acme", location_text="Paris, France",
            description="Build Python APIs.", fetch_status="FETCHED",
        )
        job_id, _ = upsert_job(self.connection, parsed, "developer Germany", NOW.isoformat())
        upsert_job(self.connection, parsed, "developer France", NOW.isoformat())
        first = filter_stored_jobs(self.connection, "query-guard", rebuild=True)
        stored = self.connection.execute(
            "SELECT * FROM job_filter_results WHERE job_id=? AND policy_version='query-guard'",
            (job_id,),
        ).fetchone()
        terms = json.loads(stored["matched_terms_json"])
        self.assertEqual(first["counts"]["REVIEW"], 1)
        self.assertEqual(terms["matched_source_query"], ["developer France"])
        self.assertEqual(len(terms["source_queries"]), 2)
        before = stored["reasons_json"], stored["matched_terms_json"]
        filter_stored_jobs(self.connection, "query-guard", rebuild=True)
        stored = self.connection.execute(
            "SELECT * FROM job_filter_results WHERE job_id=? AND policy_version='query-guard'",
            (job_id,),
        ).fetchone()
        self.assertEqual(before, (stored["reasons_json"], stored["matched_terms_json"]))
