"""Offline coverage for bounded, provenance-aware job discovery."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from job_search.discovery import discover_jobs, inspection_cap, search_query
from job_search.providers import ParsedJob
from job_search.storage import (
    connect_database, record_job_rediscovery, upsert_job,
)
from utils.google_search_client import _google_result_date_text
from utils.google_search_discovery import load_queries


def parsed_job(url):
    source_id = url.rstrip("/").rsplit("/", 1)[-1]
    return ParsedJob(
        canonical_url=url, provider="lever", source_job_id=source_id,
        title="Backend Engineer", fetch_status="FETCHED",
    )


class QueryLoadingTests(TestCase):
    def test_comments_blanks_duplicates_and_original_text(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "google_queries.txt"
            path.write_text(
                "# group\n\n  Backend   Developer France  \n"
                "backend developer france\nNestJS developer Paris\n",
                encoding="utf-8",
            )
            self.assertEqual(
                load_queries(path),
                ["Backend   Developer France", "NestJS developer Paris"],
            )


class RecentDaysTests(TestCase):
    def test_recent_days_adds_google_time_filter_without_rewriting_query(self):
        driver = Mock()
        with patch(
            "job_search.discovery.read_current_search_results",
            return_value=([], ""),
        ):
            search_query(
                driver, "backend developer France", 10, 5,
                start=20, recent_days=14,
            )
        driver.get.assert_called_once_with(
            "https://www.google.com/search?q=backend+developer+France&start=20&tbs=qdr:d14"
        )

    def test_omitted_recent_days_preserves_request_shape(self):
        driver = Mock()
        with patch(
            "job_search.discovery.read_current_search_results",
            return_value=([], ""),
        ):
            search_query(driver, "backend developer France", 10, 5)
        driver.get.assert_called_once_with(
            "https://www.google.com/search?q=backend+developer+France"
        )

    def test_relative_date_is_evidence_only(self):
        self.assertEqual(
            _google_result_date_text("2 days ago — New backend position"),
            "2 days ago",
        )


class DiscoveryCoverageTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "jobs.db"
        self.query_file = self.root / "google_queries.txt"
        self.driver = Mock()

    def run_discovery(self, pages, limit, queries="backend developer France", next_page=True):
        self.query_file.write_text(queries + "\n", encoding="utf-8")

        def page_results(*args, **kwargs):
            start = kwargs.get("start", 0)
            return pages.get(start, []), ""

        fetcher = Mock(side_effect=lambda url, timeout: parsed_job(url))
        with patch("job_search.discovery.search_query", side_effect=page_results) as search, patch(
            "job_search.discovery.has_next_search_page", return_value=next_page
        ):
            stats = discover_jobs(
                self.query_file, self.database, limit=limit, delay=0,
                driver_factory=lambda **_: self.driver, fetcher=fetcher,
            )
        return stats, fetcher, search

    def test_limit_counts_only_new_and_paginates_past_known_and_noise(self):
        connection = connect_database(self.database)
        upsert_job(
            connection, parsed_job("https://jobs.lever.co/acme/known"),
            "older query", "2026-01-01T00:00:00+00:00",
        )
        connection.close()
        pages = {
            0: [
                {"title": "Acme", "url": "https://acme.test/"},
                {"title": "Known", "url": "https://jobs.lever.co/acme/known"},
                {"title": "One", "url": "https://jobs.lever.co/acme/new-one"},
            ],
            10: [
                {"title": "Two", "url": "https://jobs.lever.co/acme/new-two"},
            ],
        }
        stats, fetcher, search = self.run_discovery(pages, limit=2)
        self.assertEqual(
            (stats["new"], stats["known"], stats["rejected"], stats["inspected"]),
            (2, 1, 1, 4),
        )
        self.assertEqual(fetcher.call_count, 2)
        self.assertEqual(
            [call.kwargs["start"] for call in search.call_args_list], [0, 10]
        )

    def test_repeated_page_triggers_exhaustion(self):
        row = {"title": "One", "url": "https://jobs.lever.co/acme/one"}
        stats, fetcher, search = self.run_discovery(
            {0: [row], 10: [row]}, limit=3,
        )
        query = stats["query_stats"][0]
        self.assertTrue(query["exhausted"])
        self.assertEqual((query["pages"], query["inspected"]), (2, 1))
        self.assertEqual(fetcher.call_count, 1)
        self.assertEqual(search.call_count, 2)

    def test_empty_page_triggers_exhaustion(self):
        stats, fetcher, _ = self.run_discovery({0: []}, limit=3)
        query = stats["query_stats"][0]
        self.assertTrue(query["exhausted"])
        self.assertEqual((query["pages"], query["inspected"]), (1, 0))
        fetcher.assert_not_called()

    def test_deterministic_inspection_cap_stops_unique_noise(self):
        cap = inspection_cap(3)
        pages = {
            start: [
                {
                    "title": f"Homepage {start + index}",
                    "url": f"https://noise{start + index}.test/",
                }
                for index in range(10)
            ]
            for start in range(0, cap, 10)
        }
        stats, fetcher, search = self.run_discovery(pages, limit=3)
        query = stats["query_stats"][0]
        self.assertTrue(query["inspection_cap_reached"])
        self.assertEqual(query["inspected"], cap)
        self.assertEqual(search.call_count, cap // 10)
        fetcher.assert_not_called()

    def test_same_job_two_queries_fetches_once_and_preserves_both_provenances(self):
        row = {
            "title": "Backend Engineer",
            "url": "https://jobs.lever.co/acme/shared",
            "result_snippet": "2 days ago — Build APIs",
            "displayed_domain": "jobs.lever.co",
            "google_result_date_text": "2 days ago",
        }
        stats, fetcher, _ = self.run_discovery(
            {0: [row]}, limit=2,
            queries="backend developer France\nsoftware engineer Europe",
            next_page=False,
        )
        self.assertEqual((stats["new"], stats["known"]), (1, 1))
        self.assertEqual(fetcher.call_count, 1)
        connection = connect_database(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        provenance = connection.execute(
            """SELECT source_query, result_snippet, displayed_domain,
                      google_result_date_text
               FROM job_source_queries ORDER BY job_source_query_id"""
        ).fetchall()
        self.assertEqual(
            [row["source_query"] for row in provenance],
            ["backend developer France", "software engineer Europe"],
        )
        self.assertEqual(
            tuple(provenance[0])[1:],
            ("2 days ago — Build APIs", "jobs.lever.co", "2 days ago"),
        )


class RediscoveryTimestampTests(TestCase):
    def test_first_seen_survives_while_last_seen_and_query_relation_advance(self):
        with TemporaryDirectory() as directory:
            connection = connect_database(Path(directory) / "jobs.db")
            self.addCleanup(connection.close)
            job = parsed_job("https://jobs.lever.co/acme/one")
            job_id, _ = upsert_job(
                connection, job, "Query   One", "2026-01-01T00:00:00+00:00",
            )
            record_job_rediscovery(
                connection, job_id, "lever", "one", job.canonical_url,
                " query one ", "2026-01-02T00:00:00+00:00",
            )
            stored = connection.execute(
                "SELECT first_seen_at, last_seen_at FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            query = connection.execute(
                """SELECT source_query, first_seen_at, last_seen_at
                   FROM job_source_queries WHERE job_id=?""",
                (job_id,),
            ).fetchone()
            self.assertEqual(
                tuple(stored),
                ("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
            )
            self.assertEqual(query["source_query"], "Query   One")
            self.assertEqual(
                (query["first_seen_at"], query["last_seen_at"]),
                ("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
            )
