"""Tests for exact, no-network job source relationship resolution."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from job_search.network import NetworkProtectionRelay
from job_search.qualification import qualify_jobs
from job_search.relationship_resolution import (
    normalize_company_identity, resolve_relationships,
)
from job_search.storage import connect_database, utc_now


class RelationshipResolutionTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "jobs.db"
        self.connection = connect_database(self.database)
        self.addCleanup(self.connection.close)

    def add_source(
        self, company, provider, url, relationship="UNKNOWN", source_type="ATS",
        *, add_filter=False,
    ):
        now = utc_now()
        company_id = None
        if company:
            company_id = self.connection.execute(
                """INSERT INTO companies
                   (canonical_name, first_seen_at, last_seen_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""", (company, now, now, now, now)
            ).lastrowid
        job_id = self.connection.execute(
            """INSERT INTO jobs
               (company_id, canonical_url, title, location_text, country, region,
                city, remote_policy, description, first_seen_at, last_seen_at,
                status, content_hash, created_at, updated_at)
               VALUES (?, ?, 'Backend Engineer', 'Tunis, Tunisia', 'Tunisia',
                       'AFRICA', 'Tunis', 'ONSITE', ?, ?, ?, 'OPEN', 'hash', ?, ?)""",
            (company_id, url, "Backend engineer building APIs. " * 20,
             now, now, now, now),
        ).lastrowid
        source_id = self.connection.execute(
            """INSERT INTO job_sources
               (job_id, provider, source_url, apply_url, first_seen_at,
                last_seen_at, last_fetched_at, fetch_status, source_type,
                employer_relationship)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'FETCHED', ?, ?)""",
            (job_id, provider, url, url, now, now, now, source_type, relationship),
        ).lastrowid
        if add_filter:
            reasons = [
                {"code": "PASS_RELEVANT_ROLE", "message": "role"},
                {"code": "PASS_LOCATION_TUNISIA", "message": "location"},
            ]
            self.connection.execute(
                """INSERT INTO job_filter_results
                   (job_id, policy_version, evaluated_at, status, primary_reason,
                    reasons_json, matched_terms_json, detected_remote_policy,
                    created_at, updated_at)
                   VALUES (?, 'v1.1', ?, 'PASS', 'PASS_RELEVANT_ROLE', ?, '{}',
                           'ONSITE', ?, ?)""",
                (job_id, now, json.dumps(reasons), now, now),
            )
        self.connection.commit()
        return job_id, source_id

    def resolution(self, job_id):
        return resolve_relationships(self.connection, job_ids=[job_id])[0]

    def test_greenhouse_tenant_company_match_is_direct(self):
        job_id, _ = self.add_source(
            "Dashlane", "greenhouse",
            "https://job-boards.greenhouse.io/dashlane/jobs/8169067",
        )
        result = self.resolution(job_id)
        self.assertEqual((result.relationship, result.method),
                         ("DIRECT", "ATS_TENANT_COMPANY_MATCH"))

    def test_lever_tenant_company_match_is_direct(self):
        job_id, _ = self.add_source(
            "MOO", "lever", "https://jobs.lever.co/moo/posting-id",
        )
        self.assertEqual(self.resolution(job_id).relationship, "DIRECT")

    def test_ashby_organization_company_match_is_direct(self):
        job_id, _ = self.add_source(
            "Abound", "ashby", "https://jobs.ashbyhq.com/Abound/posting-id",
        )
        self.assertEqual(self.resolution(job_id).relationship, "DIRECT")

    def test_legal_suffix_case_and_punctuation_normalization(self):
        self.assertEqual(normalize_company_identity("ACME, L.L.C."), "acme")
        job_id, _ = self.add_source(
            "Arcesium LLC", "greenhouse",
            "https://job-boards.greenhouse.io/arcesiumllc/jobs/1",
        )
        self.assertEqual(self.resolution(job_id).relationship, "DIRECT")
        job_id, _ = self.add_source(
            "M.O.O.", "lever", "https://jobs.lever.co/moo/2",
        )
        self.assertEqual(self.resolution(job_id).relationship, "DIRECT")

    def test_missing_or_conflicting_company_remains_unknown(self):
        missing, _ = self.add_source(
            "", "greenhouse",
            "https://job-boards.greenhouse.io/artefactlinkedin/jobs/1",
        )
        conflict, _ = self.add_source(
            "Different Company", "lever", "https://jobs.lever.co/acme/1",
        )
        self.assertEqual(self.resolution(missing).method, "COMPANY_MISSING")
        self.assertEqual(self.resolution(conflict).method,
                         "ATS_TENANT_COMPANY_CONFLICT")

    def test_recruiter_and_aggregator_are_preserved(self):
        recruiter, _ = self.add_source(
            "Jobgether", "lever", "https://jobs.lever.co/jobgether/1",
            relationship="RECRUITER",
        )
        aggregator, _ = self.add_source(
            "Acme", "greenhouse", "https://job-boards.greenhouse.io/acme/jobs/1",
            relationship="AGGREGATOR",
        )
        self.assertEqual(self.resolution(recruiter).relationship, "RECRUITER")
        self.assertEqual(self.resolution(aggregator).relationship, "AGGREGATOR")

    def test_ats_alone_does_not_imply_direct_but_repairs_source_type(self):
        job_id, source_id = self.add_source(
            "", "generic", "https://jobs.lever.co/unknown/1",
            source_type="UNKNOWN",
        )
        result = self.resolution(job_id)
        stored = self.connection.execute(
            "SELECT source_type, employer_relationship FROM job_sources WHERE job_source_id=?",
            (source_id,),
        ).fetchone()
        self.assertEqual(result.relationship, "UNKNOWN")
        self.assertEqual(tuple(stored), ("ATS", "UNKNOWN"))

    def test_idempotent_rerun_and_mutation_provenance(self):
        job_id, source_id = self.add_source(
            "Dashlane", "greenhouse",
            "https://job-boards.greenhouse.io/dashlane/jobs/1",
        )
        first = self.resolution(job_id)
        audit = self.connection.execute(
            "SELECT * FROM job_repair_results WHERE job_id=?", (job_id,)
        ).fetchall()
        second = self.resolution(job_id)
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(len(audit), 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM job_repair_results WHERE job_id=?", (job_id,)
            ).fetchone()[0], 1,
        )
        changes = json.loads(audit[0]["field_changes_json"])
        relationship = changes[f"source[{source_id}].employer_relationship"]
        self.assertEqual(relationship["old_value"], "UNKNOWN")
        self.assertEqual(relationship["new_value"], "DIRECT")
        self.assertEqual(relationship["method"], "ATS_TENANT_COMPANY_MATCH")
        self.assertEqual(relationship["evidence"]["tenant"], "dashlane")

    def test_no_network_dependency(self):
        job_id, _ = self.add_source(
            "Dashlane", "greenhouse",
            "https://job-boards.greenhouse.io/dashlane/jobs/1",
        )
        with patch("requests.get", side_effect=AssertionError("network used")):
            self.assertEqual(self.resolution(job_id).relationship, "DIRECT")

    def test_run_id_bounds_resolution(self):
        included, _ = self.add_source(
            "Dashlane", "greenhouse",
            "https://job-boards.greenhouse.io/dashlane/jobs/1",
        )
        excluded, _ = self.add_source(
            "MOO", "lever", "https://jobs.lever.co/moo/1",
        )
        now = utc_now()
        self.connection.execute(
            """INSERT INTO workflow_runs
               (run_id, started_at, finished_at, status, mode, maps_enabled,
                job_discovery_enabled, completion_enabled, filter_enabled,
                priority_enabled, created_at)
               VALUES ('scope', ?, ?, 'SUCCESS', 'DAILY', 0, 1, 1, 1, 1, ?)""",
            (now, now, now),
        )
        self.connection.execute(
            "INSERT INTO workflow_run_jobs VALUES ('scope', ?, 'NEW', ?)",
            (included, now),
        )
        self.connection.commit()
        results = resolve_relationships(self.connection, run_id="scope")
        self.assertEqual({item.job_id for item in results}, {included})
        self.assertEqual(
            self.connection.execute(
                "SELECT employer_relationship FROM job_sources WHERE job_id=?",
                (excluded,),
            ).fetchone()[0], "UNKNOWN",
        )

    def test_qualification_consumes_resolved_relationship(self):
        job_id, _ = self.add_source(
            "Dashlane", "greenhouse",
            "https://job-boards.greenhouse.io/dashlane/jobs/1", add_filter=True,
        )
        before = qualify_jobs(
            self.connection, job_ids=[job_id],
            network_relay=NetworkProtectionRelay(enabled=False),
        )[0]
        self.assertEqual(before.qualification_status, "REVIEW")
        self.resolution(job_id)
        after = qualify_jobs(
            self.connection, job_ids=[job_id],
            network_relay=NetworkProtectionRelay(enabled=False),
        )[0]
        self.assertEqual(after.qualification_status, "QUALIFIED")
        self.assertEqual(after.employer_relationship, "DIRECT")

