import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from maps import GMapsScraper, main
from utils.threading_controller import FastSearchAlgo


class MapsCliValidationTests(TestCase):
    def parse(self, *arguments):
        app = GMapsScraper()
        with patch.object(sys, "argv", ["maps.py", *arguments]):
            app.arg_parser()
        return app

    def assert_parse_error(self, expected, *arguments):
        with patch.object(sys, "argv", ["maps.py", *arguments]), patch(
            "sys.stderr.write"
        ) as stderr:
            with self.assertRaises(SystemExit) as raised:
                GMapsScraper().arg_parser()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn(expected, "".join(call.args[0] for call in stderr.call_args_list))

    def test_zero_and_negative_limits_are_rejected(self):
        for value in ("0", "-1"):
            with self.subTest(value=value):
                self.assert_parse_error("--limit must be >= 1", "-l", value)

    def test_safe_default_configuration_still_parses(self):
        app = self.parse()
        self.assertEqual(app._args.limit, 1)
        self.assertEqual(app._args.threads, 1)
        self.assertEqual(app._args.browser_wait, 15)
        self.assertEqual(app._args.scroll_minutes, 1)
        self.assertIsNone(app._args.suggested_ext)
        self.assertEqual(app._args.known_companies_dir, "./CSV_FILES")
        self.assertFalse(app._args.allow_empty_known_companies)
        self.assertEqual(app._effective_mode.value, "legacy")

    def test_discovery_modes_are_explicit_and_mutually_exclusive(self):
        shadow = self.parse("--discovery-mode", "shadow")
        self.assertEqual(shadow._effective_mode.value, "shadow")
        legacy_shadow = self.parse("--company-registry-shadow")
        self.assertEqual(legacy_shadow._effective_mode.value, "shadow")
        self.assert_parse_error(
            "cannot be combined",
            "--company-registry-shadow", "--discovery-mode", "authoritative",
            "--registry-database", "/tmp/disposable.db",
        )
        self.assert_parse_error(
            "require --registry-database",
            "--discovery-mode", "authoritative-canary",
        )
        authoritative = self.parse(
            "--discovery-mode", "authoritative-canary",
            "--registry-database", "/tmp/disposable.db",
        )
        self.assertTrue(authoritative._effective_mode.is_authoritative)

    def test_resume_is_explicit_authoritative_and_mutually_exclusive(self):
        self.assert_parse_error(
            "requires an authoritative",
            "--resume-run-id", "run-1",
        )
        self.assert_parse_error(
            "mutually exclusive",
            "--discovery-mode", "authoritative-canary",
            "--registry-database", "/tmp/disposable.db",
            "--registry-run-id", "fresh",
            "--resume-run-id", "existing",
        )
        resumed = self.parse(
            "--discovery-mode", "authoritative-canary",
            "--registry-database", "/tmp/disposable.db",
            "--resume-run-id", "existing",
        )
        self.assertEqual(resumed._args.resume_run_id, "existing")

    def test_production_registry_opt_in_requires_exact_authoritative_mode(self):
        self.assert_parse_error(
            "requires --discovery-mode authoritative",
            "--registry-database", "/tmp/disposable.db",
            "--allow-production-registry",
        )
        self.assert_parse_error(
            "requires --discovery-mode authoritative",
            "--discovery-mode", "authoritative-canary",
            "--registry-database", "/tmp/disposable.db",
            "--allow-production-registry",
        )
        app = self.parse(
            "--discovery-mode", "authoritative",
            "--registry-database", "/tmp/disposable.db",
            "--allow-production-registry",
        )
        self.assertTrue(app._args.allow_production_registry)

    def test_interrupted_authoritative_run_has_no_export(self):
        app = self.parse(
            "--discovery-mode", "authoritative-canary",
            "--registry-database", "/tmp/disposable.db",
        )
        app._stats = {
            "run_observability": {"termination_reason": "INTERRUPTED"},
        }
        self.assertIsNone(app.export_maps_csv())

    def test_non_interrupted_authoritative_missing_export_fails_closed(self):
        app = self.parse(
            "--discovery-mode", "authoritative-canary",
            "--registry-database", "/tmp/disposable.db",
        )
        app._stats = {
            "run_observability": {"termination_reason": "NORMAL_COMPLETION"},
        }
        with self.assertRaisesRegex(RuntimeError, "without export metadata"):
            app.export_maps_csv()

    def test_main_reports_interruption_without_export_paths(self):
        app = Mock()
        app.export_maps_csv.return_value = None
        with patch("maps.GMapsScraper", return_value=app), patch(
            "builtins.print"
        ) as output:
            main()
        output.assert_called_once_with(
            "Authoritative Maps run interrupted; no export finalized."
        )

    def test_main_preserves_successful_authoritative_export_reporting(self):
        app = Mock()
        app.export_maps_csv.return_value = {
            "csv": "/tmp/result.csv", "manifest": "/tmp/result.manifest.json",
        }
        with patch("maps.GMapsScraper", return_value=app), patch(
            "builtins.print"
        ) as output:
            main()
        self.assertEqual(
            [call.args[0] for call in output.call_args_list],
            [
                "Authoritative Maps CSV: /tmp/result.csv",
                "Manifest: /tmp/result.manifest.json",
            ],
        )

    def test_zero_and_negative_workers_are_rejected(self):
        for value in ("0", "-2"):
            with self.subTest(value=value):
                self.assert_parse_error("--threads must be >= 1", "-w", value)

    def test_unsafe_wait_and_scroll_boundaries_are_rejected(self):
        cases = (("--browser-wait", "0"), ("--browser-wait", "-1"),
                 ("--scroll-minutes", "0"), ("--scroll-minutes", "-1"))
        for option, value in cases:
            with self.subTest(option=option, value=value):
                self.assert_parse_error(f"{option} must be >= 1", option, value)

    def test_missing_query_file_is_rejected_before_coordinator_creation(self):
        app = self.parse("-q", "/definitely/missing/maps-queries.txt", "-l", "15")
        with patch("maps.FastSearchAlgo") as coordinator:
            with self.assertRaises(SystemExit) as raised:
                app.scrape_maps_data()
        self.assertEqual(raised.exception.code, 2)
        coordinator.assert_not_called()

    def test_empty_and_whitespace_only_query_files_are_rejected(self):
        for contents in ("", " \n\t\n"):
            with self.subTest(contents=repr(contents)), TemporaryDirectory() as directory:
                query_file = Path(directory) / "queries.txt"
                query_file.write_text(contents, encoding="utf-8")
                app = self.parse("-q", str(query_file), "-l", "15")
                with patch("maps.FastSearchAlgo") as coordinator:
                    coordinator.load_query_file.side_effect = FastSearchAlgo.load_query_file
                    with self.assertRaises(SystemExit) as raised:
                        app.scrape_maps_data()
                self.assertEqual(raised.exception.code, 2)
                coordinator.assert_not_called()

    def test_valid_one_query_and_existing_configuration_are_accepted(self):
        captured = {}

        class FakeAlgo:
            load_query_file = staticmethod(FastSearchAlgo.load_query_file)

            def __init__(self, **kwargs):
                captured.update(kwargs)

            def fast_search_algorithm(self, queries):
                captured["queries"] = queries

        with TemporaryDirectory() as directory:
            query_file = Path(directory) / "queries.txt"
            query_file.write_text("  coffee shops Tunis  \n\n", encoding="utf-8")
            app = self.parse(
                "-q", str(query_file), "-l", "15", "-w", "2",
                "-bw", "10", "-sm", "2", "-of", "CSV",
            )
            with patch("maps.FastSearchAlgo", FakeAlgo):
                app.scrape_maps_data()
        self.assertEqual(captured["queries"], ["coffee shops Tunis"])
        self.assertEqual(captured["workers"], 1)
        self.assertEqual(captured["result_range"], 15)
        self.assertIsNone(captured["suggested_ext"])

    def test_explicit_suggested_extensions_are_forwarded_unchanged(self):
        captured = {}

        class FakeAlgo:
            load_query_file = staticmethod(FastSearchAlgo.load_query_file)

            def __init__(self, **kwargs):
                captured.update(kwargs)

            def fast_search_algorithm(self, queries):
                return {}

        with TemporaryDirectory() as directory:
            query_file = Path(directory) / "queries.txt"
            query_file.write_text("software Tunis\n", encoding="utf-8")
            app = self.parse(
                "-q", str(query_file), "-se", "contact", "-se", "about",
            )
            with patch("maps.FastSearchAlgo", FakeAlgo):
                app.scrape_maps_data()

        self.assertEqual(captured["suggested_ext"], ["contact", "about"])

    def test_valid_incremental_configuration_is_accepted(self):
        with TemporaryDirectory() as directory:
            query_file = Path(directory) / "queries.txt"
            query_file.write_text("software Tunis\n", encoding="utf-8")
            app = self.parse(
                "-q", str(query_file), "-l", "15", "--incremental", "-of", "CSV"
            )
        self.assertTrue(app._args.incremental)
        self.assertEqual(app._args.limit, 15)

    def test_independent_output_and_known_company_directories_are_forwarded(self):
        captured = {}

        class FakeAlgo:
            load_query_file = staticmethod(FastSearchAlgo.load_query_file)

            def __init__(self, **kwargs):
                captured.update(kwargs)

            def fast_search_algorithm(self, queries):
                return {}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            query_file = root / "queries.txt"
            query_file.write_text("software Tunis\n", encoding="utf-8")
            app = self.parse(
                "-q", str(query_file), "--incremental",
                "--output-folder", str(root / "output"),
                "--known-companies-dir", str(root / "history"),
                "--allow-empty-known-companies",
            )
            with patch("maps.FastSearchAlgo", FakeAlgo):
                app.scrape_maps_data()
        self.assertEqual(captured["output_path"], str(root / "output"))
        self.assertEqual(captured["known_companies_dir"], str(root / "history"))
        self.assertTrue(captured["allow_empty_known_companies"])


class WorkerCoordinatorValidationTests(TestCase):
    def test_invalid_numeric_constructor_values_fail_before_executor_creation(self):
        cases = (
            {"workers": 0}, {"workers": -1}, {"result_range": 0},
            {"result_range": -1}, {"wait_time": 0}, {"wait_time": -1},
            {"scroll_minutes": 0}, {"scroll_minutes": -1},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), patch(
                "utils.threading_controller.ThreadPoolExecutor"
            ) as executor:
                with self.assertRaises(ValueError):
                    FastSearchAlgo(**kwargs)
                executor.assert_not_called()

    def test_query_loader_discards_blank_lines(self):
        with TemporaryDirectory() as directory:
            query_file = Path(directory) / "queries.txt"
            query_file.write_text("\n one \n\t\ntwo\n", encoding="utf-8")
            self.assertEqual(FastSearchAlgo.load_query_file(query_file), ["one", "two"])
