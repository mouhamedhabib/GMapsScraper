"""Offline tests for deterministic job discovery and persistence."""

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from requests import ConnectionError

from job_search.discovery import discover_jobs
from job_search.normalization import normalize_job_url
from job_search.providers import (
    GENERIC_LISTING_REASON,
    ParsedJob,
    classify_source_quality,
    classify_job_result,
    detect_provider,
    extract_source_job_id,
    fetch_job,
    generic_listing_reason,
    is_job_result,
    parse_job_html,
)
from job_search.schema import SCHEMA_VERSION
from job_search.storage import connect_database, upsert_job
from utils.google_search_client import (
    resolve_google_result_url,
    unwrap_google_result_url,
)
from utils.google_search_discovery import load_queries


class JobUrlTests(TestCase):
    def test_greenhouse_url(self):
        url = "https://boards.greenhouse.io/acme/jobs/12345"
        self.assertEqual(detect_provider(url), "greenhouse")
        self.assertEqual(extract_source_job_id("greenhouse", url), "12345")
        self.assertTrue(is_job_result("Engineer", url))

    def test_greenhouse_company_board_is_not_a_posting(self):
        result = classify_job_result(
            "Acme careers", "https://boards.greenhouse.io/acme"
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection_reason, "PROVIDER_BOARD_NOT_POSTING")

    def test_new_greenhouse_host_posting(self):
        url = "https://job-boards.greenhouse.io/acme/jobs/456789"
        self.assertEqual(detect_provider(url), "greenhouse")
        self.assertTrue(is_job_result("Unusual title formatting | Acme", url))

    def test_lever_url(self):
        url = "https://jobs.lever.co/acme/a-posting-id"
        self.assertEqual(detect_provider(url), "lever")
        self.assertEqual(extract_source_job_id("lever", url), "a-posting-id")
        self.assertTrue(is_job_result("Engineer", url))

    def test_lever_company_board_is_not_a_posting(self):
        self.assertFalse(is_job_result("Acme jobs", "https://jobs.lever.co/acme"))

    def test_ashby_url(self):
        url = "https://jobs.ashbyhq.com/acme/job-slug"
        self.assertEqual(detect_provider(url), "ashby")
        self.assertEqual(extract_source_job_id("ashby", url), "job-slug")
        self.assertTrue(is_job_result("Engineer", url))

    def test_additional_providers_are_classified(self):
        cases = {
            "https://apply.workable.com/acme/j/ABC123/": "workable",
            "https://jobs.workable.com/view/ABC123/backend-engineer": "workable",
            "https://jobs.smartrecruiters.com/Acme/743999-engineer": "smartrecruiters",
            "https://careers.smartrecruiters.com/Acme/job/744000-engineer": "smartrecruiters",
            "https://acme.teamtailor.com/jobs/123-engineer": "teamtailor",
        }
        for url, provider in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_provider(url), provider)
                self.assertTrue(is_job_result("Engineer", url))

    def test_generic_career_url(self):
        self.assertTrue(is_job_result("Backend Engineer", "https://acme.test/careers/backend"))

    def test_generic_careers_homepage_is_not_an_individual_job(self):
        result = classify_job_result("Careers", "https://acme.test/careers/")
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection_reason, "NON_JOB_PATH")

    def test_generic_collection_pages_are_not_individual_jobs(self):
        cases = (
            ("NestJS jobs in Paris, France | 39 open jobs", "https://www.wearedevelopers.com/jobs/ls/france/paris/nestjs"),
            ("FastAPI jobs in Germany | 216 open jobs", "https://www.wearedevelopers.com/jobs/ls/germany/fastapi"),
            ("Missions freelance et emplois NestJS", "https://www.free-work.com/fr/tech-it/jobs/nestjs"),
            ("Missions freelance et emplois FastAPI", "https://www.free-work.com/fr/tech-it/jobs/fastapi"),
            (
                "Software Engineering Careers & Job Opportunities | Accenture",
                "https://www.accenture.com/be-en/careers/explore-careers/area-of-interest/software-engineering-careers",
            ),
            (
                "Junior developer jobs - 29 vacancies on JobScout24",
                "https://www.jobscout24.ch/en/jobs/junior%20developer",
            ),
        )
        for title, url in cases:
            with self.subTest(url=url):
                result = classify_job_result(title, url)
                self.assertFalse(result.accepted)
                self.assertEqual(result.rejection_reason, GENERIC_LISTING_REASON)

    def test_remoterocketship_role_collection_is_not_an_individual_job(self):
        result = classify_job_result(
            "Remote Software Engineer Jobs",
            "https://www.remoterocketship.com/jobs/software-engineer",
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection_reason, GENERIC_LISTING_REASON)

    def test_generic_individual_job_path_remains_eligible(self):
        result = classify_job_result(
            "Java Backend Developer",
            "https://careers.cognizant.com/india-en/jobs/00069080335/java-backend-developer",
        )
        self.assertTrue(result.accepted)

    def test_tanitjobs_and_bayt_individual_paths_remain_eligible(self):
        cases = (
            "https://www.tanitjobs.com/job/754705/software-engineer",
            "https://www.bayt.com/en/tunisia/jobs/full-stack-ai-developer-talent-pool-75157668",
        )
        for url in cases:
            with self.subTest(url=url):
                result = classify_job_result("Software developer", url)
                self.assertTrue(result.accepted)
                self.assertEqual(result.provider, "generic")

    def test_listing_title_does_not_break_supported_ats_identity(self):
        result = classify_job_result(
            "39 open jobs", "https://jobs.lever.co/acme/individual-id"
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.provider, "lever")

    def test_article_and_homepage_rejected(self):
        self.assertFalse(is_job_result("Career advice", "https://acme.test/blog/careers"))
        self.assertFalse(is_job_result("10 best jobs", "https://acme.test/news/best-jobs"))
        self.assertFalse(is_job_result("Acme", "https://acme.test/"))

    def test_provider_posting_does_not_require_a_normal_title(self):
        self.assertTrue(is_job_result("", "https://jobs.lever.co/acme/posting-123"))

    def test_google_redirect_unwraps_before_provider_detection(self):
        cases = {
            "https://www.google.com/url?q=https://jobs.lever.co/acme/abc": (
                "lever", "https://jobs.lever.co/acme/abc"
            ),
            "https://www.google.com/url?url=https://jobs.lever.co/acme/abc": (
                "lever", "https://jobs.lever.co/acme/abc"
            ),
            "/url?q=https://jobs.ashbyhq.com/acme/posting-id": (
                "ashby", "https://jobs.ashbyhq.com/acme/posting-id"
            ),
            "/url?url=https://jobs.lever.co/acme/abc": (
                "lever", "https://jobs.lever.co/acme/abc"
            ),
            "https://google.com/url?sa=t&url=https%3A%2F%2Fjobs.lever.co%2Facme%2Fabc&ved=x": (
                "lever", "https://jobs.lever.co/acme/abc"
            ),
            "https://www.google.com/url?sa=t&q=https%3A%2F%2Fjob-boards.greenhouse.io%2Facme%2Fjobs%2F123&ved=x": (
                "greenhouse", "https://job-boards.greenhouse.io/acme/jobs/123"
            ),
        }
        for raw_url, (provider, expected) in cases.items():
            with self.subTest(raw_url=raw_url):
                self.assertEqual(unwrap_google_result_url(raw_url), expected)
                result = classify_job_result("odd title", raw_url)
                self.assertTrue(result.accepted)
                self.assertEqual(result.provider, provider)
                self.assertEqual(result.normalized_url, expected)

    def test_google_redirect_prefers_url_over_q(self):
        wrapped = (
            "https://www.google.com/url?"
            "q=https://jobs.ashbyhq.com/wrong/id&"
            "url=https://jobs.lever.co/acme/right"
        )
        self.assertEqual(
            unwrap_google_result_url(wrapped),
            "https://jobs.lever.co/acme/right",
        )

    def test_non_http_google_target_is_not_unwrapped(self):
        for target in ("javascript:alert(1)", "data:text/plain,x", "file:///tmp/x", "/relative"):
            wrapped = "https://www.google.com/url?q=" + target
            with self.subTest(target=target):
                self.assertEqual(unwrap_google_result_url(wrapped), wrapped)
                self.assertEqual(
                    classify_job_result("Result", wrapped).rejection_reason,
                    "GOOGLE_URL",
                )

    def test_direct_provider_urls_are_unchanged_by_unwrapper(self):
        urls = (
            "https://jobs.lever.co/acme/abc",
            "https://boards.greenhouse.io/acme/jobs/123",
            "https://jobs.ashbyhq.com/acme/posting-id",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(unwrap_google_result_url(url), url)

    def test_resolved_goto_target_is_provider_detectable(self):
        response = Mock()
        response.status = 302
        response.headers = {
            "Location": "https://jobs.lever.co/acme/posting-id",
            "Content-Type": "",
        }
        result = resolve_google_result_url(
            "https://www.google.com/goto?url=CAESopaque",
            cache={},
            open_request=Mock(return_value=response),
        )
        classification = classify_job_result("Engineer", result.url)
        self.assertTrue(classification.accepted)
        self.assertEqual(classification.provider, "lever")

    def test_unresolved_google_and_malformed_urls_have_reasons(self):
        self.assertEqual(
            classify_job_result("Result", "https://www.google.com/url?ved=x").rejection_reason,
            "GOOGLE_URL",
        )
        self.assertEqual(classify_job_result("Result", "mailto:x@y.test").rejection_reason, "MALFORMED_URL")

    def test_tracking_parameters_and_fragment_removed(self):
        self.assertEqual(
            normalize_job_url("HTTPS://Jobs.Lever.co/acme/abc/?utm_source=x&team=eng#apply"),
            "https://jobs.lever.co/acme/abc?team=eng",
        )
        self.assertEqual(
            normalize_job_url(
                "https://www.tanitjobs.com/job/754705/software-engineer?"
                "__cf_chl_rt_tk=ephemeral&utm_source=search&job=754705#apply"
            ),
            "https://www.tanitjobs.com/job/754705/software-engineer?job=754705",
        )


class ParserTests(TestCase):
    def test_job_board_title_evidence_beats_generic_site_title(self):
        tanit_url = "https://www.tanitjobs.com/job/754705/software-engineer"
        h1 = parse_job_html(
            tanit_url,
            """<title>Offres d'emploi et travail en Tunisie</title>
            <meta property="og:title" content="Offres d'emploi et travail en Tunisie">
            <main><h1>Software Engineer</h1></main>""",
        )
        self.assertEqual(h1.title, "Software Engineer")
        self.assertEqual(h1.evidence_sources["title"], "html_job_heading")

        slug = parse_job_html(
            tanit_url, "<title>Offres d'emploi et travail en Tunisie</title>"
        )
        self.assertEqual(slug.title, "Software Engineer")
        self.assertEqual(slug.evidence_sources["title"], "url_structured_title")

        french_slug = parse_job_html(
            "https://www.tanitjobs.com/job/458385/developpeur-full-stack",
            "<title>Offres d'emploi et travail en Tunisie</title>",
        )
        self.assertEqual(french_slug.title, "Developpeur Full Stack")

        bayt = parse_job_html(
            "https://www.bayt.com/en/tunisia/jobs/full-stack-ai-developer-talent-pool-75157668",
            '<meta property="og:title" content="Full Stack (AI) Developer (Talent Pool) at Viseven - Tunis">',
        )
        self.assertEqual(
            bayt.title,
            "Full Stack (AI) Developer (Talent Pool) at Viseven - Tunis",
        )

    def test_individual_url_title_survives_blocked_fetch(self):
        session = Mock()
        session.get.side_effect = ConnectionError("blocked")
        result = fetch_job(
            "https://www.tanitjobs.com/job/754705/software-engineer?"
            "__cf_chl_rt_tk=ephemeral",
            session=session,
        )
        self.assertEqual(result.fetch_status, "FAILED")
        self.assertEqual(result.title, "Software Engineer")
        self.assertEqual(result.canonical_url, "https://www.tanitjobs.com/job/754705/software-engineer")
        self.assertEqual(result.evidence_sources["title"], "url_structured_title")

    def test_json_ld_job_fields(self):
        html = '''<script type="application/ld+json">{
          "@type":"JobPosting", "title":"Backend Engineer",
          "description":"<p>Build APIs</p>", "datePosted":"2026-09-01",
          "employmentType":"FULL_TIME", "jobLocationType":"TELECOMMUTE",
          "hiringOrganization":{"name":"Acme","sameAs":"https://acme.test"},
          "jobLocation":{"address":{"addressLocality":"Paris","addressCountry":"FR"}}
        }</script>'''
        job = parse_job_html("https://jobs.lever.co/acme/abc", html)
        self.assertEqual(job.title, "Backend Engineer")
        self.assertEqual(job.company_name, "Acme")
        self.assertEqual(job.location_text, "Paris, FR")
        self.assertEqual(job.remote_policy, "REMOTE")
        self.assertEqual(job.status, "OPEN")

    def test_generic_json_ld_extracts_complete_job_posting(self):
        html = '''<script type="application/ld+json">{
          "@graph":[{"@type":["Thing","JobPosting"],
          "title":"Java Backend Developer", "description":"<p>Build Java APIs</p>",
          "datePosted":"2026-09-10", "employmentType":"FULL_TIME",
          "hiringOrganization":{"@type":"Organization","name":"Cognizant"},
          "jobLocation":{"address":{"addressLocality":"Chennai","addressCountry":"IN"}}}]
        }</script>'''
        parsed = parse_job_html(
            "https://careers.example.test/jobs/123/java-backend-developer", html
        )
        self.assertEqual(parsed.title, "Java Backend Developer")
        self.assertEqual(parsed.company_name, "Cognizant")
        self.assertEqual(parsed.location_text, "Chennai, IN")
        self.assertEqual(parsed.published_at, "2026-09-10")
        self.assertEqual(parsed.description, "Build Java APIs")
        self.assertEqual(parsed.employment_type, "FULL_TIME")

    def test_json_ld_job_posting_overrides_weak_collection_shape(self):
        html = '''<script type="application/ld+json">{
          "@type":"JobPosting", "title":"Backend Developer Jobs",
          "description":"Build APIs for Acme.",
          "hiringOrganization":{"name":"Acme"}
        }</script>'''
        parsed = parse_job_html(
            "https://careers.example.test/jobs/backend-developer", html,
        )
        self.assertTrue(parsed.has_structured_job_posting)
        self.assertEqual(
            generic_listing_reason(
                parsed.title, parsed.canonical_url, parsed.description,
                page_fetched=True,
                has_structured_job_posting=parsed.has_structured_job_posting,
            ),
            "",
        )

    def test_generic_microdata_and_h1_fallback_safely(self):
        microdata = '''<main itemscope itemtype="https://schema.org/JobPosting">
          <h1 itemprop="title">Python Developer</h1>
          <div itemprop="hiringOrganization"><span itemprop="name">Acme</span></div>
          <div itemprop="jobLocation"><span itemprop="address" itemscope>
            <span itemprop="addressLocality">Paris</span><span itemprop="addressCountry">FR</span>
          </span></div><time itemprop="datePosted" datetime="2026-09-09"></time>
          <div itemprop="description"><p>Build useful software.</p></div>
        </main>'''
        parsed = parse_job_html("https://acme.test/jobs/123/python-developer", microdata)
        self.assertEqual((parsed.title, parsed.company_name), ("Python Developer", "Acme"))
        self.assertEqual(parsed.location_text, "Paris, FR")
        self.assertEqual(parsed.published_at, "2026-09-09")
        self.assertEqual(parsed.description, "Build useful software.")

        fallback = parse_job_html(
            "https://acme.test/jobs/456/backend", "<html><head><title>Careers</title></head><body><main><h1>Backend Developer</h1></main></body></html>"
        )
        self.assertEqual(fallback.title, "Backend Developer")
        self.assertEqual(fallback.description, "")

    def test_strong_location_fallbacks_and_ambiguity(self):
        title_location = parse_job_html(
            "https://example.test/jobs/123",
            "<h1>Développeur Full Stack Java Junior - Nantes - F/H</h1>",
        )
        self.assertEqual((title_location.location_text, title_location.city), ("Nantes", "Nantes"))

        prefix_location = parse_job_html(
            "https://example.test/jobs/124", "<h1>Casablanca - Développeur Full Stack Junior</h1>"
        )
        self.assertEqual(prefix_location.location_text, "Casablanca")

        url_location = parse_job_html(
            "https://example.test/jobs/Casablanca-D%C3%A9veloppeur-Full-Stack-Junior/",
            "<h1>Développeur Full Stack Junior</h1>",
        )
        self.assertEqual((url_location.location_text, url_location.city), ("Casablanca", "Casablanca"))

        ambiguous = parse_job_html(
            "https://example.test/jobs/backend-developer", "<h1>Backend Developer for our Paris team</h1>"
        )
        self.assertEqual(ambiguous.location_text, "")

    def test_location_evidence_priority(self):
        html = '''<meta name="job:location" content="Metadata City">
          <div class="posting-categories"><span class="location">Provider City</span></div>
          <h1>Backend Developer - Title City - F/H</h1>'''
        parsed = parse_job_html("https://jobs.lever.co/acme/abc", html)
        self.assertEqual(parsed.location_text, "Provider City")

    def test_source_quality_is_metadata_not_rejection(self):
        cases = {
            "https://boards.greenhouse.io/acme/jobs/123": "ATS",
            "https://jobs.lever.co/acme/abc": "ATS",
            "https://jobs.ashbyhq.com/acme/abc": "ATS",
            "https://careers.capgemini.com/job/123": "DIRECT_COMPANY",
            "https://jobs.infineon.com/job/123": "DIRECT_COMPANY",
            "https://bitwarden.com/careers/123": "DIRECT_COMPANY",
            "https://craegroup.com/careers/123": "DIRECT_COMPANY",
            "https://remotive.com/remote/jobs/software-development/example": "JOB_PLATFORM",
            "https://www.simplyhired.co.uk/job/example": "JOB_PLATFORM",
            "https://www.jobleads.com/us/job/example": "JOB_PLATFORM",
            "https://bebee.com/us/jobs/example": "JOB_PLATFORM",
            "https://internshala.com/job/detail/example": "JOB_PLATFORM",
            "https://www.wizbii.com/company/acme/job/backend": "JOB_PLATFORM",
            "https://waytolearnx.com/jobs/backend-developer": "JOB_PLATFORM",
            "https://jobs.welovedevs.com/company/backend": "JOB_PLATFORM",
            "https://example.test/opening/123": "UNKNOWN",
            "https://example.test/careers/123": "UNKNOWN",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(classify_source_quality(url), expected)

    def test_technology_tokens_cannot_be_derived_locations(self):
        for token in ("AI", "ML", "backend", "frontend", "fullstack", "software",
                      "developer", "engineer", "Java", "Python", "ReactJS",
                      "NestJS", "FastAPI", "Laravel"):
            with self.subTest(token=token):
                parsed = parse_job_html(
                    f"https://example.test/jobs/{token}-developer-123",
                    f"<h1>{token} Developer</h1>",
                )
                self.assertEqual(parsed.city, "")
                self.assertEqual(parsed.location_text, "")


class StorageTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "job_search.db"
        self.connection = connect_database(self.path)
        self.addCleanup(self.connection.close)

    def job(self, url="https://jobs.lever.co/acme/one", source_id="one", **values):
        defaults = {
            "canonical_url": url,
            "provider": "lever",
            "source_job_id": source_id,
            "title": "Backend Engineer",
            "company_name": "Acme",
            "company_url": "https://acme.test/",
            "fetch_status": "FETCHED",
        }
        defaults.update(values)
        return ParsedJob(**defaults)

    def test_identical_jobs_deduplicate_and_timestamps_advance(self):
        job_id, was_new = upsert_job(self.connection, self.job(), "query one", "2026-01-01T00:00:00+00:00")
        same_id, was_new_again = upsert_job(self.connection, self.job(), "query two", "2026-01-02T00:00:00+00:00")
        self.assertTrue(was_new)
        self.assertFalse(was_new_again)
        self.assertEqual(job_id, same_id)
        row = self.connection.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(row["first_seen_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(row["last_seen_at"], "2026-01-02T00:00:00+00:00")
        source = self.connection.execute("SELECT * FROM job_sources").fetchone()
        self.assertEqual(source["source_query"], "query one;query two")

    def test_same_title_at_two_companies_stays_separate(self):
        upsert_job(self.connection, self.job(), "q")
        upsert_job(
            self.connection,
            self.job(
                url="https://jobs.lever.co/other/two", source_id="two",
                company_name="Other", company_url="https://other.test/",
            ),
            "q",
        )
        self.assertEqual(self.connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)

    def test_provider_source_id_uniqueness_wins_over_changed_url(self):
        first, _ = upsert_job(self.connection, self.job(), "q")
        second, was_new = upsert_job(
            self.connection,
            self.job(url="https://jobs.lever.co/acme/one-renamed"),
            "q",
        )
        self.assertEqual(first, second)
        self.assertFalse(was_new)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM job_sources").fetchone()[0], 1)

    def test_database_survives_restart_and_schema_is_idempotent(self):
        upsert_job(self.connection, self.job(), "q")
        self.connection.close()
        self.connection = connect_database(self.path)
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        connect_database(self.path).close()

    def test_existing_empty_database_initializes(self):
        other = Path(self.temporary.name) / "existing.db"
        sqlite3.connect(other).close()
        connection = connect_database(other)
        self.addCleanup(connection.close)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"companies", "jobs", "job_sources"}.issubset(tables))


class QueryAndDiscoveryTests(TestCase):
    def test_blank_query_lines_ignored(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "queries.txt"
            path.write_text("one\n\n  \ntwo\n", encoding="utf-8")
            self.assertEqual(load_queries(path), ["one", "two"])

    def _run(self, database, marker=""):
        driver = Mock()
        driver.quit = Mock()
        row = {"title": "Engineer", "url": "https://jobs.lever.co/acme/abc"}
        fetched = ParsedJob(
            canonical_url=row["url"], provider="lever", source_job_id="abc",
            title="Engineer", fetch_status="FETCHED",
        )
        query_file = database.parent / "queries.txt"
        query_file.write_text("engineer\n", encoding="utf-8")
        with patch("job_search.discovery.search_query", return_value=([row], marker)), patch(
            "job_search.discovery.has_next_search_page", return_value=False
        ):
            return discover_jobs(
                query_file, database, limit=3, delay=0, windowed=bool(marker),
                driver_factory=lambda **_: driver,
                fetcher=lambda url, timeout: fetched,
                input_function=lambda _: "",
            )

    def test_repeated_discovery_does_not_duplicate(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.db"
            first = self._run(database)
            second = self._run(database)
            self.assertEqual((first["new"], first["known"]), (1, 0))
            self.assertEqual((second["new"], second["known"]), (0, 1))
            connection = connect_database(database)
            self.addCleanup(connection.close)
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_structured_job_posting_survives_weak_listing_heuristic(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            query_file = root / "queries.txt"
            query_file.write_text("backend developer\n", encoding="utf-8")
            url = "https://careers.example.test/jobs/backend-developer"
            row = {"title": "Backend Developer Jobs", "url": url}
            fetched = ParsedJob(
                canonical_url=url, provider="generic",
                title="Backend Developer Jobs",
                description="Build APIs for Acme.",
                fetch_status="FETCHED",
                has_structured_job_posting=True,
            )
            driver = Mock()
            with patch(
                "job_search.discovery.search_query", return_value=([row], ""),
            ), patch(
                "job_search.discovery.has_next_search_page", return_value=False,
            ):
                stats = discover_jobs(
                    query_file, database, limit=1, delay=0,
                    driver_factory=lambda **_: driver,
                    fetcher=lambda candidate, timeout: fetched,
                )
            self.assertEqual(stats["new"], 1)
            self.assertEqual(
                stats["rejection_reasons"][GENERIC_LISTING_REASON], 0,
            )

    def test_captcha_uses_shared_manual_callback(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.db"
            resumed = [{"title": "Engineer", "url": "https://jobs.lever.co/acme/abc"}]
            with patch(
                "job_search.discovery.wait_for_manual_verification",
                return_value=(resumed, False),
            ) as callback:
                stats = self._run(database, marker="recaptcha")
            callback.assert_called_once()
            self.assertEqual(stats["new"], 1)

    def test_rejection_reasons_and_examples_are_bounded(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.db"
            query_file = Path(directory) / "queries.txt"
            query_file.write_text("engineer\n", encoding="utf-8")
            rows = [
                {"title": f"Acme {index}", "url": f"https://acme{index}.test/"}
                for index in range(5)
            ]
            driver = Mock()
            with patch("job_search.discovery.search_query", return_value=(rows, "")), patch(
                "job_search.discovery.has_next_search_page", return_value=False
            ):
                stats = discover_jobs(
                    query_file, database, limit=1, delay=0,
                    driver_factory=lambda **_: driver,
                )
            self.assertEqual(stats["rejection_reasons"]["NON_JOB_PATH"], 5)
            self.assertEqual(len(stats["rejection_examples"]["NON_JOB_PATH"]), 3)

    def test_goto_resolution_failure_is_not_collapsed_to_google_url(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.db"
            query_file = Path(directory) / "queries.txt"
            query_file.write_text("engineer\n", encoding="utf-8")
            row = {
                "title": "Engineer",
                "raw_url": "https://www.google.com/goto?url=CAESopaque",
                "url": "https://www.google.com/goto?url=CAESopaque",
                "resolution_error": "GOOGLE_GOTO_TIMEOUT",
            }
            driver = Mock()
            fetcher = Mock()
            with patch("job_search.discovery.search_query", return_value=([row], "")), patch(
                "job_search.discovery.has_next_search_page", return_value=False
            ):
                stats = discover_jobs(
                    query_file, database, limit=1, delay=0,
                    driver_factory=lambda **_: driver, fetcher=fetcher,
                )
            self.assertEqual(stats["rejection_reasons"]["GOOGLE_GOTO_TIMEOUT"], 1)
            self.assertEqual(stats["rejection_reasons"]["GOOGLE_URL"], 0)
            fetcher.assert_not_called()

    def test_diagnostic_mode_does_not_create_database_or_fetch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.db"
            query_file = root / "queries.txt"
            query_file.write_text("engineer\nsecond query\n", encoding="utf-8")
            rows = [{"title": "Engineer", "url": "https://jobs.lever.co/acme/abc"}]
            driver = Mock()
            fetcher = Mock()
            with patch("job_search.discovery.search_query", return_value=(rows, "")) as search, patch(
                "job_search.discovery.print_diagnostic_result"
            ) as diagnostic_output:
                stats = discover_jobs(
                    query_file, database, limit=3, delay=0,
                    driver_factory=lambda **_: driver, fetcher=fetcher,
                    diagnose_results=True,
                )
            self.assertEqual(stats["queries"], 1)
            self.assertEqual(stats["candidates"], 1)
            self.assertFalse(database.exists())
            fetcher.assert_not_called()
            search.assert_called_once()
            diagnostic_output.assert_called_once()
