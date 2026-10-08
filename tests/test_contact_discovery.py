"""Offline regressions for bounded actual-person discovery."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from job_search.contact_discovery import (
    DISCOVERY_POLICY_VERSION, FirstPartyPersonDiscoverer, build_search_queries,
    discover_contacts, evaluate_person, extract_first_party_people, select_contacts,
)
from job_search.storage import connect_database, utc_now


def strategy_row(primary="Engineering Manager", secondary=None, **extra):
    row = {
        "job_id": 1, "company": "Acme", "company_website": "https://acme.test",
        "canonical_url": "https://acme.test/jobs/1", "description": "Build software.",
        "city": "Paris", "country": "France", "region": "Europe",
        "primary_role": primary,
        "secondary_roles_json": json.dumps(secondary or ["Technical Recruiter"]),
    }
    row.update(extra)
    return row


def person(name="Alice Martin", title="Engineering Manager", company="Acme", **extra):
    value = {
        "person_name": name, "current_title": title, "company": company,
        "source_url": f"https://linkedin.com/in/{name.casefold().replace(' ', '-')}",
        "source_type": "LINKEDIN", "result_snippet": f"{title} at {company}",
        "discovery_query": 'site:linkedin.com/in "Acme" "Engineering Manager"',
    }
    value.update(extra)
    return value


class PersonRuleTests(TestCase):
    def test_engineering_manager_role_match_and_current_company(self):
        result = evaluate_person(strategy_row(), person(), ["Engineering Manager"])
        self.assertEqual(result.target_role_category, "Engineering Manager")
        self.assertEqual(result.confidence, "MEDIUM")
        self.assertIn("CONTACT_CURRENT_COMPANY", result.reason_codes)

    def test_technical_recruiter_role_match(self):
        result = evaluate_person(
            strategy_row("Technical Recruiter", []),
            person(title="Talent Acquisition Partner - Engineering"),
            ["Technical Recruiter"],
        )
        self.assertEqual(result.target_role_category, "Technical Recruiter")

    def test_early_careers_role_match(self):
        result = evaluate_person(
            strategy_row("Early Careers Recruiter", []), person(title="University Recruiter"),
            ["Early Careers Recruiter"],
        )
        self.assertEqual(result.target_role_category, "Early Careers Recruiter")

    def test_former_employee_rejected(self):
        result = evaluate_person(
            strategy_row(), person(result_snippet="Former Engineering Manager at Acme"),
            ["Engineering Manager"],
        )
        self.assertEqual(result.selection_status, "REJECTED_FORMER_EMPLOYEE")

    def test_company_mismatch_rejected(self):
        result = evaluate_person(strategy_row(), person(company="OtherCo"), ["Engineering Manager"])
        self.assertEqual(result.selection_status, "REJECTED_COMPANY_MISMATCH")

    def test_generic_hr_and_ceo_are_not_role_equivalents(self):
        for title in ("HR Director", "Chief Executive Officer"):
            with self.subTest(title=title):
                result = evaluate_person(strategy_row(), person(title=title), ["Engineering Manager"])
                self.assertEqual(result.selection_status, "REJECTED_ROLE_MISMATCH")

    def test_region_aligned_person_is_preferred(self):
        local = person("Zoë Martin", result_snippet="Engineering Manager at Acme in Paris, France")
        remote = person("Alice Martin")
        result = select_contacts(strategy_row(), [remote, local])
        self.assertEqual(result.primary.person_name, "Zoë Martin")
        self.assertEqual(result.primary.confidence, "HIGH")

    def test_duplicate_person_is_merged(self):
        duplicate = person()
        result = select_contacts(strategy_row(), [duplicate, dict(duplicate)])
        self.assertEqual(len(result.candidates), 1)

    def test_no_confident_contact(self):
        result = select_contacts(strategy_row(), [person(company="OtherCo")])
        self.assertEqual(result.status, "NO_CONFIDENT_CONTACT")
        self.assertIsNone(result.primary)
        self.assertIsNone(result.backup)

    def test_exactly_one_primary_and_at_most_one_backup(self):
        candidates = [person("Alice Martin"), person("Bob Martin"), person("Chloé Martin")]
        result = select_contacts(strategy_row(), candidates)
        self.assertIsNotNone(result.primary)
        self.assertIsNotNone(result.backup)
        self.assertEqual(sum(item.selection_status == "SELECTED_PRIMARY" for item in result.candidates), 1)
        self.assertLessEqual(sum(item.selection_status == "SELECTED_BACKUP" for item in result.candidates), 1)

    def test_search_query_generation_is_bounded(self):
        row = strategy_row(secondary=["Technical Recruiter", "Head of Engineering"])
        self.assertEqual(build_search_queries(row, 2), [
            'site:linkedin.com/in "Acme" "Engineering Manager"',
            'site:linkedin.com/in "Acme" "Technical Recruiter"',
        ])

    def test_official_team_page_person_is_high_confidence(self):
        candidates = extract_first_party_people(
            """<article class="team-member"><h3>Alice Martin</h3>
                   <p>Software Engineering Manager</p></article>""",
            "https://acme.test/team", "Acme",
        )
        result = evaluate_person(strategy_row(), candidates[0], ["Engineering Manager"])
        self.assertEqual(result.confidence, "HIGH")
        self.assertIn("CONTACT_OFFICIAL_COMPANY_PAGE", result.reason_codes)

    def test_first_party_careers_page_and_page_bound(self):
        inspected = []

        def fetch(url, timeout):
            inspected.append(url)
            if url.endswith("/careers"):
                return """<article class="person-card"><h3>Sarah Jones</h3>
                          <p>Technical Recruiter</p></article>"""
            return "<html></html>"

        discoverer = FirstPartyPersonDiscoverer(fetcher=fetch)
        people, page_count = discoverer(strategy_row(), 5)
        self.assertEqual(page_count, 5)
        self.assertEqual(len(inspected), 5)
        self.assertEqual(people[0]["person_name"], "Sarah Jones")
        self.assertEqual(people[0]["source_url"], "https://acme.test/careers")

    def test_github_requires_profile_shape_and_explicit_company_title(self):
        from job_search.contact_discovery import _parse_search_result

        rejected = _parse_search_result({
            "title": "Acme engineering repositories", "url": "https://github.com/acme/platform",
            "result_snippet": "Engineering projects",
        }, "Acme", "acme.test", "query")
        self.assertIsNone(rejected)
        accepted = _parse_search_result({
            "title": "Alice Martin - Engineering Manager - Acme",
            "url": "https://github.com/alicemartin",
            "result_snippet": "Engineering Manager at Acme",
        }, "Acme", "acme.test", "query")
        self.assertEqual(accepted["source_type"], "GITHUB")


class DiscoveryPersistenceTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = connect_database(Path(self.temp.name) / "jobs.db")
        self.addCleanup(self.connection.close)

    def add_job(self, *, status="QUALIFIED", description="Build software.", primary="Engineering Manager"):
        now = utc_now()
        company = self.connection.execute(
            "SELECT company_id FROM companies WHERE normalized_domain='acme.test'"
        ).fetchone()
        company_id = company["company_id"] if company else self.connection.execute(
            """INSERT INTO companies
               (canonical_name,normalized_domain,website_url,first_seen_at,last_seen_at,created_at,updated_at)
               VALUES ('Acme','acme.test','https://acme.test',?,?,?,?)""",
            (now, now, now, now),
        ).lastrowid
        job_id = self.connection.execute("SELECT COALESCE(MAX(job_id),0)+1 FROM jobs").fetchone()[0]
        url = f"https://acme.test/jobs/{job_id}"
        self.connection.execute(
            """INSERT INTO jobs
               (job_id,company_id,canonical_url,title,description,location_text,country,region,city,
                first_seen_at,last_seen_at,status,content_hash,created_at,updated_at)
               VALUES (?,?,?,'Software Engineer',?,'Paris, France','France','Europe','Paris',
                       ?,?,'OPEN','hash',?,?)""",
            (job_id, company_id, url, description, now, now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_qualifications
               (job_id,policy_version,qualification_status,activity_status,employer_status,
                application_channel,reason_codes_json,evidence_json,input_evidence_hash,
                qualified_at,created_at,updated_at)
               VALUES (?,'q1',?,'ACTIVE','CONFIRMED','DIRECT_COMPANY','[]','[]','qh',?,?,?)""",
            (job_id, status, now, now, now),
        )
        self.connection.execute(
            """INSERT INTO job_contact_strategies
               (job_id,policy_version,primary_role,secondary_roles_json,avoid_roles_json,
                confidence,rationale,reason_codes_json,input_fingerprint,resolved_at,created_at,updated_at)
               VALUES (?,'s1',?,'[\"Technical Recruiter\"]','[\"CEO\",\"Generic HR\"]',
                       'HIGH','test','[]','sf',?,?,?)""",
            (job_id, primary, now, now, now),
        )
        self.connection.commit()
        return job_id

    @staticmethod
    def search_result(name="Alice Martin", title="Engineering Manager", company="Acme"):
        return {
            "title": f"{name} - {title} - {company} | LinkedIn",
            "url": f"https://linkedin.com/in/{name.casefold().replace(' ', '-')}",
            "person_name": name, "current_title": title, "company": company,
            "result_snippet": f"{title} at {company} in Paris, France",
        }

    def test_explicit_recruiter_in_posting_requires_no_search(self):
        job_id = self.add_job(
            description="Recruiter: Sarah Jones, Technical Recruiter.", primary="EXPLICIT_CONTACT",
        )
        searcher = Mock(return_value=[])
        result = discover_contacts(self.connection, [job_id], searcher=searcher)[0]
        self.assertEqual(result.primary.person_name, "Sarah Jones")
        self.assertEqual(result.primary.source_type, "JOB_POSTING")
        self.assertEqual(result.primary.confidence, "HIGH")

    def test_max_search_queries_enforced(self):
        job_id = self.add_job()
        searcher = Mock(return_value=[])
        result = discover_contacts(
            self.connection, [job_id], max_search_queries=1, searcher=searcher,
        )[0]
        self.assertEqual((searcher.call_count, result.search_queries_used), (1, 1))

    def test_search_failure_is_no_confident_contact_not_abort(self):
        job_id = self.add_job()
        result = discover_contacts(
            self.connection, [job_id], max_search_queries=1,
            searcher=Mock(side_effect=TimeoutError("search timed out")),
        )[0]
        self.assertEqual(result.status, "NO_CONFIDENT_CONTACT")

    def test_max_people_inspected_enforced(self):
        job_id = self.add_job()
        many = [self.search_result(f"Person {letter}") for letter in ("One", "Two", "Three")]
        result = discover_contacts(
            self.connection, [job_id], max_people=2, searcher=Mock(return_value=many),
        )[0]
        self.assertEqual(result.people_inspected, 2)

    def test_persistence_idempotence_and_qualification_unchanged(self):
        job_id = self.add_job()
        searcher = Mock(return_value=[self.search_result()])
        before = tuple(self.connection.execute(
            "SELECT qualification_status,updated_at FROM job_qualifications WHERE job_id=?", (job_id,),
        ).fetchone())
        first = discover_contacts(self.connection, [job_id], searcher=searcher)[0]
        calls_after_first = searcher.call_count
        stored_first = tuple(self.connection.execute(
            "SELECT selected_contact_id,created_at,updated_at FROM job_selected_contacts WHERE job_id=?",
            (job_id,),
        ).fetchone())
        second = discover_contacts(self.connection, [job_id], searcher=searcher)[0]
        stored_second = tuple(self.connection.execute(
            "SELECT selected_contact_id,created_at,updated_at FROM job_selected_contacts WHERE job_id=?",
            (job_id,),
        ).fetchone())
        after = tuple(self.connection.execute(
            "SELECT qualification_status,updated_at FROM job_qualifications WHERE job_id=?", (job_id,),
        ).fetchone())
        self.assertEqual(first.primary.person_name, "Alice Martin")
        self.assertTrue(second.reused)
        self.assertEqual(searcher.call_count, calls_after_first)
        self.assertEqual(stored_first, stored_second)
        self.assertEqual(before, after)

    def test_only_qualified_jobs_with_strategy_are_eligible(self):
        qualified = self.add_job()
        review = self.add_job(status="REVIEW")
        result = discover_contacts(
            self.connection, [qualified, review], searcher=Mock(return_value=[]),
        )
        self.assertEqual([item.job_id for item in result], [qualified])

    def test_schema_contains_no_email_fields(self):
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(job_contact_candidates)")
        }
        self.assertFalse(any("email" in name.casefold() for name in columns))

    def test_v2_reruns_without_overwriting_v1_audit(self):
        job_id = self.add_job()
        now = utc_now()
        candidate_id = self.connection.execute(
            """INSERT INTO job_contact_candidates
               (job_id,policy_version,person_key,person_name,current_title,company,
                target_role_category,source_url,source_type,relationship_to_job,confidence,
                selection_status,reason_codes_json,evidence_json,discovery_query,input_fingerprint,
                checked_at,discovered_at,updated_at)
               VALUES (?,'contact-discovery-v1','v1-key','Old Candidate','Engineering Manager',
                       'Acme','Engineering Manager','https://linkedin.com/in/old','LINKEDIN',
                       'COMPANY_HIRING_ROLE','MEDIUM','SELECTED_PRIMARY','[]','[]','old-query',
                       'same-fingerprint',?,?,?)""",
            (job_id, now, now, now),
        ).lastrowid
        self.connection.execute(
            """INSERT INTO job_selected_contacts
               (job_id,policy_version,discovery_status,primary_contact_candidate_id,
                search_queries_used,people_inspected,input_fingerprint,discovered_at,
                created_at,updated_at,first_party_pages_inspected)
               VALUES (?,'contact-discovery-v1','CONTACTS_SELECTED',?,1,1,
                       'same-fingerprint',?,?,?,0)""",
            (job_id, candidate_id, now, now, now),
        )
        self.connection.commit()
        result = discover_contacts(
            self.connection, [job_id], max_search_queries=1, searcher=Mock(return_value=[]),
        )[0]
        self.assertEqual(DISCOVERY_POLICY_VERSION, "contact-discovery-v2")
        self.assertFalse(result.reused)
        versions = [row[0] for row in self.connection.execute(
            "SELECT policy_version FROM job_selected_contacts WHERE job_id=? ORDER BY policy_version",
            (job_id,),
        )]
        self.assertEqual(versions, ["contact-discovery-v1", "contact-discovery-v2"])
