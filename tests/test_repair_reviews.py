"""Offline regression tests for deterministic REVIEW repair."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from job_search.filtering import evaluate_job, filter_stored_jobs
from job_search.geography import infer_title_location
from job_search.providers import (
    ParsedJob, classify_source_context, infer_trusted_company, parse_job_html,
)
from job_search.repair_reviews import cleanup_invalid_repairs, repair_review_jobs
from job_search.storage import connect_database, upsert_job


NOW = "2026-09-12T10:00:00+00:00"


class ReviewRepairTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.connection = connect_database(Path(self.temporary.name) / "jobs.db")
        self.addCleanup(self.connection.close)

    def add_job(self, suffix, filter_status="REVIEW", **values):
        defaults = dict(
            canonical_url=f"https://careers.example.test/job/{suffix}",
            provider="generic", source_job_id=suffix,
            title="Junior Backend Developer", fetch_status="FETCHED",
        )
        defaults.update(values)
        job_id, _ = upsert_job(self.connection, ParsedJob(**defaults), "query", NOW)
        filter_stored_jobs(self.connection, "v1.1")
        self.connection.execute(
            "UPDATE job_filter_results SET status=? WHERE job_id=? AND policy_version='v1.1'",
            (filter_status, job_id),
        )
        self.connection.commit()
        return job_id

    @staticmethod
    def parsed(url, **values):
        result = ParsedJob(url, "generic", fetch_status="FETCHED", **values)
        for name, value in values.items():
            if value:
                result.evidence_sources[name] = "JSON_LD"
        return result

    def test_only_review_jobs_are_selected_and_pass_reject_are_untouched(self):
        review = self.add_job("review")
        passed = self.add_job("pass", "PASS")
        rejected = self.add_job("reject", "REJECT")
        fetched = Mock(side_effect=lambda url, timeout: self.parsed(url, description="Build APIs."))
        summary = repair_review_jobs(self.connection, fetcher=fetched)
        self.assertEqual(summary["selected"], 1)
        self.assertEqual(fetched.call_count, 1)
        self.assertEqual(self.connection.execute("SELECT description FROM jobs WHERE job_id=?", (review,)).fetchone()[0], "Build APIs.")
        for job_id in (passed, rejected):
            self.assertIsNone(self.connection.execute("SELECT description FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0])

    def test_json_ld_fills_company_location_description_date_and_employment(self):
        job_id = self.add_job("json")
        html = '''<script type="application/ld+json">{
          "@type":"JobPosting", "title":"Junior Backend Developer",
          "description":"<p>Build APIs.</p>", "datePosted":"2026-09-10",
          "employmentType":"FULL_TIME",
          "hiringOrganization":{"name":"Acme"},
          "jobLocation":{"address":{"addressLocality":"Nantes","addressCountry":"FR"}}
        }</script>'''
        fetched = lambda url, timeout: parse_job_html(url, html)
        summary = repair_review_jobs(self.connection, fetcher=fetched)
        row = self.connection.execute(
            """SELECT j.*, c.canonical_name company FROM jobs j
               LEFT JOIN companies c ON c.company_id=j.company_id WHERE j.job_id=?""",
            (job_id,),
        ).fetchone()
        self.assertEqual(summary["repaired"], 1)
        self.assertEqual((row["company"], row["location_text"], row["city"], row["country"]), ("Acme", "Nantes, FR", "Nantes", "FR"))
        self.assertEqual((row["description"], row["published_at"], row["employment_type"]), ("Build APIs.", "2026-09-10", "FULL_TIME"))
        audit = self.connection.execute("SELECT * FROM job_repair_results WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(audit["status"], "REPAIRED")
        self.assertIn("jsonld_hiringOrganization", audit["source_type"])

    def test_nonblank_fields_are_preserved(self):
        job_id = self.add_job(
            "preserve", company_name="Original", location_text="Tunis",
            country="TN", city="Tunis", description="Original description",
            published_at="2026-09-01", employment_type="CONTRACT",
        )
        incoming = self.parsed(
            "https://careers.example.test/job/preserve", company_name="Replacement",
            location_text="Paris", country="FR", city="Paris",
            description="Replacement", published_at="2026-09-10",
            employment_type="FULL_TIME",
        )
        repair_review_jobs(self.connection, fetcher=lambda url, timeout: incoming)
        row = self.connection.execute(
            """SELECT j.*, c.canonical_name company FROM jobs j
               LEFT JOIN companies c ON c.company_id=j.company_id WHERE j.job_id=?""",
            (job_id,),
        ).fetchone()
        self.assertEqual((row["company"], row["location_text"], row["country"], row["city"]), ("Original", "Tunis", "TN", "Tunis"))
        self.assertEqual((row["description"], row["published_at"], row["employment_type"]), ("Original description", "2026-09-01", "CONTRACT"))
        self.assertEqual(self.connection.execute("SELECT status FROM job_repair_results WHERE job_id=?", (job_id,)).fetchone()[0], "NO_CHANGE")

    def test_invalid_published_date_is_not_saved(self):
        job_id = self.add_job("bad-date")
        incoming = self.parsed("https://careers.example.test/job/bad-date", published_at="yesterday")
        repair_review_jobs(self.connection, fetcher=lambda url, timeout: incoming)
        self.assertIsNone(self.connection.execute("SELECT published_at FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0])

    def test_http_success_does_not_start_browser(self):
        self.add_job("http")
        factory = Mock()
        repair_review_jobs(
            self.connection,
            fetcher=lambda url, timeout: self.parsed(url, description="Useful"),
            browser_fallback=True, driver_factory=factory,
        )
        factory.assert_not_called()

    def test_http_failure_can_use_one_browser_session(self):
        first = self.add_job("browser-one")
        second = self.add_job("browser-two")
        failure = lambda url, timeout: ParsedJob(url, "generic", fetch_status="FAILED", fetch_error="timeout")
        driver = Mock()
        factory = Mock(return_value=driver)
        browser = Mock(side_effect=lambda url, timeout, current: self.parsed(url, description="Rendered"))
        summary = repair_review_jobs(
            self.connection, fetcher=failure, browser_fallback=True,
            driver_factory=factory, browser_fetcher=browser,
        )
        self.assertEqual(summary["repaired"], 2)
        factory.assert_called_once_with(windowed=False)
        self.assertEqual(browser.call_count, 2)
        driver.quit.assert_called_once()
        for job_id in (first, second):
            self.assertEqual(self.connection.execute("SELECT description FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0], "Rendered")

    def test_browser_failure_leaves_job_unchanged_and_is_blocked(self):
        job_id = self.add_job("blocked")
        failure = lambda url, timeout: ParsedJob(url, "generic", fetch_status="FAILED", fetch_error="HTTP 403")
        summary = repair_review_jobs(
            self.connection, fetcher=failure, browser_fallback=True,
            driver_factory=Mock(return_value=Mock()), browser_fetcher=Mock(return_value=None),
        )
        self.assertEqual(summary["blocked"], 1)
        self.assertIsNone(self.connection.execute("SELECT description FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0])
        self.assertEqual(self.connection.execute("SELECT status FROM job_repair_results WHERE job_id=?", (job_id,)).fetchone()[0], "BLOCKED")

    def test_transaction_rolls_back_job_and_company_on_update_failure(self):
        job_id = self.add_job("rollback")
        self.connection.execute(
            f"""CREATE TRIGGER fail_repair BEFORE UPDATE OF description ON jobs
                WHEN NEW.job_id={job_id} BEGIN SELECT RAISE(ABORT, 'test failure'); END"""
        )
        incoming = self.parsed(
            "https://careers.example.test/job/rollback",
            company_name="Transient Company", description="Should roll back",
        )
        summary = repair_review_jobs(self.connection, fetcher=lambda url, timeout: incoming)
        self.assertEqual(summary["failed"], 1)
        row = self.connection.execute("SELECT company_id, description FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(tuple(row), (None, None))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM companies WHERE canonical_name='Transient Company'").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT status FROM job_repair_results WHERE job_id=?", (job_id,)).fetchone()[0], "FAILED")

    def test_rerun_is_idempotent(self):
        job_id = self.add_job("idempotent")
        incoming = self.parsed(
            "https://careers.example.test/job/idempotent",
            company_name="Acme", description="Stable",
        )
        first = repair_review_jobs(self.connection, fetcher=lambda url, timeout: incoming)
        second = repair_review_jobs(self.connection, fetcher=lambda url, timeout: incoming)
        self.assertEqual((first["repaired"], second["no_change"]), (1, 1))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM companies WHERE canonical_name='Acme'").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT description FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0], "Stable")

    def test_job_id_targets_one_review(self):
        first = self.add_job("target-one")
        second = self.add_job("target-two")
        repair_review_jobs(
            self.connection, job_ids=[second],
            fetcher=lambda url, timeout: self.parsed(url, description="Targeted"),
        )
        self.assertIsNone(self.connection.execute("SELECT description FROM jobs WHERE job_id=?", (first,)).fetchone()[0])
        self.assertEqual(self.connection.execute("SELECT description FROM jobs WHERE job_id=?", (second,)).fetchone()[0], "Targeted")

    def test_final_pass_is_attempted_only_once_and_excluded_later(self):
        job_id = self.add_job("final-once")
        first_fetch = Mock(return_value=self.parsed(
            "https://careers.example.test/job/final-once", description="Final attempt",
        ))
        first = repair_review_jobs(
            self.connection, job_ids=[job_id], fetcher=first_fetch,
            final_pass=True, refilter=True,
        )
        second_fetch = Mock()
        second = repair_review_jobs(
            self.connection, job_ids=[job_id], fetcher=second_fetch,
            final_pass=True, refilter=True,
        )
        automatic = repair_review_jobs(self.connection, fetcher=second_fetch)
        self.assertEqual((first["attempted"], second["attempted"], automatic["attempted"]), (1, 0, 0))
        first_fetch.assert_called_once()
        second_fetch.assert_not_called()
        audit = self.connection.execute(
            "SELECT final_pass FROM job_repair_results WHERE job_id=?", (job_id,),
        ).fetchone()
        self.assertEqual(audit["final_pass"], 1)

    def test_limit_is_respected(self):
        ids = [self.add_job(f"limit-{index}") for index in range(3)]
        summary = repair_review_jobs(
            self.connection, limit=2,
            fetcher=lambda url, timeout: self.parsed(url, description="Limited"),
        )
        self.assertEqual((summary["selected"], summary["attempted"]), (2, 2))
        descriptions = [self.connection.execute("SELECT description FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0] for job_id in ids]
        self.assertEqual(descriptions, ["Limited", "Limited", None])

    def test_refilter_updates_only_repaired_job(self):
        repaired = self.add_job("refilter")
        untouched = self.add_job("refilter-untouched")
        before = self.connection.execute("SELECT evaluated_at FROM job_filter_results WHERE job_id=?", (untouched,)).fetchone()[0]
        summary = repair_review_jobs(
            self.connection, job_ids=[repaired], refilter=True,
            fetcher=lambda url, timeout: self.parsed(
                url, location_text="Tunis, Tunisia", country="TN", city="Tunis",
                description="Python APIs. One year of experience.", published_at="2026-09-10",
            ),
        )
        self.assertEqual(sum(summary["refilter"].values()), 1)
        after = self.connection.execute("SELECT evaluated_at FROM job_filter_results WHERE job_id=?", (untouched,)).fetchone()[0]
        self.assertEqual(before, after)

    def test_title_and_url_patterns_are_strong_and_ambiguous_title_is_ignored(self):
        title = parse_job_html(
            "https://example.test/job/1",
            "<main><h1>Développeur Full Stack Java Junior - Nantes - F/H</h1></main>",
        )
        url = parse_job_html(
            "https://example.test/job/Casablanca-D%C3%A9veloppeur-Full-Stack-Junior/1368",
            "<main><h1>Développeur Full Stack Junior</h1></main>",
        )
        ambiguous = parse_job_html(
            "https://example.test/job/3", "<main><h1>Backend Developer Paris CDI</h1></main>",
        )
        em_dash = parse_job_html(
            "https://example.test/job/4", "<main><h1>Backend Developer — Paris — CDI</h1></main>",
        )
        self.assertEqual((title.location_text, title.evidence_sources["location_text"]), ("Nantes", "title_structured_location"))
        self.assertEqual((url.location_text, url.evidence_sources["location_text"]), ("Casablanca", "url_structured_location"))
        self.assertEqual(em_dash.location_text, "Paris")
        self.assertEqual(ambiguous.location_text, "")

    def test_bitwarden_inertia_job_payload_requires_matching_greenhouse_id(self):
        html = '''<div id="app" data-page="{&quot;component&quot;:&quot;careers-page&quot;,
          &quot;props&quot;:{&quot;job&quot;:{&quot;greenhouse_id&quot;:7872214003,
          &quot;location&quot;:&quot;Noida, Delhi NCR&quot;,
          &quot;content&quot;:&quot;&lt;p&gt;Build secure services.&lt;/p&gt;&quot;,
          &quot;createdAt&quot;:&quot;2026-09-07T02:08:48-04:00&quot;}}}"></div>'''
        matched = parse_job_html(
            "https://bitwarden.com/careers/7872214003?gh_jid=7872214003", html,
        )
        mismatched = parse_job_html(
            "https://bitwarden.com/careers/7872214003?gh_jid=999", html,
        )
        self.assertEqual(matched.location_text, "Noida, Delhi NCR")
        self.assertEqual(matched.description, "Build secure services.")
        self.assertEqual(matched.published_at, "2026-09-07T02:08:48-04:00")
        self.assertEqual(matched.evidence_sources["description"], "provider_description_field")
        self.assertEqual((mismatched.location_text, mismatched.description), ("", ""))

    def test_waytolearnx_job_block_uses_company_and_idf_as_region(self):
        parsed = parse_job_html(
            "https://jobs.waytolearnx.com/job/backend-junior-idf",
            '''<section class="job-detail-section"><div class="job-block-seven">
              <span class="company-logo"><img alt="Capgemini"></span>
              <h4>Développeuse / Développeur Backend Junior en CDI H/F - IDF</h4>
              <ul class="job-info"><li>Paris</li></ul>
            </div></section>''',
        )
        self.assertEqual(parsed.company_name, "Capgemini")
        self.assertEqual((parsed.location_text, parsed.region, parsed.country, parsed.city), ("Île-de-France", "Île-de-France", "France", ""))
        self.assertEqual(parsed.evidence_sources["company_name"], "provider_company_field")

    def test_welovedevs_server_payload_extracts_reusable_job_fields(self):
        parsed = parse_job_html(
            "https://welovedevs.com/fr/app/job/backend-developer-nestjs-hf-startup",
            '''<main><h1>Backend Developer – Nest.js (H/F) #StartUp</h1>
              <a href="https://jobs.stationf.co/companies/circularplace/jobs/backend_paris"><span>CircularPlace</span></a>
              <a href="https://www.google.com/maps/search/?api=1&amp;query=Paris">Paris, France</a>
              <span class="prose"><p>Build and maintain REST APIs.</p></span>
            </main><script>self.__next_f.push([1,"{\\"publishDate\\":1712734077409,\\"seoAlias\\":\\"backend-developer-nestjs-hf-startup\\"}"])</script>''',
        )
        self.assertEqual(parsed.company_name, "CircularPlace")
        self.assertEqual((parsed.location_text, parsed.country, parsed.city), ("Paris, France", "France", "Paris"))
        self.assertEqual(parsed.description, "Build and maintain REST APIs.")
        self.assertEqual(parsed.published_at, "2024-04-10")

    def test_role_vocabulary_and_idf_never_become_derived_city(self):
        cases = (
            "developpeuse-developpeur-backend-junior-en-cdi-hf-idf",
            "developer-developer-backend", "engineer-developer-python",
            "junior-developer-react", "backend-developer-java",
            "idf-developpeur-full-stack",
        )
        for slug in cases:
            with self.subTest(slug=slug):
                parsed = parse_job_html(
                    f"https://example.test/job/{slug}",
                    "<main><h1>Développeuse / Développeur Backend Junior en CDI H/F - IDF</h1></main>",
                )
                self.assertEqual(parsed.location_text, "")
                self.assertEqual(parsed.city, "")

    def test_alias_and_weak_platform_brand_are_not_companies(self):
        alias = parse_job_html(
            "https://example.test/job/alias",
            '''<main itemscope itemtype="https://schema.org/JobPosting">
              <div itemprop="hiringOrganization"><span itemprop="name">alias</span></div>
            </main>''',
        )
        platform = parse_job_html(
            "https://example.test/job/platform",
            '<meta property="og:site_name" content="WIZBII"><main><h1>Developer</h1></main>',
        )
        weak_title = parse_job_html(
            "https://example.test/job/weak",
            '<title>Developer - Imagined Employer</title>',
        )
        self.assertEqual(alias.company_name, "")
        self.assertEqual(platform.company_name, "")
        self.assertEqual(weak_title.company_name, "")

    def test_wizbii_is_accepted_only_as_explicit_jsonld_organization(self):
        parsed = parse_job_html(
            "https://example.test/job/structured",
            '''<script type="application/ld+json">{
              "@type":"JobPosting", "hiringOrganization":{"name":"WIZBII"}
            }</script>''',
        )
        self.assertEqual(parsed.company_name, "WIZBII")
        self.assertEqual(parsed.evidence_sources["company_name"], "jsonld_hiringOrganization")

    def test_repair_write_boundary_discards_invalid_parser_values(self):
        job_id = self.add_job("write-boundary")
        incoming = self.parsed(
            "https://careers.example.test/job/write-boundary",
            company_name="alias", location_text="developpeuse", city="developpeuse",
        )
        incoming.evidence_sources.update({
            "company_name": "MICRODATA", "location_text": "URL_PATTERN", "city": "URL_PATTERN",
        })
        summary = repair_review_jobs(self.connection, fetcher=lambda url, timeout: incoming)
        row = self.connection.execute("SELECT company_id, location_text, city FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(tuple(row), (None, None, None))
        self.assertEqual(summary["no_change"], 1)

    def test_cleanup_removes_only_invalid_repair_derived_values_and_refilters(self):
        job_id = self.add_job("legacy-bad", description="Legitimate source description")
        company = self.connection.execute(
            """INSERT INTO companies
               (canonical_name, first_seen_at, last_seen_at, created_at, updated_at)
               VALUES ('alias', ?, ?, ?, ?)""", (NOW, NOW, NOW, NOW),
        ).lastrowid
        self.connection.execute(
            "UPDATE jobs SET company_id=?, location_text='developpeuse', city='developpeuse' WHERE job_id=?",
            (company, job_id),
        )
        self.connection.execute(
            """INSERT INTO job_repair_results
               (job_id,policy_version,attempted_at,status,fields_filled_json,source_type,created_at,updated_at)
               VALUES (?, 'v1.1', ?, 'REPAIRED', ?, 'MICRODATA,URL_PATTERN', ?, ?)""",
            (job_id, NOW,
             '{"company_name":"MICRODATA","location_text":"URL_PATTERN","city":"URL_PATTERN"}',
             NOW, NOW),
        )
        self.connection.commit()
        first = cleanup_invalid_repairs(self.connection, [job_id], refilter=True)
        row = self.connection.execute("SELECT company_id, location_text, city, description FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(tuple(row), (None, None, None, "Legitimate source description"))
        self.assertEqual(first["cleaned"], 1)
        self.assertEqual(sum(first["refilter"].values()), 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM job_repair_cleanups WHERE job_id=?", (job_id,)).fetchone()[0], 1)
        status = self.connection.execute("SELECT status FROM job_filter_results WHERE job_id=? AND policy_version='v1.1'", (job_id,)).fetchone()[0]
        second = cleanup_invalid_repairs(self.connection, [job_id], refilter=True)
        self.assertEqual(second["no_change"], 1)
        self.assertEqual(self.connection.execute("SELECT status FROM job_filter_results WHERE job_id=? AND policy_version='v1.1'", (job_id,)).fetchone()[0], status)

    def test_job_specific_title_repairs_slogan_but_valid_title_beats_url_slug(self):
        url = "https://craegroup.com/careers/laravel-backend-developer?cid=one"
        slogan = self.add_job(
            "slogan", canonical_url=url,
            title="CRAE Group — The technology partner for digital operators",
            company_name="CRAE Group",
        )
        parsed = parse_job_html(url, '''<head>
          <title>CRAE Group — The technology partner for digital operators</title>
          <meta property="og:title" content="CRAE Group — The technology partner for digital operators">
          <meta property="og:site_name" content="CRAE Group"></head><div id="root"></div>''')
        self.assertEqual(parsed.title, "Laravel Backend Developer")
        repair_review_jobs(self.connection, job_ids=[slogan], fetcher=lambda url, timeout: parsed)
        self.assertEqual(
            self.connection.execute("SELECT title FROM jobs WHERE job_id=?", (slogan,)).fetchone()[0],
            "Laravel Backend Developer",
        )

        valid = self.add_job(
            "valid-title", canonical_url="https://craegroup.com/careers/python-developer",
            title="Junior Backend Developer",
        )
        incoming = parse_job_html(
            "https://craegroup.com/careers/python-developer", "<div id='root'></div>",
        )
        repair_review_jobs(self.connection, job_ids=[valid], fetcher=lambda url, timeout: incoming)
        self.assertEqual(
            self.connection.execute("SELECT title FROM jobs WHERE job_id=?", (valid,)).fetchone()[0],
            "Junior Backend Developer",
        )

    def test_tanitjobs_generic_title_is_repaired_from_individual_url(self):
        url = (
            "https://www.tanitjobs.com/job/754705/software-engineer?"
            "__cf_chl_rt_tk=ephemeral"
        )
        job_id = self.add_job(
            "tanit-generic-title", canonical_url=url,
            title="Offres d'emploi et travail en Tunisie",
        )
        parsed = parse_job_html(url, "<title>Offres d'emploi et travail en Tunisie</title>")
        summary = repair_review_jobs(
            self.connection, job_ids=[job_id],
            fetcher=lambda current_url, timeout: parsed,
        )
        self.assertEqual(summary["repaired"], 1)
        stored = self.connection.execute(
            "SELECT title FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()[0]
        self.assertEqual(stored, "Software Engineer")

    def test_chennai_and_paris_title_location_completion(self):
        chennai = parse_job_html(
            "https://internshala.com/job/detail/example",
            "<main><h1>Full Stack Developer Job in Chennai at Indsafri</h1></main>",
        )
        paris = parse_job_html(
            "https://example.test/job/80",
            "<main><h1>Stage Software Engineer - Paris - H/F/X</h1></main>",
        )
        self.assertEqual((chennai.location_text, chennai.city, chennai.country), ("Chennai", "Chennai", "India"))
        self.assertEqual((paris.location_text, paris.city, paris.country), ("Paris", "Paris", "France"))

    def test_greenhouse_embedded_job_data_has_provider_precedence(self):
        html = '''<script>window.__remixContext = {"state":{"job":{
          "title":"Software Engineer - Security Features",
          "company_name":"Dashlane", "job_post_location":"Paris, France",
          "published_at":"2026-09-02T06:36:11-04:00",
          "content":"<p>Minimum 3 years of experience.</p>"}}};</script>
          <script type="application/ld+json">{"@type":"JobPosting",
          "title":"Wrong weaker title", "datePosted":"2020-01-01",
          "jobLocation":{"address":{"addressLocality":"London","addressCountry":"GB"}}}</script>'''
        parsed = parse_job_html(
            "https://job-boards.greenhouse.io/dashlane/jobs/8169067", html,
        )
        self.assertEqual(parsed.title, "Software Engineer - Security Features")
        self.assertEqual(parsed.company_name, "Dashlane")
        self.assertEqual((parsed.location_text, parsed.city, parsed.country), ("Paris, France", "Paris", "France"))
        self.assertEqual(parsed.published_at, "2026-09-02T06:36:11-04:00")
        self.assertEqual(parsed.evidence_sources["published_at"], "provider_date_field")

    def test_short_placeholder_description_is_repairable_by_full_structured_posting(self):
        job_id = self.add_job("short-description", description="Security role.")
        incoming = self.parsed(
            "https://careers.example.test/job/short-description",
            description="Detailed job-specific responsibilities and requirements. " * 6,
        )
        repair_review_jobs(self.connection, job_ids=[job_id], fetcher=lambda url, timeout: incoming)
        description = self.connection.execute(
            "SELECT description FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()[0]
        self.assertGreater(len(description), 200)

    def test_known_wrapper_label_is_not_saved_as_employer(self):
        parsed = ParsedJob(
            "https://job-boards.greenhouse.io/wrapper/jobs/1", "greenhouse",
            company_name="LinkedIn Job Wrapping", fetch_status="FETCHED",
        )
        parsed.evidence_sources["company_name"] = "provider_company_field"
        job_id = self.add_job("wrapper-company", provider="greenhouse")
        repair_review_jobs(self.connection, job_ids=[job_id], fetcher=lambda url, timeout: parsed)
        self.assertIsNone(
            self.connection.execute("SELECT company_id FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        )

    def test_source_type_is_separate_from_employer_relationship(self):
        direct_unknown = classify_source_context(
            "https://jobs.lever.co/acme/one", "lever", "Acme",
        )
        recruiter = classify_source_context(
            "https://jobs.lever.co/jobgether/one", "lever", "",
        )
        self.assertEqual((direct_unknown.source_type, direct_unknown.employer_relationship), ("ATS", "UNKNOWN"))
        self.assertEqual((recruiter.source_type, recruiter.employer_relationship), ("ATS", "RECRUITER"))

    def test_direct_hosted_and_platform_source_classification(self):
        direct = classify_source_context(
            "https://group.bnpparibas/en/careers/job-offer/developer", "generic", "",
        )
        hosted = classify_source_context(
            "https://careers-page.com/keystone-solutions/job/ABC", "generic", "Keystone Solutions",
        )
        platform = classify_source_context(
            "https://careerkit.me/jobs/ch/backend-developer-example", "generic", "Example",
        )
        self.assertEqual((direct.source_type, direct.employer_relationship), ("COMPANY_SITE", "DIRECT"))
        self.assertEqual((hosted.source_type, hosted.employer_relationship), ("ATS", "UNKNOWN"))
        self.assertEqual((platform.source_type, platform.employer_relationship), ("JOB_PLATFORM", "AGGREGATOR"))

    def test_company_recovery_requires_trusted_or_cross_validated_evidence(self):
        direct = infer_trusted_company(
            "https://group.bnpparibas/en/careers/job-offer/developer", "Job offer AI Engineer",
        )
        tenant = infer_trusted_company(
            "https://careers-page.com/keystone-solutions/job/ABC",
            "Backend developer - Keystone Solutions | Career Page",
        )
        arbitrary = infer_trusted_company(
            "https://careers-page.com/imagined-company/job/ABC", "Backend Developer",
        )
        mismatch = infer_trusted_company(
            "https://careers-page.com/imagined-company/job/ABC",
            "Backend Developer - Different Company | Career Page",
        )
        self.assertEqual(direct, ("BNP Paribas", "direct_domain_company"))
        self.assertEqual(tenant, ("Keystone Solutions", "validated_hosted_tenant_title"))
        self.assertEqual(arbitrary, ("", ""))
        self.assertEqual(mismatch, ("", ""))

    def test_ile_de_france_title_evidence_is_canonical_and_filter_consistent(self):
        title = "Développeur/se - Fullstack Java Angular - Ile-de-France (H/F)"
        parsed = parse_job_html(
            "https://tuniatlas.com/jobs/81c02a2a-2e61-465e-868c-a0f4c930e56e",
            f'''<span class="pill good">Publiée le 08 Sep 2026</span>
                <h1 class="page-title">{title}</h1>
                <p class="job-desc">Detailed job-specific responsibilities and requirements.</p>''',
        )
        evidence = infer_title_location(title)
        self.assertIsNotNone(evidence)
        self.assertEqual((parsed.location_text, parsed.region, parsed.country, parsed.city), ("Île-de-France", "Île-de-France", "France", ""))
        self.assertEqual(parsed.description, "Detailed job-specific responsibilities and requirements.")
        self.assertEqual(parsed.published_at, "2026-09-08")
        decision = evaluate_job({
            "title": title, "description": "", "location_text": "",
            "city": "", "country": "", "remote_policy": "",
            "published_at": "", "canonical_url": "https://tuniatlas.com/jobs/one",
        })
        self.assertEqual(decision.matched_terms["normalized_country"], ["France"])
        self.assertIn("REVIEW_LOCATION_PREFERRED_MARKET", [item.code for item in decision.reasons])

        job_id = self.add_job("idf-canonical", title=title)
        repair_review_jobs(
            self.connection, job_ids=[job_id],
            fetcher=lambda requested, timeout: parsed,
        )
        stored = self.connection.execute(
            "SELECT location_text,region,country,city FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        audit = self.connection.execute(
            "SELECT field_changes_json FROM job_repair_results WHERE job_id=? ORDER BY repair_id DESC",
            (job_id,),
        ).fetchone()[0]
        changes = json.loads(audit)
        self.assertEqual(tuple(stored), ("Île-de-France", "Île-de-France", "France", None))
        self.assertEqual(changes["region"]["method"], "TITLE_INFERENCE")

    def test_trusted_company_repair_records_existing_provenance(self):
        url = "https://group.bnpparibas/en/careers/job-offer/ai-ml-junior-developer"
        job_id = self.add_job("bnp", canonical_url=url)
        failure = ParsedJob(url, "generic", fetch_status="FAILED", fetch_error="HTTP 403")
        repair_review_jobs(
            self.connection, job_ids=[job_id],
            fetcher=lambda requested, timeout: failure,
        )
        row = self.connection.execute(
            """SELECT c.canonical_name FROM jobs j LEFT JOIN companies c
               ON c.company_id=j.company_id WHERE j.job_id=?""", (job_id,),
        ).fetchone()
        audit = self.connection.execute(
            "SELECT field_changes_json FROM job_repair_results WHERE job_id=? ORDER BY repair_id DESC",
            (job_id,),
        ).fetchone()[0]
        change = json.loads(audit)["company_name"]
        self.assertEqual(row[0], "BNP Paribas")
        self.assertEqual(change["source"], "direct_domain_company")
        self.assertEqual(change["method"], "DIRECT_DOMAIN")

    def test_valid_zurich_data_survives_platform_metadata_repair(self):
        url = "https://careerkit.me/jobs/ch/backend-developer-intern-paretolabs-zurich-1i74d79"
        job_id = self.add_job(
            "zurich", canonical_url=url, company_name="ParetoLabs",
            location_text="Zürich, ZH, CH", city="Zürich", country="CH",
            description="Detailed posting content. " * 20,
            published_at="2026-08-05T20:17:44.67+00:00",
        )
        self.connection.execute(
            "UPDATE job_sources SET source_type='UNKNOWN', employer_relationship='UNKNOWN' WHERE job_id=?",
            (job_id,),
        )
        self.connection.commit()
        incoming = self.parsed(
            url, company_name="ParetoLabs", location_text="Zürich, ZH, CH",
            city="Zürich", country="CH", description="Replacement text",
            published_at="2026-09-10",
        )
        first = repair_review_jobs(
            self.connection, job_ids=[job_id], fetcher=lambda requested, timeout: incoming,
        )
        second = repair_review_jobs(
            self.connection, job_ids=[job_id], fetcher=lambda requested, timeout: incoming,
        )
        row = self.connection.execute(
            """SELECT c.canonical_name,j.location_text,j.city,j.country,j.description,
                      j.published_at,s.source_type,s.employer_relationship
               FROM jobs j LEFT JOIN companies c ON c.company_id=j.company_id
               JOIN job_sources s ON s.job_id=j.job_id WHERE j.job_id=?""", (job_id,),
        ).fetchone()
        self.assertEqual(tuple(row), (
            "ParetoLabs", "Zürich, ZH, CH", "Zürich", "CH",
            "Detailed posting content. " * 20,
            "2026-08-05T20:17:44.67+00:00", "JOB_PLATFORM", "AGGREGATOR",
        ))
        self.assertEqual((first["repaired"], second["no_change"]), (1, 1))

    def test_multiple_explicit_job_ids_bound_execution(self):
        ids = [self.add_job(f"bounded-{index}") for index in range(5)]
        selected = {ids[index] for index in (0, 2, 4)}
        fetched = Mock(side_effect=lambda url, timeout: self.parsed(url, description="Selected"))
        summary = repair_review_jobs(
            self.connection, job_ids=list(selected), fetcher=fetched,
        )
        self.assertEqual((summary["selected"], fetched.call_count), (3, 3))
        changed = {
            row["job_id"] for row in self.connection.execute(
                "SELECT job_id FROM jobs WHERE description='Selected'"
            )
        }
        self.assertEqual(changed, selected)

    def test_generic_metadata_is_not_promoted_to_job_description(self):
        parsed = parse_job_html(
            "https://craegroup.com/careers/laravel-backend-developer",
            '<head><meta property="og:description" content="Company homepage slogan"></head>',
        )
        self.assertEqual(parsed.description, "")

    def test_completion_audits_field_changes_and_explicit_experience_only(self):
        job_id = self.add_job("experience-audit")
        incoming = self.parsed(
            "https://careers.example.test/job/experience-audit",
            description="Build APIs. Minimum 3 years of experience.",
        )
        repair_review_jobs(self.connection, job_ids=[job_id], fetcher=lambda url, timeout: incoming)
        audit = self.connection.execute(
            "SELECT * FROM job_repair_results WHERE job_id=? ORDER BY repair_id DESC", (job_id,),
        ).fetchone()
        changes = json.loads(audit["field_changes_json"])
        evidence = json.loads(audit["experience_evidence_json"])
        self.assertEqual(changes["description"]["old_value"], "")
        self.assertEqual(changes["description"]["method"], "JSON_LD")
        self.assertEqual((evidence[0]["minimum"], evidence[0]["text"]), (3, "Minimum 3 years of experience"))
        self.assertIn(audit["completion_status"], {"SUCCESS", "PARTIAL"})

        vague = self.add_job("vague-experience", description="We need an experienced developer.")
        repair_review_jobs(
            self.connection, job_ids=[vague],
            fetcher=lambda url, timeout: self.parsed(url),
        )
        vague_audit = self.connection.execute(
            "SELECT experience_evidence_json FROM job_repair_results WHERE job_id=? ORDER BY repair_id DESC",
            (vague,),
        ).fetchone()[0]
        self.assertEqual(json.loads(vague_audit), [])

    def test_demonstrably_invalid_existing_location_is_cleared_with_provenance(self):
        job_id = self.add_job("invalid-location", location_text="AI", city="developpeuse")
        repair_review_jobs(
            self.connection, job_ids=[job_id],
            fetcher=lambda url, timeout: self.parsed(url),
        )
        row = self.connection.execute(
            "SELECT location_text,city FROM jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        audit = self.connection.execute(
            "SELECT field_changes_json FROM job_repair_results WHERE job_id=? ORDER BY repair_id DESC",
            (job_id,),
        ).fetchone()[0]
        self.assertEqual(tuple(row), (None, None))
        self.assertEqual(json.loads(audit)["location_text"]["new_value"], "")
