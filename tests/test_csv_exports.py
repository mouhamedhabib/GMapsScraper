"""Offline tests for automatic run-scoped CSV exports."""

from argparse import Namespace
from csv import DictReader
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

import job_search.csv_exports as exports
from job_search.csv_exports import JOB_FIELDS, export_jobs_run, export_maps_rows
from job_search.discovery import empty_stats, main as discovery_main
from job_search.providers import ParsedJob
from job_search.storage import connect_database, upsert_job, utc_now
from maps import main as maps_main, run_maps_discovery


class JobCsvExportTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "jobs.db"
        self.export_dir = self.root / "exports"
        self.connection = connect_database(self.database)
        self.addCleanup(self.connection.close)

    def create_run(self, run_id, status="SUCCESS"):
        now = "2026-09-14T14:42:08+00:00"
        with self.connection:
            self.connection.execute(
                """INSERT INTO workflow_runs
                   (run_id, started_at, finished_at, status, mode,
                    maps_enabled, job_discovery_enabled, completion_enabled,
                    filter_enabled, priority_enabled, created_at)
                   VALUES (?, ?, ?, ?, 'TEST', 0, 1, 0, 0, 0, ?)""",
                (run_id, now, now, status, now),
            )

    def add_job(self, run_id, suffix, state="NEW", title="Engineer"):
        parsed = ParsedJob(
            canonical_url=f"https://jobs.lever.co/acme/{suffix}",
            provider="lever", source_job_id=suffix, title=title,
            fetch_status="FETCHED",
        )
        job_id, _ = upsert_job(self.connection, parsed, "query")
        with self.connection:
            self.connection.execute(
                """INSERT INTO workflow_run_jobs
                   (run_id, job_id, discovery_state, created_at)
                   VALUES (?, ?, ?, ?)""",
                (run_id, job_id, state, utc_now()),
            )
        return job_id

    def read(self, path):
        with Path(path).open("r", newline="", encoding="utf-8-sig") as handle:
            return list(DictReader(handle))

    def test_success_is_scoped_sorted_utf8_and_repeatable(self):
        self.create_run("run-one")
        second = self.add_job("run-one", "2", title="مهندس برمجيات")
        first = self.add_job("run-one", "1", state="KNOWN")
        self.create_run("run-two")
        self.add_job("run-two", "other")

        initial = export_jobs_run(self.connection, "run-one", self.export_dir)
        repeated = export_jobs_run(self.connection, "run-one", self.export_dir)

        self.assertEqual(initial["timestamped"], repeated["timestamped"])
        self.assertEqual(initial["rows"], 2)
        self.assertEqual(
            [int(row["job_id"]) for row in self.read(initial["timestamped"])],
            sorted((first, second)),
        )
        self.assertEqual(
            initial["timestamped"].read_bytes(), initial["latest"].read_bytes()
        )
        self.assertTrue(initial["timestamped"].read_bytes().startswith(b"\xef\xbb\xbf"))
        rows = self.read(initial["timestamped"])
        self.assertEqual(len(rows), 2)
        self.assertNotIn(
            "https://jobs.lever.co/acme/other",
            {row["job_url"] for row in rows},
        )
        self.assertEqual(rows[0]["qualification_status"], "")
        self.assertEqual(rows[0]["application_url"], "")
        self.assertIn("مهندس برمجيات", initial["timestamped"].read_text("utf-8-sig"))

    def test_partial_and_empty_success_write_headers(self):
        self.create_run("partial", "PARTIAL")
        self.add_job("partial", "partial")
        partial = export_jobs_run(self.connection, "partial", self.export_dir)
        self.assertEqual(self.read(partial["timestamped"])[0]["run_status"], "PARTIAL")

        self.create_run("empty")
        empty = export_jobs_run(self.connection, "empty", self.export_dir)
        self.assertEqual(empty["rows"], 0)
        self.assertEqual(
            empty["timestamped"].read_text("utf-8-sig").splitlines()[0],
            ",".join(JOB_FIELDS),
        )

    def test_failed_timestamp_write_preserves_good_latest(self):
        self.create_run("failure")
        self.export_dir.mkdir()
        latest = self.export_dir / "google_jobs_latest.csv"
        latest.write_bytes(b"known-good")
        with patch.object(exports, "_atomic_csv", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                export_jobs_run(self.connection, "failure", self.export_dir)
        self.assertEqual(latest.read_bytes(), b"known-good")

    def test_export_module_has_no_pandas_dependency(self):
        source = Path(exports.__file__).read_text(encoding="utf-8")
        self.assertNotIn("pandas", source.casefold())

    def test_diagnostic_cli_never_calls_exporter(self):
        arguments = Namespace(
            query_file=self.root / "queries.txt", database=self.database,
            limit=1, delay=0, timeout=1, windowed=False, verbose=False,
            diagnose_results=True, recent_days=None,
            disable_network_protection=True, network_max_pause=0,
            network_probe_interval=1, export_dir=self.export_dir,
        )
        with patch("job_search.discovery.parse_arguments", return_value=arguments), \
             patch("job_search.discovery.discover_jobs", return_value=empty_stats()), \
             patch("job_search.discovery.print_summary"), \
             patch("job_search.discovery.export_jobs_run") as exporter:
            discovery_main()
        exporter.assert_not_called()
        self.assertFalse(self.export_dir.exists())

    def test_direct_discovery_cli_automatically_exports(self):
        arguments = Namespace(
            query_file=self.root / "queries.txt", database=self.database,
            limit=1, delay=0, timeout=1, windowed=False, verbose=False,
            diagnose_results=False, recent_days=None,
            disable_network_protection=True, network_max_pause=0,
            network_probe_interval=1, export_dir=self.export_dir,
        )
        stats = empty_stats()
        stats["query_stats"] = [{"status": "EXHAUSTED"}]
        with patch("job_search.discovery.parse_arguments", return_value=arguments), \
             patch("job_search.discovery.discover_jobs", return_value=stats), \
             patch("job_search.discovery.print_summary"):
            discovery_main()
        timestamped = [
            path for path in self.export_dir.glob("google_jobs_*.csv")
            if path.name != "google_jobs_latest.csv"
        ]
        self.assertEqual(len(timestamped), 1)
        self.assertTrue((self.export_dir / "google_jobs_latest.csv").is_file())
        self.assertEqual(self.read(timestamped[0]), [])

    def test_direct_discovery_two_new_jobs_are_members_before_export(self):
        query_file = self.root / "queries.txt"
        query_file.write_text("backend engineer\n", encoding="utf-8")
        arguments = Namespace(
            query_file=query_file, database=self.database,
            limit=2, delay=0, timeout=1, windowed=False, verbose=False,
            diagnose_results=False, recent_days=None,
            disable_network_protection=True, network_max_pause=0,
            network_probe_interval=1, export_dir=self.export_dir,
        )
        driver = Mock()
        parsed_jobs = [
            ParsedJob(
                canonical_url=f"https://jobs.lever.co/acme/direct-{number}",
                provider="lever", source_job_id=f"direct-{number}",
                title=f"Backend Engineer {number}", fetch_status="FETCHED",
            )
            for number in (1, 2)
        ]
        membership_seen = []
        real_export = export_jobs_run

        def audited_export(connection, run_id, export_directory):
            membership_seen.extend(connection.execute(
                """SELECT job_id, discovery_state FROM workflow_run_jobs
                   WHERE run_id=? ORDER BY job_id""",
                (run_id,),
            ).fetchall())
            return real_export(connection, run_id, export_directory)

        with patch("job_search.discovery.parse_arguments", return_value=arguments), \
             patch("job_search.discovery.search_query", return_value=([{
                 "title": parsed.title, "url": parsed.canonical_url,
             } for parsed in parsed_jobs], "")), \
             patch("job_search.discovery.has_next_search_page", return_value=False), \
             patch("job_search.discovery.create_chrome_driver", return_value=driver), \
             patch("job_search.discovery.fetch_job", side_effect=parsed_jobs), \
             patch("job_search.discovery.export_jobs_run", side_effect=audited_export), \
             patch("job_search.discovery.print_summary"):
            discovery_main()

        run = self.connection.execute(
            """SELECT run_id, status FROM workflow_runs
               WHERE mode='DIRECT_DISCOVERY'"""
        ).fetchone()
        self.assertEqual(run["status"], "SUCCESS")
        self.assertEqual(len(membership_seen), 2)
        self.assertEqual(
            [row["discovery_state"] for row in membership_seen], ["NEW", "NEW"]
        )
        rows = self.read(self.export_dir / "google_jobs_latest.csv")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["run_id"] for row in rows}, {run["run_id"]})
        self.assertEqual(
            {row["job_url"] for row in rows},
            {parsed.canonical_url for parsed in parsed_jobs},
        )
        driver.quit.assert_called_once_with()

    def test_direct_discovery_known_job_is_attached_to_current_run(self):
        query_file = self.root / "queries.txt"
        query_file.write_text("backend engineer\n", encoding="utf-8")
        known = ParsedJob(
            canonical_url="https://jobs.lever.co/acme/known-1",
            provider="lever", source_job_id="known-1",
            title="Known Backend Engineer", fetch_status="FETCHED",
        )
        known_id, _ = upsert_job(self.connection, known, "earlier query")
        arguments = Namespace(
            query_file=query_file, database=self.database,
            limit=1, delay=0, timeout=1, windowed=False, verbose=False,
            diagnose_results=False, recent_days=None,
            disable_network_protection=True, network_max_pause=0,
            network_probe_interval=1, export_dir=self.export_dir,
        )
        driver = Mock()
        with patch("job_search.discovery.parse_arguments", return_value=arguments), \
             patch("job_search.discovery.search_query", return_value=([{
                 "title": known.title, "url": known.canonical_url,
             }], "")), \
             patch("job_search.discovery.has_next_search_page", return_value=False), \
             patch("job_search.discovery.create_chrome_driver", return_value=driver), \
             patch("job_search.discovery.fetch_job") as fetcher, \
             patch("job_search.discovery.print_summary"):
            discovery_main()

        membership = self.connection.execute(
            """SELECT run_id, job_id, discovery_state FROM workflow_run_jobs
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        self.assertEqual(membership["job_id"], known_id)
        self.assertEqual(membership["discovery_state"], "KNOWN")
        rows = self.read(self.export_dir / "google_jobs_latest.csv")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], membership["run_id"])
        self.assertEqual(rows[0]["observation_status"], "KNOWN")
        self.assertEqual(rows[0]["job_url"], known.canonical_url)
        fetcher.assert_not_called()
        driver.quit.assert_called_once_with()

    def test_export_preserves_new_known_and_updated_observation_statuses(self):
        self.create_run("status-run")
        self.add_job("status-run", "new", "NEW")
        self.add_job("status-run", "known", "KNOWN")
        self.add_job("status-run", "updated", "UPDATED")

        exported = export_jobs_run(self.connection, "status-run", self.export_dir)

        self.assertEqual(
            {row["observation_status"] for row in self.read(exported["timestamped"])},
            {"NEW", "KNOWN", "UPDATED"},
        )

    def test_failed_direct_discovery_does_not_export(self):
        arguments = Namespace(
            query_file=self.root / "queries.txt", database=self.database,
            limit=1, delay=0, timeout=1, windowed=False, verbose=False,
            diagnose_results=False, recent_days=None,
            disable_network_protection=True, network_max_pause=0,
            network_probe_interval=1, export_dir=self.export_dir,
        )
        stats = empty_stats()
        stats["query_stats"] = [{"status": "FAILED"}]
        with patch("job_search.discovery.parse_arguments", return_value=arguments), \
             patch("job_search.discovery.discover_jobs", return_value=stats), \
             patch("job_search.discovery.print_summary"):
            discovery_main()
        self.assertFalse(self.export_dir.exists())
        status = self.connection.execute(
            "SELECT status FROM workflow_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
        self.assertEqual(status, "FAILED")


class MapsCsvExportTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_maps_export_and_latest_are_separate_from_jobs(self):
        export_dir = self.root / "exports"
        result = export_maps_rows(
            [{"title": "شركة", "map_link": "https://maps.test/1"}],
            export_directory=export_dir,
            run_time="2026-09-14T14:44:31+00:00",
        )
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["timestamped"].name, "google_maps_2026-09-14_15-44-31.csv")
        self.assertEqual(result["timestamped"].read_bytes(), result["latest"].read_bytes())
        self.assertFalse((export_dir / "google_jobs_latest.csv").exists())

    def test_maps_runner_automatically_exports_only_its_records(self):
        class FakeAlgo:
            @staticmethod
            def load_query_file(file_name):
                return ["one"]

            def __init__(self, **kwargs):
                pass

            def fast_search_algorithm(self, queries):
                return {"new": 1}

            def run_records(self):
                return [{"title": "One", "map_link": "map-1"}]

        with patch("maps.FastSearchAlgo", FakeAlgo):
            stats = run_maps_discovery(
                query_file="unused", export_directory=self.root / "exports",
            )
        self.assertEqual(stats["csv_rows"], 1)
        self.assertTrue(Path(stats["csv_path"]).is_file())
        self.assertTrue(Path(stats["csv_latest"]).is_file())

    def test_maps_run_records_deduplicate_neural_ai_across_queries(self):
        from threading import Lock
        from utils.threading_controller import FastSearchAlgo

        neural = {
            "title": "Neural AI", "category": "Software company",
            "map_link": "https://google.com/maps/place/Neural/data=!4m1!1sChIJNeural",
            "webpage": "https://neuralai.mt/", "address": "San Gwann, Malta",
        }
        coordinator = FastSearchAlgo.__new__(FastSearchAlgo)
        coordinator._summary_lock = Lock()
        coordinator._run_rows = [
            {**neural, "source_query": "software company Malta"},
            {**neural, "source_query": "web development company Malta"},
        ]
        self.assertEqual(len(coordinator.run_records()), 1)

    def test_maps_latest_replaces_previous_run_even_when_empty(self):
        export_dir = self.root / "exports"
        first = export_maps_rows(
            [{"title": "First"}], export_directory=export_dir,
            run_time="2026-09-14T10:00:00+00:00",
        )
        second = export_maps_rows(
            [], export_directory=export_dir,
            run_time="2026-09-14T11:00:00+00:00",
        )
        self.assertNotEqual(first["timestamped"], second["timestamped"])
        self.assertEqual(second["rows"], 0)
        self.assertEqual(second["timestamped"].read_bytes(), second["latest"].read_bytes())

    def test_direct_maps_cli_invokes_automatic_export(self):
        app = Mock()
        app.export_maps_csv.return_value = {
            "timestamped": self.root / "exports" / "google_maps_timestamp.csv",
            "latest": self.root / "exports" / "google_maps_latest.csv",
        }
        with patch("maps.GMapsScraper", return_value=app):
            maps_main()
        app.arg_parser.assert_called_once_with()
        app.scrape_maps_data.assert_called_once_with()
        app.export_maps_csv.assert_called_once_with()
