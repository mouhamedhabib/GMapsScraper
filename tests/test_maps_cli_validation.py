import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from maps import GMapsScraper
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

    def test_valid_incremental_configuration_is_accepted(self):
        with TemporaryDirectory() as directory:
            query_file = Path(directory) / "queries.txt"
            query_file.write_text("software Tunis\n", encoding="utf-8")
            app = self.parse(
                "-q", str(query_file), "-l", "15", "--incremental", "-of", "CSV"
            )
        self.assertTrue(app._args.incremental)
        self.assertEqual(app._args.limit, 15)


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
